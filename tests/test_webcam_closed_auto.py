"""Auto mouth-closed: audio ETA + want_closed_mouth_auto truth table."""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pytest

from livelingo.webcam.audio_ring import AudioRingBuffer


def test_seconds_until_play_idle_upcoming_playing():
    ring = AudioRingBuffer(sample_rate=16000, max_seconds=4.0)
    assert ring.seconds_until_play() is None

    now = time.monotonic()
    # Schedule a clip that starts in 2.0s
    with ring._lock:
        from livelingo.webcam.audio_ring import _Clip

        samples = np.zeros(16000, dtype=np.float32)  # 1s
        ring._clips.append(
            _Clip(samples=samples, sample_rate=16000, start_t=now + 2.0, end_t=now + 3.0)
        )
    eta = ring.seconds_until_play(now=now)
    assert eta is not None
    assert 1.9 <= eta <= 2.1

    # While playing
    assert ring.seconds_until_play(now=now + 2.5) == 0.0

    # After finished, idle
    assert ring.seconds_until_play(now=now + 3.5) is None


def test_want_closed_mouth_auto_truth_table():
    from livelingo.webcam.service import WebcamLipSyncService

    cfg = SimpleNamespace(
        WEBCAM_CLOSED_AUTO=True,
        WEBCAM_MOUTH_RELEASE_LEAD_S=1.0,
        WEBCAM_CLOSED_UNTIL_TTS=True,
        WEBCAM_CLOSED_REGION="mouth",
        WEBCAM_SPEECH_HANGOVER_S=1.5,
        WEBCAM_FORCE_CLOSED_IDLE=True,
        WEBCAM_TEMPLATE_FLIP_H=False,
        WEBCAM_QUEUE_SIZE=2,
        WEBCAM_LIP_ENGINE="passthrough",
        WEBCAM_AUDIO_PLAY_DELAY_S=0.0,
        WEBCAM_SYNC_MARKER=False,
        WEBCAM_SUBTITLE=False,
        WEBCAM_SUBTITLE_HOLD_S=0.0,
        WEBCAM_SUBTITLE_MAX_LINES=2,
        WEBCAM_SUBTITLE_FONT_SCALE=0.0,
        WEBCAM_SUBTITLE_MARGIN_BOTTOM=2,
        WEBCAM_SUBTITLE_BAR_ALPHA=0.48,
        WEBCAM_SUBTITLE_BLUR_PX=0,
        WEBCAM_SUBTITLE_MIRROR=True,
        SAMPLE_RATE=16000,
    )
    # Avoid heavy start — construct lightly by bypassing __init__ deps if needed
    svc = object.__new__(WebcamLipSyncService)
    svc.cfg = cfg
    svc._closed_auto = True
    svc._mouth_release_lead_s = 1.0
    svc._closed_until_tts = True
    svc._closed_region = "mouth"
    svc._vad_hangover_s = 1.5
    svc._vad_speech = __import__("threading").Event()
    svc._vad_lock = __import__("threading").Lock()
    svc._vad_end_mono = 0.0
    svc._closed_manual_mode = False
    svc._closed_manual_on = False
    svc._closed_full_frame_on = False
    svc._template_store = MagicMock()
    svc._template_store.loaded = True
    svc.audio = MagicMock()

    # No speech, no TTS
    svc._vad_speech.clear()
    svc.audio.seconds_until_play.return_value = None
    svc.audio.is_playing.return_value = False
    assert svc.want_closed_mouth_auto() is False

    # Speaking
    svc._vad_speech.set()
    assert svc.want_closed_mouth_auto() is True

    # Speaking but TTS starts in 0.5s → release
    svc.audio.seconds_until_play.return_value = 0.5
    assert svc.want_closed_mouth_auto() is False

    # TTS far away, silence after speech → still closed (until TTS)
    svc._vad_speech.clear()
    svc.audio.seconds_until_play.return_value = 3.0
    assert svc.want_closed_mouth_auto() is True

    # Playing TTS
    svc.audio.is_playing.return_value = True
    assert svc.want_closed_mouth_auto() is False

    # Manual F10 mode disables auto
    svc.audio.is_playing.return_value = False
    svc._vad_speech.set()
    svc.audio.seconds_until_play.return_value = None
    svc._closed_manual_mode = True
    assert svc.want_closed_mouth_auto() is False


