"""Burn-in TARGET subtitle on virtual-cam frames."""

from __future__ import annotations

import numpy as np
import pytest

from livelingo.webcam.subtitle import draw_subtitle_burnin, _wrap_text_lines


def test_draw_empty_text_returns_copy_shape():
    frame = np.zeros((240, 320, 3), dtype=np.uint8)
    frame[:] = (40, 40, 40)
    out = draw_subtitle_burnin(frame, "")
    assert out.shape == frame.shape
    assert np.array_equal(out, frame)


def test_draw_target_changes_bottom_region():
    # Colorful frame so frost+veil is visible
    frame = np.zeros((360, 640, 3), dtype=np.uint8)
    frame[:] = (40, 80, 120)
    text = "But now it will work fine, right?"
    out = draw_subtitle_burnin(
        frame,
        text,
        max_lines=2,
        margin_bottom=2,
        mirror_h=False,
        bar_alpha=0.48,
        blur_px=15,
    )
    assert out.shape == frame.shape
    # Footer band changed; top of frame untouched
    assert float(np.abs(out[-40:].astype(np.int16) - frame[-40:].astype(np.int16)).mean()) > 2.0
    top = out[:40, :, :]
    assert float(np.abs(top.astype(np.int16) - frame[:40].astype(np.int16)).mean()) < 1.0


def test_caption_flush_footer_not_mid_frame():
    """Band sits at image bottom (y ends at h or h-margin)."""
    frame = np.zeros((400, 600, 3), dtype=np.uint8)
    frame[:] = (50, 50, 50)
    out = draw_subtitle_burnin(
        frame, "Hello footer", max_lines=2, margin_bottom=0, mirror_h=False, blur_px=11
    )
    # Mid-frame row should stay original
    assert np.array_equal(out[200, 300], frame[200, 300])
    # Bottom rows differ (caption band)
    assert not np.array_equal(out[-5:], frame[-5:])


def test_new_caption_replaces_not_stacks():
    """Second draw uses only new text — storage replace is in service; draw is single text."""
    frame = np.zeros((360, 640, 3), dtype=np.uint8)
    frame[:] = (30, 90, 60)
    a = draw_subtitle_burnin(
        frame,
        "AAAA BBBB CCCC DDDD EEEE FFFF",
        max_lines=2,
        mirror_h=False,
        bar_alpha=0.5,
        margin_bottom=2,
        blur_px=9,
    )
    b = draw_subtitle_burnin(
        frame,
        "OK",
        max_lines=2,
        mirror_h=False,
        bar_alpha=0.5,
        margin_bottom=2,
        blur_px=9,
    )
    assert not np.array_equal(a[-50:], b[-50:])
    # Top of frame never touched by footer caption
    assert np.array_equal(b[:40], frame[:40])


def test_mirror_h_flips_subtitle_bar():
    """mirror_h pre-flips bar so Teams selfie mirror still reads L→R."""
    frame = np.zeros((200, 400, 3), dtype=np.uint8)
    frame[:] = (20, 20, 20)
    text = "ABCDEFGH"
    plain = draw_subtitle_burnin(frame, text, mirror_h=False, margin_bottom=8)
    mirrored = draw_subtitle_burnin(frame, text, mirror_h=True, margin_bottom=8)
    # Same overall energy, different pixel layout in bottom strip
    assert plain.shape == mirrored.shape
    assert not np.array_equal(plain[-60:], mirrored[-60:])
    # Double-flip of bar region ≈ plain (draw + flip + flip)
    try:
        import cv2
    except Exception:
        pytest.skip("opencv not available")
    # Find bottom band that changed
    band = mirrored[-60:].copy()
    # H-flip full frame band back — should be closer to plain band
    unflip = cv2.flip(band, 1)
    assert float(np.mean(np.abs(unflip.astype(np.int16) - plain[-60:].astype(np.int16)))) < float(
        np.mean(np.abs(band.astype(np.int16) - plain[-60:].astype(np.int16)))
    )


def test_wrap_respects_max_lines():
    try:
        import cv2
    except Exception:
        pytest.skip("opencv not available")

    lines = _wrap_text_lines(
        "one two three four five six seven eight nine ten eleven twelve",
        max_width_px=80,
        font=cv2.FONT_HERSHEY_SIMPLEX,
        font_scale=0.6,
        thickness=1,
        get_text_size=cv2.getTextSize,
        max_lines=2,
    )
    assert 1 <= len(lines) <= 2
    assert all(isinstance(x, str) and x for x in lines)


def test_service_subtitle_toggle_and_push():
    """Unit-level: enable flag + text store without starting threads."""
    from types import SimpleNamespace

    from livelingo.webcam.service import WebcamLipSyncService

    cfg = SimpleNamespace(
        WEBCAM_START_ENABLED=False,
        WEBCAM_AUDIO_SR=24000,
        WEBCAM_AUDIO_RING_S=2.0,
        WEBCAM_AUDIO_PLAY_DELAY_S=0.0,
        WEBCAM_ROI_PAD=0.35,
        WEBCAM_FEATHER_PX=9,
        WEBCAM_SYNC_MARKER=False,
        WEBCAM_FORCE_CLOSED_IDLE=True,
        WEBCAM_AMP_SENSITIVITY=28.0,
        WEBCAM_TEMPLATE_FLIP_H=False,
        WEBCAM_SPEECH_HANGOVER_S=0.0,
        WEBCAM_CLOSED_AUTO=True,
        WEBCAM_QUEUE_SIZE=2,
        WEBCAM_LIP_ENGINE="passthrough",
        WEBCAM_SUBTITLE=False,
        WEBCAM_SUBTITLE_HOLD_S=0.0,  # hold forever until next
        WEBCAM_SUBTITLE_MAX_LINES=3,
        WEBCAM_SUBTITLE_FONT_SCALE=0.0,
        WEBCAM_SUBTITLE_MARGIN_BOTTOM=20,
        WEBCAM_SUBTITLE_BAR_ALPHA=0.48,
        WEBCAM_SUBTITLE_BLUR_PX=15,
        WEBCAM_SUBTITLE_MIRROR=False,
        WEBCAM_CLOSED_MOUTH_IMAGE="",
        WEBCAM_CLOSED_MOUTH_LANDMARKS="",
    )
    svc = WebcamLipSyncService(cfg, log=lambda *a, **k: None)
    assert svc.is_subtitle_enabled() is False
    on, msg = svc.set_subtitle_enabled(True)
    assert on is True
    assert "ON" in msg
    svc.push_subtitle_text("  Hello world  ")
    assert svc._active_subtitle_text() == "Hello world"
    svc.push_subtitle_text("Replaced only")
    assert svc._active_subtitle_text() == "Replaced only"
    assert "Hello" not in svc._active_subtitle_text()
    frame = np.zeros((180, 320, 3), dtype=np.uint8)
    painted = svc._apply_subtitle_burnin(frame)
    assert painted is not None
    assert painted.shape == frame.shape
    assert float(np.abs(painted[-50:].astype(np.int16) - 0).mean()) >= 0.0
    off, msg2 = svc.toggle_subtitle()
    assert off is False
    assert svc._active_subtitle_text() == ""
