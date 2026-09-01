"""
log_bus.py
==========
Localhost TCP tee for LiveLingo log panels (LC / Coach / VOZ).

Host (main TUI) publishes NDJSON lines; secondary processes run
``python main.py view <lc|coach|voz>`` and render one panel.

Never blocks the pipeline: per-client bounded queues + drop-oldest.
Bind is loopback-only (UI mirror, not an authenticated API).
"""

from __future__ import annotations

import json
import queue
import socket
import threading
import time
from typing import Any, Callable, Generator, Iterable, Optional

PROTOCOL_VERSION = 1

# View CLI aliases → canonical ui panel id
_VIEW_ALIASES = {
    "main": "main",
    "voz": "main",
    "trad": "main",
    "traducao": "main",
    "tradução": "main",
    "translation": "main",
    "lc": "lc",
    "livecaptions": "lc",
    "captions": "lc",
    "caption": "lc",
    "coach": "coach",
    "entrevista": "coach",
    "interview": "coach",
}


def normalize_view_panel(name: str) -> str:
    """Map CLI / alias → ``main`` | ``lc`` | ``coach``."""
    key = str(name or "").strip().lower()
    if key in _VIEW_ALIASES:
        return _VIEW_ALIASES[key]
    if key in ("main", "lc", "coach"):
        return key
    raise ValueError(
        f"Painel inválido: {name!r}. Use: lc | coach | voz (ou trad / entrevista)."
    )


def panel_matches(event_panel: str, filter_panel: str) -> bool:
    """True if an event should appear in a viewer filtered to ``filter_panel``."""
    try:
        want = normalize_view_panel(filter_panel)
    except ValueError:
        want = "main"
    got = str(event_panel or "main").lower()
    if got in ("voz", "traducao", "tradução", "translation"):
        got = "main"
    if got in ("entrevista", "interview", "interview_coach"):
        got = "coach"
    if got in ("livecaptions", "captions", "caption", "main-lc"):
        got = "lc"
    return got == want


def encode_event(
    kind: str,
    text: str,
    panel: str = "main",
    *,
    t: float | None = None,
) -> bytes:
    """Serialize one log event as a single NDJSON line (UTF-8 + ``\\n``)."""
    payload = {
        "v": PROTOCOL_VERSION,
        "kind": str(kind or "info"),
        "panel": str(panel or "main"),
        "text": "" if text is None else str(text),
        "t": float(time.time() if t is None else t),
    }
    return (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )


def decode_event(raw: bytes | str) -> Optional[dict[str, Any]]:
    """Parse one NDJSON line → dict, or None if invalid."""
    if isinstance(raw, bytes):
        try:
            line = raw.decode("utf-8", errors="replace")
        except Exception:
            return None
    else:
        line = str(raw)
    line = line.strip()
    if not line:
        return None
    try:
        data = json.loads(line)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    return {
        "v": int(data.get("v") or PROTOCOL_VERSION),
        "kind": str(data.get("kind") or "info"),
        "panel": str(data.get("panel") or "main"),
        "text": str(data.get("text") if data.get("text") is not None else ""),
        "t": float(data.get("t") or 0.0),
    }


def format_view_markup(kind: str, text: str) -> Optional[str]:
    """
    Mirror ``LiveLingoApp.post_log`` markup rules for the secondary viewer.

    Returns ``None`` for ``clear`` (caller should clear the widget).
    """
    if kind == "clear":
        return None
    # Structured Spoken / Trade-offs / errors for follow-along — not in scrollback
    if kind in ("coach_spoken", "coach_tradeoffs", "coach_error"):
        return None
    if text is None:
        return ""
    if text == "" or str(text).strip() == "":
        return ""
    t = str(text).rstrip("\n")
    try:
        from rich.markup import escape

        safe = escape(t)
    except Exception:
        safe = t.replace("[", "\\[")
    if kind == "rich":
        return t
    if kind == "success":
        return f"[green][ok][/] {safe}"
    if kind == "warn":
        return f"[yellow][!][/] {safe}"
    if kind == "error":
        return f"[bold red][x][/] {safe}"
    if kind == "dim":
        return f"[dim]{safe}[/]"
    if kind == "info":
        return f"[cyan][i][/] {safe}"
    if kind == "list":
        return f"[bold]{safe}[/]"
    return safe


class _ClientOut:
    """One connected viewer: bounded queue + writer thread."""

    __slots__ = ("sock", "q", "alive", "thread")

    def __init__(self, sock: socket.socket, max_buf: int):
        self.sock = sock
        self.q: queue.Queue[bytes] = queue.Queue(maxsize=max(32, int(max_buf or 400)))
        self.alive = True
        self.thread = threading.Thread(
            target=self._writer, name="log-bus-client", daemon=True
        )
        self.thread.start()

    def offer(self, line: bytes) -> None:
        if not self.alive:
            return
        try:
            self.q.put_nowait(line)
            return
        except queue.Full:
            pass
        # Drop-oldest, then try once more
        try:
            self.q.get_nowait()
        except queue.Empty:
            pass
        try:
            self.q.put_nowait(line)
        except queue.Full:
            pass

    def _writer(self) -> None:
        while self.alive:
            try:
                line = self.q.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                self.sock.sendall(line)
            except Exception:
                self.alive = False
                break
        try:
            self.sock.close()
        except Exception:
            pass

    def close(self) -> None:
        self.alive = False
        try:
            self.sock.close()
        except Exception:
            pass


