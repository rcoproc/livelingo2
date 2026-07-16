"""
ui.py
=====
Tiny helpers for colored, readable terminal output. Uses colorama so the ANSI
colors work on every Windows console (legacy conhost included).
"""

import sys
from colorama import Fore, Style, init

# autoreset=True -> every print resets the color automatically afterwards.
init(autoreset=True)

__all__ = [
    "banner",
    "info",
    "success",
    "warn",
    "error",
    "dim",
    "device_line",
    "chunk_status",
    "synonyms_result",
    "favorites_popup",
]


def banner():
    """Print the startup banner."""
    line = "=" * 64
    print(Fore.CYAN + line)
    print(Fore.CYAN + Style.BRIGHT + "        L I V E L I N G O   \U0001f399️  ->  \U0001f30d")
    print(Fore.CYAN + "        Real-time speech translation into a virtual mic")
    print(Fore.CYAN + "        mic -> Whisper -> translate -> Edge TTS -> VB-Cable")
    print(Fore.CYAN + line + Style.RESET_ALL)


def info(msg):
    print("\r\033[K" + Fore.CYAN + "[i] " + Style.RESET_ALL + str(msg))


def success(msg):
    print("\r\033[K" + Fore.GREEN + "[ok] " + Style.RESET_ALL + str(msg))


def warn(msg):
    print("\r\033[K" + Fore.YELLOW + "[!] " + Style.RESET_ALL + str(msg))


def error(msg):
    print("\r\033[K" + Fore.RED + Style.BRIGHT + "[x] " + Style.RESET_ALL + Fore.RED + str(msg))


def dim(msg):
    print("\r\033[K" + Style.DIM + str(msg) + Style.RESET_ALL)


def device_line(role, index, name):
    """Pretty one-liner confirming a selected device."""
    idx = "default" if index is None else f"#{index}"
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


def chunk_status(n, heard, translated, timings):
    """
    Print the live per-chunk status line plus a dim timing breakdown.

    timings: dict with keys stt, translate, tts, total (seconds).
    """
    heard = (heard or "").strip()
    translated = (translated or "").strip()
    
    prefix = f"[chunk {n}] "
    indent = " " * len(prefix)
    
    # 1. First line: Chunk number and Heard text
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
    
    # 2. Second line: Translated text (Blue label, White text)
    # Aligned dynamically under the "Heard: " start position
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
    
    # 3. Third line: Timing breakdown
    timing = (
        "timing: STT {stt:.2f}s | translate {translate:.2f}s | "
        "TTS {tts:.2f}s | total {total:.2f}s".format(**timings)
    )
    print("\r\033[K" + indent + Style.DIM + timing + Style.RESET_ALL)
    
    # 4. Spacing: empty line between chunks
    print()


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
