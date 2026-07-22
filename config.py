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
import platform

from dotenv import load_dotenv

_IS_WINDOWS = platform.system() == "Windows"

# Load variables from a local .env file if it exists (does nothing otherwise).
load_dotenv()


# --------------------------------------------------------------------------- #
# Small typed helpers so .env strings become the right Python types.
# --------------------------------------------------------------------------- #
def _strip_env_comment(value: str) -> str:
    """Strip inline # comments from .env values (e.g. MONITOR_DEVICE=13 # fone)."""
    if value is None:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    if "#" in text:
        text = text.split("#", 1)[0].strip()
    return text


def _get_str(name, default):
    value = os.getenv(name)
    if value is None:
        return default
    text = _strip_env_comment(value)
    if text == "":
        return default
    return text


def _get_int(name, default):
    value = os.getenv(name)
    text = _strip_env_comment(value) if value not in (None, "") else ""
    try:
        return int(text) if text != "" else default
    except ValueError:
        return default


def _get_float(name, default):
    value = os.getenv(name)
    text = _strip_env_comment(value) if value not in (None, "") else ""
    try:
        return float(text) if text != "" else default
    except ValueError:
        return default


def _get_bool(name, default):
    value = os.getenv(name)
    if value in (None, ""):
        return default
    text = _strip_env_comment(value).lower()
    return text in ("1", "true", "yes", "on", "y")


# --------------------------------------------------------------------------- #
# Languages
# --------------------------------------------------------------------------- #
# Source language you speak, target language you want others to hear.
SOURCE_LANG = _get_str("SOURCE_LANG", "fr")  # French
TARGET_LANG = _get_str("TARGET_LANG", "en")  # English


# --------------------------------------------------------------------------- #
# Speech-to-text engine selection
# --------------------------------------------------------------------------- #
# Which engine turns your speech into text:
#   "auto"  -> use Groq's hosted Whisper if GROQ_API_KEY is set, else run locally
#   "groq"  -> always use Groq cloud Whisper (best accuracy, needs key + internet)
#   "local" -> always run faster-whisper on this machine (offline, uses the CPU)
#
# Groq runs whisper-large-v3, which is dramatically more accurate than the small
# local model below — recommended when you have a key and internet, especially
# on a modest CPU. It also offloads the work from your machine.
STT_ENGINE = _get_str("STT_ENGINE", "auto")

# Groq speech-to-text model (used when STT_ENGINE resolves to "groq"):
#   "whisper-large-v3"        -> best accuracy (recommended)
#   "whisper-large-v3-turbo"  -> faster, almost as accurate
#   "distil-whisper-large-v3-en" -> fastest, ENGLISH ONLY
GROQ_STT_MODEL = _get_str("GROQ_STT_MODEL", "whisper-large-v3")

# Seconds to wait for a Groq transcription before giving up on a chunk.
GROQ_STT_TIMEOUT = _get_float("GROQ_STT_TIMEOUT", 20.0)

# Optional text that biases recognition toward expected words, names, acronyms,
# and correct spelling/accents. Used by BOTH the Groq and local engines and can
# noticeably reduce wrong-word errors on domain vocabulary.
# CRITICAL: write this prompt in the SAME language as SOURCE_LANG (or leave empty).
# A Portuguese prompt with SOURCE_LANG=en makes Whisper emit Portuguese text even
# when the speaker used English — then translation becomes "portunhol".
# Example (SOURCE_LANG=fr):
#   STT_INITIAL_PROMPT=Réunion sur LiveLingo, VB-Cable, Whisper, Groq, Teams.
STT_INITIAL_PROMPT = _get_str("STT_INITIAL_PROMPT", "")

# Drop common silence hallucinations (e.g. "Legenda por …", "Thanks for watching").
STT_HALLUCINATION_FILTER = _get_bool("STT_HALLUCINATION_FILTER", True)
# Heuristic: very quiet + short chunk + few words → discard.
STT_MIN_RMS = _get_float("STT_MIN_RMS", 0.010)
STT_LOW_ENERGY_MAX_WORDS = _get_int("STT_LOW_ENERGY_MAX_WORDS", 6)
STT_LOW_ENERGY_MAX_SEC = _get_float("STT_LOW_ENERGY_MAX_SEC", 2.5)
# Do not enqueue capture chunks shorter than this (seconds) when RMS is very low.
CAPTURE_TAIL_MAX_SEC = _get_float("CAPTURE_TAIL_MAX_SEC", 2.0)


