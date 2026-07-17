"""
ui.py
=====
Tiny helpers for colored, readable terminal output. Uses colorama so the ANSI
colors work on every Windows console (legacy conhost included).
"""

import os
import sys
import threading
from colorama import Fore, Style, init

# autoreset=True -> every print resets the color automatically afterwards.
init(autoreset=True)

# Serialize all terminal writes — sound-OFF parallel workers otherwise interleave
# Heard/Translated lines with filter messages and corrupt the display.
_print_lock = threading.RLock()

# Optional TUI sink: callable(kind: str, text: str) — when set, prints go there.
_log_sink = None


def set_log_sink(sink):
    """Route ui.* output to a TUI log (or None to restore classic terminal)."""
    global _log_sink
    with _print_lock:
        _log_sink = sink


def get_log_sink():
    return _log_sink


def _emit(kind, text):
    """kind: info|success|warn|error|dim|raw"""
    sink = _log_sink
    if sink is not None:
        try:
            sink(kind, text)
            return True
        except Exception:
            return False
    return False


__all__ = [
    "banner",
    "info",
    "success",
    "warn",
    "error",
    "dim",
    "device_line",
    "chunk_status",
    "chunk_timings",
    "format_timing_line",
    "clock_hhmmss",
    "format_recorded_stamp",
    "resolve_share_path",
    "format_audio_lines",
    "print_audio_ref",
    "chunk_text_preview",
    "chunk_stream_start",
    "chunk_stream_update",
    "chunk_stream_done",
    "synonyms_result",
    "favorites_popup",
    "set_log_sink",
    "get_log_sink",
]


def _term_width():
    try:
        return max(40, os.get_terminal_size().columns)
    except OSError:
        return 80


def _one_line(text, budget):
    """Collapse whitespace and truncate so the string fits one terminal line."""
    text = " ".join((text or "").split())
    if budget < 2:
        return "…"
    if len(text) <= budget:
        return text
    return text[: budget - 1] + "…"


def _pad(indent):
    """Left margin spaces (e.g. menu/list blocks use indent=3)."""
    try:
        n = int(indent or 0)
    except (TypeError, ValueError):
        n = 0
    return " " * max(0, n)


def banner(indent=3):
    """Print the startup banner (default 3-char left margin)."""
    pad = _pad(indent)
    line = "=" * 64
    with _print_lock:
        if _emit("info", line):
            _emit("info", "L I V E L I N G O   🎙️  ->  🌍")
            _emit("dim", "Real-time speech translation into a virtual mic")
            _emit("dim", "mic -> Whisper -> translate -> Edge TTS -> VB-Cable")
            _emit("info", line)
            return
        print(pad + Fore.CYAN + line)
        print(
            pad
            + Fore.CYAN
            + Style.BRIGHT
            + "        L I V E L I N G O   \U0001f399️  ->  \U0001f30d"
        )
        print(
            pad
            + Fore.CYAN
            + "        Real-time speech translation into a virtual mic"
        )
        print(
            pad
            + Fore.CYAN
            + "        mic -> Whisper -> translate -> Edge TTS -> VB-Cable"
        )
        print(pad + Fore.CYAN + line + Style.RESET_ALL)


def info(msg, indent=0):
    text = str(msg)
    with _print_lock:
        if _emit("info", text):
            return
        print(
            "\r\033[K"
            + _pad(indent)
            + Fore.CYAN
            + "[i] "
            + Style.RESET_ALL
            + text
        )


def success(msg, indent=0):
    text = str(msg)
    with _print_lock:
        if _emit("success", text):
            return
        print(
            "\r\033[K"
            + _pad(indent)
            + Fore.GREEN
            + "[ok] "
            + Style.RESET_ALL
            + text
        )


def warn(msg, indent=0):
    text = str(msg)
    with _print_lock:
        if _emit("warn", text):
            return
        print(
            "\r\033[K"
            + _pad(indent)
            + Fore.YELLOW
            + "[!] "
            + Style.RESET_ALL
            + text
        )


