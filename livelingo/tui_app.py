"""
tui_app.py
==========
Textual TUI for LiveLingo: scrollable log + command input + fixed listen bar.

Pipeline (mic/STT/TTS) keeps running in background threads; this module only
owns the screen. Logs arrive via ui.set_log_sink; commands reuse main dispatch
in a worker thread with stdin/stdout proxies for prompts and prints.
"""

from __future__ import annotations

import queue
import re
import sys
import threading
from typing import Callable, Optional

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Footer, Header, Input, RichLog, Static

from . import ui as ui_mod

# Strip ANSI for log cleanliness when proxying print()
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text or "")


class _StdinProxy:
    """Blocks worker threads until the TUI provides a line of input."""

    def __init__(self, app: "LiveLingoApp"):
        self._app = app

    def readline(self, size: int = -1) -> str:  # noqa: ARG002
        return self._app._wait_for_prompt_line()

    def read(self, size: int = -1) -> str:  # noqa: ARG002
        return self.readline()


class _StdoutProxy:
    """Route print() from command workers into the TUI log."""

    def __init__(self, app: "LiveLingoApp", real):
        self._app = app
        self._real = real
        self._buf = ""

    def write(self, data: str) -> int:
        if not data:
            return 0
        text = _strip_ansi(data)
        # Drop pure cursor/control noise
        if not text.replace("\r", "").replace("\n", "").strip():
            if "\n" in data:
                self._flush_line("")
            return len(data)
        self._buf += text.replace("\r", "")
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            self._flush_line(line)
        return len(data)

    def _flush_line(self, line: str) -> None:
        line = line.rstrip()
        if line:
            self._app.post_log("raw", line)

    def flush(self) -> None:
        if self._buf.strip():
            self._flush_line(self._buf)
            self._buf = ""

    def isatty(self) -> bool:
        return False

    def fileno(self):
        return self._real.fileno()