# --------------------------------------------------------------------------- #
# Speech-to-text (faster-whisper, runs locally)
# --------------------------------------------------------------------------- #
# Model size: "tiny", "base", "small", "medium", "large-v3", "large-v3-turbo".
# "small" is a good speed/accuracy trade-off on CPU; "medium" and especially
# "large-v3-turbo" are much more accurate but heavier without a GPU.
WHISPER_MODEL = _get_str("WHISPER_MODEL", "small")

# "cpu" for everyone; "cuda" only if you have an NVIDIA GPU + CUDA libraries.
WHISPER_DEVICE = _get_str("WHISPER_DEVICE", "cpu")

# Compute type. CPU: "int8" (fast) or "int8_float32". GPU: "float16".
WHISPER_COMPUTE_TYPE = _get_str("WHISPER_COMPUTE_TYPE", "int8")

# Beam size for decoding. 1 = fastest/greedy, 5 = a bit slower but more robust.
WHISPER_BEAM_SIZE = _get_int("WHISPER_BEAM_SIZE", 1)

# Let Whisper run its own internal VAD to drop silence inside a chunk. This
# strongly reduces "hallucinated" phrases on near-silent audio.
WHISPER_VAD_FILTER = _get_bool("WHISPER_VAD_FILTER", True)

# CPU threads used for transcription. 0 = auto (CTranslate2 picks physical
# cores). If STT feels slow, try setting this to your physical core count.
WHISPER_CPU_THREADS = _get_int("WHISPER_CPU_THREADS", 2)


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
# Provider failover / HA (runtime — no app exit on transient cloud failures)
# --------------------------------------------------------------------------- #
# STT: when primary (Groq) fails mid-session → local faster-whisper (or none).
STT_FALLBACK = _get_str("STT_FALLBACK", "local").lower()
# Translation: when primary (Groq LLM) fails → Google deep-translator (or none).
TRANSLATION_FALLBACK = _get_str("TRANSLATION_FALLBACK", "google").lower()
# Extra attempts on the *primary* only for transient errors (timeout/429/5xx).
FAILOVER_MAX_RETRIES = _get_int("FAILOVER_MAX_RETRIES", 1)
FAILOVER_RETRY_SLEEP_S = _get_float("FAILOVER_RETRY_SLEEP_S", 0.35)
# Circuit breaker: after N failures, skip primary for COOLDOWN seconds.
CIRCUIT_FAIL_THRESHOLD = _get_int("CIRCUIT_FAIL_THRESHOLD", 3)
CIRCUIT_COOLDOWN_S = _get_float("CIRCUIT_COOLDOWN_S", 60.0)
# Max seconds the processor may wait for local Whisper warm-up on STT fallback.
STT_FALLBACK_WAIT_S = _get_float("STT_FALLBACK_WAIT_S", 8.0)
# Pre-load local Whisper in a daemon thread when Groq is primary (no UI block).
STT_WARMUP_LOCAL = _get_bool("STT_WARMUP_LOCAL", True)
# Log failover / circuit events to the System panel (rate-limited).
FAILOVER_LOG = _get_bool("FAILOVER_LOG", True)

# Max estimated tokens of transcript per Groq request when generating the
# share/export AI summary (`c`). Free-tier llama-3.1-8b-instant is often ~6k TPM;
# default 4000 leaves room for system prompt + output. 0 = use built-in default.
SUMMARY_MAX_INPUT_TOKENS = _get_int("SUMMARY_MAX_INPUT_TOKENS", 4000)

# --------------------------------------------------------------------------- #
# Phrase translation cache (TM) — exact full-sentence memory for latency tests
# --------------------------------------------------------------------------- #
# When true, lookup (SOURCE_LANG, TARGET_LANG, normalized heard) before calling
# Google/LLM. HIT skips the network; MISS translates then stores.
# Toggle at runtime: [pc on] / [pc off]. Force next live translate: [pc force].
PHRASE_CACHE = _get_bool("PHRASE_CACHE", False)
# Max entries in the in-process LRU dict (SQLite keeps full history).
PHRASE_CACHE_SIZE = _get_int("PHRASE_CACHE_SIZE", 10000)
# On startup, load frequent pairs from chunks + translation_pairs tables.
PHRASE_CACHE_WARMUP = _get_bool("PHRASE_CACHE_WARMUP", True)
# When true, log every HIT/MISS to the Sistema panel (always for HIT when on).
PHRASE_CACHE_LOG = _get_bool("PHRASE_CACHE_LOG", True)
# LiveCaptions store: also write the inverted pair (e.g. LC EN→PT also stores
# PT→EN with the same texts swapped). Grows the opposite-direction TM so voice
# in Portuguese can HIT phrases learned from English captions.
PHRASE_CACHE_LC_ALSO_REVERSE = _get_bool("PHRASE_CACHE_LC_ALSO_REVERSE", True)