def _mouth_landmarks(cx: float, cy: float, half_w: float = 20.0, angle_deg: float = 0.0):
    import math

    pts = np.zeros((478, 2), dtype=np.float32)
    rad = math.radians(angle_deg)
    c, s = math.cos(rad), math.sin(rad)

    def rot(dx: float, dy: float):
        return (cx + dx * c - dy * s, cy + dx * s + dy * c)

    pts[61] = rot(-half_w, 0.0)
    pts[291] = rot(half_w, 0.0)
    pts[13] = rot(0.0, -6.0)
    pts[14] = rot(0.0, 6.0)
    # Outer lip ring (enough for hull mask)
    for i, idx in enumerate(
        (61, 146, 91, 181, 84, 17, 314, 405, 321, 375, 291, 308, 324, 318, 402, 317, 14, 87, 178, 88, 95)
    ):
        t = (i / 21.0) * 2.0 * math.pi
        pts[idx] = rot(half_w * math.cos(t), 8.0 * math.sin(t))
    return pts


def test_align_and_blend_mouth_smoke():
    from livelingo.webcam.face_roi import MouthROI
    from livelingo.webcam.mouth_template import MouthTemplate, align_and_blend_mouth

    live = np.full((240, 320, 3), 40, dtype=np.uint8)
    tpl_img = np.full((240, 320, 3), 200, dtype=np.uint8)
    # Paint a bright mouth blob on template
    tpl_img[110:130, 140:180] = (20, 20, 220)

    lm = _mouth_landmarks(160, 120)
    tpl = MouthTemplate(
        image_bgr=tpl_img,
        landmarks_xy=lm.copy(),
        mouth_cx=160,
        mouth_cy=120,
        mouth_w=40,
        mouth_h=16,
    )
    h, w = 240, 320
    roi = MouthROI(
        crop_bgr=live[100:140, 130:190].copy(),
        x0=130,
        y0=100,
        x1=190,
        y1=140,
        mask_full=np.zeros((h, w), dtype=np.float32),
        landmarks_xy=lm.copy(),
        face_ok=True,
        mouth_cx=160,
        mouth_cy=120,
        mouth_w=40,
        mouth_h=16,
        mesh_ok=True,
    )
    out = align_and_blend_mouth(live, roi, tpl, feather_px=8)
    assert out.shape == live.shape
    # Mask is biased downward — sample a bit below mouth center
    band = out[118:135, 150:170].astype(np.int16)
    assert int(band.max()) > 80
    # Above nose area should stay live gray
    assert int(out[95, 160].max()) <= 50


def test_align_and_blend_mouth_track_on_moves_with_center():
    pytest.importorskip("cv2")
    from livelingo.webcam.face_roi import MouthROI
    from livelingo.webcam.mouth_template import MouthTemplate, align_and_blend_mouth

    live = np.full((240, 320, 3), 30, dtype=np.uint8)
    tpl_img = np.full((240, 320, 3), 30, dtype=np.uint8)
    tpl_img[110:130, 140:180] = (10, 10, 240)

    tpl = MouthTemplate(
        image_bgr=tpl_img,
        landmarks_xy=_mouth_landmarks(160, 120),
        mouth_cx=160,
        mouth_cy=120,
        mouth_w=40,
        mouth_h=16,
    )

    def _roi(cx: int, cy: int) -> MouthROI:
        return MouthROI(
            crop_bgr=live[cy - 20 : cy + 20, cx - 30 : cx + 30].copy(),
            x0=cx - 30,
            y0=cy - 20,
            x1=cx + 30,
            y1=cy + 20,
            mask_full=np.zeros((240, 320), dtype=np.float32),
            landmarks_xy=_mouth_landmarks(cx, cy),
            face_ok=True,
            mouth_cx=cx,
            mouth_cy=cy,
            mouth_w=40,
            mouth_h=16,
            mesh_ok=True,
        )

    out_a = align_and_blend_mouth(live, _roi(160, 120), tpl, track=True, rotate=False)
    out_b = align_and_blend_mouth(live, _roi(220, 150), tpl, track=True, rotate=False)
    # New center should pick up the bright clone; old center should stay darkish
    assert int(out_b[150:165, 210:230].max()) > int(out_a[150:165, 210:230].max())
    assert int(out_b[118:130, 150:170].mean()) < int(out_a[118:130, 150:170].mean()) + 5