class LiveLingoApp(App):
    """Main LiveLingo TUI — log scrolls; listen status stays docked at bottom."""

    TITLE = "LiveLingo"
    SUB_TITLE = "real-time voice translation"
    CSS = """
    Screen {
        layout: vertical;
    }
    #log {
        height: 1fr;
        border: tall $accent 40%;
        padding: 0 1;
    }
    #bottom {
        height: auto;
        dock: bottom;
    }
    #cmd-row {
        height: 3;
        padding: 0 1;
    }
    #cmd {
        width: 1fr;
    }
    #status {
        height: 1;
        background: $warning 30%;
        color: $text;
        padding: 0 1;
        text-style: bold;
    }
    #status.sound-on {
        background: $success 35%;
    }
    #hint {
        height: 1;
        color: $text-muted;
        padding: 0 1;
    }
    """

    BINDINGS = [
        Binding("ctrl+c", "quit_app", "Quit", show=True, priority=True),
        Binding("f1", "show_help", "Help", show=True),
    ]

    def __init__(
        self,
        pipeline,
        synonym_lookup,
        dispatch_command: Callable,
        listen_msgs_fn: Callable,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.pipeline = pipeline
        self.synonym_lookup = synonym_lookup
        self._dispatch = dispatch_command
        self._listen_msgs_fn = listen_msgs_fn  # () -> (idle, active)
        self._prompt_q: queue.Queue = queue.Queue()
        self._prompt_waiting = threading.Event()
        self._prompt_label = ""
        self._cmd_busy = False
        self._frame_i = 0
        self._speaking = False
        self._sound_on = False
        self._mic_muted = False

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield RichLog(id="log", highlight=True, markup=True, wrap=True, auto_scroll=True)
        with Vertical(id="bottom"):
            yield Static(
                "comandos: s áudio | g swap | t target | r replay | l lista | m menu | q sair  ·  F1 ajuda",
                id="hint",
            )
            with Horizontal(id="cmd-row"):
                yield Input(
                    placeholder="Digite um comando e Enter (ex: s, g, t, r, l, m, q)…",
                    id="cmd",
                )
            yield Static("🤖  …", id="status")
        yield Footer()

    def on_mount(self) -> None:
        ui_mod.set_log_sink(self._sink_from_worker)
        log = self.query_one("#log", RichLog)
        log.write("[bold cyan]LiveLingo TUI[/] — log rolável · status de escuta fixo embaixo")
        log.write(
            "[dim]Áudio de tradução OFF por padrão — [s] para ouvir · "
            "paths de áudio no log para anexar[/]"
        )
        self.set_interval(0.2, self._tick_status)
        self.query_one("#cmd", Input).focus()
        # Reflect pipeline defaults
        try:
            self._sound_on = bool(self.pipeline.is_sound_enabled())
            self._mic_muted = bool(self.pipeline.is_mic_muted())
        except Exception:
            pass
        self._tick_status()

    def on_unmount(self) -> None:
        ui_mod.set_log_sink(None)

    # ------------------------------------------------------------------ #
    # Logging bridge (called from worker threads via ui.set_log_sink)
    # ------------------------------------------------------------------ #
    def _sink_from_worker(self, kind: str, text: str) -> None:
        # May be called from non-UI threads
        self.call_from_thread(self.post_log, kind, text)

    def post_log(self, kind: str, text: str) -> None:
        log = self.query_one("#log", RichLog)
        t = (text or "").rstrip()
        if not t:
            return
        if kind == "success":
            log.write(f"[green][ok][/] {t}")
        elif kind == "warn":
            log.write(f"[yellow][!][/] {t}")
        elif kind == "error":
            log.write(f"[bold red][x][/] {t}")
        elif kind == "dim":
            log.write(f"[dim]{t}[/]")
        elif kind == "info":
            log.write(f"[cyan][i][/] {t}")
        else:
            log.write(t)

    # ------------------------------------------------------------------ #
    # Status bar (fixed bottom)
    # ------------------------------------------------------------------ #
    def set_speaking(self, speaking: bool) -> None:
        self._speaking = bool(speaking)

    def set_sound_on(self, on: bool) -> None:
        # Flag only — safe from worker threads; #status class updated in _tick_status.
        self._sound_on = bool(on)

    def set_mic_muted(self, muted: bool) -> None:
        self._mic_muted = bool(muted)

    def _tick_status(self) -> None:
        if self._mic_muted:
            self.query_one("#status", Static).update(
                "🔇  MIC MUTED  ·  tela livre para leitura  ·  [n] para reativar"
            )
            return

        frames = (
            "🤖 [ •      ]",
            "🤖 [  •     ]",
            "🤖 [   •    ]",
            "🤖 [    •   ]",
            "🤖 [     •  ]",
            "🤖 [      • ]",
            "🤖 [     •  ]",
            "🤖 [    •   ]",
            "🤖 [   •    ]",
            "🤖 [  •     ]",
        )
        self._frame_i = (self._frame_i + 1) % len(frames)
        frame = frames[self._frame_i]

        try:
            import config as cfg

            src = (getattr(cfg, "SOURCE_LANG", "") or "?").upper()
            tgt = (getattr(cfg, "TARGET_LANG", "") or "?").upper()
        except Exception:
            src, tgt = "?", "?"
        pair = f"{src} → {tgt}"

        try:
            idle_msg, active_msg = self._listen_msgs_fn()
        except Exception:
            idle_msg, active_msg = "Waiting…", "Listening…"

        audio = (
            "🔊 ÁUDIO ON"
            if self._sound_on
            else "🔇 ÁUDIO OFF → [s] para ouvir"
        )
        body = active_msg if self._speaking else idle_msg
        line = f"{frame} {pair}  {audio}  {body}"
        self.query_one("#status", Static).update(line)
        self.query_one("#status", Static).set_class(self._sound_on, "sound-on")

    # ------------------------------------------------------------------ #
    # Prompt / command input
    # ------------------------------------------------------------------ #
    def _wait_for_prompt_line(self) -> str:
        """Called from worker thread when code does sys.stdin.readline()."""
        self._prompt_waiting.set()
        self.call_from_thread(
            self._set_placeholder,
            self._prompt_label or "Digite a resposta e Enter…",
        )
        try:
            line = self._prompt_q.get()
        finally:
            self._prompt_waiting.clear()
            self.call_from_thread(
                self._set_placeholder,
                "Digite um comando e Enter (ex: s, g, t, r, l, m, q)…",
            )
        return line if line.endswith("\n") else line + "\n"

    def _set_placeholder(self, text: str) -> None:
        self.query_one("#cmd", Input).placeholder = text

    def provide_prompt_line(self, line: str) -> None:
        self._prompt_q.put(line)

    @on(Input.Submitted, "#cmd")
    def on_command(self, event: Input.Submitted) -> None:
        value = (event.value or "").strip()
        event.input.value = ""
        if self._prompt_waiting.is_set():
            self.provide_prompt_line(value)
            return
        if not value:
            return
        if self._cmd_busy:
            self.post_log("warn", "Aguarde o comando anterior terminar…")
            return
        self.run_command(value)

    @work(thread=True)
    def run_command(self, raw: str) -> None:
        self._cmd_busy = True
        try:
            cmd = raw.lower().strip()
            if cmd in ("q", "quit"):
                self.call_from_thread(self.post_log, "info", "Encerrando…")
                try:
                    self.pipeline.stop()
                except Exception:
                    pass
                self.call_from_thread(self.exit)
                return

            # Sync status flags after commands that change sound/mic
            old_out, old_in = sys.stdout, sys.stdin
            sys.stdout = _StdoutProxy(self, old_out)
            sys.stdin = _StdinProxy(self)
            try:
                self._dispatch(
                    self.pipeline,
                    self.synonym_lookup,
                    raw,
                    cmd,
                    self,  # indicator-like: set_sound_on / set_mic_muted / pause…
                )
            finally:
                sys.stdout = old_out
                sys.stdin = old_in
                try:
                    self.call_from_thread(
                        self.set_sound_on, self.pipeline.is_sound_enabled()
                    )
                    self.call_from_thread(
                        self.set_mic_muted, self.pipeline.is_mic_muted()
                    )
                except Exception:
                    pass
            # Session switch or pipeline stop → leave TUI (main loop may restart)
            if getattr(self.pipeline, "switch_session", False) or (
                self.pipeline.stop_event.is_set() and cmd not in ("",)
            ):
                if cmd in ("v", "q", "quit") or getattr(
                    self.pipeline, "switch_session", False
                ):
                    self.call_from_thread(self.exit)
                    return
        except Exception as exc:
            self.call_from_thread(self.post_log, "error", f"Command error: {exc}")
        finally:
            self._cmd_busy = False
            try:
                self.call_from_thread(self.query_one("#cmd", Input).focus)
            except Exception:
                pass

    # Indicator-compatible API so main._dispatch can call these on "indicator"
    def pause_for_command(self) -> None:
        pass

    def resume_after_command(self) -> None:
        pass

    def is_mic_muted_ui(self) -> bool:
        return self._mic_muted

    def action_show_help(self) -> None:
        self.post_log(
            "info",
            "Sentence: e/eN d/dN f/fN F l c | "
            "Audio: r/rN s n x a/aN p/pN | "
            "Idiom: g t o | Session: v m q",
        )

    def action_quit_app(self) -> None:
        try:
            self.pipeline.stop()
        except Exception:
            pass
        self.exit()


def run_tui(pipeline, synonym_lookup, dispatch_command, listen_msgs_fn) -> None:
    """Block until the TUI exits."""
    app = LiveLingoApp(
        pipeline=pipeline,
        synonym_lookup=synonym_lookup,
        dispatch_command=dispatch_command,
        listen_msgs_fn=listen_msgs_fn,
    )
    # Keep speaking flag in sync via pipeline callback if provided
    app.run()
