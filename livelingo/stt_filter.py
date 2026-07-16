"""
stt_filter.py
=============
Drop or strip common Whisper/Groq STT hallucinations on near-silent or tail audio.
"""

import re
import unicodedata

import numpy as np

# Whole-chunk phrases (chunk is only hallucination).
_HALLUCINATION_PATTERNS = (
    r"^legenda(?:s)?\s+por\b",
    r"^subtitle(?:s)?\s+by\b",
    r"^subtitulado\s+por\b",
    r"^translated\s+by\b",
    r"^traduzido\s+por\b",
    r"^caption(?:s)?\s+by\b",
    r"^obrigad[oa]\s+por\s+assistir",
    r"^thanks?\s+for\s+watching",
    r"^inscreva-se\b",
    r"^subscribe\b",
    r"^amara\.org\b",
    r"^www\.",
    r"^http",
    r"^\s*\.+\s*$",
    r"^you$",
    r"^thank you\.?$",
    r"^obrigad[oa]\.?$",
    r"^silence\.?$",
    r"^music\.?$",
    r"^applause\.?$",
    r"^\[.*\]$",
)

# Trailing credit tails Whisper appends after real speech (often after silence).
# Narrow on purpose: short credit suffix only. Do NOT strip normal speech that
# happens to contain "traduzido por" / "subscribe to" mid-sentence.
# Name part: up to ~5 short tokens (credits are rarely longer).
_CREDIT_NAME = r"\S+(?:\s+\S+){0,4}"
_END = r"[.!?…]*\s*$"
_TAIL_STRIP_PATTERNS = (
    rf"[\s.,;:!?…\-–—]+legenda(?:s)?\s+por\s+{_CREDIT_NAME}{_END}",
    rf"[\s.,;:!?…\-–—]+subtitle(?:s)?\s+by\s+{_CREDIT_NAME}{_END}",
    rf"[\s.,;:!?…\-–—]+subtitulado\s+por\s+{_CREDIT_NAME}{_END}",
    # English credit only ("Translated by Foo"). Skip "traduzido por …" here —
    # too easy to eat real PT speech like "foi traduzido por nosso time".
    rf"[\s.,;:!?…\-–—]+translated\s+by\s+{_CREDIT_NAME}{_END}",
    rf"[\s.,;:!?…\-–—]+caption(?:s)?\s+by\s+{_CREDIT_NAME}{_END}",
    rf"[\s.,;:!?…\-–—]+obrigad[oa]\s+por\s+assistir{_END}",
    rf"[\s.,;:!?…\-–—]+thanks?\s+for\s+watching{_END}",
    rf"[\s.,;:!?…\-–—]+inscreva-se(?:\s+no\s+canal)?{_END}",
    rf"[\s.,;:!?…\-–—]+subscribe(?:\s+to\s+(?:my|the)\s+channel)?{_END}",
    r"[\s.,;:!?…\-–—]+amara\.org\b.*$",
)


def _normalize(text):
    text = unicodedata.normalize("NFKD", (text or "").strip().lower())
    return "".join(ch for ch in text if not unicodedata.combining(ch))


def is_hallucination(text):
    """Return True if the entire transcript is a known silence-hallucination phrase."""
    normalized = _normalize(text)
    if not normalized:
        return True
    for pattern in _HALLUCINATION_PATTERNS:
        if re.search(pattern, normalized, re.IGNORECASE):
            return True
    return False


def strip_hallucinations(text):
    """
    Remove trailing hallucination phrases from a transcript that still has
    real speech before them.

    Pure credit-only lines are left to is_hallucination() / pipeline — this
    function only peels a suffix when a non-empty speech prefix remains.
    """
    cleaned = (text or "").strip()
    if not cleaned:
        return ""
    if is_hallucination(cleaned):
        return ""

    changed = True
    while changed:
        changed = False
        for pattern in _TAIL_STRIP_PATTERNS:
            match = re.search(pattern, cleaned, flags=re.IGNORECASE | re.UNICODE)
            if not match:
                continue
            # Only strip when there is real content before the credit tail.
            prefix = cleaned[: match.start()].strip().rstrip(".,;:!?… ")
            if not prefix:
                continue
            cleaned = prefix
            changed = True
            break

    if is_hallucination(cleaned):
        return ""
    return cleaned.strip()


def clean_transcript(text, config=None):
    """
    Strip embedded hallucination tails from STT text.

    Returns (cleaned_text, was_modified).
    """
    if config is not None and not getattr(config, "STT_HALLUCINATION_FILTER", True):
        return (text or "").strip(), False
    original = (text or "").strip()
    cleaned = strip_hallucinations(original)
    return cleaned, cleaned != original


def audio_rms(audio):
    if audio is None or len(audio) == 0:
        return 0.0
    block = np.asarray(audio, dtype=np.float64)
    return float(np.sqrt(np.mean(np.square(block))))


def should_discard_transcript(audio, text, config=None):
    """
    Discard STT output that is likely a silence hallucination.

    Uses phrase blocklist plus optional low-energy + short-text heuristics.
    """
    if config is not None and not getattr(config, "STT_HALLUCINATION_FILTER", True):
        return False

    text = (text or "").strip()
    if not text:
        return True
    if is_hallucination(text):
        return True

    if audio is None or not isinstance(audio, np.ndarray):
        return False

    rms = audio_rms(audio)
    words = len(text.split())
    duration = len(audio) / float(getattr(config, "SAMPLE_RATE", 16000) if config else 16000)

    min_rms = getattr(config, "STT_MIN_RMS", 0.010) if config else 0.010
    max_words = getattr(config, "STT_LOW_ENERGY_MAX_WORDS", 6) if config else 6
    max_duration = getattr(config, "STT_LOW_ENERGY_MAX_SEC", 2.5) if config else 2.5

    if rms < min_rms and duration < max_duration and words <= max_words:
        return True

    return False