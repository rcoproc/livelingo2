"""
config.py
=========
Central, tunable configuration for the real-time voice translation tool.

Every setting has a sensible default below and can be overridden without
touching this file by either:
  * setting an environment variable, or
  * creating a `.env` file (copy `.env.example` to `.env` and edit it).

Device settings (INPUT_DEVICE / OUTPUT_DEVICE) accept either:
  * an integer index   (e.g.  3)        -> as shown by `python list_devices.py`
  * a name substring   (e.g. "CABLE Input")  -> case-insensitive match
  * empty / unset      -> system default (for the input mic only)
"""

import os

from dotenv import load_dotenv

# Load variables from a local .env file if it exists (does nothing otherwise).
load_dotenv()


# --------------------------------------------------------------------------- #
# Small typed helpers so .env strings become the right Python types.
# --------------------------------------------------------------------------- #
def _get_str(name, default):
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return value.strip()


def _get_int(name, default):
    value = os.getenv(name)
    try:
        return int(value) if value not in (None, "") else default
    except ValueError:
        return default


def _get_float(name, default):
    value = os.getenv(name)
    try:
        return float(value) if value not in (None, "") else default
    except ValueError:
        return default


def _get_bool(name, default):
    value = os.getenv(name)
    if value in (None, ""):
        return default
    return value.strip().lower() in ("1", "true", "yes", "on", "y")


# --------------------------------------------------------------------------- #
# Languages
# --------------------------------------------------------------------------- #
# Source language you speak, target language you want others to hear.
SOURCE_LANG = _get_str("SOURCE_LANG", "fr")   # French
TARGET_LANG = _get_str("TARGET_LANG", "en")   # English


# --------------------------------------------------------------------------- #
# Speech-to-text (faster-whisper, runs locally)
# --------------------------------------------------------------------------- #
# Model size: "tiny", "base", "small", "medium", "large-v3".
# "small" is a good speed/accuracy trade-off on CPU; "medium" is more accurate
# but noticeably slower without a GPU.
WHISPER_MODEL = _get_str("WHISPER_MODEL", "small")

# "cpu" for everyone; "cuda" only if you have an NVIDIA GPU + CUDA libraries.
WHISPER_DEVICE = _get_str("WHISPER_DEVICE", "cpu")

# Compute type. CPU: "int8" (fast) or "int8_float32". GPU: "float16".
WHISPER_COMPUTE_TYPE = _get_str("WHISPER_COMPUTE_TYPE", "int8")

# Beam size for decoding. 1 = fastest/greedy, 5 = a bit slower but more robust.
WHISPER_BEAM_SIZE = _get_int("WHISPER_BEAM_SIZE", 5)

# Let Whisper run its own internal VAD to drop silence inside a chunk. This
# strongly reduces "hallucinated" phrases on near-silent audio.
WHISPER_VAD_FILTER = _get_bool("WHISPER_VAD_FILTER", True)

# CPU threads used for transcription. 0 = auto (CTranslate2 picks physical
# cores). If STT feels slow, try setting this to your physical core count.
WHISPER_CPU_THREADS = _get_int("WHISPER_CPU_THREADS", 0)


# --------------------------------------------------------------------------- #
# Translation engine
# --------------------------------------------------------------------------- #
# "auto"   -> use the LLM if GROQ_API_KEY is set, otherwise Google Translate
# "llm"    -> always use the Groq LLM (better quality; needs GROQ_API_KEY)
# "google" -> always use deep-translator / Google (free, no key)
TRANSLATION_ENGINE = _get_str("TRANSLATION_ENGINE", "auto")

# Free Groq API key (no credit card): https://console.groq.com/keys
GROQ_API_KEY = _get_str("GROQ_API_KEY", "")

# Groq model. "llama-3.3-70b-versatile" = best quality and still very fast.
# "llama-3.1-8b-instant" = even faster, slightly lower quality.
GROQ_MODEL = _get_str("GROQ_MODEL", "llama-3.3-70b-versatile")

# Seconds to wait for the LLM response before giving up on a chunk.
LLM_TIMEOUT = _get_float("LLM_TIMEOUT", 15.0)


# --------------------------------------------------------------------------- #
# Text-to-speech (edge-tts, needs internet)
# --------------------------------------------------------------------------- #
# Run `edge-tts --list-voices` to see every available voice.
# A few English voices:
#   en-US-AriaNeural   (female, US)   en-US-GuyNeural (male, US)
#   en-GB-SoniaNeural  (female, UK)   en-US-JennyNeural (female, US)
TTS_VOICE = _get_str("TTS_VOICE", "en-US-AriaNeural")
TTS_RATE = _get_str("TTS_RATE", "+0%")      # e.g. "+10%" faster, "-10%" slower
TTS_VOLUME = _get_str("TTS_VOLUME", "+0%")  # e.g. "+20%" louder


# --------------------------------------------------------------------------- #
# Audio devices
# --------------------------------------------------------------------------- #
# Microphone. Leave empty to use the Windows default recording device.
INPUT_DEVICE = _get_str("INPUT_DEVICE", "")

# Where the translated speech is sent. To feed Teams/Zoom/etc. this MUST be the
# VB-Cable *playback* side, normally named "CABLE Input (VB-Audio Virtual Cable)".
# Apps then select "CABLE Output" as their microphone.
OUTPUT_DEVICE = _get_str("OUTPUT_DEVICE", "CABLE Input")

# Set to True to ALSO hear the translation through your own speakers/headphones
# while it is sent to VB-Cable (helpful for testing, or to monitor yourself
# during a call).
MONITOR_PLAYBACK = _get_bool("MONITOR_PLAYBACK", False)

# Which device the monitor copy plays on (index or name substring). Leave empty
# to use the system default output. Only used when MONITOR_PLAYBACK is True.
MONITOR_DEVICE = _get_str("MONITOR_DEVICE", "")


# --------------------------------------------------------------------------- #
# Audio capture / chunking
# --------------------------------------------------------------------------- #
# Whisper expects 16 kHz mono — do not change these two.
SAMPLE_RATE = 16000
CHANNELS = 1

# Voice Activity Detection (VAD). When True the recorder waits until you stop
# speaking before sending a chunk (more natural sentences). When False it cuts
# fixed-length chunks every CHUNK_DURATION seconds.
VAD_ENABLED = _get_bool("VAD_ENABLED", True)

# Fixed-chunk length (VAD disabled) AND the soft target in VAD mode.
CHUNK_DURATION = _get_float("CHUNK_DURATION", 4.0)

# Hard upper bound: even if you never pause, a chunk is emitted after this long.
MAX_CHUNK_DURATION = _get_float("MAX_CHUNK_DURATION", 10.0)

# RMS energy below which a 30 ms block counts as silence. If short utterances
# get cut off, lower this; if background noise triggers chunks, raise it.
# Typical mic values: silence ~0.002-0.01, speech ~0.02-0.15.
SILENCE_THRESHOLD = _get_float("SILENCE_THRESHOLD", 0.015)

# How long the audio must stay quiet to mark the end of an utterance.
SILENCE_DURATION = _get_float("SILENCE_DURATION", 0.7)

# Utterances shorter than this (after trimming) are ignored as noise/clicks.
MIN_SPEECH_DURATION = _get_float("MIN_SPEECH_DURATION", 0.4)

# Analysis block size used for VAD (seconds). 30 ms is standard.
BLOCK_DURATION = 0.03

# Keep this much audio *before* speech onset so the first syllable isn't clipped.
PREROLL_DURATION = 0.25
