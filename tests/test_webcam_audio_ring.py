"""Unit tests for webcam audio schedule + amplitude engine (no OpenCV required)."""

from __future__ import annotations

import time

import numpy as np
import pytest

from livelingo.webcam.audio_ring import AudioRingBuffer
from livelingo.webcam.engines import AmplitudeEngine, PassthroughEngine, build_engine
from livelingo.webcam.face_roi import MouthROI
from livelingo.webcam.service import check_webcam_deps


def test_audio_ring_push_latest_and_rms():
    ring = AudioRingBuffer(sample_rate=16000, max_seconds=2.0)
    ring.set_play_delay(0.0)
    silence = np.zeros(1600, dtype=np.float32)
    tone = np.ones(1600, dtype=np.float32) * 0.5
    ring.push(silence, 16000)
    ring.push(tone, 16000)
    # Timeline: silence 0.1s then tone 0.1s — wait into the tone
    time.sleep(0.15)
    assert ring.is_playing()
    samples, sr = ring.latest(0.08)
    assert sr == 16000
    assert samples.size > 0
    assert ring.rms(0.08) > 0.1


def test_audio_ring_idle_after_clip_ends():
    ring = AudioRingBuffer(sample_rate=16000, max_seconds=1.0)
    ring.set_play_delay(0.0)
    # 50 ms of audio only
    short = np.ones(800, dtype=np.float32) * 0.4
    ring.push(short, 16000)
    time.sleep(0.03)
    assert ring.is_playing()
    time.sleep(0.12)
    assert not ring.is_playing()
    samples, _ = ring.latest(0.1)
    assert samples.size == 0
    assert ring.rms(0.1) == 0.0


def test_audio_ring_mixdown_and_resample():
    ring = AudioRingBuffer(sample_rate=16000, max_seconds=2.0)
    ring.set_play_delay(0.0)
    # 1.0 s of stereo @ 8k → 16k mono after resample
    stereo = np.stack([np.ones(8000), np.zeros(8000)], axis=-1).astype(np.float32)
    ring.push(stereo, 8000)
    time.sleep(0.12)
    samples, sr = ring.latest(0.1)
    assert sr == 16000
    assert samples.size > 0
    assert ring.is_playing()


def test_audio_ring_empty():
    ring = AudioRingBuffer(sample_rate=24000, max_seconds=0.5)
    s, sr = ring.latest(0.2)
    assert s.size == 0
    assert ring.rms() == 0.0
    assert not ring.is_playing()


def test_passthrough_and_amplitude_engines():
    mouth = np.zeros((40, 60, 3), dtype=np.uint8)
    mouth[10:30, 10:50] = 128
    roi = MouthROI(
        crop_bgr=mouth,
        x0=0,
        y0=0,
        x1=60,
        y1=40,
        mask_full=np.ones((40, 60), dtype=np.float32),
        landmarks_xy=np.zeros((0, 2), dtype=np.float32),
        face_ok=True,
    )
    audio = np.ones(1000, dtype=np.float32) * 0.4
    p = PassthroughEngine()
    assert p.infer(mouth, audio, 16000, roi) is mouth or np.array_equal(
        p.infer(mouth, audio, 16000, roi), mouth
    )
    a = AmplitudeEngine(max_open_px=8.0, sensitivity=20.0)
    out = a.infer(mouth, audio, 16000, roi)
    assert out.shape == mouth.shape
    assert out.dtype == np.uint8


def test_heuristic_mouth_roi_when_no_mediapipe():
    from livelingo.webcam.face_roi import FaceMouthROI

    frame = np.zeros((240, 320, 3), dtype=np.uint8)
    frame[:] = 40
    roi_h = FaceMouthROI()
    # Force heuristic path without importing mediapipe (lazy init)
    roi_h._init_attempted = True
    roi_h._mesh = None
    roi_h._cv2 = None
    roi_h._mp = None
    r = roi_h.process(frame)
    assert r.face_ok is True
    assert r.crop_bgr.size > 0
    assert r.x1 > r.x0 and r.y1 > r.y0


def test_draw_sync_marker_active_and_idle():
    from livelingo.webcam.face_roi import FaceMouthROI

    frame = np.zeros((240, 320, 3), dtype=np.uint8)
    frame[:] = 30
    roi = MouthROI(
        crop_bgr=frame[120:200, 80:240].copy(),
        x0=80,
        y0=120,
        x1=240,
        y1=200,
        mask_full=np.ones((240, 320), dtype=np.float32),
        landmarks_xy=np.zeros((0, 2), dtype=np.float32),
        face_ok=True,
        mouth_cx=160,
        mouth_cy=160,
        mouth_w=60,
        mouth_h=10,
    )
    idle = FaceMouthROI.draw_sync_marker(frame.copy(), roi, open_amt=0.0, active=False)
    assert idle.shape == frame.shape
    active = FaceMouthROI.draw_sync_marker(frame.copy(), roi, open_amt=0.7, active=True)
    assert active.shape == frame.shape
    # Active marker should paint non-background pixels near mouth center
    assert not np.array_equal(active, frame)