def error(msg, indent=0):
    text = str(msg)
    with _print_lock:
        if _emit("error", text):
            return
        print(
            "\r\033[K"
            + _pad(indent)
            + Fore.RED
            + Style.BRIGHT
            + "[x] "
            + Style.RESET_ALL
            + Fore.RED
            + text
        )


def dim(msg, indent=0):
    text = str(msg)
    with _print_lock:
        if _emit("dim", text):
            return
        print(
            "\r\033[K"
            + _pad(indent)
            + Style.DIM
            + text
            + Style.RESET_ALL
        )


def device_line(role, index, name, indent=0):
    """Pretty one-liner confirming a selected device."""
    idx = "default" if index is None else f"#{index}"
    line = f"{role:<8} {idx:>8}  {name}"
    with _print_lock:
        if _emit("info", line):
            return
        print(
            "\r\033[K"
            + _pad(indent)
            + Fore.MAGENTA
            + f"  {role:<8}"
            + Style.RESET_ALL
            + f" {idx:>8}  "
            + Fore.WHITE
            + Style.BRIGHT
            + str(name)
        )


def chunk_status(n, heard, translated, timings, finalize=False, at=None):
    """
    Print the live per-chunk status line plus a dim timing breakdown.

    timings: dict with keys stt, translate, tts, total (seconds).
    finalize: when True, print the trailing blank line after timings.
    at: optional timestamp for HH:MM:SS on the timing line (default: now).
    """
    heard = (heard or "").strip()
    translated = (translated or "").strip()
    prefix = f"[chunk {n}] "
    indent = " " * len(prefix)
    timing = format_timing_line(timings or {}, at=at)

    with _print_lock:
        if _log_sink is not None:
            _emit("success", f"{prefix}Heard: {heard}")
            _emit("info", f"{indent}Translated: {translated}")
            if timing:
                _emit("dim", f"{indent}{timing}")
            return
        print(
            "\r\033[K"
            + Fore.YELLOW
            + Style.BRIGHT
            + prefix
            + Style.RESET_ALL
            + Fore.WHITE
            + "Heard: "
            + Fore.GREEN
            + heard
        )
        print(
            "\r\033[K"
            + indent
            + Fore.BLUE
            + Style.BRIGHT
            + "Translated: "
            + Style.RESET_ALL
            + Fore.WHITE
            + translated
        )
        if timing:
            print("\r\033[K" + indent + Style.DIM + timing + Style.RESET_ALL)
        if finalize:
            print()


def chunk_text_preview(n, heard, translated):
    """Show heard + translated without timing (timing comes after TTS)."""
    heard = (heard or "").strip()
    translated = (translated or "").strip()
    prefix = f"[chunk {n}] "
    indent = " " * len(prefix)
    with _print_lock:
        if _log_sink is not None:
            _emit("success", f"{prefix}Heard: {heard}")
            _emit("info", f"{indent}Translated: {translated}")
            return
        print(
            "\r\033[K"
            + Fore.YELLOW
            + Style.BRIGHT
            + prefix
            + Style.RESET_ALL
            + Fore.WHITE
            + "Heard: "
            + Fore.GREEN
            + heard
        )
        print(
            "\r\033[K"
            + indent
            + Fore.BLUE
            + Style.BRIGHT
            + "Translated: "
            + Style.RESET_ALL
            + Fore.WHITE
            + translated
        )


def clock_hhmmss(stamp=None):
    """
    Return HH:MM:SS for a DB/local timestamp string, datetime, or now.
    Accepts 'YYYY-MM-DD HH:MM:SS', 'HH:MM:SS', or empty → current time.
    """
    import datetime as _dt

    if stamp is None or stamp == "":
        return _dt.datetime.now().strftime("%H:%M:%S")
    if hasattr(stamp, "strftime"):
        return stamp.strftime("%H:%M:%S")
    text = str(stamp).strip()
    # '2026-07-16 14:32:05' or '14:32:05'
    if len(text) >= 19 and text[10] in (" ", "T"):
        return text[11:19]
    if len(text) >= 8 and text[2] == ":" and text[5] == ":":
        return text[:8]
    return _dt.datetime.now().strftime("%H:%M:%S")


