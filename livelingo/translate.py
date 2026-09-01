"""
translate.py
============
Text translation using deep-translator.

Primary: GoogleTranslator (free public endpoint, no API key).
Fallback: MyMemoryTranslator when Google returns TranslationNotFound,
Error 500 HTML, or empty — common flake that left LC with
``[ERROR] … No translation was found``.
"""

from __future__ import annotations

from deep_translator import GoogleTranslator

try:
    from deep_translator import MyMemoryTranslator
except Exception:  # pragma: no cover
    MyMemoryTranslator = None  # type: ignore


def normalize_google_lang(code: str) -> str:
    """Map LiveLingo / UI codes to deep-translator Google codes."""
    c = (code or "").strip().lower().replace("_", "-")
    if not c:
        return "en"
    if "-" in c:
        c = c.split("-", 1)[0]
    aliases = {
        "br": "pt",
        "bra": "pt",
        "por": "pt",
        "ptbr": "pt",
        "eng": "en",
        "us": "en",
        "gb": "en",
        "cmn": "zh-CN",
        "zh": "zh-CN",
        "cn": "zh-CN",
        "jp": "ja",
        "jpn": "ja",
        "kr": "ko",
        "kor": "ko",
    }
    return aliases.get(c, c)


def normalize_mymemory_lang(code: str) -> str:
    """Map to MyMemory locale tags (en-US, pt-BR, …)."""
    c = (code or "").strip().lower().replace("_", "-")
    base = c.split("-", 1)[0] if c else "en"
    aliases = {
        "br": "pt-BR",
        "bra": "pt-BR",
        "por": "pt-BR",
        "pt": "pt-BR",
        "ptbr": "pt-BR",
        "en": "en-US",
        "eng": "en-US",
        "us": "en-US",
        "gb": "en-GB",
        "es": "es-ES",
        "fr": "fr-FR",
        "de": "de-DE",
        "it": "it-IT",
        "ja": "ja-JP",
        "zh": "zh-CN",
        "ko": "ko-KR",
    }
    if c in ("pt-br", "pt-pt", "en-us", "en-gb", "zh-cn", "zh-tw"):
        # preserve known full tags with canonical casing
        parts = c.split("-")
        return parts[0] + "-" + parts[1].upper()
    return aliases.get(base, aliases.get(c, f"{base}-{base.upper()}"))


def _looks_like_translator_garbage(text: str) -> bool:
    """True for Google error pages / empty junk returned as 'translation'."""
    t = (text or "").strip()
    if not t:
        return True
    low = t.lower()
    markers = (
        "error 500",
        "server error",
        "there was an error",
        "please try again later",
        "no translation was found",
        "try another translator",
        "!!1500",
    )
    return any(m in low for m in markers)


class Translator:
    def __init__(self, config):
        self.cfg = config
        src = normalize_google_lang(getattr(config, "SOURCE_LANG", "en"))
        tgt = normalize_google_lang(getattr(config, "TARGET_LANG", "en"))
        self._src = src
        self._tgt = tgt
        self._translator = GoogleTranslator(source=src, target=tgt)

    def set_language_pair(self, source=None, target=None):
        """Recreate the Google client after a SOURCE/TARGET swap ([g])."""
        src = normalize_google_lang(
            source if source is not None else getattr(self.cfg, "SOURCE_LANG", "en")
        )
        tgt = normalize_google_lang(
            target if target is not None else getattr(self.cfg, "TARGET_LANG", "en")
        )
        self._src = src
        self._tgt = tgt
        self._translator = GoogleTranslator(source=src, target=tgt)

    def translate(self, text):
        """
        Translate ``text`` from source to target.

        Tries Google first; on flake/unsupported response falls back to MyMemory.
        Raises TranslationError only if every backend fails.
        """
        text = (text or "").strip()
        if not text:
            return ""

        errors: list[str] = []

        # 1) Google
        try:
            result = self._translator.translate(text)
            out = (result or "").strip()
            if out and not _looks_like_translator_garbage(out):
                return out
            if out:
                errors.append(f"google garbage: {out[:60]}")
            else:
                errors.append("google empty")
        except Exception as exc:
            errors.append(f"google: {exc}")

        # 2) MyMemory (often works when Google scrape is down)
        try:
            out = self._translate_mymemory(text)
            if out and not _looks_like_translator_garbage(out):
                return out
            if out:
                errors.append(f"mymemory garbage: {out[:60]}")
            else:
                errors.append("mymemory empty")
        except Exception as exc:
            errors.append(f"mymemory: {exc}")

        # 3) Google again with source=auto (sometimes recovers)
        try:
            alt = GoogleTranslator(source="auto", target=self._tgt).translate(text)
            out = (alt or "").strip()
            if out and not _looks_like_translator_garbage(out):
                return out
        except Exception as exc:
            errors.append(f"google-auto: {exc}")

        detail = " | ".join(errors[:3]) if errors else "unknown"
        raise TranslationError(
            f"No translation ({self._src}→{self._tgt}): {detail}"
        ) from None

    def _translate_mymemory(self, text: str) -> str:
        if MyMemoryTranslator is None:
            raise RuntimeError("MyMemoryTranslator unavailable")
        src = normalize_mymemory_lang(self._src)
        tgt = normalize_mymemory_lang(self._tgt)
        # MyMemory free tier ~500 chars
        chunk = text if len(text) <= 450 else text[:450]
        result = MyMemoryTranslator(source=src, target=tgt).translate(chunk)
        return (result or "").strip()


class TranslationError(Exception):
    """Raised when the translation backend fails for a chunk."""
