"""
llm.py
======
High-quality translation via a free LLM (Groq's OpenAI-compatible API).

Instead of a literal machine translation, an instruction-tuned LLM takes the
*raw* (often imperfect) speech-to-text transcription and produces a clean,
natural, fluent translation in one step — fixing recognition glitches,
punctuation and filler words along the way. This is what makes tools like
Typeless feel so polished.

Get a free API key (no credit card) at: https://console.groq.com/keys

Drop-in compatible with translate.Translator: exposes `.translate(text)`.
Uses `requests` (already installed as a dependency of deep-translator).
"""

import requests

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

# Friendly names for the most common language codes (used in the prompt).
_LANG_NAMES = {
    "fr": "French", "en": "English", "es": "Spanish", "de": "German",
    "it": "Italian", "pt": "Portuguese", "nl": "Dutch", "ru": "Russian",
    "ar": "Arabic", "zh": "Chinese", "ja": "Japanese", "ko": "Korean",
    "pl": "Polish", "tr": "Turkish", "hi": "Hindi",
}

GROQ_KEY_HELP = """\
No Groq API key is set. It's free (no credit card):
  1. Go to https://console.groq.com/keys  and sign up / log in.
  2. Click "Create API Key", copy it.
  3. Paste it into your .env file:   GROQ_API_KEY=gsk_xxxxxxxx
Then run the tool again. (Or set TRANSLATION_ENGINE=google to use Google instead.)
"""


def _lang_name(code):
    return _LANG_NAMES.get((code or "").lower(), code)


class LLMError(Exception):
    """Raised when the LLM translation request fails."""


class LLMTranslator:
    def __init__(self, config):
        self.cfg = config
        self.api_key = config.GROQ_API_KEY
        self.model = config.GROQ_MODEL
        self.timeout = config.LLM_TIMEOUT

        src = _lang_name(config.SOURCE_LANG)
        tgt = _lang_name(config.TARGET_LANG)
        self.system_prompt = (
            f"You are a professional real-time interpreter. You receive a raw "
            f"speech-to-text transcription in {src}. It may contain recognition "
            f"errors, missing punctuation, filler words, or be only a fragment.\n"
            f"Produce a clean, natural, fluent {tgt} translation of what the "
            f"speaker most likely meant.\n"
            f"Rules:\n"
            f"- Output ONLY the {tgt} translation. No quotes, no notes, no "
            f"explanations, no preamble.\n"
            f"- Silently fix obvious transcription errors and punctuation so it "
            f"reads naturally in {tgt}.\n"
            f"- Preserve meaning and tone; never add information.\n"
            f"- If the input is empty, gibberish, or not real speech, output "
            f"nothing at all."
        )

    # ------------------------------------------------------------------ #
    def translate(self, text):
        """Clean + translate `text`. Returns the target-language string."""
        text = (text or "").strip()
        if not text:
            return ""

        payload = {
            "model": self.model,
            "temperature": 0.2,
            "max_tokens": 400,
            "messages": [
                {"role": "system", "content": self.system_prompt},
                {"role": "user", "content": text},
            ],
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        try:
            resp = requests.post(
                GROQ_URL, headers=headers, json=payload, timeout=self.timeout
            )
        except requests.RequestException as exc:
            raise LLMError(f"network error contacting Groq: {exc}") from exc

        # Turn common HTTP errors into clear, actionable messages.
        if resp.status_code == 401:
            raise LLMError("Groq rejected the API key (401). Check GROQ_API_KEY.")
        if resp.status_code == 404:
            raise LLMError(
                f"Groq model '{self.model}' not found (404). "
                f"Set a valid GROQ_MODEL (e.g. llama-3.1-8b-instant)."
            )
        if resp.status_code == 429:
            raise LLMError(
                "Groq rate limit reached (429). Wait a moment or use a smaller "
                "model (GROQ_MODEL=llama-3.1-8b-instant)."
            )
        if resp.status_code >= 400:
            raise LLMError(f"Groq error {resp.status_code}: {resp.text[:200]}")

        try:
            data = resp.json()
            out = data["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError) as exc:
            raise LLMError(f"unexpected Groq response: {exc}") from exc

        # Strip stray surrounding quotes the model might add.
        return out.strip().strip('"').strip()
