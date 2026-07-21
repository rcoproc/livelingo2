"""
livecaptions.py
===============
Windows LiveCaptions scrape (UI Automation) → sentence queue → translate.

PoC inspired by LiveCaptions-Translator (C#): launch LiveCaptions.exe, read
AutomationId=CaptionsTextBlock, segment by EOS/idle/sync, push to TUI.

Requires: Windows 11 22H2+ with LiveCaptions, and ``uiautomation`` (optional
dep). Voice pipeline (mic→Whisper→TTS) is independent and keeps running.
"""

from __future__ import annotations

import platform
import queue
import re
import subprocess
import threading
import time
from typing import Callable, Optional

from . import ui

# --------------------------------------------------------------------------- #
# Constants (mirrors LiveCaptions-Translator TextUtil / Setting defaults)
# --------------------------------------------------------------------------- #
PROCESS_NAME = "LiveCaptions"
WINDOW_CLASS = "LiveCaptionsDesktopWindow"
CAPTIONS_AUTOMATION_ID = "CaptionsTextBlock"

PUNC_EOS = ".?!。？！"
PUNC_COMMA = ",，、—\n"
SHORT_THRESHOLD = 10
MEDIUM_THRESHOLD = 40
VERYLONG_THRESHOLD = 220

_RE_ACRONYM = re.compile(r"([A-Z])\s*\.\s*([A-Z])(?![A-Za-z]+)")
_RE_ACRONYM_WORDS = re.compile(r"([A-Z])\s*\.\s*([A-Z])(?=[A-Za-z]+)")
_RE_PUNC_SPACE = re.compile(r"\s*([.!?,])\s*")
_RE_CJ_PUNC = re.compile(r"\s*([。！？，、])\s*")


class LiveCaptionsError(Exception):
    """Launch / UIA / platform failure."""


def is_windows() -> bool:
    return platform.system() == "Windows"


def uia_available() -> bool:
    if not is_windows():
        return False
    try:
        import uiautomation  # noqa: F401

        return True
    except ImportError:
        return False


def caption_lang_pair(config) -> tuple[str, str]:
    """
    Source/target for Live Captions translation.

    Default: **invert** voice pair so inbound captions match what you hear
    (e.g. voice BR→EN ⇒ captions EN→BR).
    """
    src_o = (getattr(config, "LIVE_CAPTIONS_SOURCE_LANG", None) or "").strip()
    tgt_o = (getattr(config, "LIVE_CAPTIONS_TARGET_LANG", None) or "").strip()
    if src_o and tgt_o:
        return src_o.lower(), tgt_o.lower()

    voice_src = (getattr(config, "SOURCE_LANG", "en") or "en").lower()
    voice_tgt = (getattr(config, "TARGET_LANG", "pt") or "pt").lower()
    invert = bool(getattr(config, "LIVE_CAPTIONS_INVERT_LANGS", True))
    if invert:
        return voice_tgt, voice_src
    return voice_src, voice_tgt


class _CfgLangProxy:
    """Config view with overridden SOURCE_LANG / TARGET_LANG (no global mutate)."""

    def __init__(self, base, source: str, target: str):
        object.__setattr__(self, "_base", base)
        object.__setattr__(self, "SOURCE_LANG", source)
        object.__setattr__(self, "TARGET_LANG", target)

    def __getattr__(self, name: str):
        return getattr(object.__getattribute__(self, "_base"), name)


def build_caption_translator(config, source: str, target: str):
    """
    Dedicated translator for captions (never shares mutable lang with voice).
    Same engine as main app (LLM if key, else Google).
    """
    proxy = _CfgLangProxy(config, source, target)
    engine = (getattr(config, "TRANSLATION_ENGINE", "auto") or "auto").lower()
    if engine == "auto":
        engine = "llm" if getattr(config, "GROQ_API_KEY", "") else "google"
    if engine == "llm" and getattr(config, "GROQ_API_KEY", ""):
        from .llm import LLMTranslator

        return LLMTranslator(proxy)
    from .translate import Translator

    return Translator(proxy)


# --------------------------------------------------------------------------- #
# Text helpers (port of TextUtil + RegexPatterns)
# --------------------------------------------------------------------------- #
def _is_cj_char(ch: str) -> bool:
    if not ch:
        return False
    o = ord(ch)
    return (
        (0x4E00 <= o <= 0x9FFF)
        or (0x3400 <= o <= 0x4DBF)
        or (0x3000 <= o <= 0x303F)
        or (0x3040 <= o <= 0x309F)
        or (0x30A0 <= o <= 0x30FF)
        or (0x31F0 <= o <= 0x31FF)
    )


def _utf8_len(text: str) -> int:
    return len((text or "").encode("utf-8"))


def preprocess_captions(full_text: str) -> str:
    """Normalize LiveCaptions dump before sentence split."""
    t = full_text or ""
    t = _RE_ACRONYM.sub(r"\1\2", t)
    t = _RE_ACRONYM_WORDS.sub(r"\1 \2", t)
    t = _RE_PUNC_SPACE.sub(r"\1 ", t)
    t = _RE_CJ_PUNC.sub(r"\1", t)
    t = replace_newlines(t, MEDIUM_THRESHOLD)
    return t


