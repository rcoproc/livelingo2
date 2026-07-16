"""
translate.py
============
Text translation using deep-translator's GoogleTranslator (free public
endpoint, no API key). Requires internet access.
"""

from deep_translator import GoogleTranslator


class Translator:
    def __init__(self, config):
        self.cfg = config
        # A single reusable translator instance for the configured language pair.
        self._translator = GoogleTranslator(
            source=config.SOURCE_LANG, target=config.TARGET_LANG
        )

    def translate(self, text):
        """
        Translate `text` from the source to the target language.

        Returns the translated string, or the original text if translation
        fails (e.g. transient network error) so the pipeline can keep going.
        """
        text = (text or "").strip()
        if not text:
            return ""
        try:
            result = self._translator.translate(text)
            # GoogleTranslator returns None for some inputs; guard against it.
            return (result or "").strip()
        except Exception as exc:
            raise TranslationError(str(exc)) from exc


class TranslationError(Exception):
    """Raised when the translation backend fails for a chunk."""
