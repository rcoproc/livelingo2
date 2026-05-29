"""
transcribe.py
=============
Speech-to-text using faster-whisper (CTranslate2). Runs fully locally; the
model is downloaded automatically on first use and cached under
~/.cache/huggingface.
"""

from faster_whisper import WhisperModel


class Transcriber:
    def __init__(self, config, log=print):
        self.cfg = config
        log(
            f"Loading Whisper model '{config.WHISPER_MODEL}' "
            f"({config.WHISPER_DEVICE}/{config.WHISPER_COMPUTE_TYPE})..."
        )
        log("(first run downloads the model — this can take a few minutes)")
        # WhisperModel downloads + caches the model automatically if missing.
        self.model = WhisperModel(
            config.WHISPER_MODEL,
            device=config.WHISPER_DEVICE,
            compute_type=config.WHISPER_COMPUTE_TYPE,
            cpu_threads=config.WHISPER_CPU_THREADS,
        )

    def transcribe(self, audio):
        """
        Transcribe a 16 kHz mono float32 numpy array.

        Returns the recognized text (source language), stripped. Returns an
        empty string when nothing intelligible was detected.
        """
        segments, _info = self.model.transcribe(
            audio,
            language=self.cfg.SOURCE_LANG,
            beam_size=self.cfg.WHISPER_BEAM_SIZE,
            vad_filter=self.cfg.WHISPER_VAD_FILTER,
            # Each chunk is independent, so don't carry context across chunks —
            # this avoids text "bleeding" / repeating between utterances.
            condition_on_previous_text=False,
        )
        text = "".join(segment.text for segment in segments)
        return text.strip()
