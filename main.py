"""
main.py
=======
Entry point for the real-time FR -> EN voice translator.

    python main.py
    python main.py <session_id>          # resume session, skip picker
    livelingo <session_id>               # same via wrapper
    livelingo --session <session_id>

Flow: microphone -> Whisper STT (Groq cloud or local faster-whisper)
      -> translation (Groq LLM or Google) -> edge-tts (TTS)
      -> VB-Cable output device (so Teams hears English).

Press Ctrl+C / Ctrl+Q to stop (session id printed on exit for easy resume).
"""

import os
import subprocess
import sys
import threading
import datetime
import re
import textwrap
import unicodedata
import time
from colorama import Fore, Style

import numpy as np

import config as cfg
from livelingo import db, devices, ui
from livelingo.groq_transcribe import GroqSTTError, GroqTranscriber
from livelingo.llm import GROQ_KEY_HELP, LLMError, LLMTranslator
from livelingo.pipeline import Pipeline
from livelingo.synthesize import build_synthesizer
from livelingo.transcribe import Transcriber
from livelingo.synonyms import SynonymError, build_synonym_lookup
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
    code = (source_lang if source_lang is not None else getattr(cfg, "SOURCE_LANG", "en"))
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
                audio_tag = (
                    Fore.GREEN + Style.BRIGHT + "🔊 ÁUDIO ON" + Style.RESET_ALL
                )
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
                msg = (
                    f"\r\033[K{pad}{frame} {pair}  {audio_tag}  {active_msg}"
                )
                delay = 0.12
            else:
                frame = idle_frames[idx % len(idle_frames)]
                msg = (
                    f"\r\033[K{pad}{frame} {pair}  {audio_tag}  {idle_msg}"
                )
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
    if engine == "hybrid" or (
        engine == "piper" and getattr(cfg, "TTS_HYBRID", False)
    ):
        voice = getattr(cfg, "PIPER_VOICE", "") or f"auto:{cfg.TARGET_LANG}"
        return f"hybrid (edge+piper / {voice})"
    if engine == "piper":
        voice = getattr(cfg, "PIPER_VOICE", "") or f"auto:{cfg.TARGET_LANG}"
        return f"piper ({voice})"
    return f"edge ({cfg.TTS_VOICE})"