# Synonym command [o]: wordnet (offline WordNet + Moby, default) | llm (Groq) | auto
SYNONYMS_ENGINE = _get_str("SYNONYMS_ENGINE", "wordnet").lower()
# Translate WordNet definitions/examples to Portuguese via Google (needs internet).
SYNONYMS_PT_TRANSLATE = _get_bool("SYNONYMS_PT_TRANSLATE", True)


# --------------------------------------------------------------------------- #
# Text-to-speech (edge-tts, needs internet)
# --------------------------------------------------------------------------- #
# Run `edge-tts --list-voices` to see every available voice.
# Voice locale prefix MUST match TARGET_LANG (fr-FR-*, es-ES-*, en-US-*, …).
# A Spanish voice can read French text but keeps a Spanish accent.
# Examples by target:
#   en: en-US-AriaNeural / en-US-GuyNeural / en-GB-SoniaNeural
#   fr: fr-FR-DeniseNeural / fr-FR-HenriNeural / fr-FR-EloiseNeural
#   es: es-ES-ElviraNeural / es-ES-AlvaroNeural / es-MX-DaliaNeural
#   pt: pt-BR-FranciscaNeural / pt-BR-AntonioNeural
# TTS engine: edge | piper | hybrid (edge first chunk + piper tail/cache).
TTS_ENGINE = _get_str("TTS_ENGINE", "edge")

# When TTS_ENGINE=piper, use edge-tts for the first live chunk (Windows default).
TTS_HYBRID = _get_bool("TTS_HYBRID", _IS_WINDOWS)

TTS_VOICE = _get_str("TTS_VOICE", "en-US-AriaNeural")
# Voice for the *other* language in the pair — used when swapping with [g].
# After swap, TTS_VOICE ↔ TTS_VOICE_ALT. Empty = pick a default elegant Edge voice
# for SOURCE_LANG (the language that becomes the new target).
TTS_VOICE_ALT = _get_str("TTS_VOICE_ALT", "")
TTS_RATE = _get_str("TTS_RATE", "+0%")  # e.g. "+10%" faster, "-10%" slower
TTS_VOLUME = _get_str("TTS_VOLUME", "+0%")  # e.g. "+20%" louder

# Piper (local TTS) — used when TTS_ENGINE=piper
# Voice list: https://huggingface.co/rhasspy/piper-voices
# Empty PIPER_VOICE -> auto-pick from TARGET_LANG (e.g. en -> en_US-lessac-medium)
PIPER_VOICE = _get_str("PIPER_VOICE", "")
PIPER_MODEL_DIR = _get_str("PIPER_MODEL_DIR", ".cache/models/piper")
# Speech speed: 1.0 = normal, 0.85 = faster, 1.15 = slower
PIPER_LENGTH_SCALE = _get_float("PIPER_LENGTH_SCALE", 1.0)

# Stream Piper ONNX chunks to playback (~80ms buffers). Much lower latency than
# waiting for the full utterance. Enabled by default for TTS_ENGINE=piper.
PIPER_CHUNK_STREAMING = _get_bool("PIPER_CHUNK_STREAMING", True)

# Min audio buffer (ms) before first Piper playback chunk is sent.
PIPER_PLAYBACK_BUFFER_MS = _get_int("PIPER_PLAYBACK_BUFFER_MS", 40)

# Only split long text into sentences when above this length (chars).
# Short utterances use a single ONNX run (faster). Values above 120 are capped.
PIPER_SEGMENT_MIN_CHARS = _get_int("PIPER_SEGMENT_MIN_CHARS", 70)

# ONNX Runtime CPU threads for Piper (0 = auto: 4 on Windows, else library default).
PIPER_ORT_THREADS = _get_int("PIPER_ORT_THREADS", 0)

# Piper ONNX provider: auto | cpu | dml (Windows GPU) | cuda (NVIDIA GPU).
PIPER_ONNX_PROVIDER = _get_str("PIPER_ONNX_PROVIDER", "auto").lower()

