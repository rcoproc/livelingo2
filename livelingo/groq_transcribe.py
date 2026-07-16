"""
groq_transcribe.py
==================
Speech-to-text via Groq's hosted Whisper (OpenAI-compatible audio endpoint).

Instead of running a small Whisper model on the local CPU, each captured audio
chunk is sent to Groq and transcribed with `whisper-large-v3` — far more
accurate (especially for non-English speech), very fast on Groq's hardware, and
free on a generous tier. This also offloads work from a weak local CPU.

Get a free API key (no credit card) at: https://console.groq.com/keys

Drop-in compatible with transcribe.Transcriber: exposes `.transcribe(audio)`
taking a 16 kHz mono float32 numpy array and returning the recognized text.
Uses `requests` (already installed) and `soundfile` (already installed) to
encode the chunk as an in-memory WAV.
"""

import io

import numpy as np
import requests
import soundfile as sf

GROQ_STT_URL = "https://api.groq.com/openai/v1/audio/transcriptions"


class GroqSTTError(Exception):
    """Raised when the Groq transcription request fails."""


class GroqTranscriber:
    def __init__(self, config, log=print):
        self.cfg = config
        self.api_key = config.GROQ_API_KEY
        self.model = config.GROQ_STT_MODEL
        self.language = config.SOURCE_LANG
        self.prompt = config.STT_INITIAL_PROMPT
        self.timeout = config.GROQ_STT_TIMEOUT
        self.sample_rate = config.SAMPLE_RATE
        self.session = requests.Session()
        log(f"Speech-to-text: Groq cloud ({self.model}).")

    # ------------------------------------------------------------------ #
    def _encode_wav(self, audio):
        """Encode a 16 kHz mono float32 array as in-memory 16-bit PCM WAV bytes."""
        audio = np.ascontiguousarray(audio, dtype=np.float32)
        buf = io.BytesIO()
        sf.write(buf, audio, self.sample_rate, format="WAV", subtype="PCM_16")
        buf.seek(0)
        return buf.read()

    # ------------------------------------------------------------------ #
    def transcribe(self, audio):
        """
        Transcribe a 16 kHz mono float32 numpy array via Groq.

        Returns the recognized text (source language), stripped. Returns an
        empty string for empty input. Raises GroqSTTError on API/network errors
        so the pipeline can log and skip the chunk.
        """
        if audio is None or len(audio) == 0:
            return ""

        wav_bytes = self._encode_wav(audio)

        files = {"file": ("chunk.wav", wav_bytes, "audio/wav")}
        form = {
            "model": self.model,
            "language": self.language,
            "temperature": "0",
            "response_format": "json",
        }
        if self.prompt:
            form["prompt"] = self.prompt
        headers = {"Authorization": f"Bearer {self.api_key}"}

        try:
            resp = self.session.post(
                GROQ_STT_URL,
                headers=headers,
                files=files,
                data=form,
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise GroqSTTError(f"network error contacting Groq: {exc}") from exc

        # Turn common HTTP errors into clear, actionable messages.
        if resp.status_code == 401:
            raise GroqSTTError("Groq rejected the API key (401). Check GROQ_API_KEY.")
        if resp.status_code == 404:
            raise GroqSTTError(
                f"Groq STT model '{self.model}' not found (404). Set a valid "
                f"GROQ_STT_MODEL (e.g. whisper-large-v3 or whisper-large-v3-turbo)."
            )
        if resp.status_code == 429:
            raise GroqSTTError(
                "Groq rate limit reached (429). Wait a moment, or switch to the "
                "local engine (STT_ENGINE=local)."
            )
        if resp.status_code >= 400:
            raise GroqSTTError(f"Groq error {resp.status_code}: {resp.text[:200]}")

        try:
            data = resp.json()
            text = data["text"]
        except (ValueError, KeyError) as exc:
            raise GroqSTTError(f"unexpected Groq response: {exc}") from exc

        return (text or "").strip()
