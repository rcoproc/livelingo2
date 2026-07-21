"""
vad_silero.py
=============
Optional Silero VAD (ONNX) for more accurate speech detection than RMS energy.
Falls back to energy VAD if onnxruntime or the model file is unavailable.

Model: https://github.com/snakers4/silero-vad
"""

import os
import urllib.request

import numpy as np

SILERO_MODEL_URL = (
    "https://github.com/snakers4/silero-vad/raw/master/"
    "src/silero_vad/data/silero_vad.onnx"
)
SILERO_WINDOW = 512


class SileroVAD:
    """Streaming Silero VAD using ONNX Runtime (no PyTorch required)."""

    def __init__(self, sample_rate=16000, threshold=0.5, log=print):
        if sample_rate != 16000:
            raise ValueError("Silero VAD only supports 16 kHz in this integration.")
        self.sample_rate = sample_rate
        self.threshold = threshold
        self._context = np.zeros((1, 64), dtype=np.float32)
        self._state = np.zeros((2, 1, 128), dtype=np.float32)
        self._pending = np.zeros(0, dtype=np.float32)
        self._session = self._load_session(log)

    def _model_path(self):
        cache_dir = os.path.join(".cache", "models")
        os.makedirs(cache_dir, exist_ok=True)
        return os.path.join(cache_dir, "silero_vad.onnx")

    def _load_session(self, log):
        try:
            import onnxruntime as ort
        except ImportError as exc:
            raise RuntimeError(
                "Silero VAD requires onnxruntime. Install with: pip install onnxruntime"
            ) from exc

        path = self._model_path()
        if not os.path.exists(path):
            log("Downloading Silero VAD model (one-time)...")
            urllib.request.urlretrieve(SILERO_MODEL_URL, path)

        return ort.InferenceSession(
            path, providers=["CPUExecutionProvider"]
        )

    def _run_window(self, window):
        window = np.ascontiguousarray(window, dtype=np.float32).reshape(1, -1)
        out, state = self._session.run(
            None,
            {
                "input": window,
                "state": self._state,
                "sr": np.array(self.sample_rate, dtype=np.int64),
            },
        )
        self._state = state
        return float(out.reshape(-1)[0])

    def speech_probability(self, block):
        """Return speech probability in [0, 1] for a float32 mono block."""
        if block is None or block.size == 0:
            return 0.0

        self._pending = np.concatenate((self._pending, block.astype(np.float32)))
        prob = 0.0
        while self._pending.size >= SILERO_WINDOW:
            window = self._pending[:SILERO_WINDOW]
            self._pending = self._pending[SILERO_WINDOW:]
            prob = self._run_window(window)
        return prob

    def is_speech(self, block):
        return self.speech_probability(block) >= self.threshold

    def reset(self):
        self._context = np.zeros((1, 64), dtype=np.float32)
        self._state = np.zeros((2, 1, 128), dtype=np.float32)
        self._pending = np.zeros(0, dtype=np.float32)