# fast = *-low voice (quicker on CPU); medium = default quality.
PIPER_QUALITY = _get_str("PIPER_QUALITY", "fast" if _IS_WINDOWS else "medium").lower()

# Emit the first TTS chunk at a word boundary once this many chars stream in.
# 0 = wait for a clause delimiter (. or ,).
PIPER_STREAM_FIRST_CHARS = _get_int("PIPER_STREAM_FIRST_CHARS", 30)

# Merge all post-first segments into one Piper call (avoids ~3s overhead each).
PIPER_MERGE_TAIL = _get_bool("PIPER_MERGE_TAIL", True)

# Start TTS on the first translated clause while the LLM is still streaming.
STREAMING_TTS_OVERLAP = _get_bool("STREAMING_TTS_OVERLAP", True)


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
# to use the system default output. Used when MONITOR_PLAYBACK and/or TTS cue.
MONITOR_DEVICE = _get_str("MONITOR_DEVICE", "")

# Soft beep on MONITOR_DEVICE ~1s before TTS hits Cable (not on Teams mic).
TTS_MONITOR_CUE = _get_bool("TTS_MONITOR_CUE", True)
TTS_MONITOR_CUE_LEAD_S = _get_float("TTS_MONITOR_CUE_LEAD_S", 1.0)
TTS_MONITOR_CUE_DURATION_S = _get_float("TTS_MONITOR_CUE_DURATION_S", 0.14)
TTS_MONITOR_CUE_FREQ_HZ = _get_float("TTS_MONITOR_CUE_FREQ_HZ", 880.0)
TTS_MONITOR_CUE_AMPLITUDE = _get_float("TTS_MONITOR_CUE_AMPLITUDE", 0.22)


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
# 60s fits long monologues (several paragraphs). Lower to 15–20 for live dialogue.
MAX_CHUNK_DURATION = _get_float("MAX_CHUNK_DURATION", 60.0)

# RMS energy below which a 30 ms block counts as silence. If short utterances
# get cut off, lower this; if background noise triggers chunks, raise it.
# Typical mic values: silence ~0.002-0.01, speech ~0.02-0.15.
# Laptop / built-in mics often need 0.02–0.04 (noise floor is higher).
SILENCE_THRESHOLD = _get_float("SILENCE_THRESHOLD", 0.015)

# Consecutive "loud" blocks (~30 ms each) required before VAD enters speech.
# Filters clicks, keyboard noise, and short room spikes. 2 ≈ 60 ms of energy.
# Raise (6–10) for noisy laptop mics; lower (1) if first syllable still cuts.
VAD_ONSET_BLOCKS = _get_int("VAD_ONSET_BLOCKS", 2)

# Quiet blocks allowed mid-onset without resetting the counter (soft PT starts).
# 2 ≈ 60 ms dip tolerance between "está" and the louder rest of the word.
VAD_ONSET_GAP_BLOCKS = _get_int("VAD_ONSET_GAP_BLOCKS", 2)

# While waiting for speech onset, multiply SILENCE_THRESHOLD by this (<1 = more
# sensitive) so unstressed first syllables still count toward onset.
VAD_ONSET_THRESHOLD_SCALE = _get_float("VAD_ONSET_THRESHOLD_SCALE", 0.75)

# Base silence (seconds) before a chunk ends. Adaptive VAD scales this up while
# you keep talking (long monologues tolerate longer pauses between paragraphs).
SILENCE_DURATION = _get_float("SILENCE_DURATION", 1.2)

# Longer speech requires longer silence to end the chunk (paragraph pauses).
VAD_ADAPTIVE_SILENCE = _get_bool("VAD_ADAPTIVE_SILENCE", True)
# Max multiplier on SILENCE_DURATION (e.g. 3.5 × 1.2s ≈ 4.2s pause after 25s+ speech).
VAD_SILENCE_SCALE_MAX = _get_float("VAD_SILENCE_SCALE_MAX", 3.5)

# Overlap (seconds) kept when MAX_CHUNK_DURATION forces a mid-speech split.
VAD_SPLIT_OVERLAP = _get_float("VAD_SPLIT_OVERLAP", 1.5)

# While already in speech, RMS threshold is multiplied by this (<1 = more tolerant
# of brief dips between words, so monologues are not cut at MAX_CHUNK_DURATION).
VAD_SPEECH_HANGOVER = _get_float("VAD_SPEECH_HANGOVER", 0.65)

