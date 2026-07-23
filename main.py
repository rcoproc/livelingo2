"""
main.py
=======
Entry point for the real-time FR -> EN voice translator.

    python main.py
    python main.py --help
    python main.py <session_id>          # resume session, skip picker
    livelingo <session_id>               # same via wrapper
    livelingo --session <session_id>
    livelingo --list-sessions            # list all sessions (same format as menu [2])
    livelingo --verbose                  # detailed debug logs

Flow: microphone -> Whisper STT (Groq cloud or local faster-whisper)
      -> translation (Groq LLM or Google) -> edge-tts (TTS)
      -> VB-Cable output device (so Teams hears English).

Press Ctrl+C / Ctrl+Q to stop (session id printed on exit for easy resume).
"""

import datetime
import os
import re
import subprocess
import sys
import textwrap
import threading
import time
import unicodedata

import numpy as np
from colorama import Fore, Style

import config as cfg
from livelingo import db, devices, ui
from livelingo.groq_transcribe import GroqSTTError, GroqTranscriber
from livelingo.llm import GROQ_KEY_HELP, LLMError, LLMTranslator
from livelingo.pipeline import Pipeline
from livelingo.synonyms import SynonymError, build_synonym_lookup
from livelingo.synthesize import build_synthesizer
from livelingo.transcribe import Transcriber
from livelingo.translate import Translator

# Left margin (spaces) for startup log + menu/list blocks.
UI_MARGIN = 3

# Listen-indicator status text by SOURCE_LANG (idle / active).
# Only PT, EN, ES, FR, DE, ZH, JA — fallback English.
_LISTEN_STATUS = {
    "en": {
        "idle": "Waiting for speech... (Type any command)",
        "active": "Listening to active voice...  (cmd: g=swap)",
    },
    "pt": {
        "idle": "Aguardando fala... (Digite um comando)",
        "active": "Ouvindo voz ativa...  (cmd: g=trocar idioma)",
    },
    "es": {
        "idle": "Esperando habla... (Escriba un comando)",
        "active": "Escuchando voz activa...  (cmd: g=cambiar idioma)",
    },
    "fr": {
        "idle": "En attente de parole... (Tapez une commande)",
        "active": "Écoute de la voix active...  (cmd: g=changer langue)",
    },
    "de": {
        "idle": "Warte auf Sprache... (Befehl eingeben)",
        "active": "Aktive Stimme wird gehört...  (cmd: g=Sprache tauschen)",
    },
    "zh": {
        "idle": "等待说话... (输入任意命令)",
        "active": "正在听语音...  (命令: g=切换语言)",
    },
    "ja": {
        "idle": "音声待機中... (コマンドを入力)",
        "active": "音声を聞いています...  (cmd: g=言語切替)",
    },
    "it": {
        "idle": "In attesa di parlato... (Digita un comando)",
        "active": "Ascolto voce attiva...  (cmd: g=cambia lingua)",
    },
}


def _listen_status_messages(source_lang=None):
    """Return (idle_msg, active_msg) for the current SOURCE_LANG."""
    code = source_lang if source_lang is not None else getattr(cfg, "SOURCE_LANG", "en")
    code = (code or "en").lower().strip()
    if "-" in code:
        code = code.split("-", 1)[0]
    # Aliases
    if code in ("cn", "zh-cn", "zh-tw", "cmn"):
        code = "zh"
    if code in ("jp",):
        code = "ja"
    if code in ("ger", "deu"):
        code = "de"
    if code in ("ita",):
        code = "it"
    pack = _LISTEN_STATUS.get(code) or _LISTEN_STATUS["en"]
    return pack["idle"], pack["active"]


def _log_info(msg):
    ui.info(msg, indent=UI_MARGIN)


def _log_success(msg):
    ui.success(msg, indent=UI_MARGIN)


def _log_warn(msg):
    ui.warn(msg, indent=UI_MARGIN)


def _log_error(msg):
    ui.error(msg, indent=UI_MARGIN)


def _log_dim(msg):
    ui.dim(msg, indent=UI_MARGIN)


def _log_cam(msg):
    """Webcam/vcam logs → TUI aba Sistema (not translation tab)."""
    ui.info(msg, indent=UI_MARGIN, panel="app")


def _log_cam_warn(msg):
    ui.warn(msg, indent=UI_MARGIN, panel="app")


def _log_cam_success(msg):
    ui.success(msg, indent=UI_MARGIN, panel="app")


def _copy_path_to_clipboard(text: str) -> bool:
    """Copy text to OS clipboard. Returns True on success."""
    text = (text or "").strip()
    if not text:
        return False
    try:
        if os.name == "nt":
            # PowerShell handles Unicode paths cleanly.
            subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-Command",
                    "Set-Clipboard",
                    "-Value",
                    text,
                ],
                check=True,
                capture_output=True,
                timeout=8,
            )
            return True
        # WSL / Linux: try clip.exe (Windows) then xclip/xsel
        try:
            subprocess.run(
                ["clip.exe"],
                input=text.encode("utf-16le"),
                check=True,
                capture_output=True,
                timeout=5,
            )
            return True
        except (FileNotFoundError, subprocess.CalledProcessError, OSError):
            pass
        for cmd in (
            ["xclip", "-selection", "clipboard"],
            ["xsel", "--clipboard", "--input"],
        ):
            try:
                subprocess.run(
                    cmd,
                    input=text.encode("utf-8"),
                    check=True,
                    capture_output=True,
                    timeout=5,
                )
                return True
            except (FileNotFoundError, subprocess.CalledProcessError, OSError):
                continue
    except Exception:
        return False
    return False


def _open_audio_in_explorer(path: str) -> bool:
    """Open Explorer (or folder) selecting the audio file. Returns True on success."""
    share = ui.resolve_share_path(path)
    if not share:
        return False
    try:
        if os.name == "nt":
            # /select, needs no space after comma for best compatibility
            subprocess.Popen(["explorer", f"/select,{share}"])
            return True
        # WSL: Windows Explorer via explorer.exe
        try:
            subprocess.Popen(["explorer.exe", f"/select,{share}"])
            return True
        except FileNotFoundError:
            pass
        folder = os.path.dirname(path) or "."
        if sys.platform == "darwin":
            subprocess.Popen(["open", folder])
        else:
            subprocess.Popen(["xdg-open", folder])
        return True
    except Exception:
        return False


def _resolve_chunk_audio(pipeline, chunk_num=None):
    """
    Return (chunk_num, share_path, raw_path) for last or N.
    share_path is host-friendly absolute path (empty if none).
    """
    if chunk_num is None:
        raw = pipeline.get_last_audio_path()
        with pipeline.history_lock:
            n = pipeline.history[-1][0] if pipeline.history else None
    else:
        n = chunk_num
        raw = pipeline.get_audio_path_by_chunk(chunk_num)
    if not raw:
        # try conventional path
        if n is not None:
            cand = os.path.join(pipeline.cache_dir, f"chunk_{n}.wav")
            if os.path.isfile(cand):
                raw = cand
    share = ui.resolve_share_path(raw) if raw else ""
    return n, share, raw or ""


class ListeningIndicator:
    """
    Animated status line for mic activity.

    Priority pauses (no drawing, no screen jump):
      1. Command typing / handling (_cmd_pause)
      2. Mic muted via [n] (_mic_muted) — full quiet mode for reading the log
    """

    def __init__(self):
        self.thread = None
        self._stop_event = threading.Event()
        self._cmd_pause = threading.Event()  # set => do not draw / yield terminal
        self._mic_muted = threading.Event()  # set => mic [n] muted, freeze UI
        self._lock = threading.Lock()
        self.is_speaking = False
        # Live TTS playback (pipeline sound_enabled); default OFF.
        self.sound_on = False

    def start(self):
        """Start (or keep) the animation thread. Safe to call repeatedly."""
        with self._lock:
            if self.thread and self.thread.is_alive():
                return
            self._stop_event.clear()
            self.thread = threading.Thread(
                target=self._animate, name="listen-indicator", daemon=True
            )
            self.thread.start()

    def stop(self):
        """Stop animation thread and clear the status line."""
        self._stop_event.set()
        self._cmd_pause.clear()
        self._mic_muted.clear()
        t = self.thread
        if t and t.is_alive():
            t.join(timeout=0.4)
        self.thread = None
        self._clear_line()

    def set_speaking(self, state):
        # Ignore speech UI while mic is muted (capture gate already drops audio).
        if self._mic_muted.is_set():
            self.is_speaking = False
            return
        self.is_speaking = bool(state)

    def set_mic_muted(self, muted: bool):
        """
        Mic mute ([n]): freeze listen icons and free the screen for reading.
        Unmute: resume idle/active animation (unless a command is still active).
        """
        if muted:
            self._mic_muted.set()
            self.is_speaking = False
            self._clear_line()
        else:
            self._mic_muted.clear()

    def is_mic_muted_ui(self) -> bool:
        return self._mic_muted.is_set()

    def set_sound_on(self, enabled: bool):
        """Mirror pipeline sound_enabled for the robot status line."""
        self.sound_on = bool(enabled)

    def pause_for_command(self):
        """Yield the terminal to the command handler (priority over animation)."""
        self._cmd_pause.set()
        self._clear_line()

    def resume_after_command(self):
        """Allow animation again after a command finishes (if mic is live)."""
        self._cmd_pause.clear()
        # Stay fully quiet while mic is muted — no icons, no status line thrash.
        if self._mic_muted.is_set():
            self._clear_line()

    def _is_drawing_suspended(self) -> bool:
        return self._cmd_pause.is_set() or self._mic_muted.is_set()

    def _clear_line(self):
        try:
            sys.stdout.write("\r\033[K")
            sys.stdout.flush()
        except Exception:
            pass

    def _check_kbhit(self):
        try:
            if sys.platform == "win32":
                import msvcrt

                return msvcrt.kbhit()
            else:
                import select

                r, _w, _e = select.select([sys.stdin], [], [], 0.0)
                return bool(r)
        except Exception:
            return False

    def _animate(self):
        active_frames = [
            "🎙️  [  •      ]",
            "🎙️  [  ••     ]",
            "🎙️  [  •••    ]",
            "🎙️  [   •••   ]",
            "🎙️  [    •••  ]",
            "🎙️  [     ••  ]",
            "🎙️  [      •  ]",
            "🎙️  [     ••  ]",
            "🎙️  [    •••  ]",
            "🎙️  [   •••   ]",
            "🎙️  [  •••    ]",
            "🎙️  [  ••     ]",
        ]
        idle_frames = [
            "🤖 [ •       ]",
            "🤖 [  •      ]",
            "🤖 [   •     ]",
            "🤖 [    •    ]",
            "🤖 [     •   ]",
            "🤖 [      •  ]",
            "🤖 [       • ]",
            "🤖 [      •  ]",
            "🤖 [     •   ]",
            "🤖 [    •    ]",
            "🤖 [   •     ]",
            "🤖 [  •      ]",
        ]
        idx = 0
        while not self._stop_event.is_set():
            # Mic muted or command: never draw — leave the scrollback readable.
            if self._is_drawing_suspended():
                time.sleep(0.15)
                continue

            # Key waiting in buffer → pause immediately (priority to typing).
            if self._check_kbhit():
                self.pause_for_command()
                continue

            # Same 3-char left margin as menu / list for readable alignment.
            # Text follows SOURCE_LANG; pair shows current SOURCE → TARGET ([g]/[t]).
            pad = "   "
            idle_msg, active_msg = _listen_status_messages()
            src = (getattr(cfg, "SOURCE_LANG", "") or "?").upper()
            tgt = (getattr(cfg, "TARGET_LANG", "") or "?").upper()
            pair = f"{src} → {tgt}"
            # Highlighted audio state next to pair (default OFF — enable with [s]).
            if self.sound_on:
                audio_tag = Fore.GREEN + Style.BRIGHT + "🔊 ÁUDIO ON" + Style.RESET_ALL
            else:
                audio_tag = (
                    Fore.YELLOW
                    + Style.BRIGHT
                    + "🔇 ÁUDIO OFF  →  [s] para ouvir"
                    + Style.RESET_ALL
                )
            if self.is_speaking:
                frame = active_frames[idx % len(active_frames)]
                # Pair + audio flag immediately after robot/mic icon block.
                msg = f"\r\033[K{pad}{frame} {pair}  {audio_tag}  {active_msg}"
                delay = 0.12
            else:
                frame = idle_frames[idx % len(idle_frames)]
                msg = f"\r\033[K{pad}{frame} {pair}  {audio_tag}  {idle_msg}"
                delay = 0.25

            try:
                sys.stdout.write(msg)
                sys.stdout.flush()
            except Exception:
                pass
            idx += 1
            time.sleep(delay)


def _resolve_input():
    """Resolve the microphone device, exiting on a bad explicit setting."""
    try:
        return devices.resolve_device(cfg.INPUT_DEVICE, "input")
    except ValueError as exc:
        ui.error(f"Input device problem: {exc}")
        sys.exit(1)


def _resolve_output():
    """
    Resolve the output (VB-Cable) device. If it can't be resolved, detect
    whether VB-Cable is installed at all and give the right guidance.
    """
    try:
        idx, name = devices.resolve_device(cfg.OUTPUT_DEVICE, "output")
        return idx, name
    except ValueError:
        pass  # fall through to VB-Cable detection below

    vb_idx, vb_name = devices.find_vbcable_output()
    if vb_idx is None:
        ui.error(f"Output device '{cfg.OUTPUT_DEVICE}' was not found.")
        print(devices.VBCABLE_INSTALL_MESSAGE)
        sys.exit(1)
    return vb_idx, vb_name


def _vad_label():
    if not cfg.VAD_ENABLED:
        return "off"
    label = getattr(cfg, "VAD_MODE", "energy")
    if getattr(cfg, "ROLLING_CHUNKS", False):
        label += "+rolling"
    return label


def _tts_menu_label():
    engine = (getattr(cfg, "TTS_ENGINE", "edge") or "edge").lower()
    if engine == "hybrid" or (engine == "piper" and getattr(cfg, "TTS_HYBRID", False)):
        voice = getattr(cfg, "PIPER_VOICE", "") or f"auto:{cfg.TARGET_LANG}"
        return f"hybrid (edge+piper / {voice})"
    if engine == "piper":
        voice = getattr(cfg, "PIPER_VOICE", "") or f"auto:{cfg.TARGET_LANG}"
        return f"piper ({voice})"
    return f"edge ({cfg.TTS_VOICE})"


def _print_streaming_info(indent=0):
    if not (
        getattr(cfg, "STREAMING_LLM", False) or getattr(cfg, "STREAMING_TTS", False)
    ):
        return
    # Leading spaces inside msg align under "[i] " when indent is applied.
    ui.dim(
        f"   streaming: LLM={'on' if cfg.STREAMING_LLM else 'off'} | "
        f"TTS={'on' if cfg.STREAMING_TTS else 'off'} | "
        f"playback_interrupt={'on' if cfg.PLAYBACK_INTERRUPT else 'off'}",
        indent=indent,
    )


def _print_f1_help(pipeline=None):
    """
    F1 = same information shown at application entry (after session pick).

    Mirrors the startup log: banner, selected devices, anti-feedback,
    languages/engines, listening tips. Blank line between major blocks.
    """
    m = UI_MARGIN
    from livelingo.groq_transcribe import GroqTranscriber
    from livelingo.llm import LLMTranslator

    # Same banner as main() entry
    ui.banner(indent=m)
    ui.raw("")

    # --- Selected devices (same labels as _print_device_overview tail) ---
    ui.info("Selected devices:", indent=m)
    if pipeline is not None:
        in_idx = getattr(pipeline, "input_device", None)
        out_idx = getattr(pipeline, "output_device", None)
        in_name = getattr(pipeline, "input_device_name", None) or (
            devices.device_name(in_idx) if in_idx is not None else "?"
        )
        try:
            out_name = devices.device_name(out_idx) if out_idx is not None else "?"
        except Exception:
            out_name = "?"
        ui.device_line("INPUT", in_idx, in_name, indent=m)
        ui.device_line("OUTPUT", out_idx, out_name, indent=m)
        if out_name and "cable" not in str(out_name).lower():
            ui.warn(
                "The output device is not a VB-Cable device. Other apps "
                "(Teams/Zoom) will only hear the translation if you route to VB-Cable.",
                indent=m,
            )
    else:
        ui.dim("   (pipeline not ready)", indent=m)

    # --- Anti-feedback (same string as main() startup) ---
    if getattr(cfg, "MUTE_CAPTURE_DURING_PLAYBACK", True):
        hang_ms = int(getattr(cfg, "MUTE_CAPTURE_HANGOVER_MS", 350))
        ui.info(
            f"Anti-feedback: mic gated during TTS "
            f"(hangover {hang_ms} ms). Set MUTE_CAPTURE_DURING_PLAYBACK=false to disable.",
            indent=m,
        )

    ui.raw("")

    # --- Languages / TTS / Sound / VAD (same format as main() startup) ---
    if pipeline is not None:
        try:
            sound = "ON" if pipeline.is_sound_enabled() else "OFF (default)"
        except Exception:
            sound = "OFF (default)"
    else:
        sound = "OFF (default)"
    ui.info(
        f"Languages: {cfg.SOURCE_LANG} -> {cfg.TARGET_LANG}   |   "
        f"TTS: {_tts_menu_label()}   |   "
        f"Sound: {sound}  |  "
        f"VAD: {_vad_label()}",
        indent=m,
    )
    _print_streaming_info(indent=m)

    # --- Engines (same messages as _build_translator / STT / TTS startup) ---
    from livelingo.failover import (
        FailoverTranscriber,
        FailoverTranslator,
        translator_uses_llm,
        transcriber_uses_groq,
    )

    tr = getattr(pipeline, "translator", None) if pipeline is not None else None
    st = getattr(pipeline, "transcriber", None) if pipeline is not None else None
    if pipeline is not None and translator_uses_llm(tr):
        ui.success(
            f"LLM translation ready (Groq / {cfg.GROQ_MODEL}).",
            indent=m,
        )
        if isinstance(tr, FailoverTranslator) and tr.secondary is not None:
            ui.dim(
                f"   HA: fallback → {tr.secondary_name} on LLM failure",
                indent=m,
            )
    else:
        ui.info(
            "Translation engine: Google (free). Tip: add a free GROQ_API_KEY in "
            ".env for much more natural results.",
            indent=m,
        )

    if pipeline is not None and transcriber_uses_groq(st):
        ui.info(
            f"Speech-to-text: Groq cloud ({cfg.GROQ_STT_MODEL}).",
            indent=m,
        )
        ui.success(
            f"Speech-to-text ready (Groq cloud / {cfg.GROQ_STT_MODEL}).",
            indent=m,
        )
        if isinstance(st, FailoverTranscriber):
            ui.dim(
                f"   HA: fallback → {st.secondary_name} on Groq STT failure",
                indent=m,
            )
    else:
        ui.success("Speech-to-text ready (local Whisper).", indent=m)

    # TTS line mirrors build_synthesizer log style
    tts_engine = (getattr(cfg, "TTS_ENGINE", "edge") or "edge").lower()
    if tts_engine == "piper":
        voice = getattr(cfg, "PIPER_VOICE", "") or f"auto:{cfg.TARGET_LANG}"
        ui.info(f"Text-to-speech: piper ({voice}).", indent=m)
    elif tts_engine == "hybrid" or getattr(cfg, "TTS_HYBRID", False):
        voice = getattr(cfg, "PIPER_VOICE", "") or f"auto:{cfg.TARGET_LANG}"
        ui.info(f"Text-to-speech: hybrid edge+piper ({voice}).", indent=m)
    else:
        ui.info(
            f"Text-to-speech: edge-tts ({cfg.TTS_VOICE}).",
            indent=m,
        )

    ui.raw("")

    # --- Listening block (same as main() after pipeline start) ---
    ui.success(
        f"Listening — speak {cfg.SOURCE_LANG.upper()} now. "
        f"Press Ctrl+C to stop. [n]=mic mute. [g]=swap langs.",
        indent=m,
    )
    ui.warn(
        "Áudio de tradução DESLIGADO por padrão (só texto). "
        "Pressione [s] para ouvir ao vivo, ou [r]/[rN] para um chunk.",
        indent=m,
    )
    ui_mode = (getattr(cfg, "UI_MODE", "tui") or "tui").lower()
    if ui_mode == "tui":
        ui.info("UI_MODE=tui — log rolável · escuta fixa embaixo.", indent=m)
    else:
        ui.info(f"UI_MODE={ui_mode}.", indent=m)
    ui.raw("")

    # Phrase-cache inventory (same summary as Tradução tab after "Audio OFF…")
    try:
        from livelingo.phrase_cache import (
            format_cache_inventory_summary,
            get_phrase_cache,
        )

        pc = None
        if pipeline is not None:
            pc = getattr(pipeline, "phrase_cache", None)
        if pc is None:
            try:
                pc = get_phrase_cache(cfg)
            except Exception:
                pc = None
        ui.info("Phrase cache (resumo):", indent=m)
        for line in format_cache_inventory_summary(cfg, pc):
            # Lines already carry Rich markup for TUI; classic strips via raw path
            if ui.get_log_sink() is not None:
                ui.rich(line)
            else:
                # Strip simple markup for classic terminal
                plain = (
                    line.replace("[/]", "")
                    .replace("[bold cyan]", "")
                    .replace("[bold]", "")
                    .replace("[dim]", "")
                    .replace("[/dim]", "")
                )
                # drop residual [tag] chunks
                import re as _re

                plain = _re.sub(r"\[[^\]]+\]", "", plain)
                ui.dim(plain, indent=m)
    except Exception as exc:
        ui.dim(f"Cache de frases: (resumo indisponível — {exc})", indent=m)
    ui.raw("")