def _print_streaming_info(indent=0):
    if not (
        getattr(cfg, "STREAMING_LLM", False)
        or getattr(cfg, "STREAMING_TTS", False)
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
            sound = (
                "ON"
                if pipeline.is_sound_enabled()
                else "OFF (default)"
            )
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
    if pipeline is not None and isinstance(
        getattr(pipeline, "translator", None), LLMTranslator
    ):
        ui.success(
            f"LLM translation ready (Groq / {cfg.GROQ_MODEL}).",
            indent=m,
        )
    else:
        ui.info(
            "Translation engine: Google (free). Tip: add a free GROQ_API_KEY in "
            ".env for much more natural results.",
            indent=m,
        )

    if pipeline is not None and isinstance(
        getattr(pipeline, "transcriber", None), GroqTranscriber
    ):
        ui.info(
            f"Speech-to-text: Groq cloud ({cfg.GROQ_STT_MODEL}).",
            indent=m,
        )
        ui.success(
            f"Speech-to-text ready (Groq cloud / {cfg.GROQ_STT_MODEL}).",
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
    `.translate(text)` method. For the LLM engine, do a quick self-test so a
    bad key / model name fails fast with a clear message.
    """
    engine = (cfg.TRANSLATION_ENGINE or "auto").lower()
    if engine == "auto":
        engine = "llm" if cfg.GROQ_API_KEY else "google"

    if engine == "llm":
        if not cfg.GROQ_API_KEY:
            _log_error("TRANSLATION_ENGINE=llm but GROQ_API_KEY is empty.")
            print(GROQ_KEY_HELP)
            sys.exit(1)
        translator = LLMTranslator(cfg)
        try:
            sample = translator.translate("Bonjour, ceci est un test.")
        except LLMError as exc:
            _log_error(f"Groq self-test failed: {exc}")
            print(GROQ_KEY_HELP)
            sys.exit(1)
        _log_success(f"LLM translation ready (Groq / {cfg.GROQ_MODEL}).")
        _log_dim(f'   self-test: "Bonjour, ceci est un test." -> "{sample}"')
        return translator

    _log_info(
        "Translation engine: Google (free). Tip: add a free GROQ_API_KEY in "
        ".env for much more natural results."
    )
    return Translator(cfg)


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
    `.transcribe(audio)` method. For the Groq engine, do a quick self-test so a
    bad key / model name is caught early; on failure, fall back to local Whisper.
    """
    _warn_stt_prompt_language_mismatch()

    engine = (cfg.STT_ENGINE or "auto").lower()
    if engine == "auto":
        engine = "groq" if cfg.GROQ_API_KEY else "local"

    if engine == "groq":
        if not cfg.GROQ_API_KEY:
            _log_warn(
                "STT_ENGINE=groq but GROQ_API_KEY is empty — using local Whisper."
            )
            print(GROQ_KEY_HELP)
            return _build_local_transcriber()

        transcriber = GroqTranscriber(cfg, log=_log_info)
        # Self-test with a short silent clip so a bad key/model fails fast.
        try:
            silence = np.zeros(int(0.5 * cfg.SAMPLE_RATE), dtype=np.float32)
            transcriber.transcribe(silence)
        except GroqSTTError as exc:
            _log_error(f"Groq STT self-test failed: {exc}")
            _log_warn(
                "Falling back to the local Whisper model. Fix GROQ_API_KEY, or "
                "set STT_ENGINE=local to skip this check."
            )
            return _build_local_transcriber()
        _log_success(f"Speech-to-text ready (Groq cloud / {cfg.GROQ_STT_MODEL}).")
        return transcriber

    return _build_local_transcriber()


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
    """Print/refresh the yellow [g] swap line with the current language pair."""
    print(
        "\r\033[K"
        + Fore.YELLOW
        + Style.BRIGHT
        + _swap_lang_menu_line(pipeline, pending_new_pair=pending_new_pair)
        + Style.RESET_ALL
    )


def _print_menu(pipeline=None):
    """Print the configuration metadata (if pipeline provided) and the compact terminal menu in English."""
    # Match list command [l]: 3-char left margin for header + command grid.
    margin = 3
    pad = " " * margin

    # TUI: compact help into the scrollable log (no full-screen redraw).
    if ui.get_log_sink() is not None:
        sound = "ON" if pipeline and pipeline.is_sound_enabled() else "OFF"
        mic = "MUTED" if pipeline and pipeline.is_mic_muted() else "LIVE"
        ui.info(
            f"Languages: {cfg.SOURCE_LANG} -> {cfg.TARGET_LANG} | "
            f"TTS: {_tts_menu_label()} | Sound: {sound} | Mic: {mic}"
        )
        ui.dim(
            "Sentence: e/eN d/dN f/fN F l lo lt cls gt gf c | "
            "Audio: r/rN s n x a/aN p/pN | "
            "Idiom: g t o | Session: v m q"
        )
        if pipeline is not None:
            ui.success(
                f"Pair {pipeline.language_pair_label()} · "
                f"áudio {'ON' if pipeline.is_sound_enabled() else 'OFF [s]'}"
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
        from livelingo.llm import LLMTranslator

        if isinstance(pipeline.translator, LLMTranslator):
            ui.success(
                f"LLM translation ready (Groq / {cfg.GROQ_MODEL}).",
                indent=margin,
            )
            ui.dim(
                '   self-test: "Bonjour, ceci est un test." -> "Hello, this is a test."',
                indent=margin,
            )
        else:
            ui.info("Translation engine: Google (free).", indent=margin)

        # Speech-to-text Engine status
        from livelingo.groq_transcribe import GroqTranscriber

        if isinstance(pipeline.transcriber, GroqTranscriber):
            ui.success(
                f"Speech-to-text ready (Groq cloud / {cfg.GROQ_STT_MODEL}).",
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
            "[d]  Delete last",
            "[dN] Delete chunk N",
            "[f]  Favorite last",
            "[fN] Favorite N",
            "[F]  List favorites",
            "[l]  List messages",
            "[lo] List source only",
            "[lt] List target only",
            "[co] Comment last",
            "[coN] Comment N",
            "[codN] Del comment #N",
            "[cls] Clear log",
            "[gt] Go top",
            "[gf] Go footer",
            "[c]  Export .md",
        ],
    )
    _print_menu_group(
        "Audio",
        [
            "[r]  Replay last",
            "[rN] Replay chunk N",
            f"[s]  Sound ({sound_hint})",
            f"[n]  Mic ({mic_hint})",
            "[x]  Stop playback",
            "[a]  Copy audio path",
            "[aN] Copy path N",
            "[p]  Open audio folder",
            "[pN] Open folder N",
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
            "[v]  Switch session",
            "[m]  Show this menu",
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
                c for c in unicodedata.normalize("NFD", key) if not unicodedata.combining(c)
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
      g       -> Swap SOURCE ↔ TARGET languages (fast path mid-listen)
      t       -> Change TARGET language only (prompt EN/IT/ES/…)
      a / aN  -> Copy chunk audio file path to clipboard
      p / pN  -> Open Explorer on chunk audio file/folder
      e       -> Edit the last transcribed chunk
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


def _normalize_cmd(raw_cmd, cmd):
    """
    Normalize user input before dispatch.

    Double-tap of a single letter (e.g. 'gg', 'ss') is treated as one press —
    the listen-indicator often glues the first character onto the status line,
    so users press the key twice and would otherwise get Unknown command.
    """
    raw_cmd = (raw_cmd or "").strip()
    cmd = (cmd or raw_cmd).strip().lower()
    if len(cmd) >= 2 and len(set(cmd)) == 1 and cmd[0] in _SINGLE_LETTER_CMDS:
        letter = cmd[0]
        return (letter.upper() if raw_cmd.isupper() else letter), letter
    return raw_cmd, cmd


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
    elif cmd == "r":
        pipeline.replay_last()
        if indicator is not None:
            indicator.set_sound_on(pipeline.is_sound_enabled())
    elif cmd.startswith("r") and cmd[1:].isdigit():
        chunk_num = int(cmd[1:])
        pipeline.replay_chunk(chunk_num)
        if indicator is not None:
            indicator.set_sound_on(pipeline.is_sound_enabled())
    elif cmd == "e":
        last_heard = pipeline.get_last_heard()
        if not last_heard:
            ui.warn("No sentences in history to edit.")
            return
        has_readline = False
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
            if has_readline:
                new_text = input("Edit sentence: ").strip()
            else:
                print(f'Last sentence: "{last_heard}"')
                print("Enter correction (or Enter to cancel): ", end="", flush=True)
                new_text = sys.stdin.readline().strip()
        except (KeyboardInterrupt, EOFError):
            new_text = ""
        finally:
            if has_readline:
                readline.set_pre_input_hook(None)

        if new_text and new_text != last_heard:
            pipeline.chunk_queue.put(new_text)
            ui.info("New sentence queued for translation!")
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
        has_readline = False
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
            if has_readline:
                new_text = input(f"Edit sentence {chunk_num}: ").strip()
            else:
                print(f'Sentence of chunk {chunk_num}: "{last_heard}"')
                print("Enter correction (or Enter to cancel): ", end="", flush=True)
                new_text = sys.stdin.readline().strip()
        except (KeyboardInterrupt, EOFError):
            new_text = ""
        finally:
            if has_readline:
                readline.set_pre_input_hook(None)

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
        print("Are you sure you want to delete this sentence? (y/n): ", end="", flush=True)
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
        print(f"Are you sure you want to delete sentence {chunk_num}? (y/n): ", end="", flush=True)
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
        if enabled:
            ui.success(
                "Sound ON — próximas traduções tocam ao vivo. "
                "Use [r] / [rN] para ouvir chunks sem áudio (gera TTS se faltar).",
                indent=3,
            )
        else:
            ui.warn(
                "Sound OFF — só texto (TTS omitido se TTS_SKIP_WHEN_MUTED). "
                "Pressione [s] para ouvir de novo, ou [r]/[rN] para um chunk.",
                indent=3,
            )
        _print_menu(pipeline)
    elif cmd == "g":
        info = pipeline.request_language_swap()
        status = info.get("status")
        g_pad = "   "  # 3-char left margin (align with menu)
        if status == "deferred":
            print(
                "\r\033[K"
                + Fore.YELLOW
                + Style.BRIGHT
                + f"{g_pad}[g]  Swap agendado: {info['old_pair']}  ⇒  {info['new_pair']}   "
                f"(termina a frase/tradução em curso, depois inverte)"
                + Style.RESET_ALL
            )
            ui.info(
                "A frase atual NÃO será perdida — o idioma só muda após o processamento.",
                indent=3,
            )
            # Refresh menu line so it shows pending target pair.
            _print_swap_lang_menu_line(
                pipeline, pending_new_pair=info.get("new_pair")
            )
        elif status == "cancelled_pending":
            print(
                "\r\033[K"
                + Fore.YELLOW
                + Style.BRIGHT
                + f"{g_pad}[g]  Swap pendente cancelado — permanece {info['old_pair']}"
                + Style.RESET_ALL
            )
            _print_swap_lang_menu_line(pipeline)
        else:
            # Applied immediately (pipeline idle) — full menu refresh with new pair.
            src = info.get("source") or cfg.SOURCE_LANG
            tgt = info.get("target") or cfg.TARGET_LANG
            voice = info.get("voice") or cfg.TTS_VOICE
            print(
                "\r\033[K"
                + Fore.YELLOW
                + Style.BRIGHT
                + f"{g_pad}[g]  Idiomas: {info['old_pair']}  ⇒  {info['new_pair']}   "
                f"(STT={src} · TTS voice={voice})"
                + Style.RESET_ALL
            )
            for w in info.get("warnings") or []:
                ui.warn(w, indent=3)
            _warn_stt_prompt_language_mismatch()
            ui.info(
                f"Fale {str(src).upper()} agora — os outros ouvem {str(tgt).upper()}. "
                f"Histórico antigo não é re-traduzido.",
                indent=3,
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
                f"(SOURCE={src} · TTS={voice})"
                + Style.RESET_ALL
            )
            ui.info(
                f"Próximas traduções: fale {src} → ouvem {tgt}.",
                indent=3,
            )
        _print_menu(pipeline)
    elif cmd == "n":
        muted, os_ok, mic_name = pipeline.toggle_mic()
        if indicator is not None:
            indicator.set_mic_muted(muted)
            if not muted:
                indicator.start()  # resume icons after mute (or first LIVE)
        if muted:
            if os_ok:
                ui.warn(
                    f"Mic MUTED (Windows): '{mic_name}'. "
                    f"Escuta/ícones pausados — tela livre para leitura. "
                    f"Pressione [n] de novo para desmutar."
                )
            else:
                ui.warn(
                    f"Mic MUTED (app only — OS mute falhou): '{mic_name}'. "
                    f"Escuta/ícones pausados. Pressione [n] de novo para reativar."
                )
            ui.dim("  (modo leitura: sem animação de escuta até o mic LIVE)")
        else:
            if os_ok:
                ui.success(
                    f"Mic LIVE (Windows): '{mic_name}'. "
                    f"Escuta ativa retomada. Pode falar."
                )
            else:
                ui.success(
                    f"Mic LIVE (app gate): '{mic_name}'. "
                    f"Escuta ativa retomada. Confira o mute no tray se não ouvir."
                )
        _print_menu(pipeline)
    elif cmd == "x":
        if pipeline.stop_playback():
            ui.info("Playback stopped — remaining audio for this chunk skipped.")
        else:
            ui.warn("Sound is OFF — nothing playing to stop.")
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
            transcript_full = "\n".join(
                f"- {_entry_heard(e)}" for e in full_trans
            )
            try:
                summary_text = summary_generator.generate_meeting_summary(transcript_full)
            except Exception as exc:
                ui.error(f"Could not generate AI summary: {exc}")
        else:
            ui.warn("Note: AI summary disabled (requires GROQ_API_KEY to be set in .env).")

        synonyms = pipeline.get_synonyms()

        try:
            with open(filename, "w", encoding="utf-8") as f:
                f.write(f"# {title}\n\n")

                if summary_text:
                    f.write(f"{summary_text}\n\n")
                    f.write("---\n\n")  # horizontal rule before content

                f.write("## 💬 Transcrição Detalhada\n\n")
                for entry in full_trans:
                    chunk_num, heard, translated, _created_at, _timing = (
                        _unpack_transcript_entry(entry)
                    )
                    f.write(f"### Chunk {chunk_num}\n")
                    f.write(f"{tgt_lang}: {translated}\n")
                    f.write("\n")
                    f.write(f"{src_lang}: {heard}\n")
                    f.write("\n")

                # Export synonym vocab searches chronologically
                if synonyms:
                    f.write("## 📚 Vocabulário e Sinônimos Consultados\n\n")
                    for word, explanation in synonyms:
                        f.write(f"### {word.upper()}\n")
                        f.write(f"{explanation}\n\n")

                word_count = _count_content_words(
                    _entry_heard(e) for e in full_trans
                )
                f.write("---\n")
                f.write(f"**Total de frases traduzidas:** {len(full_trans)}\n")
                f.write(f"**Total de sinônimos consultados:** {len(synonyms)}\n")
                f.write(
                    f"**Total de palavras** (fonte; >1 sílaba; "
                    f"sem e/a/ou/para/ao/à): {word_count}\n"
                )
            ui.success(f"File generated and exported successfully: '{filename}'")
        except Exception as exc:
            ui.error(f"Error saving share file: {exc}")
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
        ui.info(f"Historico da sessao — {len(full_trans)} frase(s):")

        margin = 3
        pad = " " * margin
        in_tui = ui.get_log_sink() is not None
        # Full panel/terminal width (TUI provider → live RichLog cols; not hardcoded 72/80)
        content_w = ui.content_width(margin=margin)

        lang_map = {
            "fr": "Frances",
            "en": "Ingles",
            "pt": "Portugues",
            "es": "Espanhol",
            "de": "Alemao",
            "it": "Italiano",
            "zh": "Chines",
            "ja": "Japones",
        }
        src_lang = lang_map.get(cfg.SOURCE_LANG.lower(), cfg.SOURCE_LANG.upper())
        tgt_lang = lang_map.get(cfg.TARGET_LANG.lower(), cfg.TARGET_LANG.upper())

        def _wrap_body(label_plain, body, width):
            """Wrap body so first line fits after label; later lines align under body."""
            body = " ".join((body or "").split())
            if not body:
                return [""]
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

        rule = "=" * content_w

        # --- TUI: hang-indent + classic colors (yellow chunk / blue target / green source) ---
        if in_tui:
            e = ui._rich_escape
            ui.rich(f"{pad}[bold cyan]{e(rule)}[/]")
            ui.rich(
                f"{pad}[bold cyan]CURRENT SESSION HISTORY (Chronological)[/]"
            )
            ui.rich(f"{pad}[bold cyan]{e(rule)}[/]")
            for entry in full_trans:
                chunk_num, heard, translated, created_at, timing = (
                    _unpack_transcript_entry(entry)
                )
                prefix = f"[Chunk {chunk_num}] "
                label_tgt = f"{prefix}{tgt_lang}: "
                label_src = f"{' ' * len(prefix)}{src_lang}: "
                indent_tgt = " " * len(label_tgt)
                indent_src = " " * len(label_src)

                for i, line in enumerate(
                    _wrap_body(label_tgt, translated, content_w)
                ):
                    if i == 0:
                        # Classic: yellow [Chunk N] + blue LANG: + white/bright body
                        ui.rich(
                            f"{pad}[bold yellow]{e(prefix)}[/]"
                            f"[bold blue]{e(tgt_lang)}: [/]"
                            f"[bold white]{e(line)}[/]"
                        )
                    else:
                        ui.rich(f"{pad}{indent_tgt}[bold white]{e(line)}[/]")

                for i, line in enumerate(_wrap_body(label_src, heard, content_w)):
                    if i == 0:
                        # Classic: white label + green source text
                        ui.rich(
                            f"{pad}[white]{e(label_src)}[/]"
                            f"[green]{e(line)}[/]"
                        )
                    else:
                        ui.rich(f"{pad}{indent_src}[green]{e(line)}[/]")

                # Blank after language texts (after translated/source block) before meta
                ui.raw("")

                timing_line = ui.format_timing_line(
                    timing,
                    at=created_at or None,
                    include_clock=bool(created_at),
                )
                recorded = ui.format_recorded_stamp(created_at) if created_at else ""
                audio_raw = audio_map.get(chunk_num, "")
                if not audio_raw:
                    cand = os.path.join(
                        pipeline.cache_dir, f"chunk_{chunk_num}.wav"
                    )
                    if os.path.isfile(cand):
                        audio_raw = cand
                meta_indent = " " * len(prefix)
                if timing_line:
                    ui.dim(f"{pad}{meta_indent}{timing_line}")
                if recorded:
                    ui.dim(f"{pad}{meta_indent}gravado: {recorded}")
                for al in ui.format_audio_lines(audio_raw):
                    ui.dim(f"{pad}{meta_indent}{al}")
                # Free-text comments (co / coN) with PK + date+time
                for item in comments_map.get(int(chunk_num), []) or []:
                    if len(item) >= 3:
                        c_id, c_text, c_at = item[0], item[1], item[2]
                    else:
                        # legacy RAM shape without id
                        c_id, c_text, c_at = "?", item[0], item[1] if len(item) > 1 else ""
                    stamp = ui.format_recorded_stamp(c_at) or (c_at or "")
                    body = " ".join((c_text or "").split())
                    ui.rich(
                        f"{pad}{meta_indent}[magenta]comment #{e(str(c_id))}:[/] "
                        f"[dim]{e(stamp)}[/]  {e(body)}"
                    )
                ui.raw("")  # blank between chunks
            ui.rich(f"{pad}[bold cyan]{e(rule)}[/]")
            ui.rich(
                f"{pad}[bold cyan]Total: {len(full_trans)} frase(s)[/]"
            )
            ui.rich(f"{pad}[bold cyan]{e(rule)}[/]")
            return

        # --- Classic terminal: same wrap logic, ANSI colors via print ---
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
            "CURRENT SESSION HISTORY (Chronological)",
            Fore.CYAN + Style.BRIGHT,
        )
        print(pad + Fore.CYAN + rule + Style.RESET_ALL)
        print(pad)

        for entry in full_trans:
            chunk_num, heard, translated, created_at, timing = (
                _unpack_transcript_entry(entry)
            )
            prefix = f"[Chunk {chunk_num}] "
            label_tgt = f"{prefix}{tgt_lang}: "
            label_src = f"{' ' * len(prefix)}{src_lang}: "
            indent_tgt = " " * len(label_tgt)
            indent_src = " " * len(label_src)

            for i, line in enumerate(
                _wrap_body(label_tgt, translated, content_w)
            ):
                if i == 0:
                    print(
                        pad
                        + Fore.YELLOW
                        + Style.BRIGHT
                        + prefix
                        + Fore.BLUE
                        + Style.BRIGHT
                        + f"{tgt_lang}: "
                        + Style.RESET_ALL
                        + Fore.WHITE
                        + line
                        + Style.RESET_ALL
                    )
                else:
                    print(pad + indent_tgt + Fore.WHITE + line + Style.RESET_ALL)

            for i, line in enumerate(_wrap_body(label_src, heard, content_w)):
                if i == 0:
                    print(
                        pad
                        + Fore.WHITE
                        + label_src
                        + Fore.GREEN
                        + line
                        + Style.RESET_ALL
                    )
                else:
                    print(pad + indent_src + Fore.GREEN + line + Style.RESET_ALL)

            timing_line = ui.format_timing_line(
                timing,
                at=created_at or None,
                include_clock=bool(created_at),
            )
            recorded = ui.format_recorded_stamp(created_at) if created_at else ""
            audio_raw = audio_map.get(chunk_num, "")
            if not audio_raw:
                cand = os.path.join(
                    pipeline.cache_dir, f"chunk_{chunk_num}.wav"
                )
                if os.path.isfile(cand):
                    audio_raw = cand
            audio_lines = ui.format_audio_lines(audio_raw)
            comments = comments_map.get(int(chunk_num), []) or []
            if timing_line or recorded or audio_lines or comments:
                print(pad)
                meta_indent = " " * len(prefix)
                if timing_line:
                    _print_plain(meta_indent + timing_line, Style.DIM)
                if recorded:
                    _print_plain(
                        meta_indent + f"gravado: {recorded}",
                        Style.DIM,
                    )
                for al in audio_lines:
                    _print_plain(meta_indent + al, Style.DIM)
                for item in comments:
                    if len(item) >= 3:
                        c_id, c_text, c_at = item[0], item[1], item[2]
                    else:
                        c_id, c_text, c_at = "?", item[0], item[1] if len(item) > 1 else ""
                    stamp = ui.format_recorded_stamp(c_at) or (c_at or "")
                    body = " ".join((c_text or "").split())
                    print(
                        pad
                        + meta_indent
                        + Fore.MAGENTA
                        + f"comment #{c_id}: "
                        + Style.DIM
                        + f"{stamp}  "
                        + Style.RESET_ALL
                        + body
                    )
            print(pad)

        print(pad + Fore.CYAN + rule + Style.RESET_ALL)
        _print_plain(
            f"Total translated sentences: {len(full_trans)}",
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
            ui.warn(
                f"Chunk {chunk_num} não encontrado. Use [l] para ver os números."
            )
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
    elif cmd == "cls":
        # Clear TUI log panel (or classic terminal screen)
        cleared = False
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
    elif cmd == "gt":
        # Go top — scroll log to start (TUI). Silent on success (no log noise).
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
            ui.warn("gt só funciona no modo TUI.", indent=3)
    elif cmd == "gf":
        # Go footer — scroll log to end (TUI). Silent on success.
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
            ui.warn("gf só funciona no modo TUI.", indent=3)
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
        print("Are you sure you want to switch or restart the session? (y/n): ", end="", flush=True)
        confirm = sys.stdin.readline().strip().lower()
        if confirm in ("y", "yes", "s", "sim"):
            pipeline.switch_session = True
            pipeline.stop()
            return
        else:
            ui.info("Operation canceled.")
    elif cmd == "m":
        _print_menu(pipeline)
    elif cmd in ("q", "quit"):
        ui.info("Stopping application...")
        pipeline.stop()
        return
    else:
        ui.warn(
            f"Unknown command: '{cmd}'. Use: "
            f"r/rN, e/eN, d/dN, f/fN, F, s, g (swap), t (TARGET), "
            f"a/aN (copy audio path), p/pN (open audio folder), "
            f"n (mic), x, o, c, l, lo, lt, co/coN, codN, cls, "
            f"gt (top), gf (footer), v, m, q.",
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

    Ignores known flags (--verbose, -h, --help). Returns str or None.
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

    skip = {"--verbose", "-v", "-h", "--help", "--classic", "--tui"}
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
            ui.info("Last sessions found:", indent=m)
            for idx, (sid, title, created_at) in enumerate(sessions, 1):
                _p(f"[{idx}] {title} (ID: {sid}, Created at: {created_at})")
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
        "Enter title/subject for the new session "
        "(Enter=automatic, 0=back to menu): "
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
        unicodedata.normalize("NFKD", title)
        .encode("ascii", "ignore")
        .decode("ascii")
    )
    slug = re.sub(r"[^\w\s-]", "", normalized.lower())
    slug = re.sub(r"[-\s]+", "-", slug).strip("-_")
    session_id = f"{timestamp}_{slug}"

    db.create_session(session_id, title)
    ui.success(f"New session created: '{title}' (ID: {session_id})", indent=m)
    return session_id, title


def _ensure_wrapper_scripts():
    """Ensure that livelingo.sh and livelingo.bat wrapper scripts exist in the project directory."""
    import os

    sh_path = "livelingo.sh"
    bat_path = "livelingo.bat"

    # 1. Write livelingo.sh if not exists
    if not os.path.exists(sh_path):
        try:
            with open(sh_path, "w", newline="\n", encoding="utf-8") as f:
                f.write("#!/bin/bash\n\n")
                f.write("# ======================================================================= #\n")
                f.write("# LiveLingo Global Execution Script (Linux/WSL/macOS)\n")
                f.write("# ======================================================================= #\n\n")
                f.write('PROJECT_DIR="/mnt/c/Users/rcopr/LiveLingo/LiveLingo"\n\n')
                f.write('cd "$PROJECT_DIR" || {\n')
                f.write('    echo -e "\\033[1;31m[x] Error: Project directory not found ($PROJECT_DIR).\\033[0m"\n')
                f.write('    exit 1\n')
                f.write('}\n\n')
                f.write('python3 main.py "$@"\n')

            # Make it executable
            os.chmod(sh_path, 0o755)
        except Exception:
            pass

    # 2. Write livelingo.bat if not exists
    if not os.path.exists(bat_path):
        try:
            with open(bat_path, "w", newline="\r\n", encoding="utf-8") as f:
                f.write("@echo off\n")
                f.write(":: =======================================================================\n")
                f.write(":: LiveLingo Global Execution Script (Windows)\n")
                f.write(":: =======================================================================\n\n")
                f.write('cd /d "C:\\Users\\rcopr\\LiveLingo\\LiveLingo"\n\n')
                f.write("python main.py %*\n")
        except Exception:
            pass


def main():
    # --- Ensure wrapper scripts are generated locally ---
    _ensure_wrapper_scripts()

    # --- Enable verbose debug logs if --verbose flag is passed ---
    cfg.VERBOSE = "--verbose" in sys.argv

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
        if cfg.MONITOR_PLAYBACK:
            if cfg.MONITOR_DEVICE:
                try:
                    monitor_idx, _ = devices.resolve_device(cfg.MONITOR_DEVICE, "output")
                except ValueError as exc:
                    _log_warn(f"Monitor device problem ({exc}); using default output.")
                    monitor_idx = devices.default_output_index()
            else:
                monitor_idx = devices.default_output_index()
            _log_info(f"Monitor playback ON -> {devices.device_name(monitor_idx)}")

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
            ui.success(
                f"[g] Swap aplicado: {pair}   (STT={src} · TTS={voice})",
                indent=3,
            )
            ui.info(
                f"Fale {str(src).upper()} agora — os outros ouvem {str(tgt).upper()}.",
                indent=3,
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
        pipeline.start()

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
                pipeline.stop()
                pipeline.join(timeout=5.0)
                if cmd_thread is not None:
                    cmd_thread.join(timeout=5.0)
            session_switch = bool(getattr(pipeline, "switch_session", False))
        else:
            # TUI exited — clean pipeline
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