# Paragraph splits during long monologues (emit chunk at short pauses, keep listening).
# Used when SENTENCE_SPLIT=false (legacy monologue / long-pause behaviour).
PARAGRAPH_SPLIT = _get_bool("PARAGRAPH_SPLIT", True)
# Only split by paragraph when translation audio is muted ([s] sound OFF).
PARAGRAPH_SPLIT_SOUND_OFF_ONLY = _get_bool("PARAGRAPH_SPLIT_SOUND_OFF_ONLY", True)
# Pause (seconds) between paragraphs that triggers an early chunk while still speaking.
PARAGRAPH_SILENCE = _get_float("PARAGRAPH_SILENCE", 1.0)
# Minimum speech (seconds) before a paragraph pause can split the chunk.
PARAGRAPH_MIN_SPEECH = _get_float("PARAGRAPH_MIN_SPEECH", 5.0)
# Audio overlap (seconds) kept after a paragraph split.
PARAGRAPH_SPLIT_OVERLAP = _get_float("PARAGRAPH_SPLIT_OVERLAP", 0.3)

# Sentence-early emit: on a pause after enough speech, emit that audio as its
# own chunk (STT+translate+UI) and keep listening — do not wait for the full
# monologue / long end-silence. Prefer for faster per-phrase text (esp. sound
# OFF). When true, SENTENCE_* thresholds override PARAGRAPH_* for splits.
# Tip for long unfinished sentences: raise SENTENCE_SILENCE / MIN_SPEECH, or
# set SENTENCE_SPLIT=false to only flush on true end-of-turn silence.
SENTENCE_SPLIT = _get_bool("SENTENCE_SPLIT", True)
# Only sentence-split when sound is OFF (safer with live TTS). Set false to
# also early-emit phrases while sound is ON.
SENTENCE_SPLIT_SOUND_OFF_ONLY = _get_bool("SENTENCE_SPLIT_SOUND_OFF_ONLY", True)
# Pause (seconds) treated as end-of-sentence while still in an utterance.
# ~0.9–1.1 resists breath/hesitation mid-phrase; 0.55 felt too eager.
SENTENCE_SILENCE = _get_float("SENTENCE_SILENCE", 0.95)
# Minimum speech (seconds) before a sentence pause can emit a chunk.
# Higher = fewer mid-thought cuts on long answers.
SENTENCE_MIN_SPEECH = _get_float("SENTENCE_MIN_SPEECH", 2.5)
# Overlap (seconds) kept after a sentence split (avoids clipping next onset).
SENTENCE_SPLIT_OVERLAP = _get_float("SENTENCE_SPLIT_OVERLAP", 0.25)
# Max multiplier on SENTENCE_SILENCE as the monologue grows (adaptive early-split).
# e.g. 0.95s × 2.5 ≈ 2.4s pause required after ~20s of continuous speech.
SENTENCE_SILENCE_SCALE_MAX = _get_float("SENTENCE_SILENCE_SCALE_MAX", 2.5)

# Sound OFF: process STT+translation in parallel (one worker per early chunk).
SOUND_OFF_PARALLEL = _get_bool("SOUND_OFF_PARALLEL", True)
SOUND_OFF_WORKERS = _get_int("SOUND_OFF_WORKERS", 2)
# Sound OFF: skip TTS entirely (text only; saves CPU — replay needs re-synthesis).
TTS_SKIP_WHEN_MUTED = _get_bool("TTS_SKIP_WHEN_MUTED", True)
# Base end-of-speech pause (seconds) when sound is OFF ([s] muted). Used as the
# *starting* silence (then adaptive scales up) — not a hard ceiling.
# Avoid values < 1.2 if speakers often pause mid-thought.
SOUND_OFF_SILENCE_DURATION = _get_float("SOUND_OFF_SILENCE_DURATION", 1.8)

# Utterances shorter than this (after trimming) are ignored as noise/clicks.
MIN_SPEECH_DURATION = _get_float("MIN_SPEECH_DURATION", 0.4)

# Analysis block size used for VAD (seconds). 30 ms is standard.
BLOCK_DURATION = 0.03

# Keep this much audio *before* speech onset so the first syllable isn't clipped.
# 0.5s covers soft PT openers ("vocês", "está", "e então") when onset fires late.
PREROLL_DURATION = _get_float("PREROLL_DURATION", 0.5)