def test_align_and_blend_mouth_track_off_stays_at_snap():
    pytest.importorskip("cv2")
    from livelingo.webcam.face_roi import MouthROI
    from livelingo.webcam.mouth_template import MouthTemplate, align_and_blend_mouth

    live = np.full((240, 320, 3), 30, dtype=np.uint8)
    tpl_img = np.full((240, 320, 3), 30, dtype=np.uint8)
    tpl_img[110:130, 140:180] = (10, 10, 240)
    tpl = MouthTemplate(
        image_bgr=tpl_img,
        landmarks_xy=_mouth_landmarks(160, 120),
        mouth_cx=160,
        mouth_cy=120,
        mouth_w=40,
        mouth_h=16,
    )
    roi_moved = MouthROI(
        crop_bgr=live[130:170, 190:250].copy(),
        x0=190,
        y0=130,
        x1=250,
        y1=170,
        mask_full=np.zeros((240, 320), dtype=np.float32),
        landmarks_xy=_mouth_landmarks(220, 150),
        face_ok=True,
        mouth_cx=220,
        mouth_cy=150,
        mouth_w=40,
        mouth_h=16,
        mesh_ok=True,
    )
    out = align_and_blend_mouth(live, roi_moved, tpl, track=False)
    # Clone remains near snap center, not the moved ROI
    assert int(out[118:132, 150:170].max()) > 100
    assert int(out[148:162, 210:230].max()) < 80


def test_align_and_blend_mouth_rotate_smoke():
    pytest.importorskip("cv2")
    from livelingo.webcam.face_roi import MouthROI
    from livelingo.webcam.mouth_template import MouthTemplate, align_and_blend_mouth

    live = np.full((240, 320, 3), 30, dtype=np.uint8)
    tpl_img = np.full((240, 320, 3), 30, dtype=np.uint8)
    tpl_img[110:130, 140:180] = (10, 10, 240)
    tpl = MouthTemplate(
        image_bgr=tpl_img,
        landmarks_xy=_mouth_landmarks(160, 120, angle_deg=0.0),
        mouth_cx=160,
        mouth_cy=120,
        mouth_w=40,
        mouth_h=16,
    )
    roi = MouthROI(
        crop_bgr=live[100:140, 130:190].copy(),
        x0=130,
        y0=100,
        x1=190,
        y1=140,
        mask_full=np.zeros((240, 320), dtype=np.float32),
        landmarks_xy=_mouth_landmarks(160, 120, angle_deg=18.0),
        face_ok=True,
        mouth_cx=160,
        mouth_cy=120,
        mouth_w=40,
        mouth_h=16,
        mesh_ok=True,
    )
    out = align_and_blend_mouth(live, roi, tpl, track=True, rotate=True)
    assert out.shape == live.shape
    assert int(out[115:135, 145:175].max()) > 80


def test_resolve_mouth_track_pose_hold():
    from livelingo.webcam.face_roi import MouthROI
    from livelingo.webcam.service import WebcamLipSyncService

    svc = object.__new__(WebcamLipSyncService)
    svc._mouth_track = True
    svc._mouth_track_smooth = 1.0  # raw
    svc._mouth_track_hold_s = 0.35
    svc._mouth_track_rotate = True
    svc._mouth_track_lock = __import__("threading").Lock()
    svc._mouth_track_raw = None
    svc._mouth_track_smooth_pose = None

    lm = _mouth_landmarks(160, 120)
    good = MouthROI(
        crop_bgr=np.zeros((40, 60, 3), dtype=np.uint8),
        x0=130,
        y0=100,
        x1=190,
        y1=140,
        mask_full=np.zeros((240, 320), dtype=np.float32),
        landmarks_xy=lm,
        face_ok=True,
        mouth_cx=160,
        mouth_cy=120,
        mouth_w=40,
        mouth_h=16,
        mesh_ok=True,
    )
    t0 = 1000.0
    p0 = svc.resolve_mouth_track_pose(good, now=t0)
    assert abs(p0.cx - 160) < 3

    # Heuristic miss shortly after — must hold previous center
    bad = MouthROI(
        crop_bgr=np.zeros((40, 60, 3), dtype=np.uint8),
        x0=80,
        y0=140,
        x1=240,
        y1=210,
        mask_full=np.zeros((240, 320), dtype=np.float32),
        landmarks_xy=_mouth_landmarks(160, 175),
        face_ok=True,
        mouth_cx=160,
        mouth_cy=175,
        mouth_w=80,
        mouth_h=20,
        mesh_ok=False,
    )
    p1 = svc.resolve_mouth_track_pose(bad, now=t0 + 0.1)
    assert abs(p1.cx - p0.cx) < 3
    assert abs(p1.cy - p0.cy) < 3

    # After hold expires, may follow weak pose
    p2 = svc.resolve_mouth_track_pose(bad, now=t0 + 1.0)
    assert abs(p2.cy - 175) < 8 or abs(p2.cy - p0.cy) < 8  # either path ok