def replace_newlines(text: str, byte_threshold: int) -> str:
    splits = (text or "").split("\n")
    out = []
    for i, part in enumerate(splits):
        part = part.strip()
        if i < len(splits) - 1 and part:
            last = part[-1]
            if _utf8_len(part) >= byte_threshold:
                part += "。" if _is_cj_char(last) else ". "
            else:
                part += "——" if _is_cj_char(last) else "—"
        out.append(part)
    return "".join(out)


def shorten_display(text: str, max_byte_length: int = VERYLONG_THRESHOLD) -> str:
    text = text or ""
    seps = PUNC_EOS + PUNC_COMMA
    while _utf8_len(text) >= max_byte_length:
        idx = -1
        for i, ch in enumerate(text):
            if ch in seps:
                idx = i
                break
        if idx < 0 or idx + 1 >= len(text):
            break
        text = text[idx + 1 :]
    return text


def _last_eos_index(text: str) -> int:
    """Index of last end-of-sentence punct, or -1."""
    last = -1
    for i, ch in enumerate(text):
        if ch in PUNC_EOS:
            last = i
    return last


def _ends_with_eos(text: str) -> bool:
    return bool(text) and text[-1] in PUNC_EOS


def _strip_trailing_punc(text: str) -> str:
    t = (text or "").strip()
    while t and t[-1] in (PUNC_EOS + PUNC_COMMA + " "):
        t = t[:-1].rstrip()
    return t


def _normalize_for_match(text: str) -> str:
    """Lowercase + collapse space + strip trailing punct (utterance id)."""
    t = " ".join((text or "").split()).strip().lower()
    return _strip_trailing_punc(t)


def text_similarity(a: str, b: str) -> float:
    """Prefix-friendly similarity (0..1), port of LCT TextUtil.Similarity."""
    a = _normalize_for_match(a)
    b = _normalize_for_match(b)
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    if a.startswith(b) or b.startswith(a):
        return 1.0
    # cheap char-level ratio (no full Levenshtein for long strings)
    if len(a) > 80 or len(b) > 80:
        # compare heads — growing captions share the start
        n = min(48, len(a), len(b))
        same = sum(1 for i in range(n) if a[i] == b[i])
        return same / max(n, 1)
    # Levenshtein on short strings
    la, lb = len(a), len(b)
    if la > lb:
        a, b = b, a
        la, lb = lb, la
    prev = list(range(la + 1))
    for j, cb in enumerate(b, 1):
        cur = [j]
        for i, ca in enumerate(a, 1):
            ins = cur[i - 1] + 1
            dele = prev[i] + 1
            sub = prev[i - 1] + (0 if ca == cb else 1)
            cur.append(min(ins, dele, sub))
        prev = cur
    dist = prev[la]
    return 1.0 - (dist / max(lb, 1))


def is_same_utterance(prev: str, curr: str, threshold: float = 0.55) -> bool:
    """
    True if curr is a revision/growth of prev (LiveCaptions partial stream).

    Handles false mid-stream periods: "The item." → "The item that we…"
    """
    if not prev or not curr:
        return False
    if text_similarity(prev, curr) >= threshold:
        return True
    pa, ca = _normalize_for_match(prev), _normalize_for_match(curr)
    pw, cw = pa.split(), ca.split()
    if not pw or not cw:
        return False
    take = min(3, len(pw), len(cw))
    if pw[:take] != cw[:take]:
        return False
    # Same start + one is growth of the other (word count)
    return min(len(pw), len(cw)) >= take


# --------------------------------------------------------------------------- #
# Win32 helpers
# --------------------------------------------------------------------------- #
def _user32():
    import ctypes

    return ctypes.windll.user32


def _hide_hwnd(hwnd: int) -> None:
    if not hwnd:
        return
    user32 = _user32()
    GWL_EXSTYLE = -20
    WS_EX_TOOLWINDOW = 0x00000080
    SW_MINIMIZE = 6
    user32.ShowWindow(hwnd, SW_MINIMIZE)
    ex = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    user32.SetWindowLongW(hwnd, GWL_EXSTYLE, ex | WS_EX_TOOLWINDOW)


def _restore_hwnd(hwnd: int) -> None:
    if not hwnd:
        return
    user32 = _user32()
    GWL_EXSTYLE = -20
    WS_EX_TOOLWINDOW = 0x00000080
    SW_RESTORE = 9
    ex = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    user32.SetWindowLongW(hwnd, GWL_EXSTYLE, ex & ~WS_EX_TOOLWINDOW)
    user32.ShowWindow(hwnd, SW_RESTORE)
    user32.SetForegroundWindow(hwnd)