def _print_device_overview(in_idx, in_name, out_idx, out_name):
    """Print the full device list (compact) and confirm the selected ones."""
    m = UI_MARGIN
    ui.info("Detected audio devices (idx / in-ch / out-ch / name):", indent=m)
    for idx, name, in_ch, out_ch, _hostapi in devices.summary_rows():
        marker = ""
        if idx == in_idx:
            marker = "  <= INPUT"
        elif idx == out_idx:
            marker = "  <= OUTPUT"
        ui.dim(f"   {idx:>3}  {in_ch:>2}/{out_ch:<2}  {name}{marker}", indent=m)

    print()
    ui.info("Selected devices:", indent=m)
    ui.device_line("INPUT", in_idx, in_name, indent=m)
    ui.device_line("OUTPUT", out_idx, out_name, indent=m)

    if out_name and "cable" not in out_name.lower():
        ui.warn(
            "The output device is not a VB-Cable device. Other apps (Teams/Zoom) "
            "will only hear the translation if you route to VB-Cable.",
            indent=m,
        )


def _build_translator():
    """
    Pick the translation engine from config and return an object with a
    ``.translate(text)`` method.

    P0 HA: when LLM is primary and TRANSLATION_FALLBACK=google, wrap in
    FailoverTranslator so mid-session Groq failures switch to Google without
    killing the app. Self-test failures no longer ``sys.exit`` if a fallback
    exists.
    """
    from livelingo.failover import FailoverTranslator

    engine = (cfg.TRANSLATION_ENGINE or "auto").lower()
    if engine == "auto":
        engine = "llm" if cfg.GROQ_API_KEY else "google"

    fb = (getattr(cfg, "TRANSLATION_FALLBACK", "google") or "google").lower()
    want_google_fb = fb == "google"

    def _google():
        return Translator(cfg)

    # Pure Google path (no LLM requested).
    if engine == "google":
        _log_info(
            "Translation engine: Google (free). Tip: add a free GROQ_API_KEY in "
            ".env for much more natural results."
        )
        return _google()

    # LLM requested.
    if engine == "llm" and not cfg.GROQ_API_KEY:
        _log_error("TRANSLATION_ENGINE=llm but GROQ_API_KEY is empty.")
        if want_google_fb:
            _log_warn("Falling back to Google Translate (TRANSLATION_FALLBACK=google).")
            print(GROQ_KEY_HELP)
            return _google()
        print(GROQ_KEY_HELP)
        sys.exit(1)

    primary = None
    sample = None
    if cfg.GROQ_API_KEY and engine == "llm":
        primary = LLMTranslator(cfg)
        try:
            sample = primary.translate("Bonjour, ceci est un test.")
            _log_success(f"LLM translation ready (Groq / {cfg.GROQ_MODEL}).")
            _log_dim(f'   self-test: "Bonjour, ceci est un test." -> "{sample}"')
        except LLMError as exc:
            _log_error(f"Groq self-test failed: {exc}")
            primary = None
            if want_google_fb:
                _log_warn(
                    "Using Google Translate only this session "
                    "(LLM self-test failed). Restart after fixing GROQ_API_KEY."
                )
                print(GROQ_KEY_HELP)
            else:
                print(GROQ_KEY_HELP)
                sys.exit(1)

    if primary is not None and want_google_fb:
        secondary = _google()
        _log_info(
            "Translation HA: Groq LLM primary → Google fallback "
            f"(circuit {getattr(cfg, 'CIRCUIT_FAIL_THRESHOLD', 3)} fails / "
            f"{getattr(cfg, 'CIRCUIT_COOLDOWN_S', 60)}s)."
        )
        return FailoverTranslator(
            primary,
            secondary,
            cfg,
            log=_log_info,
            primary_name="llm",
            secondary_name="google",
        )

    if primary is not None:
        return primary

    # No working LLM → Google if allowed.
    if want_google_fb or engine == "auto":
        _log_info(
            "Translation engine: Google (free). Tip: add a free GROQ_API_KEY in "
            ".env for much more natural results."
        )
        return _google()

    _log_error("No translation engine available (LLM down, TRANSLATION_FALLBACK=none).")
    sys.exit(1)


def _build_local_transcriber():
    """Load the local faster-whisper model (auto-downloads on first run)."""
    try:
        transcriber = Transcriber(cfg, log=_log_info)
    except Exception as exc:
        _log_error(f"Could not load the Whisper model '{cfg.WHISPER_MODEL}': {exc}")
        _log_warn("Check your internet connection (first download) and disk space.")
        sys.exit(1)
    _log_success("Whisper model ready (local).")
    return transcriber


def _stt_prompt_hint_lang(prompt: str):
    """
    Best-effort language guess for STT_INITIAL_PROMPT (keyword scores).
    Returns a 2-letter code or None if unclear.
    """
    p = (prompt or "").lower()
    if not p.strip():
        return None
    scores = {
        "pt": sum(
            1
            for w in (
                "português",
                "portugues",
                "brasileiro",
                "transcreva",
                "sotaque",
                "não adicione",
                "nao adicione",
                "o que for falado",
                "fale natural",
            )
            if w in p
        ),
        "en": sum(
            1
            for w in (
                "english",
                "transcribe exactly",
                "do not invent",
                "do not add",
                "american english",
                "british english",
            )
            if w in p
        ),
        "es": sum(
            1
            for w in (
                "español",
                "espanol",
                "transcribe exactamente",
                "no inventes",
                "castellano",
            )
            if w in p
        ),
        "fr": sum(
            1
            for w in (
                "français",
                "francais",
                "transcrivez",
                "réunion",
                "reunion",
            )
            if w in p
        ),
    }
    best = max(scores, key=scores.get)
    if scores[best] >= 1:
        return best
    return None


def _warn_stt_prompt_language_mismatch():
    """Warn when STT_INITIAL_PROMPT language conflicts with SOURCE_LANG."""
    prompt = getattr(cfg, "STT_INITIAL_PROMPT", "") or ""
    if not prompt.strip():
        return
    src = (cfg.SOURCE_LANG or "").lower().strip()
    hinted = _stt_prompt_hint_lang(prompt)
    if hinted and src and hinted != src:
        _log_warn(
            f"STT_INITIAL_PROMPT looks like '{hinted}' but SOURCE_LANG='{src}'. "
            f"Whisper will bias transcription toward the prompt language "
            f"(Heard in wrong language → bad translation). "
            f"Rewrite the prompt in {src.upper()}, or clear STT_INITIAL_PROMPT."
        )


def _build_transcriber():
    """
    Pick the speech-to-text engine from config and return an object with a
    ``.transcribe(audio)`` method.

    P0 HA: Groq primary + local Whisper fallback via FailoverTranscriber.
    Local model warms in a daemon thread so mid-session Groq outages do not
    block the UI; processor waits at most STT_FALLBACK_WAIT_S on fallback path.
    """
    from livelingo.failover import FailoverTranscriber, classify_error, ErrorKind

    _warn_stt_prompt_language_mismatch()

    engine = (cfg.STT_ENGINE or "auto").lower()
    if engine == "auto":
        engine = "groq" if cfg.GROQ_API_KEY else "local"

    fb = (getattr(cfg, "STT_FALLBACK", "local") or "local").lower()
    want_local_fb = fb == "local"

    # Pure local — no cloud wrapper.
    if engine == "local":
        return _build_local_transcriber()

    primary = None
    if engine == "groq":
        if not cfg.GROQ_API_KEY:
            _log_warn(
                "STT_ENGINE=groq but GROQ_API_KEY is empty — using local Whisper."
            )
            print(GROQ_KEY_HELP)
            return _build_local_transcriber()

        primary = GroqTranscriber(cfg, log=_log_info)
        # Self-test with a short silent clip so a bad key/model fails fast.
        try:
            silence = np.zeros(int(0.5 * cfg.SAMPLE_RATE), dtype=np.float32)
            primary.transcribe(silence)
            _log_success(f"Speech-to-text ready (Groq cloud / {cfg.GROQ_STT_MODEL}).")
        except GroqSTTError as exc:
            _log_error(f"Groq STT self-test failed: {exc}")
            kind = classify_error(exc)
            if kind is ErrorKind.PERMANENT or not want_local_fb:
                _log_warn(
                    "Falling back to the local Whisper model. Fix GROQ_API_KEY, or "
                    "set STT_ENGINE=local to skip this check."
                )
                primary = None
            else:
                # Transient boot failure: keep primary for later probes + warm local.
                _log_warn(
                    "Groq STT self-test had a transient error — keeping primary "
                    "with local fallback warm-up."
                )

    if primary is None:
        return _build_local_transcriber()

    if not want_local_fb:
        return primary

    def _local_factory():
        return Transcriber(cfg, log=_log_info)

    wrapper = FailoverTranscriber(
        primary,
        _local_factory,
        cfg,
        log=_log_info,
        primary_name="groq",
        secondary_name="local",
    )
    _log_info(
        "STT HA: Groq primary → local Whisper fallback "
        f"(circuit {getattr(cfg, 'CIRCUIT_FAIL_THRESHOLD', 3)} fails / "
        f"{getattr(cfg, 'CIRCUIT_COOLDOWN_S', 60)}s)."
    )
    if getattr(cfg, "STT_WARMUP_LOCAL", True):
        wrapper.start_warmup()
    return wrapper


