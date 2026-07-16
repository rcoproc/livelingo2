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
    "chunk_text_preview",
    "chunk_stream_start",
    "chunk_stream_update",
    "chunk_stream_done",
    "synonyms_result",
    "favorites_popup",
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


def banner():
    """Print the startup banner."""
    line = "=" * 64
    with _print_lock:
        print(Fore.CYAN + line)
        print(Fore.CYAN + Style.BRIGHT + "        L I V E L I N G O   \U0001f399️  ->  \U0001f30d")
        print(Fore.CYAN + "        Real-time speech translation into a virtual mic")
        print(Fore.CYAN + "        mic -> Whisper -> translate -> Edge TTS -> VB-Cable")
        print(Fore.CYAN + line + Style.RESET_ALL)


def info(msg):
    with _print_lock:
        print("\r\033[K" + Fore.CYAN + "[i] " + Style.RESET_ALL + str(msg))


def success(msg):
    with _print_lock:
        print("\r\033[K" + Fore.GREEN + "[ok] " + Style.RESET_ALL + str(msg))


def warn(msg):
    with _print_lock:
        print("\r\033[K" + Fore.YELLOW + "[!] " + Style.RESET_ALL + str(msg))


def error(msg):
    with _print_lock:
        print(
            "\r\033[K"
            + Fore.RED
            + Style.BRIGHT
            + "[x] "
            + Style.RESET_ALL
            + Fore.RED
            + str(msg)
        )


def dim(msg):
    with _print_lock:
        print("\r\033[K" + Style.DIM + str(msg) + Style.RESET_ALL)


def device_line(role, index, name):
    """Pretty one-liner confirming a selected device."""
    idx = "default" if index is None else f"#{index}"
    with _print_lock:
        print(
            "\r\033[K"
            + Fore.MAGENTA
            + f"  {role:<8}"
            + Style.RESET_ALL
            + f" {idx:>8}  "
            + Fore.WHITE
            + Style.BRIGHT
            + str(name)
        )


def chunk_status(n, heard, translated, timings, finalize=False):
    """
    Print the live per-chunk status line plus a dim timing breakdown.

    timings: dict with keys stt, translate, tts, total (seconds).
    finalize: when True, print the trailing blank line after timings.
    """
    heard = (heard or "").strip()
    translated = (translated or "").strip()

    prefix = f"[chunk {n}] "
    indent = " " * len(prefix)

    with _print_lock:
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
        timing = (
            "timing: STT {stt:.2f}s | translate {translate:.2f}s | "
            "TTS {tts:.2f}s | total {total:.2f}s".format(**timings)
        )
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


def format_timing_line(timings, extra=None):
    """
    Build the same timing string used in live logs, e.g.
    timing: STT 1.61s | translate 0.73s | TTS 15.47s | ...
    """
    if not timings:
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
    return line


def chunk_timings(n, timings, extra=None):
    """Print final timing line once TTS completes."""
    prefix = f"[chunk {n}] "
    indent = " " * len(prefix)
    timing = format_timing_line(timings, extra=extra)
    if not timing:
        return
    with _print_lock:
        print("\r\033[K" + indent + Style.DIM + timing + Style.RESET_ALL)
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