def format_recorded_stamp(stamp):
    """Human label for DB created_at (date + time)."""
    if not stamp:
        return ""
    text = str(stamp).strip().replace("T", " ")
    if len(text) >= 19:
        return text[:19]
    return text


def resolve_share_path(path):
    """
    Absolute path suited for attaching/opening on the host OS.

    Converts WSL ``/mnt/c/...`` → ``C:\\...`` so Explorer / Teams / WhatsApp
    Desktop can open the file when LiveLingo runs under WSL.
    """
    import os
    import re

    if not path:
        return ""
    text = str(path).strip()
    if not text:
        return ""
    try:
        abs_path = os.path.abspath(text)
    except OSError:
        abs_path = text

    # WSL mount: /mnt/c/Users/... → C:\Users\...
    m = re.match(r"^/mnt/([a-zA-Z])/(.*)$", abs_path.replace("\\", "/"))
    if m:
        drive = m.group(1).upper()
        rest = m.group(2).replace("/", "\\")
        return f"{drive}:\\{rest}"

    # Already Windows-style or native POSIX outside /mnt
    if os.name == "nt":
        return os.path.normpath(abs_path)
    return abs_path


def format_audio_lines(path, missing_hint="(ainda não gerado — use r / rN)"):
    """
    Return list of plain display lines for a chunk audio reference.
    Empty path → one 'sem áudio' line. Existing path → full file path only
    (folder alone is redundant — path already includes directory + filename).
    """
    import os

    if not path or not str(path).strip():
        return [f"audio: {missing_hint}"]

    share = resolve_share_path(path)
    exists = False
    try:
        exists = os.path.isfile(path) or os.path.isfile(share)
    except OSError:
        exists = False

    if not exists:
        return [f"audio: {share or path}  (arquivo ausente — use r / rN)"]

    return [f"audio: {share}"]


def print_audio_ref(n, path, indent=None):
    """Print dim audio lines under a chunk block (same indent as timing)."""
    prefix = f"[chunk {n}] "
    pad = " " * len(prefix) if indent is None else " " * int(indent)
    lines = format_audio_lines(path)
    with _print_lock:
        if _log_sink is not None:
            for line in lines:
                _emit("dim", f"{pad}{line}")
            return
        for line in lines:
            print("\r\033[K" + pad + Style.DIM + line + Style.RESET_ALL)


def format_timing_line(timings, extra=None, at=None, include_clock=True):
    """
    Build the same timing string used in live logs, e.g.
    timing: STT 1.61s | translate 0.73s | TTS 15.47s | ... | 14:32:05

    at: DB/local stamp used for HH:MM:SS (if set).
    include_clock: when True and at is empty, use current time (live logs).
    """
    if not timings:
        if include_clock and at:
            return f"timing: — | {clock_hhmmss(at)}"
        return ""
    parts = []
    if "stt" in timings:
        parts.append("STT {stt:.2f}s".format(**timings))
    if "translate" in timings:
        parts.append("translate {translate:.2f}s".format(**timings))
    if timings.get("tts_skipped"):
        parts.append("TTS —")
    elif "tts" in timings:
        parts.append("TTS {tts:.2f}s".format(**timings))
    if timings.get("tts_first") is not None:
        parts.append("first_audio {tts_first:.2f}s".format(**timings))
    if timings.get("tts_start") is not None:
        parts.append("tts_start {tts_start:.2f}s".format(**timings))
    if timings.get("time_to_audio") is not None:
        parts.append("hear {time_to_audio:.2f}s".format(**timings))
    if "total" in timings:
        parts.append("total {total:.2f}s".format(**timings))
    if not parts:
        return ""
    line = "timing: " + " | ".join(parts)
    if extra:
        line = f"{line}  {extra}"
    # Right-side clock: when the translation was produced / recorded.
    if at:
        line = f"{line} | {clock_hhmmss(at)}"
    elif include_clock:
        line = f"{line} | {clock_hhmmss()}"
    return line


