"""
stt_filter.py
=============
Drop or strip common Whisper/Groq STT hallucinations on near-silent or tail audio.
"""

import re
import unicodedata

import numpy as np

# Whole-chunk phrases (chunk is only hallucination).
# Includes Whisper silence-tails: goodbye/goodnight after real speech + room noise.
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
    # Farewells / outro (very common on quiet tails + fan/AC noise)
    r"^good\s*bye\.?$",
    r"^goodbye\.?$",
    r"^good[\s\-]*bye\.?$",
    r"^good\s*night\.?$",
    r"^goodnight\.?$",
    r"^good[\s\-]*night\.?$",
    r"^bye(?:[\s\-]*bye)?\.?$",
    r"^bye\s+bye\.?$",
    r"^see\s+you(?:\s+(?:later|soon|tomorrow|next\s+time))?\.?$",
    r"^see\s+ya\.?$",
    r"^have\s+a\s+(?:nice|good)\s+day\.?$",
    r"^the\s+end\.?$",
    r"^so\s+long\.?$",
    r"^farewell\.?$",
    r"^boa\s+noite\.?$",
    r"^bom\s+dia\.?$",
    r"^boa\s+tarde\.?$",
    r"^ate\s+logo\.?$",
    r"^ate\s+mais\.?$",
    r"^tchau\.?$",
    r"^adeus\.?$",
    r"^adios\.?$",
    r"^buenas\s+noches\.?$",
    r"^buenos\s+dias\.?$",
    r"^hasta\s+luego\.?$",
    r"^hasta\s+pronto\.?$",
    r"^au\s+revoir\.?$",
    r"^bonne\s+nuit\.?$",
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
    # Lonely farewell tail after real speech (silence hallucination)
    rf"[\s.,;:!?…\-–—]+good[\s\-]*bye{_END}",
    rf"[\s.,;:!?…\-–—]+good[\s\-]*night{_END}",
    rf"[\s.,;:!?…\-–—]+bye(?:[\s\-]*bye)?{_END}",
    rf"[\s.,;:!?…\-–—]+see\s+you(?:\s+(?:later|soon|tomorrow))?{_END}",
    rf"[\s.,;:!?…\-–—]+boa\s+noite{_END}",
    rf"[\s.,;:!?…\-–—]+tchau{_END}",
    rf"[\s.,;:!?…\-–—]+buenas\s+noches{_END}",
    rf"[\s.,;:!?…\-–—]+au\s+revoir{_END}",
    rf"[\s.,;:!?…\-–—]+bonne\s+nuit{_END}",
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


def transcript_discard_reason(audio, text, config=None):
    """
    Why STT text should be dropped, or None to keep.

    Phrase blocklist (goodbye / legenda por / …) always applies.

    Low-energy heuristic is **strict** — only near-silence + ultra-short text
    (1–2 words). Quiet real speech like "E aí, vai encarar?" (4 words) must
    never die here: older defaults (max_words=6, max_sec=2.5, rms=0.01) ate
    short PT/EN clauses on soft USB mics.
    """
    if config is not None and not getattr(config, "STT_HALLUCINATION_FILTER", True):
        return None

    text = (text or "").strip()
    if not text:
        return "texto vazio"
    if is_hallucination(text):
        return "frase de alucinação (blocklist)"

    if audio is None or not isinstance(audio, np.ndarray):
        return None

    rms = audio_rms(audio)
    words = len(text.split())
    duration = len(audio) / float(
        getattr(config, "SAMPLE_RATE", 16000) if config else 16000
    )

    # Real multi-word speech / questions — never energy-drop
    if words >= 3:
        return None
    if any(ch in text for ch in "?!¿¡"):
        return None
    if len(text) >= 18:
        return None

    min_rms = float(getattr(config, "STT_MIN_RMS", 0.004) or 0.004) if config else 0.004
    max_words = (
        int(getattr(config, "STT_LOW_ENERGY_MAX_WORDS", 2) or 2) if config else 2
    )
    max_duration = (
        float(getattr(config, "STT_LOW_ENERGY_MAX_SEC", 1.2) or 1.2) if config else 1.2
    )
    max_words = max(1, min(6, max_words))
    max_duration = max(0.4, min(3.0, max_duration))

    if rms < min_rms and duration < max_duration and words <= max_words:
        return (
            f"baixa energia (rms={rms:.4f}<{min_rms:.3f}, "
            f"{words} palavra(s), {duration:.1f}s)"
        )

    return None


def should_discard_transcript(audio, text, config=None):
    """
    Discard STT output that is likely a silence hallucination.

    Uses phrase blocklist plus optional low-energy + short-text heuristics.
    """
    return transcript_discard_reason(audio, text, config) is not None
