"""F11 full-frame closed photo covers the entire virtual-cam frame."""

from __future__ import annotations

import numpy as np
import pytest

from livelingo.webcam.mouth_template import MouthTemplate, cover_frame_with_closed_image


@pytest.fixture
def green_template():
    img = np.zeros((720, 1280, 3), dtype=np.uint8)
    img[:] = (10, 200, 10)
    lm = np.zeros((478, 2), dtype=np.float32)
    return MouthTemplate(img, lm, 640, 360, 80, 40, path="t")


def test_cover_hides_live_frame(green_template):
    live = np.zeros((480, 640, 3), dtype=np.uint8)
    live[:] = (0, 0, 250)  # red live
    out = cover_frame_with_closed_image(live, green_template, flip_h=False)
    assert out.shape == live.shape
    # Dominantly green (closed), not red (live)
    assert float(out[:, :, 1].mean()) > 150
    assert float(out[:, :, 2].mean()) < 40


def test_cover_matches_target_size(green_template):
    live = np.zeros((360, 640, 3), dtype=np.uint8)
    out = cover_frame_with_closed_image(live, green_template)
    assert out.shape == (360, 640, 3)


def test_cover_empty_template_returns_live():
    live = np.ones((100, 100, 3), dtype=np.uint8) * 7
    bad = MouthTemplate(
        np.zeros((0, 0, 3), dtype=np.uint8),
        np.zeros((0, 2), dtype=np.float32),
        0,
        0,
        0,
        0,
    )
    out = cover_frame_with_closed_image(live, bad)
    assert np.array_equal(out, live)