def chunk_timings(n, timings, extra=None, at=None, audio_path=None):
    """Print final timing line once TTS completes (includes HH:MM:SS)."""
    prefix = f"[chunk {n}] "
    indent = " " * len(prefix)
    timing = format_timing_line(timings, extra=extra, at=at, include_clock=True)
    with _print_lock:
        if _log_sink is not None:
            if timing:
                _emit("dim", f"{indent}{timing}")
            if audio_path is not None:
                for line in format_audio_lines(audio_path):
                    _emit("dim", f"{indent}{line}")
            return
        if timing:
            print("\r\033[K" + indent + Style.DIM + timing + Style.RESET_ALL)
        if audio_path is not None:
            for line in format_audio_lines(audio_path):
                print("\r\033[K" + indent + Style.DIM + line + Style.RESET_ALL)
        print()


def chunk_stream_start(n, heard):
    """
    Print heard + empty translated line for streaming updates.

    Both lines are forced to a single terminal row so \\033[1A updates stay
    aligned even when the monologue would otherwise wrap.
    """
    heard = (heard or "").strip()
    prefix = f"[chunk {n}] "
    indent = " " * len(prefix)
    width = _term_width()
    heard_budget = max(8, width - len(prefix) - len("Heard: ") - 1)
    heard_disp = _one_line(heard, heard_budget)
    with _print_lock:
        if _log_sink is not None:
            _emit("success", f"{prefix}Heard: {heard_disp}")
            _emit("info", f"{indent}Translated: …")
            return
        print(
            "\r\033[K"
            + Fore.YELLOW
            + Style.BRIGHT
            + prefix
            + Style.RESET_ALL
            + Fore.WHITE
            + "Heard: "
            + Fore.GREEN
            + heard_disp
        )
        print(
            "\r\033[K"
            + indent
            + Fore.BLUE
            + Style.BRIGHT
            + "Translated: "
            + Style.RESET_ALL
            + Fore.WHITE
            + "…"
        )


def chunk_stream_update(n, translated):
    """Overwrite the single-line translated row while LLM tokens stream in."""
    translated = (translated or "").strip() or "…"
    prefix = f"[chunk {n}] "
    indent = " " * len(prefix)
    width = _term_width()
    budget = max(8, width - len(indent) - len("Translated: ") - 1)
    disp = _one_line(translated, budget)
    with _print_lock:
        if _log_sink is not None:
            # TUI: append stream ticks lightly (no cursor-up).
            _emit("dim", f"{indent}Translated: {disp}")
            return
        sys.stdout.write(
            "\033[1A\r\033[K"
            + indent
            + Fore.BLUE
            + Style.BRIGHT
            + "Translated: "
            + Style.RESET_ALL
            + Fore.WHITE
            + disp
            + "\n"
        )
        sys.stdout.flush()


def chunk_stream_done(n, heard, translated):
    """
    Finalize streamed block: rewrite both lines with full text (may wrap).

    Cursor sits after the single-line Translated row from streaming, so we
    move up two rows and replace the compact stream block with the full preview.
    """
    heard = (heard or "").strip()
    translated = (translated or "").strip()
    prefix = f"[chunk {n}] "
    indent = " " * len(prefix)
    with _print_lock:
        if _log_sink is not None:
            _emit("success", f"{prefix}Heard: {heard}")
            _emit("info", f"{indent}Translated: {translated}")
            return
        # Clear compact Heard + Translated rows, then print full text.
        sys.stdout.write("\033[2A\r\033[K")
        print(
            Fore.YELLOW
            + Style.BRIGHT
            + prefix
            + Style.RESET_ALL
            + Fore.WHITE
            + "Heard: "
            + Fore.GREEN
            + heard
        )
        print(
            "\r\033[K"
            + indent
            + Fore.BLUE
            + Style.BRIGHT
            + "Translated: "
            + Style.RESET_ALL
            + Fore.WHITE
            + translated
        )
        sys.stdout.flush()


