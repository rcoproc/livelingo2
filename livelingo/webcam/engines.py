"""
Lip-sync engines: pluggable backends for mouth ROI synthesis.

Modes
-----
passthrough
    No model — recompose original crop (validates capture → vcam path).
amplitude
    Lightweight: vertical lip open driven by TTS audio RMS (no GPU model).
    Good demo / low-latency fallback when ONNX weights are missing.
onnx
    Generic ONNX Runtime session (CUDA / TensorRT EP when available, FP16).
    Expects a model with inputs compatible with the adapter below; replace
    ``OnnxLipSyncEngine.infer`` mapping if your export differs (Wav2Lip-style).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, Optional, Tuple

import numpy as np

from .face_roi import MouthROI


class LipSyncEngine(ABC):
    """Interface: mouth crop + audio → new mouth crop (BGR uint8)."""

    name: str = "base"

    @abstractmethod
    def infer(
        self,
        mouth_bgr: np.ndarray,
        audio: np.ndarray,
        sample_rate: int,
        roi: MouthROI,
    ) -> np.ndarray: ...

    def close(self) -> None:
        pass


class PassthroughEngine(LipSyncEngine):
    name = "passthrough"

    def infer(self, mouth_bgr, audio, sample_rate, roi):
        return mouth_bgr


class AmplitudeEngine(LipSyncEngine):
    """
    Cheap visual lip motion: expand/contract the mouth band by audio energy.

    Not photoreal — keeps <5 ms CPU and proves A/V coupling before loading
    a heavy Wav2Lip/LivePortrait ONNX export.
    """

    name = "amplitude"

    def __init__(self, max_open_px: float = 22.0, sensitivity: float = 28.0):
        # Defaults tuned to be *visible* on Teams preview (was too subtle at 10/12).
        self.max_open_px = float(max_open_px)
        self.sensitivity = float(sensitivity)

    def infer(self, mouth_bgr, audio, sample_rate, roi):
        if mouth_bgr is None or mouth_bgr.size == 0:
            return mouth_bgr
        try:
            import cv2
        except Exception:
            return mouth_bgr

        rms = 0.0
        if audio is not None and getattr(audio, "size", 0) > 0:
            rms = float(np.sqrt(np.mean(np.square(audio.astype(np.float32))) + 1e-12))
        # Peak-normalize short window so quiet TTS still moves lips
        if audio is not None and getattr(audio, "size", 0) > 16:
            peak = float(np.max(np.abs(audio.astype(np.float32))) + 1e-8)
            rms = float(
                np.sqrt(np.mean(np.square(audio.astype(np.float32) / peak)) + 1e-12)
            )
        open_amt = float(np.clip(rms * self.sensitivity, 0.0, 1.0))
        # Floor only when real energy present (caller gates on TTS playing)
        if 0.05 < open_amt < 0.15:
            open_amt = 0.15
        dy = open_amt * self.max_open_px
        if dy < 0.35:
            return mouth_bgr

        h, w = mouth_bgr.shape[:2]
        # Natural lip open: vertical stretch around mid only (no black cavity paint)
        map_x = np.tile(np.arange(w, dtype=np.float32), (h, 1))
        ys = np.arange(h, dtype=np.float32)
        mid = (h - 1) * 0.5
        # Prefer landmark lip center inside crop when ROI known
        if (
            roi is not None
            and getattr(roi, "mouth_cy", 0)
            and getattr(roi, "y0", None) is not None
        ):
            try:
                mid = float(np.clip(roi.mouth_cy - roi.y0, 0, h - 1))
            except Exception:
                pass
        scale = 1.0 + 0.55 * open_amt
        src_y = mid + (ys - mid) / scale
        src_y = src_y + np.where(ys > mid, dy * 0.4, -dy * 0.18)
        map_y = np.tile(src_y.reshape(-1, 1), (1, w)).astype(np.float32)
        warped = cv2.remap(
            mouth_bgr,
            map_x,
            map_y,
            interpolation=cv2.INTER_LINEAR,
            borderMode=cv2.BORDER_REPLICATE,
        )
        return warped


class OnnxLipSyncEngine(LipSyncEngine):
    """
    Generic ONNX Runtime engine (CUDA → TensorRT → CPU providers).

    Default I/O contract (override via config or subclass if needed)::

        inputs:
          face:  float32 [1, 3, H, W]  RGB normalized 0..1  (mouth crop resized)
          audio: float32 [1, T]       mono waveform peak-normalized
        outputs:
          face_out: float32 [1, 3, H, W] RGB 0..1

    Many public Wav2Lip ONNX exports use mel spectrograms instead of raw audio.
    Point ``preprocess`` / ``postprocess`` hooks or ship a matching export.
    """

    name = "onnx"

    def __init__(
        self,
        model_path: str,
        providers: Optional[list] = None,
        input_size: Tuple[int, int] = (96, 96),
        use_fp16: bool = True,
        log=print,
    ):
        self.model_path = model_path
        self.input_size = (int(input_size[0]), int(input_size[1]))
        self.use_fp16 = bool(use_fp16)
        self._log = log
        self._session = None
        self._in_names: list[str] = []
        self._out_names: list[str] = []
        self._err: Optional[str] = None
        self._load(providers)

    def _load(self, providers: Optional[list]) -> None:
        try:
            import onnxruntime as ort
        except Exception as exc:
            self._err = f"onnxruntime not installed: {exc}"
            self._log(self._err)
            return
        if providers is None:
            providers = []
            avail = ort.get_available_providers()
            # Prefer TensorRT → CUDA → CPU
            for p in (
                "TensorrtExecutionProvider",
                "CUDAExecutionProvider",
                "DmlExecutionProvider",
                "CPUExecutionProvider",
            ):
                if p in avail:
                    providers.append(p)
            if not providers:
                providers = ["CPUExecutionProvider"]
        so = ort.SessionOptions()
        so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        try:
            self._session = ort.InferenceSession(
                self.model_path, sess_options=so, providers=providers
            )
            self._in_names = [i.name for i in self._session.get_inputs()]
            self._out_names = [o.name for o in self._session.get_outputs()]
            self._log(
                f"Lip ONNX loaded: {self.model_path} providers={self._session.get_providers()}"
            )
        except Exception as exc:
            self._err = str(exc)
            self._log(f"Lip ONNX load failed: {exc}")
            self._session = None

    @property
    def available(self) -> bool:
        return self._session is not None

    @property
    def error(self) -> Optional[str]:
        return self._err

    def _prep_face(self, mouth_bgr: np.ndarray) -> np.ndarray:
        import cv2

        h, w = self.input_size
        rgb = cv2.cvtColor(mouth_bgr, cv2.COLOR_BGR2RGB)
        rgb = cv2.resize(rgb, (w, h), interpolation=cv2.INTER_AREA)
        x = rgb.astype(np.float32) / 255.0
        x = np.transpose(x, (2, 0, 1))[None, ...]  # NCHW
        if self.use_fp16:
            try:
                x = x.astype(np.float16)
            except Exception:
                pass
        return x

    def _prep_audio(self, audio: np.ndarray, sample_rate: int) -> np.ndarray:
        if audio is None or getattr(audio, "size", 0) == 0:
            a = np.zeros((1, 1600), dtype=np.float32)
        else:
            a = np.asarray(audio, dtype=np.float32).reshape(1, -1)
            peak = float(np.max(np.abs(a)) + 1e-8)
            a = a / peak
        if self.use_fp16:
            try:
                a = a.astype(np.float16)
            except Exception:
                pass
        return a

    def infer(self, mouth_bgr, audio, sample_rate, roi):
        if self._session is None:
            return mouth_bgr
        import cv2

        face_in = self._prep_face(mouth_bgr)
        audio_in = self._prep_audio(audio, sample_rate)
        feeds: Dict[str, Any] = {}
        # Heuristic name matching for common exports
        for name in self._in_names:
            nl = name.lower()
            if "audio" in nl or "mel" in nl or "wav" in nl or "speech" in nl:
                feeds[name] = audio_in.astype(np.float32)
            else:
                feeds[name] = face_in.astype(np.float32)
        try:
            outs = self._session.run(self._out_names, feeds)
        except Exception:
            # Retry float32 only
            feeds = {k: np.asarray(v, dtype=np.float32) for k, v in feeds.items()}
            try:
                outs = self._session.run(self._out_names, feeds)
            except Exception:
                return mouth_bgr
        if not outs:
            return mouth_bgr
        y = np.asarray(outs[0])
        # Accept NCHW or NHWC
        if y.ndim == 4:
            if y.shape[1] in (1, 3):  # NCHW
                y = np.transpose(y[0], (1, 2, 0))
            else:
                y = y[0]
        elif y.ndim == 3:
            pass
        else:
            return mouth_bgr
        y = np.clip(y.astype(np.float32), 0.0, 1.0)
        if y.shape[-1] == 1:
            y = np.repeat(y, 3, axis=-1)
        rgb = (y * 255.0).astype(np.uint8)
        bgr = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
        mh, mw = mouth_bgr.shape[:2]
        if bgr.shape[0] != mh or bgr.shape[1] != mw:
            bgr = cv2.resize(bgr, (mw, mh), interpolation=cv2.INTER_LINEAR)
        return bgr

    def close(self) -> None:
        self._session = None


def build_engine(config, log=print) -> LipSyncEngine:
    """Factory from config: WEBCAM_LIP_ENGINE = passthrough | amplitude | onnx."""
    mode = (getattr(config, "WEBCAM_LIP_ENGINE", "amplitude") or "amplitude").lower()
    if mode in ("off", "none", "passthrough", "pass"):
        log("Webcam lip engine: passthrough (no morph).")
        return PassthroughEngine()
    if mode in ("amplitude", "amp", "rms", "demo"):
        log("Webcam lip engine: amplitude (CPU RMS morph).")
        return AmplitudeEngine(
            max_open_px=float(getattr(config, "WEBCAM_AMP_MAX_OPEN_PX", 10.0) or 10.0),
            sensitivity=float(getattr(config, "WEBCAM_AMP_SENSITIVITY", 12.0) or 12.0),
        )
    if mode in ("onnx", "wav2lip", "model"):
        path = (getattr(config, "WEBCAM_ONNX_MODEL", "") or "").strip()
        if not path:
            log(
                "WEBCAM_LIP_ENGINE=onnx but WEBCAM_ONNX_MODEL empty — "
                "falling back to amplitude."
            )
            return AmplitudeEngine()
        size = int(getattr(config, "WEBCAM_ONNX_INPUT_SIZE", 96) or 96)
        eng = OnnxLipSyncEngine(
            model_path=path,
            input_size=(size, size),
            use_fp16=bool(getattr(config, "WEBCAM_ONNX_FP16", True)),
            log=log,
        )
        if not eng.available:
            log(f"ONNX engine unavailable ({eng.error}) — amplitude fallback.")
            return AmplitudeEngine()
        return eng
    log(f"Unknown WEBCAM_LIP_ENGINE={mode!r} — amplitude.")
    return AmplitudeEngine()