class LogBusServer:
    """Accept viewer connections and fan out log events (non-blocking publish)."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 8765,
        *,
        max_buf: int = 400,
    ):
        self.host = str(host or "127.0.0.1")
        # port=0 → OS ephemeral (tests); do not use `port or 8765` (0 is falsy)
        self.port = 8765 if port is None else int(port)
        self.max_buf = max(32, int(32 if max_buf is None else max_buf))
        self._sock: Optional[socket.socket] = None
        self._clients: list[_ClientOut] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._accept_thread: Optional[threading.Thread] = None
        self._started = False
        self.bound_port: Optional[int] = None

    @property
    def running(self) -> bool:
        return bool(self._started) and not self._stop.is_set()

    def start(self) -> tuple[str, int]:
        """Bind + listen. Returns (host, port). Idempotent if already started."""
        if self._started:
            return self.host, int(self.bound_port or self.port)
        self._stop.clear()
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        # Loopback only — never 0.0.0.0
        host = self.host if self.host in ("127.0.0.1", "::1", "localhost") else "127.0.0.1"
        self.host = host
        sock.bind((host, self.port))
        sock.listen(8)
        sock.settimeout(0.5)
        self._sock = sock
        self.bound_port = int(sock.getsockname()[1])
        self._started = True
        self._accept_thread = threading.Thread(
            target=self._accept_loop, name="log-bus-accept", daemon=True
        )
        self._accept_thread.start()
        return self.host, self.bound_port

    def stop(self) -> None:
        self._stop.set()
        self._started = False
        with self._lock:
            clients = list(self._clients)
            self._clients.clear()
        for c in clients:
            try:
                c.close()
            except Exception:
                pass
        sock = self._sock
        self._sock = None
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass

    def publish(self, kind: str, text: str, panel: str = "main") -> None:
        """Enqueue one event for all viewers (never raises / never blocks long)."""
        if not self._started or self._stop.is_set():
            return
        try:
            line = encode_event(kind, text, panel)
        except Exception:
            return
        with self._lock:
            clients = list(self._clients)
        dead: list[_ClientOut] = []
        for c in clients:
            if not c.alive:
                dead.append(c)
                continue
            try:
                c.offer(line)
            except Exception:
                dead.append(c)
        if dead:
            with self._lock:
                self._clients = [c for c in self._clients if c not in dead and c.alive]
            for c in dead:
                try:
                    c.close()
                except Exception:
                    pass

    def client_count(self) -> int:
        with self._lock:
            return sum(1 for c in self._clients if c.alive)

    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            sock = self._sock
            if sock is None:
                break
            try:
                conn, _addr = sock.accept()
            except socket.timeout:
                continue
            except OSError:
                if self._stop.is_set():
                    break
                continue
            try:
                conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except Exception:
                pass
            client = _ClientOut(conn, self.max_buf)
            with self._lock:
                self._clients.append(client)


def iter_events(
    host: str = "127.0.0.1",
    port: int = 8765,
    *,
    stop_event: Optional[threading.Event] = None,
    reconnect: bool = True,
    reconnect_sleep_s: float = 0.6,
    on_connection: Optional[Callable[[bool], None]] = None,
) -> Generator[dict[str, Any], None, None]:
    """
    Yield decoded events from the host bus.

    Reconnects until ``stop_event`` is set (or once if ``reconnect=False``).
    ``on_connection(True/False)`` fires on connect / disconnect (best-effort).
    """
    stop = stop_event or threading.Event()
    host = str(host or "127.0.0.1")
    port = int(port or 8765)

    def _notify(ok: bool) -> None:
        if on_connection is None:
            return
        try:
            on_connection(bool(ok))
        except Exception:
            pass

    while not stop.is_set():
        try:
            with socket.create_connection((host, port), timeout=2.0) as sock:
                sock.settimeout(1.0)
                _notify(True)
                buf = b""
                while not stop.is_set():
                    try:
                        chunk = sock.recv(8192)
                    except socket.timeout:
                        continue
                    except OSError:
                        break
                    if not chunk:
                        break
                    buf += chunk
                    while b"\n" in buf:
                        raw, buf = buf.split(b"\n", 1)
                        ev = decode_event(raw)
                        if ev is not None:
                            yield ev
        except Exception:
            pass
        _notify(False)
        if not reconnect or stop.is_set():
            return
        time.sleep(max(0.1, float(reconnect_sleep_s)))


def make_publish_sink(server: LogBusServer) -> Callable[[str, str, str], None]:
    """Adapter ``(kind, text, panel)`` → ``server.publish``."""

    def _sink(kind: str, text: str, panel: str = "main") -> None:
        server.publish(kind, text, panel)

    return _sink