def synonyms_result(word, text):
    """Print the synonym explanation result elegantly and colored."""
    print()
    print("\r\033[K" + Fore.CYAN + "=" * 64)
    print("\r\033[K" + Fore.CYAN + Style.BRIGHT + f" Requested word: {word.upper()}")
    print("\r\033[K" + Fore.CYAN + "=" * 64)
    print("\r\033[K" + Fore.YELLOW + Style.BRIGHT + text + Style.RESET_ALL)
    print("\r\033[K" + Fore.CYAN + "=" * 64)
    print()


def favorites_popup(favs, src_lang, tgt_lang):
    """Displays favorited sentences in a beautiful, retro ANSI popup frame."""
    import textwrap

    # We use a strict internal width of 60 columns (64 total visual columns with borders)
    # The format_line helper takes a string and pads it to exactly 60 internal visual columns.
    def format_line(text):
        padding = max(0, 60 - len(text))
        return "║ " + text + " " * padding + " ║"

    lines = []
    lines.append("╔" + "═" * 60 + "╗")
    lines.append(format_line("                     ★ MY FAVORITES ★"))
    lines.append("╠" + "═" * 60 + "╣")

    if not favs:
        lines.append(format_line("  No favorited items in this session."))
    else:
        for chunk_num, heard, translated in favs:
            header = f" [Chunk {chunk_num}]"
            lines.append(format_line(header))

            # Target language (translated) first
            tgt_prefix = f"   {tgt_lang}: "
            tgt_wrapper = textwrap.TextWrapper(
                width=58,
                initial_indent=tgt_prefix,
                subsequent_indent=" " * len(tgt_prefix)
            )
            for line in tgt_wrapper.wrap(translated):
                lines.append(format_line(line))

            # Source language (heard) second
            src_prefix = f"   {src_lang}: "
            src_wrapper = textwrap.TextWrapper(
                width=58,
                initial_indent=src_prefix,
                subsequent_indent=" " * len(src_prefix)
            )
            for line in src_wrapper.wrap(heard):
                lines.append(format_line(line))

            lines.append(format_line(""))

    lines.append("╠" + "═" * 60 + "╣")
    lines.append(format_line(" Press [Enter] to close this window..."))
    lines.append("╚" + "═" * 60 + "╝")

    print()
    for line in lines:
        colored_line = (
            line.replace("║", Fore.CYAN + "║" + Style.RESET_ALL)
            .replace("╔", Fore.CYAN + "╔" + Style.RESET_ALL)
            .replace("╗", Fore.CYAN + "╗" + Style.RESET_ALL)
            .replace("╠", Fore.CYAN + "╠" + Style.RESET_ALL)
            .replace("╣", Fore.CYAN + "╣" + Style.RESET_ALL)
            .replace("╚", Fore.CYAN + "╚" + Style.RESET_ALL)
            .replace("╝", Fore.CYAN + "╝" + Style.RESET_ALL)
            .replace("═", Fore.CYAN + "═" + Style.RESET_ALL)
            .replace(
                "★ MY FAVORITES ★",
                Fore.YELLOW + Style.BRIGHT + "★ MY FAVORITES ★" + Style.RESET_ALL,
            )
            .replace(
                "Press [Enter] to close this window...",
                Style.DIM + "Press [Enter] to close this window..." + Style.RESET_ALL,
            )
        )
        print(colored_line)

    try:
        sys.__stdin__.readline()
    except Exception:
        pass