# --------------------------------------------------------------------------- #
# Low-latency mode
# --------------------------------------------------------------------------- #
# When True: shorter LLM translation prompt and tighter max_tokens cap.
LOW_LATENCY = _get_bool("LOW_LATENCY", False)

# --------------------------------------------------------------------------- #
# Phase 3 — streaming & advanced capture/playback
# --------------------------------------------------------------------------- #
# Stream LLM tokens to the terminal (Groq LLM only).
STREAMING_LLM = _get_bool("STREAMING_LLM", True)

# Synthesize and play TTS sentence-by-sentence (faster time-to-first-audio).
STREAMING_TTS = _get_bool("STREAMING_TTS", True)

# VAD backend: "energy" (RMS) or "silero" (neural, needs onnxruntime).
VAD_MODE = _get_str("VAD_MODE", "energy").lower()

# Silero speech probability threshold (0.0–1.0). Used when VAD_MODE=silero.
SILERO_VAD_THRESHOLD = _get_float("SILERO_VAD_THRESHOLD", 0.45)

# Emit partial chunks while still speaking (no pause required).
ROLLING_CHUNKS = _get_bool("ROLLING_CHUNKS", False)

# Rolling chunk length in seconds (only when ROLLING_CHUNKS=true).
ROLLING_CHUNK_DURATION = _get_float("ROLLING_CHUNK_DURATION", 2.5)

# Stop current playback when a new chunk arrives (live-call mode).
PLAYBACK_INTERRUPT = _get_bool("PLAYBACK_INTERRUPT", True)

# Playback write block size in milliseconds (smaller = more responsive interrupt).
PLAYBACK_BLOCK_MS = _get_int("PLAYBACK_BLOCK_MS", 80)

# Gate mic capture while TTS plays (app-level only — no Windows tray mute).
# Breaks acoustic feedback when OUTPUT is the notebook speakers and INPUT is
# the same notebook mic (speaker → mic → STT → TTS → speaker loop).
# Side effect: you cannot barge-in while translation audio is playing.
# Keep True for speaker testing; set False for headphones + VB-Cable full-duplex.
MUTE_CAPTURE_DURING_PLAYBACK = _get_bool("MUTE_CAPTURE_DURING_PLAYBACK", True)

# Extra silence after TTS ends before re-opening the mic (speaker ring-out).
# Milliseconds. Raise if the loop still catches the last word of the TTS.
MUTE_CAPTURE_HANGOVER_MS = _get_int("MUTE_CAPTURE_HANGOVER_MS", 350)

# --------------------------------------------------------------------------- #
# UI mode
# --------------------------------------------------------------------------- #
# classic = colorama prints + readline (legacy)
# tui     = Textual full-screen: scrollable log + fixed listen status at bottom
UI_MODE = _get_str("UI_MODE", "tui").lower()
# Start TUI already in compact/minimal layout (menu strip hidden; command line stays).
# Same as pressing F4 / [u] once at launch. Default false = full menu visible.
TUI_MINIMAL = _get_bool("TUI_MINIMAL", False)