def _kill_livecaptions_processes() -> None:
    """Force-close any running LiveCaptions.exe (taskkill)."""
    if not is_windows():
        return
    try:
        subprocess.run(
            ["taskkill", "/F", "/IM", f"{PROCESS_NAME}.exe"],
            capture_output=True,
            timeout=8,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# LiveCaptions UIA reader
# --------------------------------------------------------------------------- #
class LiveCaptionsReader:
    """Launch LiveCaptions and read CaptionsTextBlock via UI Automation."""

    def __init__(self):
        self._window = None  # uiautomation Control
        self._text_block = None
        self._pid: Optional[int] = None
        self._hidden = False

    @property
    def hwnd(self) -> int:
        w = self._window
        if w is None:
            return 0
        try:
            return int(w.NativeWindowHandle or 0)
        except Exception:
            return 0

    def launch(self, hide: bool = True, timeout_s: float = 15.0) -> None:
        if not is_windows():
            raise LiveCaptionsError("LiveCaptions only runs on Windows.")
        if not uia_available():
            raise LiveCaptionsError(
                "Pacote 'uiautomation' ausente. Instale: pip install uiautomation"
            )
        import uiautomation as auto

        _kill_livecaptions_processes()
        time.sleep(0.3)

        # Process.Start("LiveCaptions") equivalent — System32 on PATH
        try:
            proc = subprocess.Popen(
                [PROCESS_NAME],
                shell=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self._pid = proc.pid
        except FileNotFoundError:
            # Explicit path (Win11)
            for candidate in (
                r"C:\Windows\System32\LiveCaptions.exe",
                r"C:\Windows\LiveCaptions.exe",
            ):
                try:
                    proc = subprocess.Popen(
                        [candidate],
                        shell=False,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    self._pid = proc.pid
                    break
                except FileNotFoundError:
                    continue
            else:
                raise LiveCaptionsError(
                    "LiveCaptions.exe não encontrado. "
                    "Requer Windows 11 22H2+ com Live Captions."
                )

        deadline = time.time() + timeout_s
        window = None
        while time.time() < deadline:
            try:
                # Prefer class name (stable)
                window = auto.WindowControl(ClassName=WINDOW_CLASS, searchDepth=1)
                if window.Exists(0, 0):
                    break
                window = None
            except Exception:
                window = None
            # Fallback: any top-level with process name
            try:
                root = auto.GetRootControl()
                for child in root.GetChildren():
                    try:
                        if (
                            getattr(child, "ClassName", "") == WINDOW_CLASS
                            or "Live Caption" in (child.Name or "")
                            or "Legendas" in (child.Name or "")
                        ):
                            window = child
                            break
                    except Exception:
                        continue
            except Exception:
                pass
            if window is not None and window.Exists(0, 0):
                break
            time.sleep(0.15)
        else:
            raise LiveCaptionsError(
                f"Falha ao abrir janela LiveCaptions (ClassName={WINDOW_CLASS})."
            )

        self._window = window
        self._text_block = None
        self._fix_window_geometry()
        if hide:
            self.hide()
        else:
            self._hidden = False

    def _fix_window_geometry(self) -> None:
        """If off-screen / tiny, move like C# FixLiveCaptions."""
        hwnd = self.hwnd
        if not hwnd:
            return
        import ctypes
        from ctypes import wintypes

        class RECT(ctypes.Structure):
            _fields_ = [
                ("left", wintypes.LONG),
                ("top", wintypes.LONG),
                ("right", wintypes.LONG),
                ("bottom", wintypes.LONG),
            ]

        rect = RECT()
        user32 = _user32()
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return
        width = rect.right - rect.left
        height = rect.bottom - rect.top
        x, y = rect.left, rect.top
        if x < 0 or y < 0 or width < 100 or height < 100:
            user32.MoveWindow(hwnd, 800, 600, 600, 200, True)

    def hide(self) -> None:
        hwnd = self.hwnd
        if hwnd:
            try:
                _hide_hwnd(hwnd)
                self._hidden = True
            except Exception:
                pass

    def restore(self) -> None:
        hwnd = self.hwnd
        if hwnd:
            try:
                _restore_hwnd(hwnd)
                self._hidden = False
            except Exception:
                pass

    def is_hidden(self) -> bool:
        return self._hidden

    def alive(self) -> bool:
        w = self._window
        if w is None:
            return False
        try:
            return bool(w.Exists(0, 0))
        except Exception:
            return False

    def get_captions(self) -> str:
        """Return current captions text (Name of CaptionsTextBlock)."""
        if not self.alive():
            raise LiveCaptionsError("LiveCaptions window gone")
        import uiautomation as auto

        if self._text_block is None:
            try:
                self._text_block = self._window.TextControl(
                    AutomationId=CAPTIONS_AUTOMATION_ID
                )
                if not self._text_block.Exists(0.5, 0.1):
                    # Broader search
                    self._text_block = auto.TextControl(
                        searchFromControl=self._window,
                        AutomationId=CAPTIONS_AUTOMATION_ID,
                    )
            except Exception as exc:
                self._text_block = None
                raise LiveCaptionsError(f"CaptionsTextBlock: {exc}") from exc
        try:
            if not self._text_block.Exists(0, 0):
                self._text_block = None
                raise LiveCaptionsError("CaptionsTextBlock unavailable")
            name = self._text_block.Name
            return name or ""
        except LiveCaptionsError:
            raise
        except Exception as exc:
            self._text_block = None
            raise LiveCaptionsError(str(exc)) from exc

    def kill(self) -> None:
        self._window = None
        self._text_block = None
        _kill_livecaptions_processes()


# --------------------------------------------------------------------------- #
# CaptionService — sync + translate threads
# --------------------------------------------------------------------------- #
DisplayCallback = Callable[[dict], None]
# dict keys: status, original, translated, original_live, error, paused


class CaptionService:
    """
    Background LiveCaptions → translate → on_display(dict).

    ``on_display`` is called from worker threads; TUI must use call_from_thread.

    Uses a **dedicated** translator with caption language pair (default:
    inverted voice pair so EN captions → PT when voice is BR→EN).
    """

    def __init__(
        self,
        config,
        translator=None,
        on_display: Optional[DisplayCallback] = None,
        *,
        log_to_ui: bool = True,
        session_id: Optional[str] = None,
        phrase_cache=None,
        pipeline=None,
    ):
        self.cfg = config
        # `translator` arg kept for API compat; captions use dedicated instance.
        self._voice_translator = translator
        self.on_display = on_display
        self.log_to_ui = log_to_ui
        self.session_id = session_id
        self.phrase_cache = phrase_cache
        self.pipeline = pipeline

        self._reader = LiveCaptionsReader()
        self._stop = threading.Event()
        self._paused = False
        self._pending: queue.Queue = queue.Queue()
        self._threads: list[threading.Thread] = []
        self._lock = threading.Lock()
        self._started = False
        self._last_original = ""
        self._last_translated = ""
        self._last_live = ""
        self._status = "idle"
        self._error: Optional[str] = None
        self._caption_src, self._caption_tgt = caption_lang_pair(config)
        self.translator = build_caption_translator(
            config, self._caption_src, self._caption_tgt
        )
        # Utterance state: grow/revise one open block; log only when stable/final
        self._open_src = ""
        self._open_tgt = ""
        self._open_from_cache = False
        self._last_logged_src = ""
        self._lc_seq = 0
        self._last_partial_translate_t = 0.0
        self._local_chunk_cursor = 0  # fallback when no pipeline

    # -- public API -------------------------------------------------------- #
    def bind(
        self,
        *,
        session_id: Optional[str] = None,
        phrase_cache=None,
        pipeline=None,
    ) -> None:
        """Attach session / cache / pipeline after construction (main.py)."""
        if session_id is not None:
            self.session_id = session_id
        if phrase_cache is not None:
            self.phrase_cache = phrase_cache
        if pipeline is not None:
            self.pipeline = pipeline
            if self.session_id is None:
                self.session_id = getattr(pipeline, "session_id", None)
            if self.phrase_cache is None:
                self.phrase_cache = getattr(pipeline, "phrase_cache", None)
        self._warmup_caption_cache()

    def _warmup_caption_cache(self) -> None:
        """Load translation_pairs + LC chunks for caption lang pair into RAM.

        Also warms the **reverse** direction (e.g. PT→EN when LC is EN→PT)
        so inverted pairs stored from LC are available for voice HIT.
        """
        cache = self.phrase_cache
        if cache is None or not getattr(cache, "enabled", False):
            return
        try:
            src, tgt = caption_lang_pair(self.cfg)
            n = cache.warmup(src, tgt, chunk_origin="livecaptions")
            n_rev = 0
            if bool(getattr(self.cfg, "PHRASE_CACHE_LC_ALSO_REVERSE", True)):
                if (src or "").lower() != (tgt or "").lower():
                    # Reverse pairs live in translation_pairs (no LC chunk origin)
                    n_rev = cache.warmup(tgt, src, chunk_origin=None)
            if getattr(self.cfg, "PHRASE_CACHE_LOG", True) or getattr(
                self.cfg, "VERBOSE", False
            ):
                extra = f" · rev {tgt}→{src}: {n_rev}" if n_rev else ""
                ui.dim(
                    f"LC phrase cache warm-up {src}→{tgt}: {n} pair(s){extra}",
                    indent=0,
                    panel="app",
                )
        except Exception:
            pass

    def is_running(self) -> bool:
        return self._started and not self._stop.is_set()

    def is_paused(self) -> bool:
        return self._paused

    def set_display_callback(self, cb: Optional[DisplayCallback]) -> None:
        self.on_display = cb

    def start(self) -> None:
        if self._started:
            return
        if not getattr(self.cfg, "LIVE_CAPTIONS_ENABLED", True):
            self._emit(
                status="disabled",
                error="LIVE_CAPTIONS_ENABLED=false",
            )
            return
        if not is_windows():
            self._emit(status="error", error="Só Windows")
            return

        self._stop.clear()
        self._started = True
        self._status = "starting"
        self._emit(status="starting")

        t = threading.Thread(
            target=self._bootstrap,
            name="livecaptions-boot",
            daemon=True,
        )
        self._threads.append(t)
        t.start()

    def stop(self) -> None:
        self._stop.set()
        self._started = False
        try:
            self._commit_open_to_log()
        except Exception:
            pass
        try:
            hide = bool(getattr(self.cfg, "LIVE_CAPTIONS_HIDE_WINDOW", True))
            # Leave LiveCaptions running unless configured to kill
            if bool(getattr(self.cfg, "LIVE_CAPTIONS_KILL_ON_EXIT", False)):
                self._reader.kill()
            elif hide:
                # keep process; just leave hidden
                pass
        except Exception:
            pass
        self._status = "stopped"
        self._emit(status="stopped")

    def pause(self) -> None:
        self._paused = True
        try:
            self._commit_open_to_log()
        except Exception:
            pass
        self._emit(status="paused", paused=True)

    def resume(self) -> None:
        self._paused = False
        self._emit(status="running", paused=False)

    def toggle_pause(self) -> bool:
        if self._paused:
            self.resume()
        else:
            self.pause()
        return self._paused

    def show_window(self) -> None:
        try:
            self._reader.restore()
            self._emit(status=self._status)
        except Exception as exc:
            self._emit(status="error", error=str(exc))

    def hide_window(self) -> None:
        try:
            self._reader.hide()
            self._emit(status=self._status)
        except Exception as exc:
            self._emit(status="error", error=str(exc))

    def snapshot(self) -> dict:
        src, tgt = self._ensure_lang_pair()
        return {
            "status": self._status,
            "original": self._last_original,
            "translated": self._last_translated,
            "original_live": self._last_live,
            "error": self._error,
            "paused": self._paused,
            "running": self.is_running(),
            "hidden": self._reader.is_hidden(),
            "caption_source_lang": src,
            "caption_target_lang": tgt,
        }

    def _ensure_lang_pair(self) -> tuple[str, str]:
        """Refresh caption pair if voice [g] swap / config changed."""
        src, tgt = caption_lang_pair(self.cfg)
        if (src, tgt) != (self._caption_src, self._caption_tgt):
            self._caption_src, self._caption_tgt = src, tgt
            try:
                self.translator = build_caption_translator(self.cfg, src, tgt)
            except Exception:
                pass
        return src, tgt

    # -- internal ---------------------------------------------------------- #
    def _emit(self, **kwargs) -> None:
        data = self.snapshot()
        data.update(kwargs)
        if "original" in kwargs:
            self._last_original = kwargs["original"] or ""
        if "translated" in kwargs:
            self._last_translated = kwargs["translated"] or ""
        if "original_live" in kwargs:
            self._last_live = kwargs["original_live"] or ""
        if "status" in kwargs:
            self._status = kwargs["status"]
        if "error" in kwargs:
            self._error = kwargs["error"]
        if "paused" in kwargs:
            self._paused = bool(kwargs["paused"])
        cb = self.on_display
        if cb is not None:
            try:
                cb(data)
            except Exception:
                pass

    def _bootstrap(self) -> None:
        hide = bool(getattr(self.cfg, "LIVE_CAPTIONS_HIDE_WINDOW", True))
        try:
            self._reader.launch(hide=hide)
        except Exception as exc:
            self._started = False
            self._emit(status="error", error=str(exc))
            ui.warn(f"LiveCaptions: {exc}", indent=0, panel="app")
            return

        self._status = "running"
        src, tgt = self._ensure_lang_pair()
        self._emit(status="running", error=None)
        pair = f"{src.upper()}→{tgt.upper()}"
        ui.info(
            f"LiveCaptions ON — scrape OK · traduz {pair} "
            f"(par independente da voz; invert="
            f"{bool(getattr(self.cfg, 'LIVE_CAPTIONS_INVERT_LANGS', True))}).",
            indent=0,
            panel="app",
        )
        ui.dim(
            "Dica: idioma de fala no LiveCaptions (⚙️ Windows) deve bater com "
            f"LC source ({src}). Comandos: [lc] · [lc show]/[lc hide].",
            indent=0,
            panel="app",
        )

        t_sync = threading.Thread(
            target=self._sync_loop, name="livecaptions-sync", daemon=True
        )
        t_tr = threading.Thread(
            target=self._translate_loop, name="livecaptions-tr", daemon=True
        )
        self._threads.extend([t_sync, t_tr])
        t_sync.start()
        t_tr.start()

    def _sync_loop(self) -> None:
        """
        Scrape captions → pending queue.

        Emits:
          ("partial", text) — growing revision (panel only after translate)
          ("stable", text)  — idle/no growth → candidate to **log once**
          ("final", text)   — EOS boundary + not a micro-false-period spam

        Queue is coalesced in translate loop (latest wins for same lineage).
        """
        idle_count = 0
        sync_count = 0
        original_caption = ""
        poll_ms = int(getattr(self.cfg, "LIVE_CAPTIONS_POLL_MS", 25) or 25)
        max_idle = int(getattr(self.cfg, "LIVE_CAPTIONS_MAX_IDLE", 50) or 50)
        max_sync = int(getattr(self.cfg, "LIVE_CAPTIONS_MAX_SYNC", 3) or 3)
        sleep_s = max(0.01, poll_ms / 1000.0)
        partial_min_s = float(
            getattr(self.cfg, "LIVE_CAPTIONS_PARTIAL_INTERVAL_S", 0.7) or 0.7
        )

        while not self._stop.is_set():
            if self._paused:
                time.sleep(0.1)
                continue
            try:
                if not self._reader.alive():
                    self._emit(
                        status="restarting",
                        error="LiveCaptions fechou — reiniciando…",
                    )
                    try:
                        hide = bool(
                            getattr(self.cfg, "LIVE_CAPTIONS_HIDE_WINDOW", True)
                        )
                        self._reader.launch(hide=hide)
                        self._emit(status="running", error=None)
                    except Exception as exc:
                        self._emit(status="error", error=str(exc))
                        time.sleep(2.0)
                    continue

                full_text = self._reader.get_captions()
            except LiveCaptionsError:
                time.sleep(0.2)
                continue
            except Exception:
                time.sleep(0.2)
                continue

            if not full_text:
                time.sleep(sleep_s)
                continue

            full_text = preprocess_captions(full_text)

            # Last sentence (partial or complete)
            if _ends_with_eos(full_text):
                last_eos = _last_eos_index(full_text[:-1])
            else:
                last_eos = _last_eos_index(full_text)
            latest = full_text[last_eos + 1 :]

            # Extend short last sentence with previous
            if last_eos > 0 and _utf8_len(latest) < SHORT_THRESHOLD:
                prev = _last_eos_index(full_text[:last_eos])
                latest = full_text[prev + 1 :]
                last_eos = prev

            live_display = shorten_display(latest)
            self._emit(original_live=live_display, status="running")

            # Prefer full latest line (including mid-stream growth), not only
            # truncated-at-first-EOS — LiveCaptions rewrites the whole phrase.
            to_translate = (latest or "").strip()
            if not to_translate:
                time.sleep(sleep_s)
                continue

            if to_translate != original_caption:
                prev_cap = original_caption
                original_caption = to_translate
                idle_count = 0
                grew = bool(prev_cap) and is_same_utterance(prev_cap, to_translate)
                if (
                    _ends_with_eos(to_translate)
                    and _utf8_len(to_translate) >= SHORT_THRESHOLD
                ):
                    # EOS may be false mid-stream; still mark as partial if
                    # growing, "final" only if long enough — translate loop
                    # decides commit via stability / new utterance.
                    kind = "partial" if grew else "final"
                    self._pending.put((kind, to_translate))
                    sync_count = 0
                elif _utf8_len(to_translate) >= SHORT_THRESHOLD:
                    sync_count += 1
                    now = time.monotonic()
                    if now - self._last_partial_translate_t >= partial_min_s:
                        self._last_partial_translate_t = now
                        self._pending.put(("partial", to_translate))
            else:
                idle_count += 1

            # Stable: no change for max_idle ticks → commit candidate
            if original_caption and idle_count == max_idle:
                sync_count = 0
                self._pending.put(("stable", original_caption))
            elif original_caption and sync_count > max_sync:
                sync_count = 0
                now = time.monotonic()
                if now - self._last_partial_translate_t >= partial_min_s:
                    self._last_partial_translate_t = now
                    self._pending.put(("partial", original_caption))

            time.sleep(sleep_s)

    def _translate_loop(self) -> None:
        """
        Translate captions for the strip; log Tradução **once per utterance**.

        - Coalesce queue → only latest text
        - Same growing utterance → update open block, no log spam
        - stable / new utterance / final → commit previous open as [LC n] block
        """
        while not self._stop.is_set():
            try:
                item = self._pending.get(timeout=0.15)
            except queue.Empty:
                continue
            if self._paused or self._stop.is_set():
                continue

            # Coalesce: drain queue, keep latest event (prefer stable/final)
            kind, text = self._unpack_pending(item)
            best_kind, best_text = kind, text
            while True:
                try:
                    nxt = self._pending.get_nowait()
                except queue.Empty:
                    break
                k, t = self._unpack_pending(nxt)
                if not t:
                    continue
                # Prefer higher-priority kind if same lineage, else latest text
                if is_same_utterance(best_text, t) or not best_text:
                    if k in ("stable", "final") or best_kind == "partial":
                        best_kind = k if k in ("stable", "final") else best_kind
                    if len(t) >= len(best_text) or not is_same_utterance(best_text, t):
                        best_text = t
                    if k in ("stable", "final"):
                        best_kind = k
                else:
                    # New utterance waiting — process current first, re-queue new
                    self._pending.put((k, t))
                    break

            text = (best_text or "").strip()
            kind = best_kind
            if not text:
                continue

            # New utterance while we still have an open uncommitted block?
            if self._open_src and not is_same_utterance(self._open_src, text):
                self._commit_open_to_log()

            # Skip identical open (already translated)
            if text == self._open_src and kind == "partial":
                continue
            if _normalize_for_match(text) == _normalize_for_match(
                self._last_logged_src
            ) and kind in ("stable", "final"):
                continue

            self._emit(original=text, status="translating")
            try:
                # Only consume [pc force] on stable commits (not mid-stream partials)
                translated, from_cache = self._translate_with_cache(
                    text, allow_force=(kind == "stable")
                )
            except Exception as exc:
                translated = f"[ERROR] {exc}"
                from_cache = False
                self._emit(
                    original=text,
                    translated=translated,
                    status="error",
                    error=str(exc),
                )
                continue

            self._open_src = text
            self._open_tgt = translated
            self._open_from_cache = bool(from_cache)
            self._emit(
                original=text,
                translated=translated,
                status="running",
                error=None,
            )

            # Commit only on idle/stable — never on every growth/false EOS.
            # New utterance already committed previous open above.
            if kind == "stable":
                self._commit_open_to_log()

    @staticmethod
    def _unpack_pending(item) -> tuple[str, str]:
        if isinstance(item, tuple) and len(item) == 2:
            return str(item[0] or "partial"), str(item[1] or "")
        return "partial", str(item or "")

    def _translate_with_cache(
        self, text: str, *, allow_force: bool = False
    ) -> tuple[str, bool]:
        """
        Translate using **caption** lang pair only (strip EN→BR, not voice).

        Returns (translated_text, from_cache).
        Lookup uses phrase_cache keys caption_source→caption_target.
        Store happens only on stable commit (_commit_open_to_log).
        """
        self._ensure_lang_pair()
        src_lang, tgt_lang = self._caption_src, self._caption_tgt
        text = (text or "").strip()
        if not text:
            return "", False

        cache = self.phrase_cache
        force_live = False
        if allow_force and cache is not None and getattr(cache, "enabled", False):
            try:
                force_live = bool(cache.consume_force_next())
            except Exception:
                force_live = False

        cache_hit = False
        translated = None
        if cache is not None and getattr(cache, "enabled", False) and not force_live:
            try:
                cached = cache.lookup(src_lang, tgt_lang, text)
            except Exception:
                cached = None
            if cached:
                translated = cached
                cache_hit = True
                if getattr(self.cfg, "PHRASE_CACHE_LOG", True) or getattr(
                    self.cfg, "VERBOSE", False
                ):
                    ui.dim(
                        f"[LC] cache HIT · {src_lang}→{tgt_lang} · "
                        f'"{text[:48]}" → "{(translated or "")[:48]}"',
                        panel="app",
                    )

        if not cache_hit:
            translated = self._do_translate_live(text)
            if (
                (
                    getattr(self.cfg, "PHRASE_CACHE_LOG", True)
                    or getattr(self.cfg, "VERBOSE", False)
                )
                and cache is not None
                and getattr(cache, "enabled", False)
            ):
                ui.dim(
                    f"[LC] cache MISS · {src_lang}→{tgt_lang} (store on commit)",
                    panel="app",
                )

        return (translated or text), cache_hit

    def _do_translate_live(self, text: str) -> str:
        tr = self.translator
        if tr is None:
            return text
        if hasattr(tr, "translate"):
            result = tr.translate(text)
            out = (result or "").strip()
            return out if out else text
        return text

    def _alloc_chunk_num(self) -> int:
        """Session chunk_num shared with voice pipeline when possible."""
        pl = self.pipeline
        if pl is not None and hasattr(pl, "_alloc_chunk_num"):
            try:
                return int(pl._alloc_chunk_num())
            except Exception:
                pass
        # Fallback: DB max + local cursor
        sid = self.session_id
        if sid:
            try:
                from . import db

                base = db.next_session_chunk_num(sid)
            except Exception:
                base = 1
        else:
            base = 1
        with self._lock:
            self._local_chunk_cursor = max(self._local_chunk_cursor + 1, base)
            return self._local_chunk_cursor

    def _persist_to_db(
        self,
        chunk_num: int,
        original: str,
        translated: str,
        *,
        from_cache: bool,
    ) -> None:
        """
        Upsert chunks row + session transcript.

        timing_json marks source=livecaptions and caption lang pair (not voice).
        """
        src_lang, tgt_lang = self._ensure_lang_pair()
        timing = {
            "source": "livecaptions",
            "origin": "livecaptions",
            "caption_source_lang": src_lang,
            "caption_target_lang": tgt_lang,
            "translate_cache": bool(from_cache),
        }
        sid = self.session_id or getattr(self.pipeline, "session_id", None)
        pl = self.pipeline

        if pl is not None and hasattr(pl, "_persist_text_only") and sid:
            try:
                pl._persist_text_only(chunk_num, original, translated, timing=timing)
                with getattr(pl, "history_lock", threading.Lock()):
                    if hasattr(pl, "history"):
                        pl.history.append((chunk_num, original, translated, ""))
                return
            except Exception as exc:
                ui.dim(f"[LC] pipeline persist falhou: {exc}", panel="app")

        if not sid:
            return
        try:
            from . import db

            db.upsert_chunk(
                sid,
                chunk_num,
                original,
                translated,
                "",
                timing=timing,
            )
        except Exception as exc:
            ui.warn(f"[LC] SQLite falhou: {exc}", panel="app")

    def _commit_open_to_log(self) -> None:
        """Log final block + persist SQLite/cache already applied on translate."""
        src = (self._open_src or "").strip()
        tgt = (self._open_tgt or "").strip()
        from_cache = bool(self._open_from_cache)
        if not src:
            return
        if _normalize_for_match(src) == _normalize_for_match(self._last_logged_src):
            return
        # Too short / noise
        if _utf8_len(src) < 4:
            return
        if str(tgt).startswith("[ERROR]"):
            # Still show in strip; skip dirty DB row
            self._last_logged_src = src
            self._open_src = ""
            self._open_tgt = ""
            self._open_from_cache = False
            return

        self._last_logged_src = src
        chunk_num = self._alloc_chunk_num()
        self._lc_seq += 1
        n = self._lc_seq

        # Phrase cache: store final caption under **caption** lang pair
        # (e.g. EN→PT). Optionally also store the **inverted** pair (PT→EN)
        # with the same texts swapped — grows the opposite-direction TM so
        # voice PT→EN can HIT phrases learned from LC EN→PT.
        #
        # - Forward store: only on MISS (not from_cache)
        # - Reverse store: also on HIT (backfill PT→EN for older LC-only pairs)
        cache = self.phrase_cache
        if (
            cache is not None
            and getattr(cache, "enabled", False)
            and tgt
            and not str(tgt).startswith("[ERROR]")
        ):
            try:
                cap_src, cap_tgt = self._ensure_lang_pair()
                also_rev = bool(getattr(self.cfg, "PHRASE_CACHE_LC_ALSO_REVERSE", True))
                from .phrase_cache import normalize_phrase

                ns, nt = normalize_phrase(src), normalize_phrase(tgt)
                distinct = bool(ns and nt and ns != nt)
                # Reverse first so last_event / pc last reflects the forward LC pair.
                if (
                    also_rev
                    and distinct
                    and (cap_src or "").lower() != (cap_tgt or "").lower()
                ):
                    try:
                        cache.store(
                            cap_tgt,
                            cap_src,
                            tgt,
                            src,
                            from_force=False,
                        )
                    except Exception:
                        pass
                if not from_cache:
                    cache.store(cap_src, cap_tgt, src, tgt, from_force=False)
                if (
                    getattr(self.cfg, "PHRASE_CACHE_LOG", True)
                    or getattr(self.cfg, "VERBOSE", False)
                ) and (not from_cache or also_rev):
                    bits = []
                    if not from_cache:
                        bits.append(f"{cap_src}→{cap_tgt}")
                    if also_rev and distinct:
                        bits.append(f"{cap_tgt}→{cap_src}")
                    if bits:
                        ui.dim(
                            f"[LC {n}] cache STORE · " + " + ".join(bits),
                            panel="app",
                        )
            except Exception:
                pass

        # SQLite session chunk (caption langs in timing_json)
        try:
            self._persist_to_db(chunk_num, src, tgt, from_cache=from_cache)
        except Exception as exc:
            ui.dim(f"[LC] persist: {exc}", panel="app")

        if self.log_to_ui and bool(getattr(self.cfg, "LIVE_CAPTIONS_LOG", True)):
            try:
                ui.live_caption_block(
                    n, src, tgt, from_cache=True if from_cache else False
                )
            except Exception:
                try:
                    ui.info(f"[LC {n}] Caption: {src}", indent=0, panel="main")
                    ui.success(f"[LC {n}] Translated: {tgt}", indent=0, panel="main")
                except Exception:
                    pass

        self._open_src = ""
        self._open_tgt = ""
        self._open_from_cache = False

    def _do_translate(self, text: str) -> str:
        """Legacy alias — prefer _translate_with_cache."""
        out, _ = self._translate_with_cache(text)
        return out


def build_caption_service(
    config,
    translator=None,
    on_display=None,
    *,
    session_id=None,
    phrase_cache=None,
    pipeline=None,
) -> CaptionService:
    """Factory used by main.py."""
    log = bool(getattr(config, "LIVE_CAPTIONS_LOG", True))
    svc = CaptionService(
        config=config,
        translator=translator,
        on_display=on_display,
        log_to_ui=log,
        session_id=session_id,
        phrase_cache=phrase_cache,
        pipeline=pipeline,
    )
    if session_id or phrase_cache is not None or pipeline is not None:
        svc.bind(
            session_id=session_id,
            phrase_cache=phrase_cache,
            pipeline=pipeline,
        )
    return svc
