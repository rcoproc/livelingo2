"""Tests for closed-mouth photo template align/blend."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from livelingo.webcam.face_roi import MouthROI
from livelingo.webcam.mouth_template import (
    MouthTemplate,
    align_and_blend,
    load_template,
    open_from_closed_template,
    save_template_from_frame,
)


def _landmarks(n: int = 478) -> np.ndarray:
    pts = np.zeros((n, 2), dtype=np.float32)
    # Rough face: mouth around (160, 200)
    pts[61] = (130, 200)
    pts[291] = (190, 200)
    pts[13] = (160, 190)
    pts[14] = (160, 210)
    pts[0] = (160, 188)
    pts[17] = (160, 230)
    for i in range(n):
        if pts[i].sum() == 0:
            pts[i] = (100 + (i % 50), 150 + (i % 40))
    return pts


def _roi(pts: np.ndarray) -> MouthROI:
    frame_h, frame_w = 360, 480
    return MouthROI(
        crop_bgr=np.zeros((40, 60, 3), dtype=np.uint8),
        x0=120,
        y0=170,
        x1=200,
        y1=240,
        mask_full=np.ones((frame_h, frame_w), dtype=np.float32),
        landmarks_xy=pts,
        face_ok=True,
        mouth_cx=160,
        mouth_cy=200,
        mouth_w=60,
        mouth_h=20,
    )


def test_save_and_load_template(tmp_path: Path):
    frame = np.full((360, 480, 3), 120, dtype=np.uint8)
    frame[180:220, 130:190] = (80, 60, 90)  # dark-ish lips
    pts = _landmarks()
    roi = _roi(pts)
    img_p = tmp_path / "closed.png"
    lm_p = tmp_path / "closed.json"
    ok, msg = save_template_from_frame(
        frame, roi, image_path=str(img_p), landmarks_path=str(lm_p)
    )
    assert ok, msg
    assert img_p.is_file()
    assert lm_p.is_file()
    tpl = load_template(str(img_p), str(lm_p))
    assert tpl is not None
    assert tpl.ok
    assert tpl.mouth_cx == 160


def test_align_and_blend_changes_mouth_region():
    # Template with green mouth plate
    tpl_img = np.full((360, 480, 3), 100, dtype=np.uint8)
    tpl_img[160:250, 100:220] = (0, 255, 0)
    pts = _landmarks()
    # Extra anchors used by large-region affine
    if pts.shape[0] > 454:
        pts[152] = (160, 250)
        pts[234] = (90, 200)
        pts[454] = (230, 200)
        pts[1] = (160, 150)
    tpl = MouthTemplate(
        image_bgr=tpl_img,
        landmarks_xy=pts.copy(),
        mouth_cx=160,
        mouth_cy=200,
        mouth_w=60,
        mouth_h=20,
        path="mem",
    )
    # Live with blue mouth, same landmarks → blend should pull green in
    live = np.full((360, 480, 3), 100, dtype=np.uint8)
    live[160:250, 100:220] = (255, 0, 0)
    roi = _roi(pts)
    out = align_and_blend(
        live, roi, tpl, color_match=False, region_scale=2.4, feather_px=24
    )
    assert out.shape == live.shape
    # Center of mouth should not stay pure blue
    c = out[200, 160]
    assert not (int(c[0]) > 200 and int(c[1]) < 50 and int(c[2]) < 50)
    # Soft edge: far corner of frame stays near live background
    assert abs(int(out[10, 10].mean()) - 100) < 5


def test_template_horizontal_flip_mirrors_cx():
    pts = _landmarks()
    img = np.zeros((360, 480, 3), dtype=np.uint8)
    img[:, :240] = (0, 0, 255)  # left half red
    img[:, 240:] = (0, 255, 0)  # right half green
    tpl = MouthTemplate(
        image_bgr=img,
        landmarks_xy=pts,
        mouth_cx=120,
        mouth_cy=200,
        mouth_w=40,
        mouth_h=12,
        path="mem",
    )
    flipped = tpl.with_horizontal_flip()
    assert flipped.mouth_cx == 480 - 1 - 120
    # Left pixel of original becomes right after flip
    assert int(flipped.image_bgr[10, 10, 1]) > 200  # green on left after flip


def test_open_from_closed_moves_when_amt():
    frame = np.full((360, 480, 3), 140, dtype=np.uint8)
    rng = np.random.default_rng(2)
    frame[185:215, 130:190] = rng.integers(80, 180, (30, 60, 3), dtype=np.uint8)
    pts = _landmarks()
    roi = _roi(pts)
    closed = frame.copy()
    opened = open_from_closed_template(closed, roi, open_amt=0.9)
    d = np.abs(
        opened[190:210, 145:175].astype(np.float32)
        - closed[190:210, 145:175].astype(np.float32)
    )
    assert float(d.max()) > 5.0 or float(d.mean()) > 0.5
    # Not a pure black blob
    assert float(opened[200, 160].mean()) > 20.0


def test_save_rejects_no_face():
    frame = np.zeros((100, 100, 3), dtype=np.uint8)
    roi = MouthROI(
        crop_bgr=frame,
        x0=0,
        y0=0,
        x1=10,
        y1=10,
        mask_full=np.zeros((100, 100), dtype=np.float32),
        landmarks_xy=np.zeros((0, 2), dtype=np.float32),
        face_ok=False,
    )
    ok, msg = save_template_from_frame(frame, roi)
    assert not ok


def test_save_allows_heuristic_empty_landmarks(tmp_path: Path):
    frame = np.full((240, 320, 3), 100, dtype=np.uint8)
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
        mouth_w=50,
        mouth_h=12,
    )
    img_p = tmp_path / "h.png"
    lm_p = tmp_path / "h.json"
    ok, msg = save_template_from_frame(
        frame, roi, image_path=str(img_p), landmarks_path=str(lm_p), allow_heuristic=True
    )
    assert ok, msg
    assert "sem MediaPipe" in msg or img_p.is_file()
    data = json.loads(lm_p.read_text(encoding="utf-8"))
    assert len(data["landmarks"]) >= 100
