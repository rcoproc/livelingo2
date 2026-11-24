"""
piper_voices.py
===============
Piper voice names, Hugging Face paths, and TARGET_LANG defaults.
"""

HF_BASE = "https://huggingface.co/rhasspy/piper-voices/resolve/main"

# voice_id -> path under rhasspy/piper-voices (without extension)
PIPER_VOICE_PATHS = {
    # en_US — female
    "en_US-lessac-low": "en/en_US/lessac/low/en_US-lessac-low",
    "en_US-lessac-medium": "en/en_US/lessac/medium/en_US-lessac-medium",
    "en_US-amy-low": "en/en_US/amy/low/en_US-amy-low",
    "en_US-amy-medium": "en/en_US/amy/medium/en_US-amy-medium",
    # en_US — male
    "en_US-ryan-low": "en/en_US/ryan/low/en_US-ryan-low",
    "en_US-ryan-medium": "en/en_US/ryan/medium/en_US-ryan-medium",
    "en_US-ryan-high": "en/en_US/ryan/high/en_US-ryan-high",
    "en_US-joe-medium": "en/en_US/joe/medium/en_US-joe-medium",
    "en_US-danny-low": "en/en_US/danny/low/en_US-danny-low",
    "en_US-hfc_male-medium": "en/en_US/hfc_male/medium/en_US-hfc_male-medium",
    # en_GB
    "en_GB-alba-medium": "en/en_GB/alba/medium/en_GB-alba-medium",
    "en_GB-northern_english_male-medium": (
        "en/en_GB/northern_english_male/medium/en_GB-northern_english_male-medium"
    ),
    "fr_FR-siwis-medium": "fr/fr_FR/siwis/medium/fr_FR-siwis-medium",
    "fr_FR-upmc-medium": "fr/fr_FR/upmc/medium/fr_FR-upmc-medium",
    "pt_BR-faber-medium": "pt/pt_BR/faber/medium/pt_BR-faber-medium",
    "es_ES-sharvard-medium": "es/es_ES/sharvard/medium/es_ES-sharvard-medium",
    "de_DE-thorsten-medium": "de/de_DE/thorsten/medium/de_DE-thorsten-medium",
    "it_IT-riccardo-medium": "it/it_IT/riccardo/medium/it_IT-riccardo-medium",
}

DEFAULT_VOICE_BY_LANG = {
    # Male US English by default (lessac/amy are female)
    "en": "en_US-ryan-medium",
    "fr": "fr_FR-siwis-medium",
    "pt": "pt_BR-faber-medium",
    "es": "es_ES-sharvard-medium",
    "de": "de_DE-thorsten-medium",
    "it": "it_IT-riccardo-medium",
}


def default_voice_for_lang(lang_code):
    return DEFAULT_VOICE_BY_LANG.get((lang_code or "").lower(), "en_US-lessac-medium")


def fast_voice_for(voice_id):
    """Map a *-medium voice to its *-low twin for faster CPU inference.

    Only switches when the ``*-low`` id exists in ``PIPER_VOICE_PATHS``
    (e.g. ``en_US-joe-medium`` has no low twin on Hugging Face → keep medium).
    """
    voice_id = (voice_id or "").strip()
    if not voice_id.endswith("-medium"):
        return voice_id
    low_id = voice_id[: -len("-medium")] + "-low"
    if low_id in PIPER_VOICE_PATHS:
        return low_id
    return voice_id
