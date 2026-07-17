"""
synthesize.py
=============
Text-to-speech using edge-tts (Microsoft Edge online neural voices, free).
edge-tts is async and streams MP3 audio; we collect it in memory and decode it
to a float32 numpy array with soundfile so it can be played by sounddevice.

Requires internet access.
"""

import asyncio
import io

import numpy as np
import soundfile as sf

from .synthesis_error import SynthesisError
from .tts_segments import split_tts_segments


# Elegant defaults when TTS_VOICE_ALT is empty and user swaps languages ([g]).
DEFAULT_EDGE_VOICE_BY_LANG = {
    "en": "en-US-ChristopherNeural",
    "fr": "fr-FR-HenriNeural",
    "es": "es-ES-AlvaroNeural",
    "pt": "pt-BR-AntonioNeural",
    "de": "de-DE-ConradNeural",
    "it": "it-IT-DiegoNeural",
    "nl": "nl-NL-MaartenNeural",
    "ru": "ru-RU-DmitryNeural",
    "ja": "ja-JP-KeitaNeural",
    "ko": "ko-KR-InJoonNeural",
    "zh": "zh-CN-YunxiNeural",
    "ar": "ar-SA-HamedNeural",
    "hi": "hi-IN-MadhurNeural",
    "pl": "pl-PL-MarekNeural",
    "tr": "tr-TR-AhmetNeural",
}


def default_edge_voice_for_lang(lang_code):
    """Pick a solid meeting-style Edge voice for a 2-letter language code."""
    code = (lang_code or "").lower().strip()
    if code in DEFAULT_EDGE_VOICE_BY_LANG:
        return DEFAULT_EDGE_VOICE_BY_LANG[code]
    # Accept BCP-47 prefixes like pt-BR → pt
    if "-" in code:
        return DEFAULT_EDGE_VOICE_BY_LANG.get(code.split("-", 1)[0], "en-US-AriaNeural")
    return "en-US-AriaNeural"


class Synthesizer:
    def __init__(self, config):
        self.cfg = config
        self.voice = config.TTS_VOICE
        self.rate = config.TTS_RATE
        self.volume = config.TTS_VOLUME

    def set_voice(self, voice_id):
        """Change Edge voice after language swap ([g])."""
        if voice_id:
            self.voice = voice_id
            if hasattr(self.cfg, "TTS_VOICE"):
                self.cfg.TTS_VOICE = voice_id

    # ------------------------------------------------------------------ #
    async def _stream_mp3(self, text):
        """Stream the TTS audio for `text` into an in-memory MP3 buffer."""
        import edge_tts

        communicate = edge_tts.Communicate(
            text, voice=self.voice, rate=self.rate, volume=self.volume
        )
        buf = io.BytesIO()
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                buf.write(chunk["data"])
        buf.seek(0)
        return buf

    # ------------------------------------------------------------------ #
    def synthesize(self, text):
        """
        Synthesize `text` to speech.

        Returns (audio, sample_rate) where audio is a 1-D float32 numpy array,
        or (None, None) for empty input.
        """
        text = (text or "").strip()
        if not text:
            return None, None

        try:
            buf = asyncio.run(self._stream_mp3(text))
        except Exception as exc:
            raise SynthesisError(str(exc)) from exc

        if buf.getbuffer().nbytes == 0:
            raise SynthesisError("edge-tts returned no audio (empty stream).")

        # Decode the MP3 bytes. soundfile >= 0.12.1 bundles MP3-capable libsndfile.
        try:
            audio, sample_rate = sf.read(buf, dtype="float32")
        except Exception as exc:
            raise SynthesisError(
                "Could not decode TTS audio. Ensure soundfile>=0.12.1 is "
                f"installed (MP3 support). Original error: {exc}"
            ) from exc

        # Downmix to mono if needed.
        if audio.ndim > 1:
            audio = audio.mean(axis=1).astype(np.float32)
        return audio, sample_rate

    def synthesize_clause(self, text, on_segment):
        """Synthesize one pre-split clause."""
        audio, sample_rate = self.synthesize(text)
        if audio is not None and len(audio) > 0:
            on_segment(audio, sample_rate)
        return audio, sample_rate

    def synthesize_streaming(self, text, on_segment):
        """
        Synthesize text in segments and call on_segment(audio, sample_rate)
        for each part as soon as it is ready. Returns the full concatenated audio.
        """
        segments = split_tts_segments(text)
        if not segments:
            return None, None

        parts = []
        sample_rate = None
        for segment in segments:
            audio, sample_rate = self.synthesize(segment)
            if audio is not None and len(audio) > 0:
                on_segment(audio, sample_rate)
                parts.append(audio)

        if not parts:
            return None, None
        return np.concatenate(parts).astype(np.float32), sample_rate


def _edge_voice_lang_prefix(voice: str) -> str:
    """
    Extract BCP-47 language from an edge-tts voice id.
    'fr-FR-HenriNeural' -> 'fr', 'es-ES-AlvaroNeural' -> 'es', 'en-US-AriaNeural' -> 'en'.
    """
    parts = (voice or "").strip().split("-")
    if not parts or not parts[0]:
        return ""
    return parts[0].lower()


def warn_tts_voice_language(config, log=print):
    """
    Warn when TTS_VOICE locale does not match TARGET_LANG.

    Edge multilingual voices can still *speak* foreign text, but keep the
    training accent of the voice locale (e.g. es-ES voice reading French → Spanish accent).
    """
    engine = (getattr(config, "TTS_ENGINE", "edge") or "edge").lower()
    if engine not in ("edge", "hybrid", ""):
        return
    voice = getattr(config, "TTS_VOICE", "") or ""
    target = (getattr(config, "TARGET_LANG", "") or "").lower().strip()
    if not voice or not target:
        return
    voice_lang = _edge_voice_lang_prefix(voice)
    if not voice_lang or voice_lang == target:
        return
    # Multilingual voices still carry the locale accent of the base tag.
    log(
        f"[warn] TTS_VOICE='{voice}' is locale '{voice_lang}' but "
        f"TARGET_LANG='{target}'. Text is fine; spoken accent will sound like "
        f"{voice_lang.upper()}. Set TTS_VOICE to a {target}-* Neural voice "
        f"(e.g. edge-tts --list-voices)."
    )


def build_synthesizer(config, log=print):
    """
    Return a TTS backend from config.TTS_ENGINE:
      edge   -> Microsoft edge-tts (online, default)
      piper  -> Piper ONNX (local); falls back to edge on error
      hybrid -> edge first chunk + Piper tail (or TTS_HYBRID=true with piper)
    """
    warn_tts_voice_language(config, log=log)

    engine = (getattr(config, "TTS_ENGINE", "edge") or "edge").lower()
    use_hybrid = engine == "hybrid" or (
        engine == "piper" and getattr(config, "TTS_HYBRID", False)
    )
    if use_hybrid:
        try:
            from .hybrid_tts import HybridSynthesizer

            synth = HybridSynthesizer(config, log=log)
            log(
                f"Text-to-speech: hybrid (edge first + Piper tail, "
                f"{synth.piper.voice_id})."
            )
            return synth
        except Exception as exc:
            log(f"Hybrid TTS unavailable ({exc}) — trying Piper only.")
            engine = "piper"

    if engine == "piper":
        try:
            from .piper_tts import PiperSynthesizer

            return PiperSynthesizer(config, log=log)
        except Exception as exc:
            log(f"Piper TTS unavailable ({exc}) — falling back to edge-tts.")
    log(f"Text-to-speech: edge-tts ({getattr(config, 'TTS_VOICE', '')}).")
    return Synthesizer(config)
