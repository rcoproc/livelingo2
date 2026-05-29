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

import edge_tts
import numpy as np
import soundfile as sf


class Synthesizer:
    def __init__(self, config):
        self.cfg = config
        self.voice = config.TTS_VOICE
        self.rate = config.TTS_RATE
        self.volume = config.TTS_VOLUME

    # ------------------------------------------------------------------ #
    async def _stream_mp3(self, text):
        """Stream the TTS audio for `text` into an in-memory MP3 buffer."""
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

        # edge-tts is asyncio-based; run its coroutine to completion here.
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


class SynthesisError(Exception):
    """Raised when text-to-speech fails for a chunk."""