def test_paint_mouth_open_is_noop():
    """Cartoon cavity disabled — must not paint black/red overlays."""
    from livelingo.webcam.face_roi import FaceMouthROI

    frame = np.full((240, 320, 3), 180, dtype=np.uint8)
    roi = MouthROI(
        crop_bgr=frame[140:190, 120:200].copy(),
        x0=120,
        y0=140,
        x1=200,
        y1=190,
        mask_full=np.ones((240, 320), dtype=np.float32),
        landmarks_xy=np.zeros((0, 2), dtype=np.float32),
        face_ok=True,
        mouth_cx=160,
        mouth_cy=165,
        mouth_w=50,
        mouth_h=8,
    )
    out = FaceMouthROI.paint_mouth_open(frame.copy(), roi, open_amt=0.8)
    assert np.array_equal(out, frame)


def _fake_lip_landmarks():
    from livelingo.webcam.face_roi import INNER_LIP_IDX

    n = max(max(INNER_LIP_IDX), 291) + 1
    pts = np.zeros((n, 2), dtype=np.float32)
    pts[13] = (160, 155)
    pts[14] = (160, 170)
    pts[61] = (140, 160)
    pts[291] = (180, 160)
    for idx in INNER_LIP_IDX:
        t = (idx % 12) / 12.0 * 2 * np.pi
        pts[idx] = (160 + 18 * np.cos(t), 162 + 8 * np.sin(t))
    return pts


def _rich_lip_landmarks():
    """Outer upper/lower arcs for column-wise lip code."""
    from livelingo.webcam.face_roi import (
        INNER_LIP_IDX,
        LOWER_OUTER_LIP,
        UPPER_OUTER_LIP,
    )

    n = max(max(INNER_LIP_IDX), max(UPPER_OUTER_LIP), max(LOWER_OUTER_LIP), 291) + 1
    pts = np.zeros((n, 2), dtype=np.float32)
    # Upper arc y≈155, lower arc y≈170, x 140..180
    for i, idx in enumerate(UPPER_OUTER_LIP):
        t = i / max(1, len(UPPER_OUTER_LIP) - 1)
        pts[idx] = (140 + t * 40, 155 + 2 * np.sin(t * np.pi))
    for i, idx in enumerate(LOWER_OUTER_LIP):
        t = i / max(1, len(LOWER_OUTER_LIP) - 1)
        pts[idx] = (140 + t * 40, 170 - 2 * np.sin(t * np.pi))
    pts[13] = (160, 155)
    pts[14] = (160, 170)
    pts[61] = (140, 162)
    pts[291] = (180, 162)
    for idx in INNER_LIP_IDX:
        t = (idx % 12) / 12.0 * 2 * np.pi
        pts[idx] = (160 + 16 * np.cos(t), 162 + 6 * np.sin(t))
    return pts


def test_force_mouth_closed_seals_gap_keeps_texture():
    from livelingo.webcam.face_roi import FaceMouthROI

    rng = np.random.default_rng(0)
    frame = np.full((240, 320, 3), 160, dtype=np.uint8)
    # Textured lips + dark gap between y 157..168
    frame[150:175, 130:190] = rng.integers(100, 200, (25, 60, 3), dtype=np.uint8)
    frame[157:169, 145:175] = (25, 20, 30)
    pts = _rich_lip_landmarks()
    roi = MouthROI(
        crop_bgr=frame[140:190, 120:200].copy(),
        x0=120,
        y0=140,
        x1=200,
        y1=190,
        mask_full=np.ones((240, 320), dtype=np.float32),
        landmarks_xy=pts,
        face_ok=True,
        mouth_cx=160,
        mouth_cy=162,
        mouth_w=40,
        mouth_h=15,
    )
    out = FaceMouthROI.force_mouth_closed(frame.copy(), roi)
    assert out.shape == frame.shape
    # Gap sealed (not pure black)
    assert int(out[162, 160].mean()) > 50
    # Lip band still has texture variance
    assert float(out[150:156, 145:175].astype(np.float32).std()) > 5.0


