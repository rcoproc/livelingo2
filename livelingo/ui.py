"""
ui.py
=====
Tiny helpers for colored, readable terminal output. Uses colorama so the ANSI
colors work on every Windows console (legacy conhost included).
"""

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
    print(Fore.CYAN + "[i] " + Style.RESET_ALL + str(msg))


def success(msg):
    print(Fore.GREEN + "[ok] " + Style.RESET_ALL + str(msg))


def warn(msg):
    print(Fore.YELLOW + "[!] " + Style.RESET_ALL + str(msg))


def error(msg):
    print(Fore.RED + Style.BRIGHT + "[x] " + Style.RESET_ALL + Fore.RED + str(msg))


def dim(msg):
    print(Style.DIM + str(msg) + Style.RESET_ALL)


def device_line(role, index, name):
    """Pretty one-liner confirming a selected device."""
    idx = "default" if index is None else f"#{index}"
    print(
        Fore.MAGENTA
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
    print(
        Fore.YELLOW
        + Style.BRIGHT
        + f"[chunk {n}] "
        + Style.RESET_ALL
        + Fore.WHITE
        + 'Heard: "'
        + Fore.GREEN
        + heard
        + Fore.WHITE
        + '" -> Translated: "'
        + Fore.CYAN
        + translated
        + Fore.WHITE
        + '"'
    )
    timing = (
        "          timing: STT {stt:.2f}s | translate {translate:.2f}s | "
        "TTS {tts:.2f}s | total {total:.2f}s".format(**timings)
    )
    print(Style.DIM + timing + Style.RESET_ALL)