# --------------------------------------------------------------------------- #
# Live Captions (Windows 11 LiveCaptions → faixa superior da TUI)
# --------------------------------------------------------------------------- #
# Scrapes OS captions via UI Automation (independent of mic→Whisper→TTS).
# Requires Windows 11 22H2+ and: pip install uiautomation
# Set language in Windows LiveCaptions settings (not only SOURCE_LANG).
LIVE_CAPTIONS_ENABLED = _get_bool("LIVE_CAPTIONS_ENABLED", True)
# Hide LiveCaptions window after launch (like LiveCaptions-Translator).
LIVE_CAPTIONS_HIDE_WINDOW = _get_bool("LIVE_CAPTIONS_HIDE_WINDOW", True)
# Kill LiveCaptions.exe when LiveLingo exits (default: leave process running).
LIVE_CAPTIONS_KILL_ON_EXIT = _get_bool("LIVE_CAPTIONS_KILL_ON_EXIT", False)
# Poll interval for CaptionsTextBlock (ms). LCT uses ~25.
LIVE_CAPTIONS_POLL_MS = _get_int("LIVE_CAPTIONS_POLL_MS", 25)
# Sentence flush heuristics (ticks of POLL_MS): idle unchanged / sync while growing.
LIVE_CAPTIONS_MAX_IDLE = _get_int("LIVE_CAPTIONS_MAX_IDLE", 50)
LIVE_CAPTIONS_MAX_SYNC = _get_int("LIVE_CAPTIONS_MAX_SYNC", 3)
# Min seconds between partial (in-progress) translate API calls for the strip.
LIVE_CAPTIONS_PARTIAL_INTERVAL_S = _get_float("LIVE_CAPTIONS_PARTIAL_INTERVAL_S", 0.7)
# Also write **final/stable** LC pairs into the Tradução log tab (not partials).
LIVE_CAPTIONS_LOG = _get_bool("LIVE_CAPTIONS_LOG", True)
# Language direction for captions (inbound) vs voice pipeline (outbound):
#   Voice BR→EN = you speak PT, others hear EN.
#   Captions usually hear EN (meeting) and you want to READ PT → invert pair.
# true  = LC source=TARGET_LANG, LC target=SOURCE_LANG (default, recommended)
# false = same direction as voice (SOURCE→TARGET)
LIVE_CAPTIONS_INVERT_LANGS = _get_bool("LIVE_CAPTIONS_INVERT_LANGS", True)
# Optional explicit pair (overrides invert when both set), e.g. en / pt
LIVE_CAPTIONS_SOURCE_LANG = _get_str("LIVE_CAPTIONS_SOURCE_LANG", "")
LIVE_CAPTIONS_TARGET_LANG = _get_str("LIVE_CAPTIONS_TARGET_LANG", "")

