"""
Webcam lip-sync package for LiveLingo2
======================================

Low-latency pipeline:

  webcam capture  ──► face ROI (MediaPipe) ──► lip engine (ONNX / amp) ──► vcam
  TTS audio ring  ─────────────────────────────▲

Optional extras (install only when enabling the feature)::

    pip install opencv-python mediapipe pyvirtualcam onnxruntime-gpu

Toggle at runtime: ``[cam]`` / ``[cam on]`` / ``[cam off]`` / ``[cam status]``.

See ``docs/webcam-lipsync.md`` for drivers (OBS Virtual Cam / v4l2loopback)
and model packaging notes.
"""

from __future__ import annotations

from .service import WebcamLipSyncService, build_webcam_service, check_webcam_deps

__all__ = [
    "WebcamLipSyncService",
    "build_webcam_service",
    "check_webcam_deps",
]
