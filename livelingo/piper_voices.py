"""
piper_voices.py
===============
Piper voice names, Hugging Face paths, and TARGET_LANG defaults.
"""

HF_BASE = "https://huggingface.co/rhasspy/piper-voices/resolve/main"

# voice_id -> path under rhasspy/piper-voices (without extension)
PIPER_VOICE_PATHS = {
    "en_US-lessac-low": "en/en_US/lessac/low/en_US-lessac-low",
    "en_US-lessac-medium": "en/en_US/lessac/medium/en_US-lessac-medium",
    "en_US-amy-low": "en/en_US/amy/low/en_US-amy-low",
    "en_US-amy-medium": "en/en_US/amy/medium/en_US-amy-medium",
    "en_GB-alba-medium": "en/en_GB/alba/medium/en_GB-alba-medium",
    "fr_FR-siwis-medium": "fr/fr_FR/siwis/medium/fr_FR-siwis-medium",
    "fr_FR-upmc-medium": "fr/fr_FR/upmc/medium/fr_FR-upmc-medium",
    "pt_BR-faber-medium": "pt/pt_BR/faber/medium/pt_BR-faber-medium",
    "es_ES-sharvard-medium": "es/es_ES/sharvard/medium/es_ES-sharvard-medium",
    "de_DE-thorsten-medium": "de/de_DE/thorsten/medium/de_DE-thorsten-medium",
    "it_IT-riccardo-medium": "it/it_IT/riccardo/medium/it_IT-riccardo-medium",
}

DEFAULT_VOICE_BY_LANG = {
    "en": "en_US-lessac-medium",
    "fr": "fr_FR-siwis-medium",
    "pt": "pt_BR-faber-medium",
    "es": "es_ES-sharvard-medium",
    "de": "de_DE-thorsten-medium",
    "it": "it_IT-riccardo-medium",
}


def default_voice_for_lang(lang_code):
    return DEFAULT_VOICE_BY_LANG.get((lang_code or "").lower(), "en_US-lessac-medium")


def fast_voice_for(voice_id):
    """Map a *-medium voice to its *-low twin for faster CPU inference."""
    voice_id = (voice_id or "").strip()
    if voice_id.endswith("-medium"):
        return voice_id.replace("-medium", "-low")
    return voice_id