# --------------------------------------------------------------------------- #
# Webcam lip-sync → virtual camera (optional; Teams/Meet)
# --------------------------------------------------------------------------- #
# Requires: pip install opencv-python mediapipe pyvirtualcam
# Optional ONNX: onnxruntime-gpu (CUDA) or onnxruntime (CPU)
# Drivers: OBS Virtual Cam (Windows/macOS) or v4l2loopback (Linux)
# Runtime: [cam] toggle · [cam on|off|status]
# cam on = open physical + virtual cam; cam off = release both (threads stay idle).
# Docs: docs/webcam-lipsync.md
WEBCAM_ENABLED = _get_bool("WEBCAM_ENABLED", False)
# Start already streaming to virtual cam (else wait for [cam on]).
WEBCAM_START_ENABLED = _get_bool("WEBCAM_START_ENABLED", False)
# Physical camera index for OpenCV VideoCapture.
WEBCAM_DEVICE_INDEX = _get_int("WEBCAM_DEVICE_INDEX", 0)
# 0 = keep camera native resolution.
WEBCAM_WIDTH = _get_int("WEBCAM_WIDTH", 0)
WEBCAM_HEIGHT = _get_int("WEBCAM_HEIGHT", 0)
WEBCAM_FPS = _get_float("WEBCAM_FPS", 30.0)
# Drop-old queues between capture / infer / emit (latency bound).
WEBCAM_QUEUE_SIZE = _get_int("WEBCAM_QUEUE_SIZE", 2)
# Lip engine: amplitude (CPU demo) | passthrough | onnx
WEBCAM_LIP_ENGINE = _get_str("WEBCAM_LIP_ENGINE", "amplitude").lower()
WEBCAM_AMP_MAX_OPEN_PX = _get_float("WEBCAM_AMP_MAX_OPEN_PX", 22.0)
WEBCAM_AMP_SENSITIVITY = _get_float("WEBCAM_AMP_SENSITIVITY", 28.0)
# When webcam streams, also turn LiveLingo sound ON (TTS → CABLE → Teams mic).
WEBCAM_AUTO_SOUND = _get_bool("WEBCAM_AUTO_SOUND", True)
# Path to ONNX export (Wav2Lip-style or custom NCHW face + audio).
WEBCAM_ONNX_MODEL = _get_str("WEBCAM_ONNX_MODEL", "")
WEBCAM_ONNX_INPUT_SIZE = _get_int("WEBCAM_ONNX_INPUT_SIZE", 96)
WEBCAM_ONNX_FP16 = _get_bool("WEBCAM_ONNX_FP16", True)
# Soft mouth mask / ROI padding.
WEBCAM_ROI_PAD = _get_float("WEBCAM_ROI_PAD", 0.35)
WEBCAM_FEATHER_PX = _get_int("WEBCAM_FEATHER_PX", 9)
# TTS audio schedule fed from pipeline playback (Cable Out path).
# Clips play out on a wall-clock timeline so mouth tracks TTS, not the
# whole buffer dump (lip morph only while audio is "playing").
WEBCAM_AUDIO_SR = _get_int("WEBCAM_AUDIO_SR", 24000)
WEBCAM_AUDIO_RING_S = _get_float("WEBCAM_AUDIO_RING_S", 2.0)
WEBCAM_AUDIO_WINDOW_S = _get_float("WEBCAM_AUDIO_WINDOW_S", 0.35)
# Start morph this many seconds after push (Cable device open lag).
WEBCAM_AUDIO_PLAY_DELAY_S = _get_float("WEBCAM_AUDIO_PLAY_DELAY_S", 0.08)
# Debug overlay near mouth (off by default — avoids fake “transparency” look).
WEBCAM_SYNC_MARKER = _get_bool("WEBCAM_SYNC_MARKER", False)
# When true: mouth forced closed if no TTS; only opens while sound → Teams.
# When false: idle shows natural webcam mouth (no force-close).
WEBCAM_FORCE_CLOSED_IDLE = _get_bool("WEBCAM_FORCE_CLOSED_IDLE", True)
# Auto-show closed photo while VAD hears speech (ignored after F10 manual mode).
# F10 toggles manual ON/OFF; [cam closed auto] returns to VAD auto.
WEBCAM_CLOSED_AUTO = _get_bool("WEBCAM_CLOSED_AUTO", True)
# Closed-mouth photo template (best idle quality). Capture: [cam snap closed]
# Leave empty to use defaults under .cache/webcam/
WEBCAM_CLOSED_MOUTH_IMAGE = _get_str(
    "WEBCAM_CLOSED_MOUTH_IMAGE", ".cache/webcam/closed_mouth.png"
)
WEBCAM_CLOSED_MOUTH_LANDMARKS = _get_str(
    "WEBCAM_CLOSED_MOUTH_LANDMARKS", ".cache/webcam/closed_mouth.json"
)
# F10 / cam snap closed: full-face freeze plate (forehead→chin), not mouth-only.
# scale 1.0 = tight face oval; 1.15 default pad; max ~1.45 (still leaves some BG).
WEBCAM_TEMPLATE_REGION_SCALE = _get_float("WEBCAM_TEMPLATE_REGION_SCALE", 1.15)
# Soft edge of face plate (px).
WEBCAM_TEMPLATE_FEATHER_PX = _get_int("WEBCAM_TEMPLATE_FEATHER_PX", 24)
# Mirror closed-mouth photo vs live frame. Default false (photo already matches
# OpenCV capture). Set true only if left/right looks swapped in Teams.
WEBCAM_TEMPLATE_FLIP_H = _get_bool("WEBCAM_TEMPLATE_FLIP_H", False)
# Keep closed photo after VAD ends (mic lag / end-of-utterance). 1.5s default.
WEBCAM_SPEECH_HANGOVER_S = _get_float("WEBCAM_SPEECH_HANGOVER_S", 1.5)
# pyvirtualcam: empty = auto backend; Windows often "obs", Linux "v4l2loopback"
WEBCAM_VCAM_BACKEND = _get_str("WEBCAM_VCAM_BACKEND", "")
WEBCAM_VCAM_DEVICE = _get_str("WEBCAM_VCAM_DEVICE", "")
# Force virtual-cam resolution (0 = 1280x720 default; physical frames are resized).
# Even sizes work best with OBS Virtual Camera.
WEBCAM_VCAM_WIDTH = _get_int("WEBCAM_VCAM_WIDTH", 1280)
WEBCAM_VCAM_HEIGHT = _get_int("WEBCAM_VCAM_HEIGHT", 720)
# Seconds before abandoning a hung pyvirtualcam.Camera() attempt (Windows/OBS).
# 0 = open vcam on emit thread (recommended). >0 enables hang-guard worker
# (only if Camera() freezes without OBS driver; avoid leaving zombie holders).
WEBCAM_VCAM_OPEN_TIMEOUT_S = _get_float("WEBCAM_VCAM_OPEN_TIMEOUT_S", 0.0)
# Open a local OpenCV preview window (debug; not Teams).
WEBCAM_DEBUG_PREVIEW = _get_bool("WEBCAM_DEBUG_PREVIEW", False)

# --------------------------------------------------------------------------- #
# Debug / Verbose Mode
# --------------------------------------------------------------------------- #
VERBOSE = False