def test_animate_speaking_thin_slit_not_black_blob():
    from livelingo.webcam.face_roi import FaceMouthROI

    rng = np.random.default_rng(1)
    frame = np.full((240, 320, 3), 170, dtype=np.uint8)
    frame[150:175, 130:190] = rng.integers(120, 200, (25, 60, 3), dtype=np.uint8)
    pts = _rich_lip_landmarks()
    roi = MouthROI(
        crop_bgr=frame[140:190, 120:200].copy(),
        x0=120,
        y0=140,
        x1=200,
        y1=190,
        mask_full=np.ones((240, 320), dtype=np.float32),
        landmarks_xy=pts,
        face_ok=True,
        mouth_cx=160,
        mouth_cy=162,
        mouth_w=40,
        mouth_h=15,
    )
    closed = FaceMouthROI.force_mouth_closed(frame.copy(), roi)
    talking = FaceMouthROI.animate_speaking(closed, roi, open_amt=0.85)
    # Opening must change center column
    d = np.abs(
        talking[155:175, 155:165].astype(np.float32)
        - closed[155:175, 155:165].astype(np.float32)
    )
    assert float(d.mean()) > 1.0 or float(d.max()) > 10.0
    # Must NOT paint a huge pure-black oval (center not near-zero everywhere)
    center = talking[158:168, 150:170]
    assert float(center.mean()) > 15.0


def test_clear_tts_audio_stops_playing():
    from livelingo.webcam.service import WebcamLipSyncService

    class Cfg:
        WEBCAM_START_ENABLED = False
        WEBCAM_LIP_ENGINE = "passthrough"
        WEBCAM_AUDIO_SR = 16000
        WEBCAM_AUDIO_RING_S = 1.0
        WEBCAM_AUDIO_PLAY_DELAY_S = 0.0
        WEBCAM_QUEUE_SIZE = 2
        WEBCAM_ROI_PAD = 0.3
        WEBCAM_FEATHER_PX = 5
        WEBCAM_SYNC_MARKER = False
        WEBCAM_FORCE_CLOSED_IDLE = True

    svc = WebcamLipSyncService(Cfg(), log=lambda *_: None)
    svc.audio.set_play_delay(0.0)
    svc._started = True
    svc.push_tts_audio(np.ones(8000, dtype=np.float32) * 0.3, 16000)
    time.sleep(0.05)
    assert svc.audio.is_playing()
    svc.clear_tts_audio()
    assert not svc.audio.is_playing()


def test_build_engine_modes():
    class Cfg:
        WEBCAM_LIP_ENGINE = "passthrough"

    e = build_engine(Cfg(), log=lambda *_: None)
    assert e.name == "passthrough"

    class Cfg2:
        WEBCAM_LIP_ENGINE = "amplitude"
        WEBCAM_AMP_MAX_OPEN_PX = 5.0
        WEBCAM_AMP_SENSITIVITY = 10.0

    e2 = build_engine(Cfg2(), log=lambda *_: None)
    assert e2.name == "amplitude"

    class Cfg3:
        WEBCAM_LIP_ENGINE = "onnx"
        WEBCAM_ONNX_MODEL = ""  # missing → amplitude fallback

    e3 = build_engine(Cfg3(), log=lambda *_: None)
    assert e3.name == "amplitude"


def test_check_webcam_deps_dict():
    d = check_webcam_deps()
    assert "cv2" in d and "mediapipe" in d and "pyvirtualcam" in d
    assert "errors" in d


def test_teams_setup_hint_mentions_cable_and_obs():
    from livelingo.webcam.service import teams_setup_hint

    h = teams_setup_hint().lower()
    assert "cable" in h
    assert "obs" in h or "virtual" in h


def test_push_tts_audio_accepts_before_enable():
    """Lip schedule should accept TTS even if cam not yet enabled (sync buffer)."""
    from livelingo.webcam.service import WebcamLipSyncService

    class Cfg:
        WEBCAM_START_ENABLED = False
        WEBCAM_LIP_ENGINE = "passthrough"
        WEBCAM_AUDIO_SR = 16000
        WEBCAM_AUDIO_RING_S = 1.0
        WEBCAM_AUDIO_PLAY_DELAY_S = 0.0
        WEBCAM_QUEUE_SIZE = 2
        WEBCAM_ROI_PAD = 0.3
        WEBCAM_FEATHER_PX = 5
        WEBCAM_SYNC_MARKER = True

    svc = WebcamLipSyncService(Cfg(), log=lambda *_: None)
    svc.audio.set_play_delay(0.0)
    # Not started: push is no-op
    svc.push_tts_audio(np.ones(100, dtype=np.float32), 16000)
    assert svc.audio.latest(0.1)[0].size == 0
    # Mark started without threads — 0.5 s tone so sleep has room
    svc._started = True
    svc.push_tts_audio(np.ones(8000, dtype=np.float32) * 0.3, 16000)
    time.sleep(0.08)
    samples, _ = svc.audio.latest(0.05)
    assert samples.size > 0
    assert svc.audio.is_playing()