def _print_session_duration(start_time, title, session_id=None):
    """Format and print the session duration beautifully (includes session id)."""
    elapsed = time.time() - start_time
    hours = int(elapsed // 3600)
    minutes = int((elapsed % 3600) // 60)
    seconds = int(elapsed % 60)

    time_str = ""
    if hours > 0:
        time_str += f"{hours}h "
    if minutes > 0 or hours > 0:
        time_str += f"{minutes}m "
    time_str += f"{seconds}s"

    sid = (session_id or "").strip()
    print()
    print(Fore.GREEN + "=" * 64)
    print(Fore.GREEN + Style.BRIGHT + " 🏁 SESSION CLOSED SUCCESSFULLY")
    print(Fore.GREEN + "=" * 64)
    print("  Subject: " + Fore.WHITE + Style.BRIGHT + str(title or ""))
    if sid:
        print("  Session ID: " + Fore.YELLOW + Style.BRIGHT + sid + Style.RESET_ALL)
    print("  Session duration: " + Fore.CYAN + Style.BRIGHT + time_str)
    if sid:
        print(
            "  Resume: "
            + Fore.WHITE
            + f"livelingo {sid}"
            + Style.RESET_ALL
            + Style.DIM
            + "   (pula o menu de sessão)"
            + Style.RESET_ALL
        )
    print(Fore.GREEN + "=" * 64)
    print()


def _swap_lang_menu_line(pipeline=None, pending_new_pair=None):
    """
    Canonical yellow [g] line for the menu (3-char left margin).
    pending_new_pair: if set, show current pair and scheduled flip, e.g. EN → PT (→ PT → EN).
    """
    margin = 3
    pad = " " * margin
    if pipeline is not None:
        pair = pipeline.language_pair_label()
    else:
        pair = f"{cfg.SOURCE_LANG.upper()} → {cfg.TARGET_LANG.upper()}"
    if pending_new_pair:
        text = (
            f"{pad}[g]  Swap idiomas   {pair}  (pendente ⇒ {pending_new_pair})     "
            f"(tecle g para cancelar ou aguardar)"
        )
    else:
        text = (
            f"{pad}[g]  Swap idiomas   {pair}     "
            f"(tecle g para inverter SOURCE ↔ TARGET)"
        )
    return text


def _print_swap_lang_menu_line(pipeline=None, pending_new_pair=None):
    """Print/refresh the yellow [g] swap line (Sistema in TUI; classic terminal)."""
    text = _swap_lang_menu_line(pipeline, pending_new_pair=pending_new_pair)
    if ui.get_log_sink() is not None:
        ui.dim(text.lstrip(), panel="app")
        return
    print(
        "\r\033[K"
        + Fore.YELLOW
        + Style.BRIGHT
        + text
        + Style.RESET_ALL
    )


def _print_menu(pipeline=None):
    """Print the configuration metadata (if pipeline provided) and the compact terminal menu in English."""
    # Match list command [l]: 3-char left margin for header + command grid.
    margin = 3
    pad = " " * margin

    # TUI: only sticky Languages|TTS|Sound|Mic on Sistema (no command cheat-sheet —
    # footer already lists cmds; Pair line is redundant with status).
    if ui.get_log_sink() is not None:
        try:
            ui.print_sistema_status(pipeline)
        except Exception:
            sound = "ON" if pipeline and pipeline.is_sound_enabled() else "OFF"
            mic = "MUTED" if pipeline and pipeline.is_mic_muted() else "LIVE"
            ui.info(
                f"Languages: {cfg.SOURCE_LANG} -> {cfg.TARGET_LANG} | "
                f"TTS: {_tts_menu_label()} | Sound: {sound} | Mic: {mic}",
                panel="app",
            )
        return

    if pipeline is not None:
        print()
        sound = "ON" if pipeline.is_sound_enabled() else "OFF"
        ui.info(
            f"Languages: {cfg.SOURCE_LANG} -> {cfg.TARGET_LANG}   |   "
            f"TTS: {_tts_menu_label()}   |   "
            f"Sound: {sound}   |   "
            f"VAD: {_vad_label()}",
            indent=margin,
        )
        _print_streaming_info(indent=margin)

        # Translation Engine status
        from livelingo.failover import (
            FailoverTranscriber,
            FailoverTranslator,
            translator_uses_llm,
            transcriber_uses_groq,
        )

        if translator_uses_llm(pipeline.translator):
            ui.success(
                f"LLM translation ready (Groq / {cfg.GROQ_MODEL}).",
                indent=margin,
            )
            ui.dim(
                '   self-test: "Bonjour, ceci est un test." -> "Hello, this is a test."',
                indent=margin,
            )
            if isinstance(pipeline.translator, FailoverTranslator):
                ui.dim(
                    f"   HA: {pipeline.translator.primary_name} → "
                    f"{pipeline.translator.secondary_name or 'none'}",
                    indent=margin,
                )
        else:
            ui.info("Translation engine: Google (free).", indent=margin)

        # Speech-to-text Engine status
        if transcriber_uses_groq(pipeline.transcriber):
            ui.success(
                f"Speech-to-text ready (Groq cloud / {cfg.GROQ_STT_MODEL}).",
                indent=margin,
            )
            if isinstance(pipeline.transcriber, FailoverTranscriber):
                ui.dim(
                    f"   HA: {pipeline.transcriber.primary_name} → "
                    f"{pipeline.transcriber.secondary_name or 'none'}",
                    indent=margin,
                )
        else:
            ui.success("Speech-to-text ready (local Whisper).", indent=margin)

        print()
        ui.success(
            f"Listening — speak {cfg.SOURCE_LANG.upper()} now. Press Ctrl+C to stop.",
            indent=margin,
        )

    print()
    ui.info("Terminal Commands:", indent=margin)
    print()  # blank line before command grid
    sound_hint = "ON/OFF"
    mic_hint = "LIVE/MUTED"
    if pipeline is not None:
        sound_hint = "ON" if pipeline.is_sound_enabled() else "OFF"
        mic_hint = "MUTED" if pipeline.is_mic_muted() else "LIVE"

    # Grouped menu: Sentence / Audio / Idiom / Session (3 cols when width allows).
    try:
        term_w = max(48, os.get_terminal_size().columns)
    except OSError:
        term_w = 100
    content_w = max(40, term_w - 2 * margin)  # left + right margins
    # Prefer 3 columns; fall back to 2 on narrow terminals.
    ncols = 3 if content_w >= 90 else 2
    col_w = max(22, content_w // ncols)

    def _print_menu_group(title, items):
        """Print a section header + items in ncols fixed-width columns."""
        print(
            "\r\033[K"
            + pad
            + Fore.MAGENTA
            + Style.BRIGHT
            + f"── {title} "
            + ("─" * max(4, content_w - len(title) - 4))
            + Style.RESET_ALL
        )
        print("\r\033[K")  # blank line after group header
        # Pad items to full rows
        row = []
        for item in items:
            row.append(item)
            if len(row) >= ncols:
                cells = [c.ljust(col_w)[:col_w] for c in row]
                print("\r\033[K" + pad + Fore.CYAN + "".join(cells) + Style.RESET_ALL)
                row = []
        if row:
            cells = [c.ljust(col_w)[:col_w] for c in row]
            print("\r\033[K" + pad + Fore.CYAN + "".join(cells) + Style.RESET_ALL)
        print("\r\033[K")  # blank line after group items

    _print_menu_group(
        "Sentence",
        [
            "[e]  Edit last",
            "[eN] Edit chunk N",
            "[enew] New text (no mic)",
            "[d]  Delete last",
            "[dN] Delete chunk N",
            "[f]  Favorite last",
            "[fN] Favorite N",
            "[F]  List favorites",
            "[l]  List messages",
            "[lo] List source only",
            "[lt] List target only",
            "[lc] LiveCaptions pause",
            "[lc show/hide] LC window",
            "[co] Comment last",
            "[coN] Comment N",
            "[codN] Del comment #N",
            "[cls] Clear log (all)",
            "[cls1] Clear LC left",
            "[cls2] Clear VOZ right",
            "[gg/gt] Go top",
            "[GG/gf] Go bottom",
            "[c]  Export .md",
        ],
    )
    _print_menu_group(
        "Audio",
        [
            "[r]  Replay last",
            "[rN] Replay chunk N",
            "[rs] Replay Heard last",
            "[rsN] Replay Heard N",
            f"[s]  Sound ({sound_hint})",
            f"[n]  Mic ({mic_hint})",
            f"[b]  Bypass voice",
            "[x]  Stop playback",
            "[a]  Copy audio path",
            "[aN] Copy path N",
            "[p]  Open audio folder",
            "[pN] Open folder N",
            "[ld]  List devices",
            "[lav] All TTS voices",
            "[lv]  Voices (en/es/fr)",
            "[ctts] Change TTS voice (ctts NomeVoz)",
        ],
    )
    _print_menu_group(
        "Idiom",
        [
            "[g]  Swap SOURCE↔TARGET",
            "[t]  Change TARGET lang",
            "[o]  Synonyms / meaning",
        ],
    )
    _print_menu_group(
        "Session",
        [
            "[pc] Phrase cache (pc …)",
            "[v]  Switch session",
            "[m]  Show this menu",
            "[u]  Compact UI (F4)",
            "[q]  Quit",
        ],
    )

    # Yellow status line: current pair (and pending swap if any).
    pending = None
    if pipeline is not None and getattr(pipeline, "_pending_language_swap", False):
        src = (cfg.SOURCE_LANG or "?").upper()
        tgt = (cfg.TARGET_LANG or "?").upper()
        pending = f"{tgt} → {src}"
    _print_swap_lang_menu_line(pipeline, pending_new_pair=pending)
    print("\r\033[K" + pad + ("─" * min(76, content_w)))
    print()  # blank line after final menu separator


def _unpack_transcript_entry(entry):
    """
    Normalize full_transcript rows.

    New format: (chunk_num, heard, translated, created_at, timing_dict)
    Legacy:     (chunk_num, heard, translated)
    """
    if not entry:
        return None, "", "", "", {}
    chunk_num = entry[0]
    heard = entry[1] if len(entry) > 1 else ""
    translated = entry[2] if len(entry) > 2 else ""
    created_at = entry[3] if len(entry) > 3 else ""
    timing = entry[4] if len(entry) > 4 else {}
    if not isinstance(timing, dict):
        timing = {}
    return chunk_num, heard or "", translated or "", created_at or "", timing


def _entry_heard(entry):
    return _unpack_transcript_entry(entry)[1]


def _is_livecaptions_entry(timing) -> bool:
    """True when chunk came from Windows LiveCaptions (inbound strip)."""
    if not isinstance(timing, dict):
        return False
    src = (timing.get("source") or timing.get("origin") or "").strip().lower()
    return src in ("livecaptions", "lc", "captions")


def _display_lang_label(code: str) -> str:
    """Short UI label: pt → BR, else upper code."""
    c = (code or "?").lower().strip()
    if "-" in c:
        c = c.split("-", 1)[0]
    if c in ("pt", "por", "pt-br", "pt_br"):
        return "BR"
    return (c or "?").upper()


def _entry_lang_pair(timing, *, is_lc: bool):
    """
    (src_label, tgt_label) for list/export.

    LC: caption_source_lang → caption_target_lang (strip pair).
    Voice: cfg SOURCE → TARGET.
    """
    if is_lc and isinstance(timing, dict):
        s = timing.get("caption_source_lang") or timing.get("source_lang") or ""
        t = timing.get("caption_target_lang") or timing.get("target_lang") or ""
        if s and t:
            return _display_lang_label(s), _display_lang_label(t)
    return (
        _display_lang_label(getattr(cfg, "SOURCE_LANG", "?")),
        _display_lang_label(getattr(cfg, "TARGET_LANG", "?")),
    )


def _wrap_labeled_body(label_plain: str, body: str, width: int) -> list[str]:
    """
    Wrap body so first line fits after label; later lines align under body.
    `width` = full line budget for label+body.
    """
    body = " ".join((body or "").split())
    if not body:
        return [""]
    width = max(8, int(width))
    limit = max(8, width - len(label_plain))
    words = body.split(" ")
    lines = []
    cur = ""
    for w in words:
        trial = w if not cur else f"{cur} {w}"
        if len(trial) <= limit:
            cur = trial
        else:
            if cur:
                lines.append(cur)
            while len(w) > limit:
                lines.append(w[:limit])
                w = w[limit:]
            cur = w
    if cur:
        lines.append(cur)
    return lines or [""]


# Stopwords ignored by export word counter (as requested).
_WORD_COUNT_STOP = frozenset({"e", "a", "ou", "para", "ao", "à"})
_VOWELS_PT = set("aeiouáéíóúâêôãõàäëïöüýy")


def _pt_syllable_count(word):
    """
    Approximate Portuguese syllable count via vowel-group runs.
    Good enough for filtering mono vs multi-syllable words.
    """
    w = unicodedata.normalize("NFC", (word or "").lower())
    w = re.sub(r"[^a-zàáâãäèéêëìíîïòóôõöùúûüçýÿ]", "", w)
    if not w:
        return 0
    count = 0
    prev_vowel = False
    for ch in w:
        is_v = ch in _VOWELS_PT
        if is_v and not prev_vowel:
            count += 1
        prev_vowel = is_v
    return count if count > 0 else 1


def _count_content_words(texts):
    """
    Count words with more than one syllable, excluding short stopwords
    (e, a, ou, para, ao, à, …).
    """
    total = 0
    for text in texts:
        for raw in re.findall(r"[A-Za-zÀ-ÿ]+(?:'[A-Za-zÀ-ÿ]+)?", text or ""):
            # Strip trailing/leading punctuation already handled by regex.
            key = unicodedata.normalize("NFC", raw.lower())
            # Compare stopwords with and without combining marks for "à".
            key_plain = "".join(
                c
                for c in unicodedata.normalize("NFD", key)
                if not unicodedata.combining(c)
            )
            if key in _WORD_COUNT_STOP or key_plain in _WORD_COUNT_STOP:
                continue
            if _pt_syllable_count(key) > 1:
                total += 1
    return total


def _input_loop(pipeline, synonym_lookup, indicator=None):
    """
    Read user input from standard input in a daemon thread.
    Supported commands:
      r       -> Replay last chunk (synthesize TTS if no WAV from sound-OFF)
      r<num>  -> Replay chunk <num> (e.g. r5, r99); generate audio if missing
      n       -> Mute/unmute Windows mic (Core Audio) + app capture gate
      N       -> Force soft-listen (yellow borders; low-energy VAD; not mute)
      g       -> Swap SOURCE ↔ TARGET languages (fast path mid-listen)
      t       -> Change TARGET language only (prompt EN/IT/ES/…)
      a / aN  -> Copy chunk audio file path to clipboard
      p / pN  -> Open Explorer on chunk audio file/folder
      e       -> Edit the last transcribed chunk
      enew    -> New translation from typed text (no mic); TTS if sound ON
    """
    while not pipeline.stop_event.is_set():
        try:
            line = sys.stdin.readline()
            if not line:
                break
            raw_cmd = line.strip()
            cmd = raw_cmd.lower()
            if not cmd:
                if indicator is not None:
                    indicator.resume_after_command()
                continue

            # Pause listen animation for the whole command (priority over icons).
            if indicator is not None:
                indicator.pause_for_command()

            try:
                _dispatch_command(pipeline, synonym_lookup, raw_cmd, cmd, indicator)
            finally:
                if indicator is not None:
                    indicator.resume_after_command()

        except Exception as exc:
            ui.error(f"Command error: {exc}")
            if indicator is not None:
                indicator.resume_after_command()


# Single-letter terminal commands (animation can make the first keystroke hard to see).
_SINGLE_LETTER_CMDS = frozenset("sngmxq")

# ANSI CSI sequences (colorama / terminal) — strip before TUI log.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
# Locales shown by [lv] (edge-tts --list-voices filter).
_LV_VOICE_RE = re.compile(r"en-US|en-GB|es-ES|es-MX|fr-FR")


def _project_root():
    return os.path.dirname(os.path.abspath(__file__))


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text or "")


def _edge_tts_list_voices_argv():
    """Resolve `edge-tts --list-voices` (PATH, venv Scripts, or python -m)."""
    import shutil

    found = shutil.which("edge-tts")
    if found:
        return [found, "--list-voices"]
    scripts = os.path.dirname(sys.executable)
    for name in ("edge-tts.exe", "edge-tts"):
        cand = os.path.join(scripts, name)
        if os.path.isfile(cand):
            return [cand, "--list-voices"]
    return [sys.executable, "-m", "edge_tts", "--list-voices"]


def _run_cmd_to_log(argv, title, *, timeout=90, line_filter=None):
    """
    Run an external command and print stdout/stderr lines into the UI log.
    line_filter: optional compiled regex — keep matching lines only.
    """
    ui.info(title)
    ui.dim("  $ " + " ".join(argv))
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=_project_root(),
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError:
        ui.error(f"Comando não encontrado: {argv[0]}")
        return
    except subprocess.TimeoutExpired:
        ui.error(f"Timeout ({timeout}s) ao executar: {argv[0]}")
        return
    except Exception as exc:
        ui.error(f"Falha ao executar: {exc}")
        return

    out = _strip_ansi(proc.stdout or "")
    err = _strip_ansi(proc.stderr or "").strip()
    lines = [ln.rstrip() for ln in out.splitlines()]
    if line_filter is not None:
        lines = [ln for ln in lines if line_filter.search(ln)]
    # Drop pure-empty leading/trailing clutter; keep internal blanks sparingly
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    if not lines:
        if err:
            for ln in err.splitlines():
                ui.warn(ln)
        else:
            ui.warn("Nenhuma linha na saída.")
        if proc.returncode not in (0, None):
            ui.warn(f"exit code {proc.returncode}")
        return
    for ln in lines:
        ui.raw(ln)
    if err and proc.returncode not in (0, None):
        for ln in err.splitlines()[:12]:
            ui.warn(ln)
    if proc.returncode not in (0, None):
        ui.warn(f"exit code {proc.returncode} · {len(lines)} linha(s)")
    else:
        ui.success(f"{len(lines)} linha(s).")


def _normalize_cmd(raw_cmd, cmd):
    """
    Normalize user input before dispatch.

    Double-tap of a single letter (e.g. 'gg', 'ss') is treated as one press —
    the listen-indicator often glues the first character onto the status line,
    so users press the key twice and would otherwise get Unknown command.

    Also strips decorative brackets from help text: ``[cam on]`` → ``cam on``,
    ``[s]`` → ``s`` (users often type the docs notation literally).
    """
    raw_cmd = (raw_cmd or "").strip()
    cmd = (cmd or raw_cmd).strip().lower()
    # Strip one pair of wrapping [] used in docs/UI hints
    if len(raw_cmd) >= 2 and raw_cmd[0] == "[" and raw_cmd[-1] == "]":
        raw_cmd = raw_cmd[1:-1].strip()
        cmd = raw_cmd.lower()
    elif len(cmd) >= 2 and cmd[0] == "[" and cmd[-1] == "]":
        cmd = cmd[1:-1].strip()
        if len(raw_cmd) >= 2 and raw_cmd[0] == "[" and raw_cmd[-1] == "]":
            raw_cmd = raw_cmd[1:-1].strip()
    if len(cmd) >= 2 and len(set(cmd)) == 1 and cmd[0] in _SINGLE_LETTER_CMDS:
        letter = cmd[0]
        return (letter.upper() if raw_cmd.isupper() else letter), letter
    return raw_cmd, cmd


def _tui_prompt_prefill(indicator, text: str) -> None:
    """
    Prefill the TUI command field for the next stdin prompt (edit sentence).

    Classic terminal keeps using readline pre_input_hook; TUI has no real
    readline, so the #cmd Input must be filled explicitly.
    """
    if indicator is None:
        return
    value = text or ""
    try:
        # Store immediately so _wait_for_prompt_line sees it even before UI hop.
        if hasattr(indicator, "_prompt_prefill"):
            indicator._prompt_prefill = value
        if not hasattr(indicator, "set_prompt_prefill"):
            return
        if hasattr(indicator, "call_from_thread"):
            indicator.call_from_thread(indicator.set_prompt_prefill, value)
        else:
            indicator.set_prompt_prefill(value)
    except Exception:
        try:
            indicator.set_prompt_prefill(value)
        except Exception:
            pass


def _dispatch_phrase_cache_cmd(pipeline, raw_cmd, cmd):
    """
    Phrase-cache (TM) commands for latency A/B and quality review.

      pc              status + stats + last event
      pc on / pc off  enable / disable cache (runtime)
      pc force        next chunk: ignore HIT, live translate, overwrite store
      pc last         show last HIT/MISS/store for evaluation
      pc good / bad   mark last pair quality
      pc undo         restore previous target from history
      pc backup       JSON snapshot under .cache/phrase_cache_backups/
      pc restore      load latest backup (or pc restore path)
      pc import PATH [reverse]   load CSV (SourceText/TranslatedText) into TM
    """
    cache = getattr(pipeline, "phrase_cache", None)
    if cache is None:
        try:
            from livelingo.phrase_cache import get_phrase_cache

            cache = get_phrase_cache(cfg)
            pipeline.phrase_cache = cache
        except Exception as exc:
            ui.error(f"Phrase cache indisponível: {exc}", indent=3)
            return

    parts = (raw_cmd or "").strip().split(None, 2)
    sub = (parts[1].strip().lower() if len(parts) > 1 else "") or ""
    rest = (parts[2].strip() if len(parts) > 2 else "") or ""

    if not sub or sub in ("status", "stat", "stats", "?"):
        ui.info(cache.stats_line(), indent=3)
        ui.dim(
            "Comandos: pc on|off · pc force · pc last · pc good|bad · "
            "pc undo · pc backup · pc restore [path] · pc import file.csv [reverse]",
            indent=3,
        )
        ui.raw(cache.format_last())
        ui.raw("")
        return

    if sub in ("on", "enable", "1", "true"):
        cache.set_enabled(True)
        try:
            cfg.PHRASE_CACHE = True
        except Exception:
            pass
        n = 0
        try:
            n = cache.warmup(cfg.SOURCE_LANG, cfg.TARGET_LANG)
        except Exception:
            pass
        ui.success(
            f"Phrase cache ON · warm-up {n} pair(s) · {cache.stats_line()}",
            indent=3,
        )
        ui.dim(
            "Próximas frases: HIT reutiliza tradução; MISS grava no SQLite. "
            "Compare latência com [pc off].",
            indent=3,
        )
        ui.raw("")
        return

    if sub in ("off", "disable", "0", "false"):
        cache.set_enabled(False)
        try:
            cfg.PHRASE_CACHE = False
        except Exception:
            pass
        cache.clear_force_next()
        ui.warn(
            f"Phrase cache OFF · traduções sempre live (Google/LLM). "
            f"{cache.stats_line()}",
            indent=3,
        )
        ui.raw("")
        return

    if sub in ("force", "f", "refresh", "live"):
        cache.request_force_next()
        ui.warn(
            "Próximo chunk: cache IGNORADO · tradução live · sobrescreve par no banco "
            "(histórico anterior salvo para [pc undo]).",
            indent=3,
        )
        ui.raw("")
        return

    if sub in ("last", "show", "review"):
        ui.info("Último evento do phrase cache:", indent=3)
        ui.raw(cache.format_last())
        ui.raw("")
        return

    if sub in ("good", "ok"):
        ok, msg = cache.mark_last_quality("good")
        (ui.success if ok else ui.warn)(msg, indent=3)
        ui.raw("")
        return

    if sub in ("bad", "ruim"):
        ok, msg = cache.mark_last_quality("bad")
        (ui.success if ok else ui.warn)(msg, indent=3)
        ui.dim(
            "Sugestão: [pc force] na próxima fala igual re-traduz e grava novo target; "
            "ou [pc undo] se sobrescreveu por engano.",
            indent=3,
        )
        ui.raw("")
        return

    if sub in ("undo", "rollback"):
        ok, msg = cache.undo_last()
        (ui.success if ok else ui.warn)(msg, indent=3)
        ui.raw("")
        return

    if sub == "backup" or sub.startswith("backup "):
        try:
            path = cache.backup()
            ui.success(f"Backup phrase cache: {path}", indent=3)
            ui.dim(
                "Também: .cache/phrase_cache_backups/phrase_cache_latest.json · "
                "Restaurar: pc restore",
                indent=3,
            )
        except Exception as exc:
            ui.error(f"Backup falhou: {exc}", indent=3)
        ui.raw("")
        return

    if sub == "restore" or sub.startswith("restore"):
        # pc restore  |  pc restore path.json  (path in rest when split max 2)
        path = rest or None
        if sub.startswith("restore ") and len(sub) > 8:
            path = sub[8:].strip() or path
        try:
            try:
                pre = cache.backup()
                ui.dim(f"Snapshot pré-restore: {pre}", indent=3)
            except Exception:
                pass
            n, used = cache.restore(path)
            ui.success(f"Restore OK · {n} par(es) de {used}", indent=3)
        except Exception as exc:
            ui.error(f"Restore falhou: {exc}", indent=3)
        ui.raw("")
        return

    if sub == "import" or sub.startswith("import"):
        # pc import exported1.csv [reverse]
        # parts: ["pc", "import", "exported1.csv reverse"] when split max 2
        # or raw_cmd has more tokens
        tokens = (raw_cmd or "").strip().split()
        # tokens[0]=pc tokens[1]=import tokens[2]=path tokens[3:]=flags
        if len(tokens) < 3:
            ui.warn(
                "Uso: pc import <arquivo.csv> [reverse]  "
                "ex: pc import exported1.csv reverse",
                indent=3,
            )
            ui.raw("")
            return
        csv_path = tokens[2]
        also_reverse = any(
            t.lower() in ("reverse", "rev", "--also-reverse") for t in tokens[3:]
        )
        if not os.path.isfile(csv_path):
            # try relative to project root
            alt = os.path.join(os.path.dirname(os.path.abspath(__file__)), csv_path)
            if os.path.isfile(alt):
                csv_path = alt
        try:
            from livelingo.import_phrase_csv import (
                format_import_stats,
                import_phrase_csv,
            )

            ui.info(f"Importando CSV → phrase cache: {csv_path}", indent=3)
            stats = import_phrase_csv(
                csv_path,
                default_source_lang="en",
                also_reverse=also_reverse,
                dry_run=False,
                phrase_cache=cache,
            )
            for line in format_import_stats(stats).splitlines():
                ui.dim(line, indent=3)
            ui.success(
                "Import concluído. Use [pc on] se o cache estiver off · "
                "HIT só se SOURCE/TARGET baterem a direção do par (CSV ≈ EN→PT).",
                indent=3,
            )
        except Exception as exc:
            ui.error(f"Import falhou: {exc}", indent=3)
        ui.raw("")
        return

    ui.warn(
        f"Subcomando desconhecido: pc {sub}. "
        f"Use: pc | pc on|off | pc force | pc last | pc good|bad | pc undo | "
        f"pc backup | pc restore | pc import file.csv",
        indent=3,
    )
    ui.raw("")


def _dispatch_command(pipeline, synonym_lookup, raw_cmd, cmd, indicator=None):
    """Handle one terminal command (indicator already paused by caller)."""
    raw_cmd, cmd = _normalize_cmd(raw_cmd, cmd)
    if raw_cmd == "F":
        favs = pipeline.get_favorites()
        lang_map = {
            "fr": "Francês",
            "en": "Inglês",
            "pt": "Português",
            "es": "Espanhol",
            "de": "Alemão",
            "it": "Italiano",
        }
        src_lang = lang_map.get(cfg.SOURCE_LANG.lower(), cfg.SOURCE_LANG.upper())
        tgt_lang = lang_map.get(cfg.TARGET_LANG.lower(), cfg.TARGET_LANG.upper())
        ui.favorites_popup(favs, src_lang, tgt_lang)
        _print_menu(pipeline)
    elif cmd == "rs":
        # Replay last chunk using Heard (source) text for TTS
        pipeline.replay_last(use_heard=True)
        if indicator is not None:
            indicator.set_sound_on(pipeline.is_sound_enabled())
    elif cmd.startswith("rs") and len(cmd) > 2 and cmd[2:].isdigit():
        # rs999 — replay chunk N synthesized from Heard text
        chunk_num = int(cmd[2:])
        pipeline.replay_chunk(chunk_num, use_heard=True)
        if indicator is not None:
            indicator.set_sound_on(pipeline.is_sound_enabled())
    elif cmd == "r":
        pipeline.replay_last()
        if indicator is not None:
            indicator.set_sound_on(pipeline.is_sound_enabled())
    elif cmd.startswith("r") and cmd[1:].isdigit():
        chunk_num = int(cmd[1:])
        pipeline.replay_chunk(chunk_num)
        if indicator is not None:
            indicator.set_sound_on(pipeline.is_sound_enabled())
    elif cmd == "enew" or cmd.startswith("enew "):
        # New translation from typed text only (no mic / STT).
        # Audio TTS follows current sound mode: ON → synthesize + play; OFF → text
        # (optional background TTS if TTS_SKIP_WHEN_MUTED is false).
        parts = (raw_cmd or "").split(None, 1)
        text = (parts[1] if len(parts) == 2 else "").strip()
        if not text:
            print("Texto a traduzir (enew): ", end="", flush=True)
            try:
                text = sys.stdin.readline().strip()
            except (KeyboardInterrupt, EOFError):
                text = ""
        if not text:
            ui.warn(
                "enew: informe o texto. Uso: enew <texto a traduzir>",
                indent=3,
            )
            return
        # Collapse accidental newlines from multi-line paste into one chunk.
        text = " ".join(text.split()).strip()
        if not text:
            ui.warn("enew: texto vazio.", indent=3)
            return
        pipeline.chunk_queue.put(text)
        sound_on = bool(pipeline.is_sound_enabled())
        preview = text if len(text) <= 80 else text[:77] + "…"
        if sound_on:
            ui.success(
                f'enew: enfileirado — traduz + áudio (sound ON): "{preview}"',
                indent=3,
            )
        else:
            ui.info(
                f"enew: enfileirado — só texto (sound OFF; [s] para ouvir): "
                f'"{preview}"',
                indent=3,
            )
    elif cmd == "e":
        last_heard = pipeline.get_last_heard()
        if not last_heard:
            ui.warn("No sentences in history to edit.")
            return
        # TUI: prefill #cmd with the current sentence (readline hooks don't apply).
        in_tui = indicator is not None and (
            hasattr(indicator, "set_prompt_prefill") or ui.get_log_sink() is not None
        )
        has_readline = False
        if not in_tui:
            try:
                import readline

                def hook():
                    readline.insert_text(last_heard)
                    readline.redisplay()

                readline.set_pre_input_hook(hook)
                has_readline = True
            except ImportError:
                ui.warn(
                    "Tip: Install 'pyreadline3' (on Windows) or 'gnureadline' (on Linux/macOS) "
                    "to pre-populate text inside the editor."
                )

        try:
            if in_tui:
                _tui_prompt_prefill(indicator, last_heard)
                ui.info("Edite a frase no campo (Enter=salvar · apague tudo=cancelar).")
                print("Edit sentence: ", end="", flush=True)
                new_text = sys.stdin.readline().strip()
            elif has_readline:
                new_text = input("Edit sentence: ").strip()
            else:
                print(f'Last sentence: "{last_heard}"')
                print("Enter correction (or Enter to cancel): ", end="", flush=True)
                new_text = sys.stdin.readline().strip()
        except (KeyboardInterrupt, EOFError):
            new_text = ""
        finally:
            if has_readline:
                try:
                    readline.set_pre_input_hook(None)
                except Exception:
                    pass
            if in_tui:
                _tui_prompt_prefill(indicator, "")

        if new_text and new_text != last_heard:
            pipeline.chunk_queue.put(new_text)
            ui.info("New sentence queued for translation!")
            ui.raw("")
        elif not new_text:
            ui.info("Editing canceled.")
        else:
            ui.info("No changes made.")
    elif cmd.startswith("e") and cmd[1:].isdigit():
        chunk_num = int(cmd[1:])
        last_heard = pipeline.get_heard_by_chunk(chunk_num)
        if not last_heard:
            ui.warn(f"Chunk {chunk_num} not found in history to edit.")
            return
        in_tui = indicator is not None and (
            hasattr(indicator, "set_prompt_prefill") or ui.get_log_sink() is not None
        )
        has_readline = False
        if not in_tui:
            try:
                import readline

                def hook():
                    readline.insert_text(last_heard)
                    readline.redisplay()

                readline.set_pre_input_hook(hook)
                has_readline = True
            except ImportError:
                ui.warn(
                    "Tip: Install 'pyreadline3' (on Windows) or 'gnureadline' (on Linux/macOS) "
                    "to pre-populate text inside the editor."
                )

        try:
            if in_tui:
                _tui_prompt_prefill(indicator, last_heard)
                ui.info(
                    f"Edite o chunk {chunk_num} no campo "
                    "(Enter=salvar · apague tudo=cancelar)."
                )
                print(f"Edit sentence {chunk_num}: ", end="", flush=True)
                new_text = sys.stdin.readline().strip()
            elif has_readline:
                new_text = input(f"Edit sentence {chunk_num}: ").strip()
            else:
                print(f'Sentence of chunk {chunk_num}: "{last_heard}"')
                print("Enter correction (or Enter to cancel): ", end="", flush=True)
                new_text = sys.stdin.readline().strip()
        except (KeyboardInterrupt, EOFError):
            new_text = ""
        finally:
            if has_readline:
                try:
                    readline.set_pre_input_hook(None)
                except Exception:
                    pass
            if in_tui:
                _tui_prompt_prefill(indicator, "")

        if new_text and new_text != last_heard:
            pipeline.edit_chunk(chunk_num, new_text)
        elif not new_text:
            ui.info("Editing canceled.")
        else:
            ui.info("No changes made.")
    elif cmd == "d":
        last_heard = pipeline.get_last_heard()
        if not last_heard:
            ui.warn("No sentences in history to delete.")
            return
        print(f'Last sentence: "{last_heard}"')
        print(
            "Are you sure you want to delete this sentence? (y/n): ", end="", flush=True
        )
        confirm = sys.stdin.readline().strip().lower()
        if confirm in ("y", "yes", "s", "sim"):
            pipeline.delete_last_chunk()
        else:
            ui.info("Deletion canceled.")
    elif cmd.startswith("d") and cmd[1:].isdigit():
        chunk_num = int(cmd[1:])
        last_heard = pipeline.get_heard_by_chunk(chunk_num)
        if not last_heard:
            ui.warn(f"Chunk {chunk_num} not found in history to delete.")
            return
        print(f'Sentence of chunk {chunk_num}: "{last_heard}"')
        print(
            f"Are you sure you want to delete sentence {chunk_num}? (y/n): ",
            end="",
            flush=True,
        )
        confirm = sys.stdin.readline().strip().lower()
        if confirm in ("y", "yes", "s", "sim"):
            pipeline.delete_chunk(chunk_num)
        else:
            ui.info("Deletion canceled.")
    elif cmd == "f":
        last_heard = pipeline.get_last_heard()
        if not last_heard:
            ui.warn("No sentences in history to favorite.")
            return
        with pipeline.history_lock:
            n = pipeline.history[-1][0]
        pipeline.add_favorite(n)
    elif cmd.startswith("f") and cmd[1:].isdigit():
        chunk_num = int(cmd[1:])
        pipeline.add_favorite(chunk_num)
    elif cmd == "s":
        enabled = pipeline.toggle_sound()
        if indicator is not None:
            indicator.set_sound_on(enabled)
        # Feedback only on Sistema (not VOZ — keeps Heard/Translated clean)
        if enabled:
            ui.success(
                "Sound ON — próximas traduções tocam ao vivo. "
                "Use [r] / [rN] para ouvir chunks sem áudio (gera TTS se faltar).",
                indent=3,
                panel="app",
            )
        else:
            ui.warn(
                "Sound OFF — só texto (TTS omitido se TTS_SKIP_WHEN_MUTED). "
                "Pressione [s] para ouvir de novo, ou [r]/[rN] para um chunk.",
                indent=3,
                panel="app",
            )
        _print_menu(pipeline)
    elif cmd == "pc" or cmd.startswith("pc "):
        # Phrase translation cache control / review / backup
        _dispatch_phrase_cache_cmd(pipeline, raw_cmd, cmd)
    elif cmd == "g":
        info = pipeline.request_language_swap()
        status = info.get("status")
        g_pad = "   "  # 3-char left margin (align with menu)
        # All [g] feedback → Sistema in TUI (VOZ stays phrases only)
        g_panel = "app" if ui.get_log_sink() is not None else "main"
        if status == "deferred":
            msg = (
                f"[g]  Swap agendado: {info['old_pair']}  ⇒  {info['new_pair']}   "
                f"(termina a frase/tradução em curso, depois inverte)"
            )
            if g_panel == "app":
                ui.warn(msg, indent=3, panel="app")
            else:
                print(
                    "\r\033[K"
                    + Fore.YELLOW
                    + Style.BRIGHT
                    + f"{g_pad}{msg}"
                    + Style.RESET_ALL
                )
            ui.info(
                "A frase atual NÃO será perdida — o idioma só muda após o processamento.",
                indent=3,
                panel=g_panel,
            )
            # Refresh menu line so it shows pending target pair.
            _print_swap_lang_menu_line(pipeline, pending_new_pair=info.get("new_pair"))
        elif status == "cancelled_pending":
            msg = (
                f"[g]  Swap pendente cancelado — permanece {info['old_pair']}"
            )
            if g_panel == "app":
                ui.warn(msg, indent=3, panel="app")
            else:
                print(
                    "\r\033[K"
                    + Fore.YELLOW
                    + Style.BRIGHT
                    + f"{g_pad}{msg}"
                    + Style.RESET_ALL
                )
            _print_swap_lang_menu_line(pipeline)
        else:
            # Applied immediately (pipeline idle) — full menu refresh with new pair.
            src = info.get("source") or cfg.SOURCE_LANG
            tgt = info.get("target") or cfg.TARGET_LANG
            voice = info.get("voice") or cfg.TTS_VOICE
            msg = (
                f"[g]  Idiomas: {info['old_pair']}  ⇒  {info['new_pair']}   "
                f"(STT={src} · TTS voice={voice})"
            )
            if g_panel == "app":
                ui.success(msg, indent=3, panel="app")
            else:
                print(
                    "\r\033[K"
                    + Fore.YELLOW
                    + Style.BRIGHT
                    + f"{g_pad}{msg}"
                    + Style.RESET_ALL
                )
            for w in info.get("warnings") or []:
                ui.warn(w, indent=3, panel=g_panel)
            if g_panel == "app":
                with ui.log_panel("app"):
                    _warn_stt_prompt_language_mismatch()
            else:
                _warn_stt_prompt_language_mismatch()
            ui.info(
                f"Fale {str(src).upper()} agora — os outros ouvem {str(tgt).upper()}. "
                f"Histórico antigo não é re-traduzido.",
                indent=3,
                panel=g_panel,
            )
            _print_menu(pipeline)
            # TUI footer follows new SOURCE_LANG immediately
            if indicator is not None and hasattr(indicator, "refresh_source_ui"):
                try:
                    if hasattr(indicator, "call_from_thread"):
                        indicator.call_from_thread(indicator.refresh_source_ui)
                    else:
                        indicator.refresh_source_ui()
                except Exception:
                    try:
                        indicator.refresh_source_ui()
                    except Exception:
                        pass
    elif cmd == "t" or cmd.startswith("t "):
        # Change TARGET language only (SOURCE + STT stay the same).
        # Language codes are forced to UPPERCASE for this command only
        # (typed "en" / "En" → "EN"; also accepts one-liner "t EN" / "t en").
        allowed = ", ".join(c.upper() for c in Pipeline.TARGET_LANG_CHOICES)
        cur = (cfg.TARGET_LANG or "?").upper()
        # Optional inline: "t EN" / "t en" / "t pt-BR"
        inline = ""
        parts = (raw_cmd or "").split(None, 1)
        if len(parts) == 2:
            inline = (parts[1] or "").strip()

        def _t_set_force_upper(on: bool) -> None:
            if indicator is None or not hasattr(indicator, "set_prompt_force_upper"):
                return
            try:
                # Bool flag: set immediately from worker so keystrokes already
                # see UPPERCASE mode; UI-thread hop only for field rewrite.
                if hasattr(indicator, "_prompt_force_upper"):
                    indicator._prompt_force_upper = bool(on)
                if hasattr(indicator, "call_from_thread"):
                    indicator.call_from_thread(indicator.set_prompt_force_upper, on)
                else:
                    indicator.set_prompt_force_upper(on)
            except Exception:
                try:
                    indicator.set_prompt_force_upper(on)
                except Exception:
                    pass

        if inline:
            new_code = inline.upper()
        else:
            print(
                "\r\033[K"
                + "   "
                + Fore.CYAN
                + f"TARGET atual: {cur}. Códigos: {allowed}"
                + Style.RESET_ALL
            )
            print(
                "   Informe o novo idioma a ser traduzido (ex: EN, IT, ES): ",
                end="",
                flush=True,
            )
            _t_set_force_upper(True)
            try:
                new_code = sys.stdin.readline().strip().upper()
            finally:
                _t_set_force_upper(False)
        if not new_code:
            ui.info("TARGET inalterado (entrada vazia).", indent=3)
            return
        # Defensive: always uppercase for this command only
        new_code = new_code.upper()
        result = pipeline.set_target_language(new_code)
        if not result.get("ok"):
            ui.warn(result.get("error") or "Falha ao alterar TARGET.", indent=3)
            return
        for w in result.get("warnings") or []:
            ui.warn(w, indent=3)
        src = (result.get("source") or cfg.SOURCE_LANG or "?").upper()
        tgt = (result.get("target") or "?").upper()
        old = (result.get("old_target") or "?").upper()
        voice = result.get("voice") or cfg.TTS_VOICE
        if result.get("unchanged"):
            ui.info(f"TARGET já era {tgt} — sem mudança.", indent=3)
        else:
            print(
                "\r\033[K"
                + Fore.YELLOW
                + Style.BRIGHT
                + f"   [t]  TARGET: {old}  ⇒  {tgt}   "
                f"(SOURCE={src} · TTS={voice})" + Style.RESET_ALL
            )
            ui.info(
                f"Próximas traduções: fale {src} → ouvem {tgt}.",
                indent=3,
            )
        _print_menu(pipeline)
    elif cmd in ("b", "bypass", "hot"):
        # Toggle: first [b] = stop Cable TTS (like [x]) + raw voice bypass;
        # second [b] = leave bypass, resume normal listen/translate/TTS.
        try:
            active = pipeline.toggle_voice_passthrough()
        except Exception as exc:
            ui.error(f"[b] Bypass falhou: {exc}")
            return
        if active:
            # Sistema tab (not VOZ/Tradução) — operational A/V routing tip
            ui.warn(
                "[b] BYPASS ON — TTS no Cable cortado (como [x]); "
                "sua voz vai direto ao CABLE/Teams (sem tradução). "
                "Pressione [b] de novo para voltar ao normal.",
                indent=3,
                panel="app",
            )
            ui.dim(
                "  Dica: fale no idioma da call. "
                "Mic do Teams = CABLE Output.",
                indent=3,
                panel="app",
            )
        else:
            ui.success(
                "[b] BYPASS OFF — fluxo normal: escuta + tradução + TTS no Cable.",
                indent=3,
                panel="app",
            )
            ui.raw("", panel="app")
        if indicator is not None:
            # Optional header cue if the TUI supports it
            try:
                if hasattr(indicator, "set_passthrough"):
                    if hasattr(indicator, "call_from_thread"):
                        indicator.call_from_thread(indicator.set_passthrough, active)
                    else:
                        indicator.set_passthrough(active)
            except Exception:
                pass
    elif (raw_cmd or "").strip() in ("N", "[N]"):
        # Capital N only: force soft-listen (low-energy VAD + yellow borders).
        # Lowercase [n] remains mic mute — do not confuse the two.
        on = pipeline.toggle_force_soft_listen()
        if indicator is not None:
            try:
                # Unmute TUI modal if we opened the gate
                if on and hasattr(indicator, "set_mic_muted"):
                    try:
                        indicator.set_mic_muted(False)
                    except TypeError:
                        indicator.set_mic_muted(False, mic_name="")
            except Exception:
                pass
            try:
                if hasattr(indicator, "set_force_soft_listen"):
                    if hasattr(indicator, "call_from_thread"):
                        indicator.call_from_thread(
                            indicator.set_force_soft_listen, on
                        )
                    else:
                        indicator.set_force_soft_listen(on)
            except Exception:
                pass
        if on:
            ui.success(
                "[N] Escuta forçada ON — bordas amarelas · "
                "aceita voz baixa (sem precisar tom alto). "
                "[N] de novo desliga · [n] = mute mic.",
                indent=3,
                panel="app",
            )
        else:
            ui.info(
                "[N] Escuta forçada OFF — VAD normal (energia). "
                "Bordas amarelas desligadas.",
                indent=3,
                panel="app",
            )
    elif cmd == "n":
        muted, os_ok, mic_name = pipeline.toggle_mic()
        # Leaving mute while force-listen was on keeps soft VAD; mute clears it.
        if muted:
            try:
                if pipeline.is_force_soft_listen():
                    pipeline.set_force_soft_listen(False)
            except Exception:
                pass
            if indicator is not None and hasattr(indicator, "set_force_soft_listen"):
                try:
                    if hasattr(indicator, "call_from_thread"):
                        indicator.call_from_thread(
                            indicator.set_force_soft_listen, False
                        )
                    else:
                        indicator.set_force_soft_listen(False)
                except Exception:
                    pass
        if indicator is not None:
            # TUI: set_mic_muted(True) opens centered red mute modal (only [n] exits).
            set_muted = getattr(indicator, "set_mic_muted", None)
            if callable(set_muted):
                try:
                    set_muted(muted, mic_name=mic_name)
                except TypeError:
                    set_muted(muted)
            if not muted:
                # Classic ListenIndicator has an animation thread; TUI (LiveLingoApp)
                # only needs set_mic_muted — it has no .start() (header ticks alone).
                start_fn = getattr(indicator, "start", None)
                if callable(start_fn):
                    start_fn()
        if muted:
            if os_ok:
                ui.warn(
                    f"Mic MUTED (Windows): '{mic_name}'. "
                    f"Popup vermelho na TUI — pressione [n] para desmutar.",
                    panel="app",
                )
            else:
                ui.warn(
                    f"Mic MUTED (app only — OS mute falhou): '{mic_name}'. "
                    f"Popup na TUI — pressione [n] para reativar.",
                    panel="app",
                )
            ui.dim(
                "  (modo leitura: sem animação de escuta até o mic LIVE)",
                panel="app",
            )
        else:
            if os_ok:
                ui.success(
                    f"Mic LIVE (Windows): '{mic_name}'. "
                    f"Escuta ativa retomada. Pode falar.",
                    panel="app",
                )
            else:
                ui.success(
                    f"Mic LIVE (app gate): '{mic_name}'. "
                    f"Escuta ativa retomada. Confira o mute no tray se não ouvir.",
                    panel="app",
                )
        _print_menu(pipeline)
    elif cmd == "x":
        if pipeline.stop_playback():
            ui.info(
                "Áudio interrompido ([x]) — escuta liberada; pode falar.",
                panel="app",
            )
        else:
            ui.warn("Nada a interromper (som OFF ou sem TTS a tocar).", panel="app")
    elif cmd == "o":
        print("Enter a word in English: ", end="", flush=True)
        word = sys.stdin.readline().strip()
        if not word:
            return
        ui.info(f"Searching meaning and synonyms for '{word}'...")
        try:
            explanation = synonym_lookup.explain(word)
            pipeline.add_synonym(word, explanation)
            ui.synonyms_result(word, explanation)
        except SynonymError as exc:
            ui.error(f"Synonym lookup failed: {exc}")
        except Exception as exc:
            ui.error(f"Error searching synonyms: {exc}")
    elif cmd == "c":
        full_trans = pipeline.get_full_transcript()
        if not full_trans:
            ui.warn("No conversations recorded in this session to export.")
            return
        print("Enter the title/subject for the file: ", end="", flush=True)
        title = sys.stdin.readline().strip()
        if not title:
            ui.info("Share operation canceled.")
            return
        # Remove accents and normalize to form slug
        normalized = (
            unicodedata.normalize("NFKD", title)
            .encode("ascii", "ignore")
            .decode("ascii")
        )
        slug = re.sub(r"[^\w\s-]", "", normalized.lower())
        slug = re.sub(r"[-\s]+", "-", slug).strip("-_")

        date_str = datetime.date.today().strftime("%Y-%m-%d")
        filename = f"{date_str}_{slug}.md"

        # Map language codes to Portuguese names
        lang_map = {
            "fr": "Francês",
            "en": "Inglês",
            "pt": "Português",
            "es": "Espanhol",
            "de": "Alemão",
            "it": "Italiano",
        }
        src_lang = lang_map.get(cfg.SOURCE_LANG.lower(), cfg.SOURCE_LANG.upper())
        tgt_lang = lang_map.get(cfg.TARGET_LANG.lower(), cfg.TARGET_LANG.upper())

        # Generate AI summary if GROQ_API_KEY is available
        summary_text = ""
        if cfg.GROQ_API_KEY:
            ui.info("Analyzing transcription and generating AI executive summary...")

            # Decouple summary generator from active translation engine:
            # Use existing translator if it's an LLMTranslator, otherwise spin up a temp one
            from livelingo.llm import LLMTranslator

            summary_generator = pipeline.translator
            if not hasattr(summary_generator, "generate_meeting_summary"):
                summary_generator = LLMTranslator(cfg)

            # Concatenate all original heard lines for analysis
            transcript_full = "\n".join(f"- {_entry_heard(e)}" for e in full_trans)
            try:
                summary_text = summary_generator.generate_meeting_summary(
                    transcript_full
                )
            except Exception as exc:
                ui.error(f"Could not generate AI summary: {exc}")
        else:
            ui.warn(
                "Note: AI summary disabled (requires GROQ_API_KEY to be set in .env)."
            )

        synonyms = pipeline.get_synonyms()

        try:
            with open(filename, "w", encoding="utf-8") as f:
                f.write(f"# {title}\n\n")

                if summary_text:
                    f.write(f"{summary_text}\n\n")
                    f.write("---\n\n")  # horizontal rule before content

                f.write("## 💬 Transcrição Detalhada\n\n")
                f.write(
                    "> **LC** = LiveCaptions (entrada · faixa superior) · "
                    "**VOZ** = LiveLingo mic + áudio TTS\n\n"
                )

                # Chronological with channel tags (easy to split on export)
                for entry in full_trans:
                    chunk_num, heard, translated, _created_at, timing = (
                        _unpack_transcript_entry(entry)
                    )
                    is_lc = _is_livecaptions_entry(timing)
                    s_lab, t_lab = _entry_lang_pair(timing, is_lc=is_lc)
                    if is_lc:
                        f.write(
                            f"### [LC {chunk_num}] LiveCaptions "
                            f"({s_lab}→{t_lab}) · entrada\n\n"
                        )
                        f.write(f"**Caption ({s_lab}):** {heard}\n\n")
                        f.write(f"**Translated ({t_lab}):** {translated}\n\n")
                    else:
                        f.write(
                            f"### [Chunk {chunk_num}] LiveLingo VOZ "
                            f"({s_lab}→{t_lab}) · mic+áudio\n\n"
                        )
                        f.write(f"**{t_lab}:** {translated}\n\n")
                        f.write(f"**{s_lab}:** {heard}\n\n")
                        # Audio path if present
                        try:
                            amap = pipeline.get_audio_path_map()
                            ap = amap.get(chunk_num, "")
                            if ap:
                                f.write(f"**Áudio:** `{ap}`\n\n")
                        except Exception:
                            pass

                # Optional grouped appendices for clean split exports
                lc_entries = [
                    e
                    for e in full_trans
                    if _is_livecaptions_entry(_unpack_transcript_entry(e)[4])
                ]
                voz_entries = [
                    e
                    for e in full_trans
                    if not _is_livecaptions_entry(_unpack_transcript_entry(e)[4])
                ]
                if lc_entries:
                    f.write("---\n\n## 📥 Anexo — só LiveCaptions (entrada)\n\n")
                    for entry in lc_entries:
                        n, heard, tr, _, timing = _unpack_transcript_entry(entry)
                        s_lab, t_lab = _entry_lang_pair(timing, is_lc=True)
                        f.write(f"- **[LC {n}]** ({s_lab}) {heard}\n")
                        f.write(f"  - ({t_lab}) {tr}\n")
                    f.write("\n")
                if voz_entries:
                    f.write("---\n\n## 🎙️ Anexo — só LiveLingo VOZ (mic+áudio)\n\n")
                    for entry in voz_entries:
                        n, heard, tr, _, timing = _unpack_transcript_entry(entry)
                        s_lab, t_lab = _entry_lang_pair(timing, is_lc=False)
                        f.write(f"- **[Chunk {n}]** ({s_lab}) {heard}\n")
                        f.write(f"  - ({t_lab}) {tr}\n")
                    f.write("\n")

                # Export synonym vocab searches chronologically
                if synonyms:
                    f.write("## 📚 Vocabulário e Sinônimos Consultados\n\n")
                    for word, explanation in synonyms:
                        f.write(f"### {word.upper()}\n")
                        f.write(f"{explanation}\n\n")

                word_count = _count_content_words(_entry_heard(e) for e in full_trans)
                n_lc = len(lc_entries)
                n_voz = len(voz_entries)
                f.write("---\n")
                f.write(f"**Total de frases:** {len(full_trans)}\n")
                f.write(f"**LiveCaptions (entrada):** {n_lc}\n")
                f.write(f"**LiveLingo VOZ (mic+áudio):** {n_voz}\n")
                f.write(f"**Total de sinônimos consultados:** {len(synonyms)}\n")
                f.write(
                    f"**Total de palavras** (fonte; >1 sílaba; "
                    f"sem e/a/ou/para/ao/à): {word_count}\n"
                )
            ui.success(
                f"Export .md gerado: '{filename}'",
                panel="app",
            )
            ui.dim(
                f"arquivo: {filename}  (sessão exportada)",
                panel="app",
            )
        except Exception as exc:
            ui.error(f"Error saving share file: {exc}", panel="app")
    elif cmd == "l":
        full_trans = pipeline.get_full_transcript()
        if not full_trans:
            ui.warn("Nenhuma frase nesta sessão ainda. Fale no microfone primeiro.")
            return

        audio_map = pipeline.get_audio_path_map()
        comments_map = {}
        try:
            comments_map = pipeline.get_comments_map()
        except Exception:
            comments_map = {}

        n_lc = sum(
            1
            for e in full_trans
            if _is_livecaptions_entry(_unpack_transcript_entry(e)[4])
        )
        n_voz = len(full_trans) - n_lc
        ui.info(
            f"Historico — {len(full_trans)} frase(s) "
            f"(LC entrada: {n_lc} · VOZ mic: {n_voz}):"
        )

        margin = 3
        pad = " " * margin
        in_tui = ui.get_log_sink() is not None
        content_w = ui.content_width(margin=margin)
        rule = ui.rule_line(width=content_w, margin=margin)
        # Dual rail: LC left · VOZ right (gutter 1 col)
        # VOZ nudged ~15 cols left (same as ui._rail_geometry / live chunks)
        gutter = 1
        voz_nudge = int(getattr(ui, "_VOZ_RAIL_LEFT_NUDGE", 15) or 15)
        left_w = max(28, (content_w - gutter) // 2)
        shift_cols = max(0, left_w + gutter - voz_nudge)
        right_w = max(28, content_w - shift_cols)
        left_shift = ""  # LC flush left within content
        right_shift = " " * shift_cols  # VOZ mid-screen, nudged left

        def _title_line(text: str) -> str:
            t = " ".join((text or "").split())
            if len(t) <= content_w:
                return t
            return t[: max(1, content_w - 1)] + "…"

        def _resolve_audio(chunk_num):
            audio_raw = audio_map.get(chunk_num, "")
            if not audio_raw:
                cand = os.path.join(pipeline.cache_dir, f"chunk_{chunk_num}.wav")
                if os.path.isfile(cand):
                    audio_raw = cand
            return audio_raw

        def _comment_items(chunk_num):
            out = []
            for item in comments_map.get(int(chunk_num), []) or []:
                if len(item) >= 3:
                    out.append((item[0], item[1], item[2]))
                else:
                    out.append(
                        (
                            "?",
                            item[0],
                            item[1] if len(item) > 1 else "",
                        )
                    )
            return out

        def _meta_pieces(
            shift: str, meta_indent: str, text: str, *, nowrap: bool = False
        ) -> list[str]:
            """
            Wrap meta text so each physical line stays in the rail.

            Re-applies shift+indent on every piece — RichLog soft-wrap would
            otherwise dump the continuation at column 0 (left edge).
            nowrap=True → single physical line (full text, no middle …).
            """
            text = (text or "").strip()
            if not text:
                return []
            head = shift + meta_indent
            budget = max(12, content_w - len(head))
            if nowrap:
                # Full string — never middle-ellipsis folder/file names
                return [text]
            if text.lower().startswith("audio:"):
                return ui._audio_display_pieces(text, budget)
            return _wrap_labeled_body("", text, budget)

        def _emit_meta_tui(
            shift,
            meta_indent,
            text,
            *,
            style="dim",
            e=None,
            nowrap=False,
            panel="main",
        ):
            """TUI: emit wrapped meta (panel=main VOZ | lc LC)."""
            e = e or (lambda x: x)
            head = shift + meta_indent
            for piece in _meta_pieces(shift, meta_indent, text, nowrap=nowrap):
                if style == "dim":
                    ui.dim(f"{pad}{head}{piece}", panel=panel)
                elif style == "magenta":
                    ui.rich(
                        f"{pad}{head}[dim magenta]{e(piece)}[/]",
                        panel=panel,
                    )
                elif style == "yellow":
                    ui.rich(
                        f"{pad}{head}[bold yellow]{e(piece)}[/]",
                        panel=panel,
                    )
                elif style == "yellow_dim":
                    ui.rich(
                        f"{pad}{head}[yellow]{e(piece)}[/]",
                        panel=panel,
                    )
                else:
                    ui.dim(f"{pad}{head}{piece}", panel=panel)

        def _emit_meta_classic(shift, meta_indent, text, style="", *, nowrap=False):
            head = shift + meta_indent
            for piece in _meta_pieces(shift, meta_indent, text, nowrap=nowrap):
                print(pad + head + style + piece + (Style.RESET_ALL if style else ""))

        def _timing_meta_lines(timing, created_at) -> list[str]:
            """
            Short meta lines for the narrow rail: stats and clock separate
            so the right rail never soft-wraps the clock to the left edge.
            """
            lines = []
            # No clock in the long stats line — clock goes alone below.
            body = ui.format_timing_line(
                timing,
                at=None,
                include_clock=False,
            )
            if body:
                lines.append(body)
            if created_at:
                lines.append(f"@ {ui.clock_hhmmss(created_at)}")
            return lines

        # ------------------------------------------------------------------ #
        # TUI — real dual panels (LC left pane · VOZ right pane)
        # ------------------------------------------------------------------ #
        if in_tui:
            e = ui._rich_escape
            # Headers on both panes
            ui.rich(
                f"{pad}[bold magenta]{e(rule)}[/]",
                panel="lc",
            )
            ui.rich(
                f"{pad}[bold magenta]"
                f"{e(_title_line(f'HISTORICO LC · {n_lc} frase(s)'))}"
                f"[/]",
                panel="lc",
            )
            ui.rich(f"{pad}[bold magenta]{e(rule)}[/]", panel="lc")

            ui.rich(f"{pad}[bold cyan]{e(rule)}[/]", panel="main")
            ui.rich(
                f"{pad}[bold cyan]"
                f"{e(_title_line(f'HISTORICO VOZ · {n_voz} frase(s) · total {len(full_trans)}'))}"
                f"[/]",
                panel="main",
            )
            ui.rich(f"{pad}[bold cyan]{e(rule)}[/]", panel="main")

            # Per-pane wrap width (full column; _rail_geometry is no-pad in TUI)
            lc_w = max(24, content_w)
            voz_w = max(24, content_w)

            for entry in full_trans:
                chunk_num, heard, translated, created_at, timing = (
                    _unpack_transcript_entry(entry)
                )
                is_lc = _is_livecaptions_entry(timing)
                src_l, tgt_l = _entry_lang_pair(timing, is_lc=is_lc)
                recorded = ui.format_recorded_stamp(created_at) if created_at else ""

                if is_lc:
                    col_w = lc_w
                    shift = ""
                    panel = "lc"
                    prefix = f"[LC {chunk_num}] "
                    lab_cap = f"{prefix}{src_l}: "
                    lab_tr = f"{' ' * len(prefix)}{tgt_l}: "
                    ind_cap = " " * len(lab_cap)
                    ind_tr = " " * len(lab_tr)

                    for i, line in enumerate(_wrap_labeled_body(lab_cap, heard, col_w)):
                        if i == 0:
                            ui.rich(
                                f"{pad}{shift}"
                                f"[bold magenta]{e(prefix)}[/]"
                                f"[bold cyan]{e(src_l)}: [/]"
                                f"[green]{e(line)}[/]",
                                panel=panel,
                            )
                        else:
                            ui.rich(
                                f"{pad}{shift}{ind_cap}[green]{e(line)}[/]",
                                panel=panel,
                            )
                    for i, line in enumerate(
                        _wrap_labeled_body(lab_tr, translated, col_w)
                    ):
                        if i == 0:
                            ui.rich(
                                f"{pad}{shift}"
                                f"[dim]{e(' ' * len(prefix))}[/]"
                                f"[bold cyan]{e(tgt_l)}: [/]"
                                f"[bold white]{e(line)}[/]",
                                panel=panel,
                            )
                        else:
                            ui.rich(
                                f"{pad}{shift}{ind_tr}[bold white]{e(line)}[/]",
                                panel=panel,
                            )
                    meta_indent = " " * len(prefix)
                    if recorded:
                        _emit_meta_tui(
                            shift,
                            meta_indent,
                            f"gravado: {recorded}",
                            style="dim",
                            e=e,
                            panel=panel,
                        )
                    for tline in _timing_meta_lines(timing, created_at):
                        _emit_meta_tui(
                            shift,
                            meta_indent,
                            tline,
                            style="dim",
                            e=e,
                            panel=panel,
                        )
                    for c_id, c_text, c_at in _comment_items(chunk_num):
                        stamp = ui.format_recorded_stamp(c_at) or (c_at or "")
                        body = " ".join((c_text or "").split())
                        _emit_meta_tui(
                            shift,
                            meta_indent,
                            f"comment #{c_id}: {stamp}  {body}",
                            style="magenta",
                            e=e,
                            panel=panel,
                        )
                    ui.raw("", panel=panel)
                else:
                    col_w = voz_w
                    shift = ""
                    panel = "main"
                    prefix = f"[Chunk {chunk_num}] "
                    lab_src = f"{prefix}{src_l}: "
                    lab_tgt = f"{' ' * len(prefix)}{tgt_l}: "
                    ind_src = " " * len(lab_src)
                    ind_tgt = " " * len(lab_tgt)

                    for i, line in enumerate(_wrap_labeled_body(lab_src, heard, col_w)):
                        if i == 0:
                            ui.rich(
                                f"{pad}{shift}"
                                f"[bold yellow]{e(prefix)}[/]"
                                f"[white]{e(src_l)}: [/]"
                                f"[green]{e(line)}[/]",
                                panel=panel,
                            )
                        else:
                            ui.rich(
                                f"{pad}{shift}{ind_src}[green]{e(line)}[/]",
                                panel=panel,
                            )
                    for i, line in enumerate(
                        _wrap_labeled_body(lab_tgt, translated, col_w)
                    ):
                        if i == 0:
                            ui.rich(
                                f"{pad}{shift}"
                                f"[white]{e(' ' * len(prefix))}[/]"
                                f"[bold blue]{e(tgt_l)}: [/]"
                                f"[bold white]{e(line)}[/]",
                                panel=panel,
                            )
                        else:
                            ui.rich(
                                f"{pad}{shift}{ind_tgt}[bold white]{e(line)}[/]",
                                panel=panel,
                            )

                    meta_indent = " " * len(prefix)
                    audio_raw = _resolve_audio(chunk_num)
                    for tline in _timing_meta_lines(timing, created_at):
                        _emit_meta_tui(
                            shift,
                            meta_indent,
                            tline,
                            style="dim",
                            e=e,
                            panel=panel,
                        )
                    if recorded:
                        _emit_meta_tui(
                            shift,
                            meta_indent,
                            f"gravado: {recorded}",
                            style="dim",
                            e=e,
                            panel=panel,
                        )
                    try:
                        ui._emit_audio_path_one_line(chunk_num, audio_raw, panel="main")
                    except Exception:
                        for al in ui.format_audio_lines(audio_raw):
                            _emit_meta_tui(
                                shift,
                                meta_indent,
                                al,
                                style="yellow",
                                e=e,
                                nowrap=True,
                                panel=panel,
                            )
                    for c_id, c_text, c_at in _comment_items(chunk_num):
                        stamp = ui.format_recorded_stamp(c_at) or (c_at or "")
                        body = " ".join((c_text or "").split())
                        _emit_meta_tui(
                            shift,
                            meta_indent,
                            f"comment #{c_id}: {stamp}  {body}",
                            style="yellow_dim",
                            e=e,
                            panel=panel,
                        )
                    ui.raw("", panel=panel)

            ui.rich(f"{pad}[bold magenta]{e(rule)}[/]", panel="lc")
            ui.rich(
                f"{pad}[bold magenta]{e(_title_line(f'Total LC: {n_lc}'))}[/]",
                panel="lc",
            )
            ui.rich(f"{pad}[bold cyan]{e(rule)}[/]", panel="main")
            tot = f"Total VOZ: {n_voz}  ·  sessão {len(full_trans)} (LC+VOZ)"
            ui.rich(f"{pad}[bold cyan]{e(_title_line(tot))}[/]", panel="main")
            return

        # ------------------------------------------------------------------ #
        # Classic terminal — same dual rail
        # ------------------------------------------------------------------ #
        def _print_plain(text, style=""):
            for line in textwrap.wrap(
                text or "",
                width=content_w,
                replace_whitespace=False,
                drop_whitespace=True,
            ) or [""]:
                print(pad + style + line + (Style.RESET_ALL if style else ""))

        print()
        print(pad + Fore.CYAN + rule + Style.RESET_ALL)
        _print_plain(
            _title_line("HISTORICO (cronologico) · LC ◄ esquerda · VOZ ► direita"),
            Fore.CYAN + Style.BRIGHT,
        )
        leg_l = "◄ LC entrada (LiveCaptions)"
        leg_r = "VOZ mic+áudio ►"
        mid = max(0, left_w - len(leg_l))
        print(
            pad
            + Fore.MAGENTA
            + Style.BRIGHT
            + leg_l
            + Style.RESET_ALL
            + (" " * mid)
            + (" " * gutter)
            + Fore.YELLOW
            + Style.BRIGHT
            + leg_r
            + Style.RESET_ALL
        )
        print(pad + Fore.CYAN + rule + Style.RESET_ALL)
        print(pad)

        for entry in full_trans:
            chunk_num, heard, translated, created_at, timing = _unpack_transcript_entry(
                entry
            )
            is_lc = _is_livecaptions_entry(timing)
            src_l, tgt_l = _entry_lang_pair(timing, is_lc=is_lc)
            recorded = ui.format_recorded_stamp(created_at) if created_at else ""

            if is_lc:
                col_w = left_w
                shift = left_shift
                prefix = f"[LC {chunk_num}] "
                lab_cap = f"{prefix}{src_l}: "
                lab_tr = f"{' ' * len(prefix)}{tgt_l}: "
                ind_cap = " " * len(lab_cap)
                ind_tr = " " * len(lab_tr)
                for i, line in enumerate(_wrap_labeled_body(lab_cap, heard, col_w)):
                    if i == 0:
                        print(
                            pad
                            + shift
                            + Fore.MAGENTA
                            + Style.BRIGHT
                            + prefix
                            + Fore.CYAN
                            + Style.BRIGHT
                            + f"{src_l}: "
                            + Style.RESET_ALL
                            + Fore.GREEN
                            + line
                            + Style.RESET_ALL
                        )
                    else:
                        print(
                            pad + shift + ind_cap + Fore.GREEN + line + Style.RESET_ALL
                        )
                for i, line in enumerate(_wrap_labeled_body(lab_tr, translated, col_w)):
                    if i == 0:
                        print(
                            pad
                            + shift
                            + (" " * len(prefix))
                            + Fore.CYAN
                            + Style.BRIGHT
                            + f"{tgt_l}: "
                            + Style.RESET_ALL
                            + Fore.WHITE
                            + line
                            + Style.RESET_ALL
                        )
                    else:
                        print(
                            pad + shift + ind_tr + Fore.WHITE + line + Style.RESET_ALL
                        )
                meta_indent = " " * len(prefix)
                if recorded:
                    _emit_meta_classic(
                        shift,
                        meta_indent,
                        f"gravado: {recorded}",
                        Style.DIM,
                    )
                for tline in _timing_meta_lines(timing, created_at):
                    _emit_meta_classic(shift, meta_indent, tline, Style.DIM)
            else:
                # SOURCE (heard) → TARGET (translated)
                col_w = right_w
                shift = right_shift
                prefix = f"[Chunk {chunk_num}] "
                lab_src = f"{prefix}{src_l}: "
                lab_tgt = f"{' ' * len(prefix)}{tgt_l}: "
                ind_src = " " * len(lab_src)
                ind_tgt = " " * len(lab_tgt)
                for i, line in enumerate(_wrap_labeled_body(lab_src, heard, col_w)):
                    if i == 0:
                        print(
                            pad
                            + shift
                            + Fore.YELLOW
                            + Style.BRIGHT
                            + prefix
                            + Style.RESET_ALL
                            + Fore.WHITE
                            + f"{src_l}: "
                            + Fore.GREEN
                            + line
                            + Style.RESET_ALL
                        )
                    else:
                        print(
                            pad + shift + ind_src + Fore.GREEN + line + Style.RESET_ALL
                        )
                for i, line in enumerate(
                    _wrap_labeled_body(lab_tgt, translated, col_w)
                ):
                    if i == 0:
                        print(
                            pad
                            + shift
                            + (" " * len(prefix))
                            + Fore.BLUE
                            + Style.BRIGHT
                            + f"{tgt_l}: "
                            + Style.RESET_ALL
                            + Fore.WHITE
                            + line
                            + Style.RESET_ALL
                        )
                    else:
                        print(
                            pad + shift + ind_tgt + Fore.WHITE + line + Style.RESET_ALL
                        )
                meta_indent = " " * len(prefix)
                audio_raw = _resolve_audio(chunk_num)
                for tline in _timing_meta_lines(timing, created_at):
                    _emit_meta_classic(shift, meta_indent, tline, Style.DIM)
                if recorded:
                    _emit_meta_classic(
                        shift,
                        meta_indent,
                        f"gravado: {recorded}",
                        Style.DIM,
                    )
                # Full path, one line, right-aligned (no wrap / no …)
                try:
                    ui._emit_audio_path_one_line(chunk_num, audio_raw, panel="main")
                except Exception:
                    for al in ui.format_audio_lines(audio_raw):
                        _emit_meta_classic(
                            shift,
                            meta_indent,
                            al,
                            Fore.YELLOW + Style.BRIGHT,
                            nowrap=True,
                        )
                for c_id, c_text, c_at in _comment_items(chunk_num):
                    stamp = ui.format_recorded_stamp(c_at) or (c_at or "")
                    body = " ".join((c_text or "").split())
                    _emit_meta_classic(
                        shift,
                        meta_indent,
                        f"comment #{c_id}: {stamp}  {body}",
                        Fore.YELLOW,
                    )
            print(pad)

        print(pad + Fore.CYAN + rule + Style.RESET_ALL)
        _print_plain(
            f"Total: {len(full_trans)}  ·  LC◄ {n_lc}  ·  VOZ► {n_voz}",
            Fore.CYAN + Style.BRIGHT,
        )
        print(pad + Fore.CYAN + rule + Style.RESET_ALL)
        print()
    elif cmd == "a" or (cmd.startswith("a") and len(cmd) > 1 and cmd[1:].isdigit()):
        # Copy audio file path to clipboard (last or aN).
        chunk_num = int(cmd[1:]) if len(cmd) > 1 and cmd[1:].isdigit() else None
        n, share, raw = _resolve_chunk_audio(pipeline, chunk_num)
        if n is None:
            ui.warn("Nenhum chunk no histórico.", indent=3)
            return
        if not share and not raw:
            ui.warn(
                f"Chunk {n}: sem arquivo de áudio. Use r{n} para gerar TTS.",
                indent=3,
            )
            return
        path = share or ui.resolve_share_path(raw)
        if _copy_path_to_clipboard(path):
            ui.success(
                f"Path do áudio [chunk {n}] copiado para a área de transferência.",
                indent=3,
            )
            ui.dim(f"   {path}", indent=3)
        else:
            ui.warn(
                "Não foi possível copiar para o clipboard. Path manual:",
                indent=3,
            )
            ui.dim(f"   {path}", indent=3)
    elif cmd == "p" or (cmd.startswith("p") and len(cmd) > 1 and cmd[1:].isdigit()):
        # Open Explorer selecting the audio file (last or pN).
        chunk_num = int(cmd[1:]) if len(cmd) > 1 and cmd[1:].isdigit() else None
        n, share, raw = _resolve_chunk_audio(pipeline, chunk_num)
        if n is None:
            ui.warn("Nenhum chunk no histórico.", indent=3)
            return
        path = share or raw
        if not path:
            ui.warn(
                f"Chunk {n}: sem arquivo de áudio. Use r{n} para gerar TTS.",
                indent=3,
            )
            return
        # Prefer path that exists on this OS for open.
        open_path = raw if raw and os.path.isfile(raw) else path
        if _open_audio_in_explorer(open_path if os.path.isfile(open_path) else path):
            ui.success(
                f"Pasta do áudio [chunk {n}] aberta no Explorer.",
                indent=3,
            )
            ui.dim(f"   {ui.resolve_share_path(path)}", indent=3)
        else:
            ui.warn(
                "Não foi possível abrir o Explorer. Path:",
                indent=3,
            )
            ui.dim(f"   {ui.resolve_share_path(path)}", indent=3)
    elif re.match(r"^co(\d+)?(\s|$)", cmd):
        # co / co108 / co108 texto na mesma linha
        # Examples:
        #   co
        #   co108
        #   co108 aqui eu quero comentar uma tradução mal feita.
        m_co = re.match(r"^co(\d+)?(?:\s+(.*))?$", (raw_cmd or "").strip(), re.I | re.S)
        if not m_co:
            ui.warn("Uso: co | coN | coN texto do comentário")
            return
        num_s = m_co.group(1)
        inline = (m_co.group(2) or "").strip()

        if num_s:
            chunk_num = int(num_s)
        else:
            with pipeline.history_lock:
                if not pipeline.history:
                    ui.warn("Nenhuma frase nesta sessão para comentar.")
                    return
                chunk_num = pipeline.history[-1][0]

        # Verify chunk exists before prompting
        exists = False
        with pipeline.history_lock:
            for n, *_r in pipeline.history:
                if n == chunk_num:
                    exists = True
                    break
            if not exists:
                for entry in pipeline.full_transcript:
                    if entry and entry[0] == chunk_num:
                        exists = True
                        break
        if not exists:
            ui.warn(f"Chunk {chunk_num} não encontrado. Use [l] para ver os números.")
            return

        saved = 0
        if inline:
            # Same-line comment: co108 texto...
            cid, stamp = pipeline.add_comment(chunk_num, inline)
            if stamp:
                saved = 1
                ui.dim(f"  + #{cid}  {stamp}  {inline}")
        else:
            # Interactive multi-line (empty line or '.' ends)
            ui.info(
                f"Comentários para [chunk {chunk_num}] — uma por linha; "
                f"Enter vazio encerra (ou '.' sozinho):"
            )
            ui.dim(
                f"  (várias linhas = vários comentários; "
                f"ou use: co{chunk_num} seu texto)"
            )
            while True:
                try:
                    line = sys.stdin.readline()
                except Exception:
                    break
                if line is None:
                    break
                text = line.rstrip("\r\n")
                if not text.strip() or text.strip() == ".":
                    break
                cid, stamp = pipeline.add_comment(chunk_num, text)
                if stamp:
                    saved += 1
                    ui.dim(f"  + #{cid}  {stamp}  {text.strip()}")

        if saved:
            ui.success(
                f"{saved} comentário(s) salvos no chunk {chunk_num}. "
                f"Veja com [l] (ex.: comment #id). Apague com codN."
            )
        else:
            ui.warn("Nenhum comentário gravado.")
    elif re.match(r"^cod\d+$", cmd):
        # cod99 — delete comment by primary key (no confirmation)
        comment_id = int(cmd[3:])
        pipeline.delete_comment(comment_id)
    elif cmd in ("cls", "cls1", "cls2"):
        # cls  = both Tradução columns + Sistema
        # cls1 = left  (LiveCaptions #log-lc)
        # cls2 = right (VOZ #log)
        side = {"cls1": 1, "cls2": 2}.get(cmd)
        cleared = False
        if side is not None:
            # One-side clear (TUI split only)
            if indicator is not None and hasattr(indicator, "clear_log_side"):
                try:
                    if hasattr(indicator, "call_from_thread"):
                        indicator.call_from_thread(indicator.clear_log_side, side)
                    else:
                        indicator.clear_log_side(side)
                    cleared = True
                except Exception:
                    try:
                        indicator.clear_log_side(side)
                        cleared = True
                    except Exception:
                        pass
            if cleared:
                which = (
                    "LC (esquerda / LiveCaptions)"
                    if side == 1
                    else "VOZ (direita / mic + comandos)"
                )
                ui.success(f"Log {which} limpo.")
            else:
                ui.warn(
                    f"[{cmd}] só na TUI (coluna esquerda=cls1, direita=cls2). "
                    f"Use [cls] para limpar tudo, ou UI_MODE=tui."
                )
        else:
            # Full clear
            if indicator is not None and hasattr(indicator, "clear_log"):
                try:
                    if hasattr(indicator, "call_from_thread"):
                        indicator.call_from_thread(indicator.clear_log)
                    else:
                        indicator.clear_log()
                    cleared = True
                except Exception:
                    try:
                        indicator.clear_log()
                        cleared = True
                    except Exception:
                        pass
            if not cleared:
                # Classic: clear terminal
                try:
                    sys.stdout.write("\033[H\033[J")
                    sys.stdout.flush()
                except Exception:
                    print("\n" * 40)
            ui.success("Log limpo." if ui.get_log_sink() else "Tela limpa.")
    elif raw_cmd == "GG" or cmd == "gf":
        # Go bottom / footer — scroll log to end (TUI).
        # GG case-sensitive (vim-style); gf lowercase alias.
        # TUI usually handles this on the UI thread in _submit_command_line.
        moved = False
        if indicator is not None and hasattr(indicator, "scroll_log_footer"):
            try:
                if hasattr(indicator, "call_from_thread"):
                    indicator.call_from_thread(indicator.scroll_log_footer)
                else:
                    indicator.scroll_log_footer()
                moved = True
            except Exception:
                try:
                    indicator.scroll_log_footer()
                    moved = True
                except Exception:
                    pass
        if not moved:
            ui.warn("GG/gf só funciona no modo TUI.", indent=3)
    elif cmd in ("gt", "gg"):
        # Go top — scroll log to start (TUI).
        # gg (vim-style) + gt. After GG check so "GG" is not treated as top.
        # TUI usually handles this on the UI thread in _submit_command_line.
        moved = False
        if indicator is not None and hasattr(indicator, "scroll_log_top"):
            try:
                if hasattr(indicator, "call_from_thread"):
                    indicator.call_from_thread(indicator.scroll_log_top)
                else:
                    indicator.scroll_log_top()
                moved = True
            except Exception:
                try:
                    indicator.scroll_log_top()
                    moved = True
                except Exception:
                    pass
        if not moved:
            ui.warn("gg/gt só funciona no modo TUI.", indent=3)
    elif cmd == "lav":
        # List all edge-tts voices → log
        _run_cmd_to_log(
            _edge_tts_list_voices_argv(),
            "edge-tts — todas as vozes (edge-tts --list-voices)",
            timeout=120,
        )
    elif cmd == "lv":
        # Filtered edge-tts voices (en-US|en-GB|es-ES|es-MX|fr-FR) → log
        _run_cmd_to_log(
            _edge_tts_list_voices_argv(),
            "edge-tts — vozes en-US|en-GB|es-ES|es-MX|fr-FR",
            timeout=120,
            line_filter=_LV_VOICE_RE,
        )
    elif cmd == "ld":
        # Audio devices (python list_devices.py) → log
        script = os.path.join(_project_root(), "list_devices.py")
        _run_cmd_to_log(
            [sys.executable, script],
            "Dispositivos de áudio (python list_devices.py)",
            timeout=60,
        )
    elif cmd == "ctts" or cmd.startswith("ctts "):
        # Change TTS_VOICE (edge-tts ShortName) — command only, no modal/click.
        # Prefer one-liner: ctts en-US-AndrewMultilingualNeural
        # Without args: prompt in #cmd / stdin (classic readline style).
        cur = (getattr(cfg, "TTS_VOICE", "") or "").strip() or "?"
        parts = (raw_cmd or "").split(None, 1)
        inline = (parts[1] or "").strip() if len(parts) == 2 else ""

        if not inline:
            ui.info(f"TTS_VOICE atual: {cur}", indent=3)
            ui.dim(
                "Uso: ctts <ShortName>  ·  lista: lav / lv  ·  "
                "ex: ctts en-US-AndrewMultilingualNeural",
                indent=3,
            )
            print(
                "   Nova voz edge-tts (Enter vazio = cancelar): ",
                end="",
                flush=True,
            )
            try:
                inline = sys.stdin.readline().strip()
            except (KeyboardInterrupt, EOFError):
                inline = ""
            if not inline:
                ui.info("TTS_VOICE inalterada (entrada vazia).", indent=3)
                return

        result = pipeline.set_tts_voice(inline)
        if not result.get("ok"):
            ui.warn(result.get("error") or "Falha ao alterar TTS_VOICE.", indent=3)
            return
        for w in result.get("warnings") or []:
            ui.warn(w, indent=3)
        voice = result.get("voice") or cfg.TTS_VOICE
        old = result.get("old_voice") or "?"
        if result.get("unchanged"):
            ui.info(f"TTS_VOICE já era {voice} — sem mudança.", indent=3)
        else:
            ui.success(f"[ctts] TTS_VOICE: {old}  ⇒  {voice}")
            ui.info("Próximos áudios usarão a nova voz.", indent=3)
        if indicator is not None:
            # Non-blocking UI refresh of TTS badge / menu
            try:
                if hasattr(indicator, "request_refresh_source_ui"):
                    indicator.request_refresh_source_ui()
                elif hasattr(indicator, "refresh_source_ui"):
                    if hasattr(indicator, "call_from_thread"):
                        indicator.call_from_thread(indicator.refresh_source_ui)
                    else:
                        indicator.refresh_source_ui()
            except Exception:
                pass
    elif cmd == "lo":
        # List only SOURCE (heard) phrases, one per line
        full_trans = pipeline.get_full_transcript()
        if not full_trans:
            ui.warn("Nenhuma frase nesta sessão ainda.")
            return
        ui.info(f"Source ({cfg.SOURCE_LANG.upper()}) — {len(full_trans)} frase(s):")
        for entry in full_trans:
            _n, heard, _tr, _at, _tm = _unpack_transcript_entry(entry)
            text = " ".join((heard or "").split()).strip()
            if text:
                ui.raw(text)
    elif cmd == "lt":
        # List only TARGET (translated) phrases, one per line
        full_trans = pipeline.get_full_transcript()
        if not full_trans:
            ui.warn("Nenhuma frase nesta sessão ainda.")
            return
        ui.info(f"Target ({cfg.TARGET_LANG.upper()}) — {len(full_trans)} frase(s):")
        for entry in full_trans:
            _n, _heard, translated, _at, _tm = _unpack_transcript_entry(entry)
            text = " ".join((translated or "").split()).strip()
            if text:
                ui.raw(text)
    elif cmd == "v":
        print(
            "Are you sure you want to switch or restart the session? (y/n): ",
            end="",
            flush=True,
        )
        confirm = sys.stdin.readline().strip().lower()
        if confirm in ("y", "yes", "s", "sim"):
            pipeline.switch_session = True
            pipeline.stop()
            return
        else:
            ui.info("Operation canceled.")
    elif cmd == "m":
        _print_menu(pipeline)
    elif cmd in ("u", "ui", "compact"):
        # Toggle compact TUI: hide command menu strip (command input stays).
        # F4 does the same. Does not resize the Windows console (breaks Textual).
        if indicator is not None and hasattr(indicator, "toggle_compact_ui"):
            try:
                if hasattr(indicator, "call_from_thread"):
                    indicator.call_from_thread(indicator.toggle_compact_ui)
                else:
                    indicator.toggle_compact_ui()
            except Exception as exc:
                ui.warn(f"UI compacta falhou: {exc}", indent=3, panel="app")
        else:
            ui.warn(
                "Comando [u]/compact só funciona no modo TUI (UI_MODE=tui).",
                indent=3,
                panel="app",
            )
    elif cmd == "sub" or cmd.startswith("sub ") or cmd in (
        "subtitle",
        "subtitles",
        "legenda",
    ) or (cmd.startswith("subtitle ") or cmd.startswith("subtitles ") or cmd.startswith("legenda ")):
        # Burn-in TARGET on virtual cam (`u` is compact UI — use [sub] here).
        # Aliases: sub | subtitle | subtitles | legenda · on|off · cam sub …
        svc = getattr(pipeline, "webcam_service", None)
        parts = (raw_cmd or cmd or "").strip().split(None, 1)
        action = (parts[1] if len(parts) > 1 else "").strip().lower()
        if svc is None:
            ui.warn(
                "Legenda vcam indisponível — WEBCAM_ENABLED=true + "
                "pip install opencv-python mediapipe pyvirtualcam.",
                indent=3,
                panel="app",
            )
        elif action in ("on", "1", "true", "enable"):
            on, msg = svc.set_subtitle_enabled(True)
            (ui.success if on else ui.info)(msg, indent=3, panel="app")
        elif action in ("off", "0", "false", "disable"):
            on, msg = svc.set_subtitle_enabled(False)
            ui.info(msg, indent=3, panel="app")
        elif action in ("clear", "cls", "x"):
            try:
                svc.clear_subtitle_text()
            except Exception:
                pass
            ui.info("Texto TARGET da legenda vcam limpo.", indent=3, panel="app")
        elif action in ("status", "st", "?"):
            on = bool(svc.is_subtitle_enabled())
            snap = svc.snapshot() if hasattr(svc, "snapshot") else {}
            preview = (snap.get("subtitle_text") or "")[:80]
            ui.info(
                f"Legenda vcam: {'ON' if on else 'OFF'} · "
                f"hold={getattr(cfg, 'WEBCAM_SUBTITLE_HOLD_S', 12)}s · "
                f'text="{preview or "—"}"',
                indent=3,
                panel="app",
            )
        else:
            on, msg = svc.toggle_subtitle()
            (ui.success if on else ui.info)(msg, indent=3, panel="app")
    elif (
        cmd == "cam"
        or cmd.startswith("cam ")
        or cmd == "webcam"
        or cmd.startswith("webcam ")
    ):
        # Webcam lip-sync → virtual camera (optional module)
        # Video → pyvirtualcam (OBS Virtual Cam). Audio → VB-Cable separately.
        # Physical cam LED only lights AFTER start+enable (OpenCV opens device).
        svc = getattr(pipeline, "webcam_service", None)
        sub = ""
        low = (cmd or "").strip().lower()
        if low.startswith("webcam "):
            sub = low[7:].strip()
        elif low == "webcam":
            sub = ""
        elif low.startswith("cam "):
            sub = low[4:].strip()
        elif (raw_cmd or "").strip().lower().startswith("cam "):
            sub = (raw_cmd or "").strip()[4:].strip().lower()
        elif (raw_cmd or "").strip().lower().startswith("webcam "):
            sub = (raw_cmd or "").strip()[7:].strip().lower()

        def _cam_info(msg):
            ui.info(msg, indent=3, panel="app")

        def _cam_ok(msg):
            ui.success(msg, indent=3, panel="app")

        def _cam_warn(msg):
            ui.warn(msg, indent=3, panel="app")

        def _cam_err(msg):
            ui.error(msg, indent=3, panel="app")

        def _cam_auto_sound():
            """TTS → CABLE is separate from video; lips need the same samples."""
            if not getattr(cfg, "WEBCAM_AUTO_SOUND", True):
                return
            try:
                if not pipeline.is_sound_enabled():
                    pipeline.set_sound_enabled(True)
                    _cam_ok(
                        "Som ON automático (WEBCAM_AUTO_SOUND) — TTS → CABLE. "
                        "Teams mic = CABLE Output. Toggle: [s]"
                    )
                    if indicator is not None and hasattr(indicator, "set_sound_on"):
                        try:
                            if hasattr(indicator, "call_from_thread"):
                                indicator.call_from_thread(indicator.set_sound_on, True)
                            else:
                                indicator.set_sound_on(True)
                        except Exception:
                            pass
            except Exception as exc:
                _cam_warn(f"WEBCAM_AUTO_SOUND falhou: {exc}")

        def _cam_teams_hint():
            from livelingo.webcam.service import teams_setup_hint

            sound_on = bool(
                getattr(pipeline, "is_sound_enabled", lambda: False)()
            )
            _cam_info(teams_setup_hint())
            if not sound_on:
                _cam_warn(
                    "Som LiveLingo está OFF — [s] liga TTS → CABLE → mic Teams. "
                    "Sem [s] o Teams não ouve tradução e a boca não mexe."
                )
            else:
                _cam_info(
                    f"Som ON · OUTPUT={getattr(cfg, 'OUTPUT_DEVICE', '?')} → "
                    "Teams deve usar mic CABLE Output."
                )

        if svc is None:
            _cam_warn(
                "Webcam lip-sync indisponível. "
                "Defina WEBCAM_ENABLED=true e instale: "
                "pip install opencv-python mediapipe pyvirtualcam "
                "(+ OBS Virtual Cam / v4l2loopback). Ver docs/webcam-lipsync.md",
            )
        elif sub in ("on", "start", "enable"):
            if not getattr(svc, "_started", False):
                ok = svc.start()
                if not ok:
                    _cam_err(
                        f"Webcam start falhou: {svc.snapshot().get('error') or '?'}",
                    )
                    return
            if svc.is_enabled():
                _cam_info(
                    "Webcam já ON — física + OBS Virtual Cam ativas. "
                    "[cam status] · [cam off] libera.",
                )
            else:
                svc.enable()
                _cam_auto_sound()
                _cam_ok(
                    "Webcam ON — abrindo câmera física + OBS Virtual Cam. "
                    "No Teams escolha OBS Virtual Camera (não a webcam física).",
                )
            _cam_teams_hint()
        elif sub in ("off", "stop", "disable"):
            was_on = bool(svc.is_enabled())
            svc.disable()
            if was_on:
                _cam_ok(
                    "Webcam OFF — câmera física e OBS Virtual Cam liberadas "
                    "(LED/device livres). [cam on] retoma sem reiniciar o app.",
                )
            else:
                _cam_info(
                    "Webcam já OFF — dispositivos de câmera livres. "
                    "[cam on] liga de novo.",
                )
        elif sub in (
            "closed",
            "closed on",
            "closed off",
            "closed auto",
            "boca",
            "boca on",
            "boca off",
            "boca auto",
            "f10",
        ):
            # Manual closed-mouth face plate (same as F10 in TUI)
            if not getattr(svc, "_started", False):
                ok = svc.start()
                if not ok:
                    _cam_err(
                        f"Webcam start falhou: {svc.snapshot().get('error') or '?'}",
                    )
                    return
            if not svc.is_enabled():
                svc.enable()
                _cam_auto_sound()
            parts = sub.split()
            action = parts[-1] if len(parts) > 1 else "toggle"
            if action in ("on", "1", "true"):
                on, msg = svc.set_closed_mouth_manual(True)
                _cam_ok(msg) if on else _cam_info(msg)
            elif action in ("off", "0", "false"):
                on, msg = svc.set_closed_mouth_manual(False)
                _cam_info(msg)
            elif action in ("auto", "vad"):
                _cam_info(svc.set_closed_mouth_auto())
            else:
                on, msg = svc.toggle_closed_mouth_manual()
                (ui.success if on else ui.info)(msg, indent=3)
        elif sub in (
            "full",
            "full on",
            "full off",
            "freeze",
            "freeze on",
            "freeze off",
            "tela",
            "tela on",
            "tela off",
            "f11",
        ):
            # F11: full-frame closed photo (entire vcam = closed image)
            if not getattr(svc, "_started", False):
                ok = svc.start()
                if not ok:
                    _cam_err(
                        f"Webcam start falhou: {svc.snapshot().get('error') or '?'}",
                    )
                    return
            if not svc.is_enabled():
                svc.enable()
                _cam_auto_sound()
            parts = sub.split()
            action = parts[-1] if len(parts) > 1 else "toggle"
            if action in ("on", "1", "true"):
                on, msg = svc.set_closed_full_frame(True)
                _cam_ok(msg) if on else _cam_info(msg)
            elif action in ("off", "0", "false"):
                on, msg = svc.set_closed_full_frame(False)
                _cam_info(msg)
            else:
                on, msg = svc.toggle_closed_full_frame()
                (ui.success if on else ui.info)(msg, indent=3)
        elif sub in (
            "snap closed",
            "snap",
            "snapshot closed",
            "capture closed",
            "snap closed preview",
        ):
            # Photo of closed mouth → template for idle (mic listening)
            if not getattr(svc, "_started", False):
                ok = svc.start()
                if not ok:
                    _cam_err(
                        f"Webcam start falhou: {svc.snapshot().get('error') or '?'}",
                    )
                    return
            if not svc.is_enabled():
                svc.enable()
                _cam_auto_sound()
            _cam_info(
                "Abrindo preview da câmera… "
                "Feche a boca | SPACE/ENTER=salvar | ESC=cancelar | "
                "auto-save ~3s com face OK.",
            )
            _cam_info(
                "Se não aparecer janela (WSL/headless), use Windows nativo "
                "ou confira: pip install mediapipe",
            )
            ok, msg = svc.snap_closed_mouth(preview=True, timeout_s=45.0)
            if ok:
                _cam_ok(
                    f"{msg} — idle usará esta foto (sem TTS). "
                    "Refaça se a iluminação mudar. [cam status] → tpl=true",
                )
            else:
                _cam_err(msg)
        elif sub in (
            "sub",
            "sub on",
            "sub off",
            "subtitle",
            "subtitle on",
            "subtitle off",
            "subtitles",
            "subtitles on",
            "subtitles off",
            "legend",
            "legend on",
            "legend off",
            "legenda",
            "legenda on",
            "legenda off",
        ):
            # Burn-in TARGET text on OBS Virtual Cam frames (not Teams CC)
            parts = sub.split()
            action = parts[-1] if len(parts) > 1 else "toggle"
            if action in ("on", "1", "true"):
                on, msg = svc.set_subtitle_enabled(True)
                _cam_ok(msg) if on else _cam_info(msg)
            elif action in ("off", "0", "false"):
                on, msg = svc.set_subtitle_enabled(False)
                _cam_info(msg)
            else:
                on, msg = svc.toggle_subtitle()
                (_cam_ok if on else _cam_info)(msg)
        elif sub in ("status", "st", "?"):
            snap = svc.snapshot()
            sound_on = bool(
                getattr(pipeline, "is_sound_enabled", lambda: False)()
            )
            _cam_info(
                f"CAM running={snap.get('running')} enabled={snap.get('enabled')} "
                f"engine={snap.get('engine')} face={snap.get('face_ok')} "
                f"cap_ok={snap.get('capture_ok')} vcam={snap.get('vcam_ready')} "
                f"phase={snap.get('emit_phase') or '—'} "
                f"fps_cap={snap.get('fps_cap')} fps_out={snap.get('fps_out')} "
                f"sent={snap.get('frames_sent')} "
                f"tts={snap.get('audio_playing')} rms={snap.get('audio_rms')} "
                f"tpl={snap.get('template_ok')} vad={snap.get('vad_speech')} "
                f"marker={snap.get('sync_marker')} sound={sound_on} "
                f"sub={snap.get('subtitle')} "
                f"out={getattr(cfg, 'OUTPUT_DEVICE', '?')} "
                f"{snap.get('width')}x{snap.get('height')} "
                f"backend={snap.get('backend') or '—'} "
                f"err={snap.get('error') or '—'}",
            )
            if not snap.get("template_ok"):
                _cam_info(
                    "Sem foto de boca fechada — digite: cam snap closed "
                    "(boca fechada, olhando a câmera).",
                )
            if snap.get("enabled") and not snap.get("vcam_ready"):
                _cam_warn(
                    "vcam ainda não abriu — OBS Virtual Camera é exclusiva: "
                    "em OBS clique Stop Virtual Camera (botão OFF), feche outros "
                    "produtores, aguarde ~2s (auto-retry). Driver: Start Virtual "
                    "Camera uma vez (Admin) só para registrar, depois Stop. "
                    "pip install pyvirtualcam · docs/webcam-lipsync.md",
                )
            if snap.get("enabled") and not snap.get("capture_ok"):
                _cam_warn(
                    "capture_ok=false — feche a câmera no Teams e use "
                    "WEBCAM_DEVICE_INDEX correto (listar no Device Manager).",
                )
            if sound_on and not snap.get("audio_playing"):
                _cam_info(
                    "tts=false — boca/marker só mexem durante TTS. "
                    "Fale e espere tradução. Confirma Teams mic = CABLE Output.",
                )
            elif sound_on and float(snap.get("audio_rms") or 0) < 1e-4:
                _cam_info(
                    "tts ativo mas rms≈0 (silêncio no clip). Marker idle se sem energia.",
                )
            _cam_teams_hint()
        else:
            # bare [cam] toggles enable (starts threads on first on)
            if not getattr(svc, "_started", False):
                ok = svc.start()
                if not ok:
                    _cam_err(
                        f"Webcam start falhou: {svc.snapshot().get('error') or '?'}",
                    )
                    return
                svc.enable()
                _cam_auto_sound()
                _cam_ok("Webcam lip-sync ON (1ª ativação).")
                _cam_teams_hint()
            else:
                on = svc.toggle()
                if on:
                    _cam_auto_sound()
                    _cam_ok(
                        "Webcam ON — física + virtual cam. "
                        "[cam off] libera dispositivos.",
                    )
                    _cam_teams_hint()
                else:
                    _cam_ok(
                        "Webcam OFF — física + OBS Virtual Cam liberadas. "
                        "[cam on] retoma.",
                    )
    elif cmd == "lc" or cmd.startswith("lc "):
        # Live Captions (Windows): on/off (start+resume / pause) · show / hide / status
        # Default: OFF at launch (LIVE_CAPTIONS_START_ON_LAUNCH=false) — only [lc on].
        svc = getattr(pipeline, "caption_service", None)
        sub = ""
        if cmd.startswith("lc "):
            sub = cmd[3:].strip().lower()
        elif (raw_cmd or "").strip().lower().startswith("lc "):
            sub = (raw_cmd or "").strip()[3:].strip().lower()
        if svc is None:
            ui.warn(
                "Live Captions indisponível. "
                "Defina LIVE_CAPTIONS_ENABLED=true e rode no Windows 11 "
                "(pip install uiautomation).",
                indent=3,
            )
        elif sub in ("show", "restore", "unhide"):
            if not svc.is_running():
                ui.warn(
                    "Live Captions ainda OFF — use [lc on] antes de [lc show].",
                    indent=3,
                )
            else:
                svc.show_window()
                ui.success("LiveCaptions: janela restaurada.", indent=3)
        elif sub in ("hide",):
            if not svc.is_running():
                ui.info("Live Captions OFF (nada a ocultar).", indent=3)
            else:
                svc.hide_window()
                ui.success("LiveCaptions: janela oculta.", indent=3)
        elif sub in ("on", "resume", "start"):
            try:
                if not svc.is_running():
                    svc.start()
                    ui.success(
                        "Live Captions: ON (iniciado). [lc off] desliga.",
                        indent=3,
                    )
                else:
                    svc.resume()
                    ui.success(
                        "Live Captions: ON. [lc off] desliga.",
                        indent=3,
                    )
            except Exception as exc:
                ui.warn(f"Live Captions falhou ao ligar: {exc}", indent=3)
        elif sub in ("off", "pause", "stop"):
            if not svc.is_running():
                ui.info("Live Captions já está OFF.", indent=3)
            else:
                svc.pause()
                ui.info(
                    "Live Captions: OFF ([lc on] retoma).",
                    indent=3,
                )
        elif sub in ("status", "st", "?"):
            snap = svc.snapshot()
            on_off = (
                "ON"
                if snap.get("running") and not snap.get("paused")
                else "OFF"
            )
            ui.info(
                f"LC {on_off} status={snap.get('status')} "
                f"paused={snap.get('paused')} running={snap.get('running')} "
                f"hidden={snap.get('hidden')} err={snap.get('error') or '—'}",
                indent=3,
            )
        else:
            # bare [lc] toggles: not running → start ON; running → pause/resume
            try:
                if not svc.is_running():
                    svc.start()
                    ui.success(
                        "Live Captions ON (iniciado). [lc] de novo pausa.",
                        indent=3,
                    )
                else:
                    paused = svc.toggle_pause()
                    if paused:
                        ui.info(
                            "Live Captions OFF ([lc] / [lc on] retoma).",
                            indent=3,
                        )
                    else:
                        ui.success("Live Captions ON.", indent=3)
            except Exception as exc:
                ui.warn(f"Live Captions falhou: {exc}", indent=3)
    elif cmd in ("q", "quit"):
        ui.info("Stopping application...")
        try:
            svc = getattr(pipeline, "caption_service", None)
            if svc is not None:
                svc.stop()
        except Exception:
            pass
        try:
            cam = getattr(pipeline, "webcam_service", None)
            if cam is not None:
                cam.stop()
        except Exception:
            pass
        pipeline.stop()
        return
    else:
        ui.warn(
            f"Unknown command: '{cmd}'. Use: "
            f"r/rN, rs/rsN, e/eN, enew, d/dN, f/fN, F, s, g (swap), t (TARGET), "
            f"lc (Live Captions), cam (webcam lip-sync), a/aN (copy audio path), "
            f"p/pN (open audio folder), "
            f"n (mic), b (bypass voice), x, o, c, l, lo, lt, ld, lav, lv, ctts, "
            f"co/coN, codN, cls/cls1/cls2, gg/gt (top), GG/gf (bottom), u (compact UI), v, m, q.",
            indent=3,
        )


def _parse_cli_session_arg(argv=None):
    """
    Extract a session id from CLI for direct resume (skip session menu).

    Supported:
      livelingo <session_id>
      livelingo --session <session_id>
      livelingo --session=<session_id>
      python main.py <session_id>

    Ignores known flags (--verbose, -h, --help, --list-sessions, …).
    Returns str or None.
    """
    if argv is None:
        args = list(sys.argv[1:])
    else:
        args = list(argv)
        # Caller may pass full sys.argv — drop script name (main.py / livelingo).
        if args:
            base = os.path.basename(args[0]).lower()
            if (
                base.endswith(".py")
                or base.endswith(".bat")
                or base.endswith(".sh")
                or base in ("main.py", "livelingo", "python", "python3", "py")
            ):
                args = args[1:]
            # Windows: sometimes argv[0] is full path to python; then argv[1]=main.py
            if args:
                base1 = os.path.basename(args[0]).lower()
                if base1.endswith(".py") or base1 in ("main.py", "livelingo"):
                    args = args[1:]

    skip = {
        "--verbose",
        "-v",
        "-h",
        "--help",
        "--classic",
        "--tui",
        "--list-sessions",
    }
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("--session", "-s") and i + 1 < len(args):
            return (args[i + 1] or "").strip() or None
        if a.startswith("--session="):
            return a.split("=", 1)[1].strip() or None
        if a in skip or a.startswith("-"):
            i += 1
            continue
        # First non-flag positional = session id (never treat main.py as id)
        cand = (a or "").strip()
        if not cand:
            i += 1
            continue
        base = os.path.basename(cand).lower()
        if base.endswith(".py") or base in ("main.py", "livelingo"):
            i += 1
            continue
        return cand
    return None


def _cli_args(argv=None):
    """Return argv tokens after the script name (best-effort)."""
    if argv is None:
        return list(sys.argv[1:])
    args = list(argv)
    if args:
        base = os.path.basename(args[0]).lower()
        if (
            base.endswith(".py")
            or base.endswith(".bat")
            or base.endswith(".sh")
            or base in ("main.py", "livelingo", "python", "python3", "py")
        ):
            args = args[1:]
        if args:
            base1 = os.path.basename(args[0]).lower()
            if base1.endswith(".py") or base1 in ("main.py", "livelingo"):
                args = args[1:]
    return args


def _cli_wants_help(argv=None):
    """True if -h or --help is present on the command line."""
    return bool({"-h", "--help"} & set(_cli_args(argv)))


def _cli_wants_list_sessions(argv=None):
    """True if --list-sessions is present on the command line."""
    return "--list-sessions" in _cli_args(argv)


def _cli_wants_verbose(argv=None):
    """True if --verbose or -v is present on the command line."""
    return bool({"--verbose", "-v"} & set(_cli_args(argv)))


def _print_cli_help():
    """Print CLI usage in English (simple explanations + example outputs)."""
    text = """
LiveLingo — real-time voice translator

USAGE
  python main.py [options] [session_id]
  livelingo [options] [session_id]

With no options, the app starts and shows the interactive session menu.

OPTIONS
  -h, --help
      Show this help message and exit.

  --list-sessions
      List every saved session and exit.
      Same line format as menu option [2] (RESUME a previous session).

      Example:
        $ livelingo --list-sessions

           Last sessions found:
           [1] Weekly standup (ID: 20260716_205709_session-2026-07-16-2057, Created at: 2026-07-16 20:57:09)
           [2] Interview EN-PT (ID: 20260716_195215_entrevista-ingles-portugues, Created at: 2026-07-16 19:52:15)

      If the database has no sessions:
           No previous sessions found.

  --session <session_id>
  -s <session_id>
  --session=<session_id>
  <session_id>
      Resume a session by full id (or a unique id prefix).
      Skips the session picker on first start.

      Example:
        $ livelingo --session 20260716_205709_session-2026-07-16-2057
        $ livelingo -s 20260716_205709
        $ livelingo 20260716_205709_session-2026-07-16-2057

           Resuming session (CLI): 'Weekly standup' (ID: 20260716_205709_session-2026-07-16-2057)

      If the id is not found:
           Session not found: 'bad-id'

      If a short prefix matches more than one session, the matches are printed
      and you must pass the full id.

  -v, --verbose
      Turn on detailed debug logs (STT filters, timing, processing chatter).
      Can be combined with a session id.

      Example:
        $ livelingo --verbose
        $ livelingo -v --session 20260716_205709_session-2026-07-16-2057

INTERACTIVE MENU (no CLI session flags)
  [1]  Start a NEW session
  [2]  RESUME a previous session
  [99] DELETE a previous session (Atomic)

EXIT / RESUME TIP
  Press Ctrl+C or Ctrl+Q to stop a live session.
  The session id is printed on exit so you can resume later with:
    livelingo <session_id>
""".strip("\n")
    print(text)
    return 0


def _print_sessions_listing(sessions, indent=None):
    """
    Print sessions in the same format as menu option [2] RESUME.

    Format:
      Last sessions found:
      [1] {title} (ID: {sid}, Created at: {created_at})
    """
    m = UI_MARGIN if indent is None else indent
    pad = " " * m
    if not sessions:
        ui.warn("No previous sessions found.", indent=m)
        return
    ui.info("Last sessions found:", indent=m)
    for idx, (sid, title, created_at) in enumerate(sessions, 1):
        print(pad + f"[{idx}] {title} (ID: {sid}, Created at: {created_at})")


def _run_list_sessions_cli():
    """List all registered sessions (CLI --list-sessions) and return exit code."""
    db.init_db()
    sessions = db.list_sessions(limit=None)
    print()
    _print_sessions_listing(sessions)
    print()
    return 0


def _resume_session_by_id(session_ref):
    """
    Resolve session_ref (exact id or unique prefix) → (session_id, title).

    Returns (id, title) or (None, None) if not found / ambiguous.
    """
    from livelingo import db

    db.init_db()
    ref = (session_ref or "").strip()
    if not ref:
        return None, None

    row = db.get_session(ref)
    if row:
        return row[0], row[1]

    matches = db.find_sessions_by_prefix(ref, limit=10)
    if len(matches) == 1:
        return matches[0][0], matches[0][1]
    if len(matches) > 1:
        m = UI_MARGIN
        ui.warn(
            f"Session id prefix '{ref}' matches {len(matches)} sessions — "
            f"use the full id:",
            indent=m,
        )
        for sid, title, created_at in matches:
            ui.dim(f"  {sid}  ·  {title}  ({created_at})", indent=m)
        return None, None

    ui.error(f"Session not found: '{ref}'", indent=UI_MARGIN)
    return None, None


def _select_session():
    """
    Prompt the user to start a new session, resume an existing one, or delete an existing one in English.
    Returns (session_id, session_title)
    """
    from livelingo import db

    db.init_db()
    m = UI_MARGIN
    pad = " " * m

    def _p(text=""):
        print(pad + text)

    def _prompt(text):
        print(pad + text, end="", flush=True)
        return sys.stdin.readline().strip()

    print()
    ui.info("Select a Session Option:", indent=m)
    _p("[1] Start a NEW session")
    _p("[2] RESUME a previous session")
    _p("[99] DELETE a previous session (Atomic)")
    print()

    choice = ""
    while choice not in ("1", "2", "99"):
        choice = _prompt("Option (1, 2 or 99): ")

    if choice == "2":
        sessions = db.list_sessions(limit=5)
        if not sessions:
            ui.warn("No previous sessions found. Creating a new session...", indent=m)
            choice = "1"
        else:
            _print_sessions_listing(sessions, indent=m)
            _p("[0] Voltar ao menu inicial / Back to start")
            print()

            sel = None
            while sel is None:
                sel_str = _prompt(
                    f"Choose session (1–{len(sessions)}, or 0 to go back): "
                ).lower()
                if sel_str in ("0", "b", "back", "voltar"):
                    ui.info("Returning to session menu…", indent=m)
                    return _select_session()
                if sel_str.isdigit():
                    num = int(sel_str)
                    if 1 <= num <= len(sessions):
                        sel = num - 1
                    else:
                        ui.warn(
                            f"Invalid number. Use 1–{len(sessions)} or 0 to go back.",
                            indent=m,
                        )
                elif sel_str:
                    ui.warn("Invalid option. Use a number, or 0 to go back.", indent=m)

            sid, title, _ = sessions[sel]
            ui.success(f"Resuming session: '{title}' (ID: {sid})", indent=m)
            return sid, title

    if choice == "99":
        sessions = db.list_sessions(limit=10)
        if not sessions:
            ui.warn("No previous sessions found to delete.", indent=m)
            return _select_session()

        ui.info("Last sessions found:", indent=m)
        for idx, (sid, title, created_at) in enumerate(sessions, 1):
            _p(f"[{idx}] {title} (ID: {sid}, Created at: {created_at})")
        _p("[0] Voltar ao menu inicial / Back to start")
        print()

        sel = None
        while sel is None:
            sel_str = _prompt(
                f"Choose session to DELETE (1–{len(sessions)}, 0=back, Enter=cancel): "
            ).lower()
            if not sel_str:
                ui.info("Deletion canceled — back to session menu.", indent=m)
                return _select_session()
            if sel_str in ("0", "b", "back", "voltar"):
                ui.info("Returning to session menu…", indent=m)
                return _select_session()
            if sel_str.isdigit():
                num = int(sel_str)
                if 1 <= num <= len(sessions):
                    sel = num - 1
                else:
                    ui.warn(
                        f"Invalid number. Use 1–{len(sessions)} or 0 to go back.",
                        indent=m,
                    )
            else:
                ui.warn(
                    "Invalid option. Use a number, 0 to go back, or Enter to cancel.",
                    indent=m,
                )

        sid, title, _ = sessions[sel]
        _p(
            f"Are you absolutely sure you want to delete session '{title}' "
            f"and ALL associated data?"
        )
        confirm = _prompt(
            "This operation is IRREVERSIBLE! (y/n, or 0 to go back): "
        ).lower()

        if confirm in ("0", "b", "back", "voltar"):
            ui.info("Returning to session menu…", indent=m)
            return _select_session()
        if confirm in ("y", "yes", "s", "sim"):
            ui.info(
                f"Starting atomic transaction to delete session '{title}'...",
                indent=m,
            )
            try:
                db.delete_session_atomic(sid)
                ui.success(
                    f"Session '{title}' and all its dependencies deleted successfully!",
                    indent=m,
                )
            except Exception as exc:
                ui.error(
                    f"Error deleting session: {exc}. Rollback executed.",
                    indent=m,
                )
        else:
            ui.info("Deletion canceled.", indent=m)

        return _select_session()

    # Choice is 1 (new session)
    title = _prompt(
        "Enter title/subject for the new session (Enter=automatic, 0=back to menu): "
    )
    if title.lower() in ("0", "b", "back", "voltar"):
        ui.info("Returning to session menu…", indent=m)
        return _select_session()
    if not title:
        title = f"Session {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}"

    # Generate unique ID
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    # for a clean ID
    normalized = (
        unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode("ascii")
    )
    slug = re.sub(r"[^\w\s-]", "", normalized.lower())
    slug = re.sub(r"[-\s]+", "-", slug).strip("-_")
    session_id = f"{timestamp}_{slug}"

    db.create_session(session_id, title)
    ui.success(f"New session created: '{title}' (ID: {session_id})", indent=m)
    return session_id, title


def _ensure_wrapper_scripts():
    """Ensure portable livelingo.sh / livelingo.bat exist (no absolute user paths)."""
    import os

    sh_path = "livelingo.sh"
    bat_path = "livelingo.bat"

    # 1. Write livelingo.sh if not exists
    if not os.path.exists(sh_path):
        try:
            with open(sh_path, "w", newline="\n", encoding="utf-8") as f:
                f.write("#!/bin/bash\n")
                f.write("# LiveLingo — run from this folder (portable)\n")
                f.write('cd "$(dirname "$0")" || exit 1\n')
                f.write('python3 main.py "$@"\n')
            os.chmod(sh_path, 0o755)
        except Exception:
            pass

    # 2. Write livelingo.bat if not exists
    if not os.path.exists(bat_path):
        try:
            with open(bat_path, "w", newline="\r\n", encoding="utf-8") as f:
                f.write("@echo off\n")
                f.write(":: LiveLingo — run from this folder (portable)\n")
                f.write('cd /d "%~dp0"\n')
                f.write("python main.py %*\n")
        except Exception:
            pass


def main():
    # --- One-shot CLI flags (before any UI / device setup) ---
    if _cli_wants_help():
        raise SystemExit(_print_cli_help())

    # --- Ensure wrapper scripts are generated locally ---
    _ensure_wrapper_scripts()

    # --- Enable verbose debug logs if --verbose / -v is passed ---
    cfg.VERBOSE = _cli_wants_verbose()

    # --- One-shot: list all sessions (same format as menu [2]) and exit ---
    if _cli_wants_list_sessions():
        raise SystemExit(_run_list_sessions_cli())

    # Direct resume: livelingo <session_id>  (only first loop; [v] returns to menu)
    pending_resume_id = _parse_cli_session_arg()

    while True:
        sys.stdout.write("\033[H\033[J")  # Clear screen on startup or restart
        sys.stdout.flush()

        ui.banner()

        # --- Session Setup ---
        session_id = None
        session_title = None
        if pending_resume_id:
            ref = pending_resume_id
            pending_resume_id = None  # consume once
            session_id, session_title = _resume_session_by_id(ref)
            if session_id:
                ui.success(
                    f"Resuming session (CLI): '{session_title}' (ID: {session_id})",
                    indent=UI_MARGIN,
                )
            else:
                ui.warn(
                    "Could not open session from CLI — showing session menu.",
                    indent=UI_MARGIN,
                )
        if not session_id:
            session_id, session_title = _select_session()
        session_start_time = time.time()

        # --- Devices ---
        in_idx, in_name = _resolve_input()
        out_idx, out_name = _resolve_output()
        _print_device_overview(in_idx, in_name, out_idx, out_name)

        monitor_idx = None
        # Monitor headphones: full TTS copy and/or pre-TTS cue (strict MONITOR_DEVICE)
        want_monitor = bool(getattr(cfg, "MONITOR_PLAYBACK", False)) or bool(
            getattr(cfg, "TTS_MONITOR_CUE", True)
        )
        if want_monitor:
            mon_spec = str(getattr(cfg, "MONITOR_DEVICE", "") or "").strip()
            from livelingo.monitor_cue import resolve_headphones

            monitor_idx, mon_name = resolve_headphones(
                mon_spec, cable_index=out_idx
            )
            if monitor_idx is None:
                _log_warn(
                    f"Monitor/cue: {mon_name}. "
                    "Defina MONITOR_DEVICE=13 (índice do fone em list_devices.py)."
                )
            elif cfg.MONITOR_PLAYBACK:
                _log_info(
                    f"Monitor playback ON -> [{monitor_idx}] {mon_name}"
                )
            elif getattr(cfg, "TTS_MONITOR_CUE", True):
                _log_info(
                    f"TTS cue → fone [{monitor_idx}] {mon_name} "
                    f"(~{getattr(cfg, 'TTS_MONITOR_CUE_LEAD_S', 1.0)}s antes; "
                    "NÃO Cable/Teams)"
                )

        if getattr(cfg, "MUTE_CAPTURE_DURING_PLAYBACK", True):
            hang_ms = int(getattr(cfg, "MUTE_CAPTURE_HANGOVER_MS", 350))
            _log_info(
                f"Anti-feedback: mic gated during TTS "
                f"(hangover {hang_ms} ms). Set MUTE_CAPTURE_DURING_PLAYBACK=false to disable."
            )

        # --- Settings summary ---
        print()
        _log_info(
            f"Languages: {cfg.SOURCE_LANG} -> {cfg.TARGET_LANG}   |   "
            f"TTS: {_tts_menu_label()}   |   "
            f"Sound: OFF (default)  |  "
            f"VAD: {_vad_label()}"
        )
        _print_streaming_info(indent=UI_MARGIN)

        # --- Translation engine (validate key/model before the slow model load) ---
        translator = _build_translator()
        synonym_lookup = build_synonym_lookup(cfg, translator, log=_log_info)

        # --- Speech-to-text engine (Groq cloud or local Whisper) ---
        transcriber = _build_transcriber()

        # --- TTS (edge online or Piper local) ---
        synthesizer = build_synthesizer(cfg, log=_log_info)

        use_tui = (getattr(cfg, "UI_MODE", "tui") or "tui").lower() == "tui"
        tui_holder = {"app": None}
        indicator = ListeningIndicator()

        def on_listening(is_speaking):
            # UX: always log listen start/end in the scrollback (TUI + classic).
            try:
                ui.listen_progress(bool(is_speaking))
            except Exception:
                pass
            # Webcam closed-mouth photo: only while VAD hears speech
            try:
                cam = getattr(pipeline, "webcam_service", None)
                if cam is not None and hasattr(cam, "notify_vad_speech"):
                    cam.notify_vad_speech(bool(is_speaking))
            except Exception:
                pass
            app = tui_holder.get("app")
            if app is not None:
                try:
                    app.call_from_thread(app.set_speaking, bool(is_speaking))
                except Exception:
                    pass
                return
            # Classic: keep animation thread alive; flip speaking/idle frames.
            if indicator.is_mic_muted_ui():
                indicator.set_speaking(False)
                return
            indicator.set_speaking(is_speaking)
            indicator.start()

        # --- Build and start the pipeline ---
        pipeline = Pipeline(
            config=cfg,
            input_device=in_idx,
            output_device=out_idx,
            transcriber=transcriber,
            translator=translator,
            synthesizer=synthesizer,
            session_id=session_id,
            monitor_device=monitor_idx,
            on_listening=on_listening,
            input_device_name=in_name,
        )
        # Default sound OFF — mirror on robot line; user enables with [s].
        indicator.set_sound_on(pipeline.is_sound_enabled())

        print()
        # Warn before listening if Windows mic is already muted / volume 0.
        pipeline.check_mic_muted_warn()
        _log_success(
            f"Listening — speak {cfg.SOURCE_LANG.upper()} now. "
            f"Press Ctrl+C to stop. [n]=mic mute. [g]=swap langs."
        )
        _log_warn(
            "Áudio de tradução DESLIGADO por padrão (só texto). "
            "Pressione [s] para ouvir ao vivo, ou [r]/[rN] para um chunk."
        )
        if not use_tui:
            _print_menu(pipeline)

        def _on_deferred_language_swap(src, tgt, voice):
            """UI when a scheduled [g] actually applies after the current phrase."""
            pair = f"{str(src).upper()} → {str(tgt).upper()}"
            g_panel = "app" if ui.get_log_sink() is not None else "main"
            ui.success(
                f"[g] Swap aplicado: {pair}   (STT={src} · TTS={voice})",
                indent=3,
                panel=g_panel,
            )
            ui.info(
                f"Fale {str(src).upper()} agora — os outros ouvem {str(tgt).upper()}. "
                f"Histórico antigo não é re-traduzido.",
                indent=3,
                panel=g_panel,
            )
            # Footer menu + placeholder follow new SOURCE_LANG
            app = tui_holder.get("app")
            if app is not None:
                try:
                    app.call_from_thread(app.refresh_source_ui)
                except Exception:
                    pass
            if not use_tui:
                _print_swap_lang_menu_line(pipeline)

        pipeline.set_language_swap_callback(_on_deferred_language_swap)
        # Start VOZ immediately (recorder thread) before LC/webcam so escuta
        # is live as soon as the TUI paints.
        pipeline.start()

        # --- Live Captions (Windows LiveCaptions → TUI strip; parallel to mic) ---
        # Default: build service but do NOT start (LIVE_CAPTIONS_START_ON_LAUNCH=false).
        # Escuta ativa = VOZ/mic path. LC only with [lc on] / off with [lc off].
        caption_service = None
        if getattr(cfg, "LIVE_CAPTIONS_ENABLED", False):
            try:
                from livelingo.livecaptions import build_caption_service, is_windows

                if is_windows():
                    caption_service = build_caption_service(
                        cfg,
                        translator,
                        session_id=session_id,
                        phrase_cache=getattr(pipeline, "phrase_cache", None),
                        pipeline=pipeline,
                    )
                    pipeline.caption_service = caption_service
                    auto_lc = bool(
                        getattr(cfg, "LIVE_CAPTIONS_START_ON_LAUNCH", False)
                    )
                    if auto_lc:
                        caption_service.start()
                    try:
                        from livelingo.livecaptions import caption_lang_pair

                        lc_s, lc_t = caption_lang_pair(cfg)
                        if auto_lc:
                            _log_info(
                                f"Live Captions ON — {lc_s}→{lc_t} (strip) · "
                                f"SQLite+cache · [lc off] pausa · "
                                f"[lc show]/[lc hide]."
                            )
                        else:
                            _log_info(
                                f"Live Captions OFF (pronto {lc_s}→{lc_t}) — "
                                f"[lc on] inicia · [lc off] pausa · "
                                f"[lc show]/[lc hide]."
                            )
                    except Exception:
                        if auto_lc:
                            _log_info(
                                "Live Captions ON — faixa superior da TUI "
                                "([lc off] pausa · [lc show]/[lc hide])."
                            )
                        else:
                            _log_info(
                                "Live Captions OFF (pronto) — "
                                "[lc on] inicia · [lc off] pausa."
                            )
                else:
                    _log_warn("LIVE_CAPTIONS_ENABLED=true mas OS ≠ Windows — ignorado.")
            except Exception as exc:
                _log_warn(f"Live Captions não iniciou: {exc}")
                caption_service = None
                pipeline.caption_service = None
        else:
            pipeline.caption_service = None

        # --- Webcam lip-sync → virtual camera (optional; Teams/Meet) ---
        # NOTE: virtual cam = VIDEO only. Translated audio still goes to
        # OUTPUT_DEVICE (CABLE Input); Teams mic must be CABLE Output.
        webcam_service = None
        if getattr(cfg, "WEBCAM_ENABLED", False):
            try:
                from livelingo.webcam import build_webcam_service
                from livelingo.webcam.service import check_webcam_deps, teams_setup_hint

                deps = check_webcam_deps()
                if not deps.get("cv2") or not deps.get("pyvirtualcam"):
                    miss = [
                        n
                        for n, ok in (
                            ("opencv-python", deps.get("cv2")),
                            ("pyvirtualcam", deps.get("pyvirtualcam")),
                            ("mediapipe", deps.get("mediapipe")),
                        )
                        if not ok
                    ]
                    _log_warn(
                        "WEBCAM_ENABLED=true mas deps ausentes: "
                        + ", ".join(miss)
                        + " → pip install opencv-python mediapipe pyvirtualcam"
                    )
                webcam_service = build_webcam_service(cfg, log=_log_cam)
                pipeline.webcam_service = webcam_service
                if webcam_service is not None:
                    # Closed-mouth photo driven by VAD via on_listening → notify_vad_speech
                    # Auto-start is deferred until TUI log sink is ready so
                    # capture/vcam thread messages appear in the TUI log.
                    # Classic CLI starts immediately below.
                    want_auto = bool(getattr(cfg, "WEBCAM_START_ENABLED", False))
                    pipeline._webcam_autostart = want_auto
                    if want_auto and not use_tui:
                        if webcam_service.start():
                            webcam_service.enable()
                            _log_cam(
                                "Webcam lip-sync ON (auto) — virtual cam · "
                                "[cam] toggle · [cam status]."
                            )
                            _log_cam(teams_setup_hint())
                        else:
                            _log_cam_warn(
                                "Webcam enabled but start failed — "
                                f"{webcam_service.snapshot().get('error')}"
                            )
                    elif want_auto and use_tui:
                        _log_cam(
                            "Webcam lip-sync: auto-start após TUI "
                            "(logs na aba Sistema)."
                        )
                        _log_cam(teams_setup_hint())
                    else:
                        _log_cam(
                            "Webcam lip-sync ready (idle) — "
                            "ainda NÃO envia vídeo. Digite [cam on] com o app aberto, "
                            "depois [cam status] (vcam=true cap_ok=true)."
                        )
                        _log_cam(teams_setup_hint())
            except Exception as exc:
                _log_cam_warn(f"Webcam lip-sync não iniciou: {exc}")
                webcam_service = None
                pipeline.webcam_service = None
        else:
            pipeline.webcam_service = None

        cmd_thread = None
        if use_tui:
            try:
                from livelingo.tui_app import LiveLingoApp

                app = LiveLingoApp(
                    pipeline=pipeline,
                    synonym_lookup=synonym_lookup,
                    dispatch_command=_dispatch_command,
                    listen_msgs_fn=_listen_status_messages,
                    help_fn=lambda p=pipeline: _print_f1_help(p),
                    caption_service=caption_service,
                )
                tui_holder["app"] = app
                if pipeline.is_mic_muted():
                    app.set_mic_muted(True)
                app.set_sound_on(pipeline.is_sound_enabled())
                _log_info("UI_MODE=tui — log rolável · escuta fixa embaixo.")
                try:
                    app.run()
                finally:
                    tui_holder["app"] = None
                    ui.set_log_sink(None)
            except ImportError as exc:
                ui.warn(
                    f"TUI indisponível ({exc}). "
                    f"Instale: pip install textual  — caindo para modo classic."
                )
                use_tui = False

        session_switch = False
        if not use_tui:
            # Classic CLI: animated indicator + stdin command thread
            if pipeline.is_mic_muted():
                indicator.set_mic_muted(True)
                _log_dim("Mic already muted — listen icons off until [n].")
            else:
                indicator.start()
            cmd_thread = threading.Thread(
                target=_input_loop,
                args=(pipeline, synonym_lookup, indicator),
                name="input_listener",
                daemon=True,
            )
            cmd_thread.start()
            try:
                while not pipeline.stop_event.is_set():
                    pipeline.stop_event.wait(0.2)
            except KeyboardInterrupt:
                print()
                ui.info("Ctrl+C received — shutting down...")
                pipeline.stop()
                if cmd_thread is not None:
                    cmd_thread.join(timeout=2.0)
            finally:
                indicator.stop()
                try:
                    if caption_service is not None:
                        caption_service.stop()
                except Exception:
                    pass
                try:
                    if webcam_service is not None:
                        webcam_service.stop()
                except Exception:
                    pass
                pipeline.stop()
                pipeline.join(timeout=5.0)
                if cmd_thread is not None:
                    cmd_thread.join(timeout=5.0)
            session_switch = bool(getattr(pipeline, "switch_session", False))
        else:
            # TUI exited — clean pipeline + Live Captions + webcam
            try:
                if caption_service is not None:
                    caption_service.stop()
            except Exception:
                pass
            # Snapshot BEFORE stop so terminal shows last cam state (TUI logs vanish).
            if webcam_service is not None:
                try:
                    snap = webcam_service.snapshot() or {}
                    ui.info(
                        f"CAM final (antes de parar): vcam={snap.get('vcam_ready')} "
                        f"cap_ok={snap.get('capture_ok')} "
                        f"fps_out={snap.get('fps_out')} sent={snap.get('frames_sent')} "
                        f"backend={snap.get('backend') or '—'} "
                        f"err={snap.get('error') or '—'}",
                        indent=3,
                    )
                    if not snap.get("vcam_ready"):
                        ui.warn(
                            "vcam nunca ficou ready — OBS Virtual Camera / "
                            "pip install pyvirtualcam. Ver .cache/webcam_status.txt",
                            indent=3,
                        )
                except Exception:
                    pass
            try:
                if webcam_service is not None:
                    webcam_service.stop()
            except Exception:
                pass
            try:
                pipeline.stop()
                pipeline.join(timeout=5.0)
            except Exception:
                pass
            session_switch = bool(getattr(pipeline, "switch_session", False))

        print("-" * 64)
        ui.success("Stopped. Au revoir!")
        _print_session_duration(
            session_start_time,
            session_title,
            session_id=session_id,
        )

        if session_switch:
            continue
        break


if __name__ == "__main__":
    main()
