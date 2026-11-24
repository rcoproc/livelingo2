"""
Closed-mouth photo template: capture once, align + blend while mic listens.

Stores a full-frame (or ROI-padded) BGR image + MediaPipe landmarks, then
warps the mouth region onto the live face with a soft lip mask.

This is the high-quality idle path — real lip texture instead of procedural
seal/blur.
"""

from __future__ import annotations

import json
import math
import threading
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

from .face_roi import (
    _LIP_LEFT,
    _LIP_LOWER,
    _LIP_RIGHT,
    _LIP_UPPER,
    OUTER_LIP_IDX,
    FaceMouthROI,
    MouthROI,
)

# Lip-only anchors (jaw/cheek indices often wrong → vertical squash ~50%)
_AFFINE_IDX = (
    _LIP_LEFT,
    _LIP_RIGHT,
    _LIP_UPPER,
    _LIP_LOWER,
)

# MediaPipe Face Mesh face-oval (silhouette) — full-face freeze plate / F10.
# https://github.com/google/mediapipe/blob/master/mediapipe/python/solutions/face_mesh_connections.py
FACE_OVAL_IDX = (
    10,
    338,
    297,
    332,
    284,
    251,
    389,
    356,
    454,
    323,
    361,
    288,
    397,
    365,
    379,
    378,
    400,
    377,
    152,
    148,
    176,
    149,
    150,
    136,
    172,
    58,
    132,
    93,
    234,
    127,
    162,
    21,
    54,
    103,
    67,
    109,
)


@dataclass
class FreezePlateGeom:
    """Geometry for F10 / snap-closed full-face freeze region."""

    center: Tuple[int, int]  # (cx, cy)
    axes: Tuple[int, int]  # (half_w, half_h) for ellipse draw
    contour: Optional[np.ndarray] = None  # Nx1x2 int32 polygon (optional)
    mode: str = "face"  # face | mouth


@dataclass
class MouthTrackPose:
    """Stable mouth pose for closed-clone tracking (jaw-resistant pivot)."""

    cx: float
    cy: float
    w: float
    h: float
    angle_deg: float = 0.0
    landmarks_xy: Optional[np.ndarray] = None
    t: float = 0.0


@dataclass
class MouthTemplate:
    """Closed-mouth reference image + landmarks in the same pixel space."""

    image_bgr: np.ndarray  # full frame HxWx3 uint8
    landmarks_xy: np.ndarray  # Nx2 float32 (MediaPipe pixel coords)
    mouth_cx: int
    mouth_cy: int
    mouth_w: int
    mouth_h: int
    path: str = ""
    flipped_h: bool = False

    @property
    def ok(self) -> bool:
        # Image alone is enough to freeze (landmarks optional for affine).
        return self.image_bgr is not None and getattr(self.image_bgr, "size", 0) > 0

    def with_horizontal_flip(self) -> "MouthTemplate":
        """Mirror image + landmarks (Teams/selfie often opposite of OpenCV)."""
        if self.image_bgr is None or getattr(self.image_bgr, "size", 0) == 0:
            return self
        # Pure numpy so unit tests / CI work without cv2 for this helper.
        img = np.ascontiguousarray(self.image_bgr[:, ::-1])
        h, w = img.shape[:2]
        lm = np.zeros_like(self.landmarks_xy)
        if self.landmarks_xy is not None and self.landmarks_xy.size:
            lm = self.landmarks_xy.copy()
            lm[:, 0] = float(w - 1) - lm[:, 0]
        return MouthTemplate(
            image_bgr=img,
            landmarks_xy=lm,
            mouth_cx=int(w - 1 - self.mouth_cx),
            mouth_cy=int(self.mouth_cy),
            mouth_w=int(self.mouth_w),
            mouth_h=int(self.mouth_h),
            path=self.path,
            flipped_h=not self.flipped_h,
        )


def default_template_dir() -> Path:
    return Path(".cache") / "webcam"


def default_image_path() -> Path:
    return default_template_dir() / "closed_mouth.png"


def default_landmarks_path() -> Path:
    return default_template_dir() / "closed_mouth.json"


def lip_angle_deg(landmarks: Optional[np.ndarray]) -> float:
    """Angle of mouth left→right corners in degrees (0 = horizontal)."""
    if landmarks is None or getattr(landmarks, "size", 0) == 0:
        return 0.0
    try:
        pts = np.asarray(landmarks, dtype=np.float64).reshape(-1, 2)
        if pts.shape[0] <= max(_LIP_LEFT, _LIP_RIGHT):
            return 0.0
        left = pts[int(_LIP_LEFT)]
        right = pts[int(_LIP_RIGHT)]
        dx = float(right[0] - left[0])
        dy = float(right[1] - left[1])
        if abs(dx) + abs(dy) < 4.0:
            return 0.0
        return float(math.degrees(math.atan2(dy, dx)))
    except Exception:
        return 0.0


def stable_mouth_pose(
    landmarks: Optional[np.ndarray],
    *,
    fallback_cx: float,
    fallback_cy: float,
    fallback_w: float,
    fallback_h: float,
    t: float = 0.0,
) -> MouthTrackPose:
    """
    Jaw-resistant mouth pivot for closed-mouth cloning.

    Uses lip corners for cx/width/angle and upper-lip + small offset for cy
    (avoids following an open jaw while speaking PT).
    """
    cx = float(fallback_cx)
    cy = float(fallback_cy)
    mw = float(max(12.0, fallback_w))
    mh = float(max(4.0, fallback_h))
    angle = 0.0
    lm_out: Optional[np.ndarray] = None
    if landmarks is not None and getattr(landmarks, "size", 0) > 0:
        try:
            pts = np.asarray(landmarks, dtype=np.float64).reshape(-1, 2)
            lm_out = pts.astype(np.float32)
            need = max(_LIP_LEFT, _LIP_RIGHT, _LIP_UPPER, _LIP_LOWER)
            if pts.shape[0] > need:
                left = pts[int(_LIP_LEFT)]
                right = pts[int(_LIP_RIGHT)]
                upper = pts[int(_LIP_UPPER)]
                lower = pts[int(_LIP_LOWER)]
                span = float(abs(right[0] - left[0]) + abs(right[1] - left[1]))
                if span >= 8.0:
                    mw = float(max(12.0, abs(right[0] - left[0])))
                    mh = float(max(4.0, abs(lower[1] - upper[1])))
                    cx = float((left[0] + right[0]) * 0.5)
                    # Upper lip + small downward bias (not midpoint with open jaw)
                    cy = float(upper[1] + mw * 0.08)
                    angle = lip_angle_deg(pts)
        except Exception:
            pass
    return MouthTrackPose(
        cx=cx,
        cy=cy,
        w=mw,
        h=mh,
        angle_deg=angle,
        landmarks_xy=lm_out,
        t=float(t),
    )


def pose_from_roi(roi: MouthROI, *, t: float = 0.0) -> MouthTrackPose:
    """Build a stable track pose from a live MouthROI."""
    return stable_mouth_pose(
        getattr(roi, "landmarks_xy", None),
        fallback_cx=float(getattr(roi, "mouth_cx", 0) or 0),
        fallback_cy=float(getattr(roi, "mouth_cy", 0) or 0),
        fallback_w=float(getattr(roi, "mouth_w", 40) or 40),
        fallback_h=float(getattr(roi, "mouth_h", 12) or 12),
        t=t,
    )


def pose_from_template(template: MouthTemplate, *, t: float = 0.0) -> MouthTrackPose:
    """Snap / static pose (used when WEBCAM_MOUTH_TRACK=false)."""
    return stable_mouth_pose(
        getattr(template, "landmarks_xy", None),
        fallback_cx=float(getattr(template, "mouth_cx", 0) or 0),
        fallback_cy=float(getattr(template, "mouth_cy", 0) or 0),
        fallback_w=float(getattr(template, "mouth_w", 40) or 40),
        fallback_h=float(getattr(template, "mouth_h", 12) or 12),
        t=t,
    )


def ema_mouth_pose(
    prev: Optional[MouthTrackPose],
    cur: MouthTrackPose,
    alpha: float,
) -> MouthTrackPose:
    """Exponential moving average on pose scalars; landmarks take latest."""
    a = float(np.clip(alpha, 0.0, 1.0))
    if prev is None or a >= 0.999:
        return cur
    if a <= 0.001:
        return replace(prev, landmarks_xy=cur.landmarks_xy, t=cur.t)
    # Unwrap angle toward current to avoid 179↔-179 jumps
    d_ang = cur.angle_deg - prev.angle_deg
    while d_ang > 180.0:
        d_ang -= 360.0
    while d_ang < -180.0:
        d_ang += 360.0
    return MouthTrackPose(
        cx=prev.cx + a * (cur.cx - prev.cx),
        cy=prev.cy + a * (cur.cy - prev.cy),
        w=prev.w + a * (cur.w - prev.w),
        h=prev.h + a * (cur.h - prev.h),
        angle_deg=prev.angle_deg + a * d_ang,
        landmarks_xy=cur.landmarks_xy,
        t=cur.t,
    )


def _anchor_points(landmarks: np.ndarray) -> Optional[np.ndarray]:
    if landmarks is None or landmarks.shape[0] <= max(_AFFINE_IDX):
        return None
    try:
        pts = []
        for i in _AFFINE_IDX:
            if i < landmarks.shape[0]:
                pts.append(landmarks[i])
        if len(pts) < 3:
            return None
        return np.asarray(pts, dtype=np.float32)
    except Exception:
        return None


def synthetic_landmarks_from_box(
    h: int,
    w: int,
    cx: int,
    cy: int,
    mouth_w: int,
    mouth_h: int,
    n: int = 478,
) -> np.ndarray:
    """
    Build fake MediaPipe-sized landmarks from a mouth box.

    Enough for affine anchors (61/291/13/14/0/17) + outer lip hull so
    template snap works without MediaPipe face lock.
    """
    pts = np.zeros((n, 2), dtype=np.float32)
    # Spread default points so nothing is all zeros
    for i in range(n):
        pts[i, 0] = float(w) * 0.5 + (i % 17 - 8) * 2.0
        pts[i, 1] = float(h) * 0.55 + (i % 13 - 6) * 2.0
    half_w = max(8.0, float(mouth_w) * 0.5)
    half_h = max(4.0, float(mouth_h) * 0.5)
    pts[_LIP_LEFT] = (cx - half_w, cy)
    pts[_LIP_RIGHT] = (cx + half_w, cy)
    pts[_LIP_UPPER] = (cx, cy - half_h)
    pts[_LIP_LOWER] = (cx, cy + half_h)
    pts[0] = (cx, cy - half_h * 1.1)
    pts[17] = (cx, cy + half_h * 2.0)
    # Outer lip ring (ellipse)
    for i, idx in enumerate(OUTER_LIP_IDX):
        if idx >= n:
            continue
        t = (i / max(1, len(OUTER_LIP_IDX))) * 2.0 * np.pi
        pts[idx] = (cx + half_w * np.cos(t), cy + half_h * 1.2 * np.sin(t))
    return pts


def save_template_from_frame(
    frame_bgr: np.ndarray,
    roi: MouthROI,
    image_path: Optional[str] = None,
    landmarks_path: Optional[str] = None,
    *,
    allow_heuristic: bool = True,
) -> Tuple[bool, str]:
    """
    Persist closed-mouth frame + landmarks. Call when user has mouth closed.
    Returns (ok, message).
    """
    if frame_bgr is None or frame_bgr.size == 0:
        return False, "frame vazio"
    if roi is None or not roi.face_ok:
        return False, "rosto/boca não detectados — olhe para a câmera"

    h, w = frame_bgr.shape[:2]
    landmarks = roi.landmarks_xy
    note = ""
    if landmarks is None or landmarks.shape[0] < 100:
        if not allow_heuristic:
            return False, "landmarks insuficientes (precisa MediaPipe)"
        landmarks = synthetic_landmarks_from_box(
            h,
            w,
            int(roi.mouth_cx or w // 2),
            int(roi.mouth_cy or int(h * 0.65)),
            int(roi.mouth_w or 40),
            int(roi.mouth_h or 12),
        )
        note = " (sem MediaPipe — alinhamento por caixa da boca)"

    img_p = Path(image_path or default_image_path())
    lm_p = Path(landmarks_path or default_landmarks_path())
    try:
        img_p.parent.mkdir(parents=True, exist_ok=True)
        import cv2

        ok = cv2.imwrite(str(img_p), frame_bgr)
        if not ok:
            return False, f"falha ao gravar {img_p}"
        meta = {
            "mouth_cx": int(roi.mouth_cx),
            "mouth_cy": int(roi.mouth_cy),
            "mouth_w": int(roi.mouth_w),
            "mouth_h": int(roi.mouth_h),
            "landmarks": landmarks.astype(float).tolist(),
            "image": str(img_p).replace("\\", "/"),
            "heuristic": bool(note),
        }
        lm_p.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        return True, f"template salvo: {img_p}{note}"
    except Exception as exc:
        return False, f"erro ao salvar template: {exc}"


def load_template(
    image_path: Optional[str] = None,
    landmarks_path: Optional[str] = None,
) -> Optional[MouthTemplate]:
    img_p = Path(image_path or default_image_path())
    lm_p = Path(landmarks_path or default_landmarks_path())
    if not img_p.is_file():
        return None
    try:
        import cv2

        img = cv2.imread(str(img_p), cv2.IMREAD_COLOR)
        if img is None or img.size == 0:
            return None
        landmarks = np.zeros((0, 2), dtype=np.float32)
        mcx = mcy = mw = mh = 0
        if lm_p.is_file():
            meta = json.loads(lm_p.read_text(encoding="utf-8"))
            landmarks = np.asarray(meta.get("landmarks") or [], dtype=np.float32)
            if landmarks.ndim != 2 or landmarks.shape[1] != 2:
                landmarks = np.zeros((0, 2), dtype=np.float32)
            mcx = int(meta.get("mouth_cx") or 0)
            mcy = int(meta.get("mouth_cy") or 0)
            mw = int(meta.get("mouth_w") or 0)
            mh = int(meta.get("mouth_h") or 0)
        if landmarks.size == 0:
            # Image only — still usable with live center box fallback
            h, w = img.shape[:2]
            mcx = mcx or w // 2
            mcy = mcy or int(h * 0.65)
            mw = mw or max(40, w // 6)
            mh = mh or max(12, h // 20)
        tpl = MouthTemplate(
            image_bgr=img,
            landmarks_xy=landmarks,
            mouth_cx=mcx,
            mouth_cy=mcy,
            mouth_w=mw,
            mouth_h=mh,
            path=str(img_p),
        )
        return tpl if tpl.ok else None
    except Exception:
        return None


def compute_freeze_plate_geom(
    h: int,
    w: int,
    landmarks: Optional[np.ndarray],
    mouth_cx: int,
    mouth_cy: int,
    mouth_w: int,
    mouth_h: int = 12,
    *,
    region_scale: float = 1.15,
) -> FreezePlateGeom:
    """
    Full-face freeze geometry for F10 / ``cam snap closed`` overlay.

    Prefer MediaPipe face-oval; fallback ellipse from mouth size covering
    forehead → chin (user request: freeze whole face, not mouth-only oval).
    """
    scale = float(np.clip(float(region_scale) if region_scale else 1.15, 0.9, 1.45))
    mw = max(24, int(mouth_w or 40))
    mh = max(10, int(mouth_h or 12))
    mcx = int(np.clip(mouth_cx, 0, max(0, w - 1)))
    mcy = int(np.clip(mouth_cy, 0, max(0, h - 1)))

    # --- Face oval from landmarks (must be larger than mouth — reject garbage) ---
    if landmarks is not None and landmarks.shape[0] > max(FACE_OVAL_IDX):
        try:
            oval = landmarks[list(FACE_OVAL_IDX)].astype(np.float32)
            if oval.shape[0] >= 8:
                ow = float(oval[:, 0].max() - oval[:, 0].min())
                oh = float(oval[:, 1].max() - oval[:, 1].min())
                # Real face oval is much larger than lip width; tiny cloud = bad mesh
                if ow >= mw * 1.15 and oh >= mw * 1.0:
                    center = oval.mean(axis=0)
                    expanded = center + (oval - center) * scale
                    xs = expanded[:, 0]
                    ys = expanded[:, 1]
                    x0, x1 = float(np.percentile(xs, 2)), float(np.percentile(xs, 98))
                    y0, y1 = float(np.percentile(ys, 2)), float(np.percentile(ys, 98))
                    fcx = int(round(0.5 * (x0 + x1)))
                    fcy = int(round(0.5 * (y0 + y1)))
                    half_w = 0.5 * (x1 - x0)
                    half_h = 0.5 * (y1 - y0)
                    half_w = float(np.clip(half_w, mw * 1.0, w * 0.48))
                    half_h = float(np.clip(half_h, mw * 1.2, h * 0.48))
                    fcx = int(np.clip(fcx, int(half_w) + 2, w - int(half_w) - 2))
                    fcy = int(np.clip(fcy, int(half_h) + 2, h - int(half_h) - 2))
                    contour = expanded.reshape(-1, 1, 2).astype(np.int32)
                    return FreezePlateGeom(
                        center=(fcx, fcy),
                        axes=(
                            max(40, int(round(half_w))),
                            max(55, int(round(half_h))),
                        ),
                        contour=contour,
                        mode="face",
                    )
        except Exception:
            pass

    # --- Fallback: full-face ellipse from mouth metrics (always large enough) ---
    # ~2.2× mouth width, ~2.9× mouth width tall; center shifted up (eyes/forehead)
    half_w = float(np.clip(mw * 1.15 * scale, 70.0, w * 0.40))
    half_h = float(np.clip(mw * 1.40 * scale + mh * 0.8, 95.0, h * 0.42))
    fcx = mcx
    fcy = int(round(mcy - half_h * 0.28))
    fcx = int(np.clip(fcx, int(half_w) + 2, w - int(half_w) - 2))
    fcy = int(np.clip(fcy, int(half_h) + 2, h - int(half_h) - 2))
    return FreezePlateGeom(
        center=(fcx, fcy),
        axes=(max(40, int(round(half_w))), max(55, int(round(half_h)))),
        contour=None,
        mode="face",
    )


def _lower_face_mask(
    h: int,
    w: int,
    landmarks: Optional[np.ndarray],
    cx: int,
    cy: int,
    mouth_w: int,
    mouth_h: int,
    *,
    region_scale: float = 1.15,
    feather_px: int = 24,
) -> np.ndarray:
    """
    Soft full-face plate for F10 freeze (face oval + feather).

    Covers forehead → chin (not mouth-only). Caps keep a bit of background
    visible so the plate is not a full-frame rectangle.
    """
    import cv2

    mask = np.zeros((h, w), dtype=np.uint8)
    feather = max(8, min(48, int(feather_px)))
    geom = compute_freeze_plate_geom(
        h,
        w,
        landmarks,
        cx,
        cy,
        mouth_w,
        mouth_h,
        region_scale=region_scale,
    )
    fcx, fcy = geom.center
    ax, ay = geom.axes

    if geom.contour is not None and len(geom.contour) >= 6:
        try:
            # Filled convex hull of face oval (smooth silhouette)
            hull = cv2.convexHull(geom.contour)
            cv2.fillConvexPoly(mask, hull, 255, cv2.LINE_AA)
        except Exception:
            cv2.ellipse(mask, (fcx, fcy), (ax, ay), 0, 0, 360, 255, -1, cv2.LINE_AA)
    else:
        cv2.ellipse(mask, (fcx, fcy), (ax, ay), 0, 0, 360, 255, -1, cv2.LINE_AA)

    # Solid core so center of face never drops alpha
    core = max(16, min(ax, ay) // 3)
    cv2.circle(mask, (fcx, fcy), core, 255, -1, cv2.LINE_AA)

    if int(mask.max()) == 0:
        return np.zeros((h, w), dtype=np.float32)

    dist = cv2.distanceTransform(mask, cv2.DIST_L2, 5)
    ramp = max(4.0, float(feather) * 0.45)
    soft = np.clip(dist / ramp, 0.0, 1.0).astype(np.float32)
    k = max(3, min(15, (feather // 5) * 2 + 1))
    if k % 2 == 0:
        k += 1
    soft = cv2.GaussianBlur(soft, (k, k), 0)
    soft = np.clip(soft, 0.0, 1.0)
    soft[mask == 0] = 0.0
    return soft


def align_and_blend(
    live_bgr: np.ndarray,
    live_roi: MouthROI,
    template: MouthTemplate,
    *,
    feather_px: int = 24,
    region_scale: float = 1.15,
    color_match: bool = True,
    flip_h: bool = False,
) -> np.ndarray:
    """
    Freeze the **full face** from the closed-mouth photo onto the live frame (F10).

    Soft-feathered face oval (not mouth-only). Anchored by mouth center/width so
    the whole head tracks the live pose.

    ``flip_h``: mirror the template first (use when Teams view is mirrored
    vs the OpenCV capture used at snap time).
    """
    try:
        import cv2
    except Exception:
        return live_bgr
    if live_bgr is None or live_bgr.size == 0:
        return live_bgr
    if template is None or not template.ok:
        return live_bgr
    # face_ok optional — F10 must still freeze even if MediaPipe missed a frame

    if flip_h:
        template = template.with_horizontal_flip()

    out = live_bgr.copy()
    h, w = out.shape[:2]
    src = template.image_bgr

    # Uniform scale from mouth WIDTH (stable anchor for full-face plate).
    M = None
    try:
        tw = float(template.mouth_w or 40)
        lw = float(live_roi.mouth_w or 40)
        s = float(np.clip(lw / max(8.0, tw), 0.80, 1.35))
        tx = float(live_roi.mouth_cx) - s * float(template.mouth_cx or 0)
        ty = float(live_roi.mouth_cy) - s * float(template.mouth_cy or 0)
        M = np.array([[s, 0.0, tx], [0.0, s, ty]], dtype=np.float64)
    except Exception:
        return out

    try:
        warped = cv2.warpAffine(
            src,
            M,
            (w, h),
            flags=cv2.INTER_LINEAR,
            borderMode=cv2.BORDER_REFLECT_101,
        )
    except Exception:
        return out

    alpha = _lower_face_mask(
        h,
        w,
        live_roi.landmarks_xy,
        int(live_roi.mouth_cx),
        int(live_roi.mouth_cy),
        int(live_roi.mouth_w or 40),
        int(live_roi.mouth_h or 12),
        region_scale=region_scale,
        feather_px=feather_px,
    )

    # Color match: *luminance only*. Per-channel BGR scale (old code) over-boosted
    # R vs G/B under mixed lighting → fake "lipstick" / red mouth (see boca-aberta).
    if color_match and float(alpha.max()) > 0.05:
        try:
            core = alpha > 0.65
            ring = (alpha > 0.12) & (alpha < 0.55)
            if int(core.sum()) > 80:
                live_px = out[core].astype(np.float32)
                warp_px = warped[core].astype(np.float32)

                # Rec.601 luma on BGR order
                def _luma(px: np.ndarray) -> float:
                    return float(
                        0.114 * px[:, 0].mean()
                        + 0.587 * px[:, 1].mean()
                        + 0.299 * px[:, 2].mean()
                    )

                live_lum = _luma(live_px)
                warp_lum = _luma(warp_px)
                scale = (live_lum + 1.0) / (warp_lum + 1.0)
                scale = float(np.clip(scale, 0.88, 1.14))
                warped_f = warped.astype(np.float32) * scale
                # Edge: mild brightness nudge only (no chroma pull → no lipstick)
                if int(ring.sum()) > 40:
                    live_r = out[ring].astype(np.float32)
                    warp_r = warped_f[ring]
                    d_lum = _luma(live_r) - _luma(warp_r)
                    d_lum = float(np.clip(d_lum * 0.35, -18.0, 18.0))
                    edge_w = np.clip(
                        (0.55 - np.abs(alpha - 0.35)) / 0.35, 0.0, 1.0
                    ).astype(np.float32)
                    warped_f = warped_f + d_lum * edge_w[:, :, None]
                warped = np.clip(warped_f, 0, 255).astype(np.uint8)
        except Exception:
            pass

    a3 = alpha[:, :, None]
    blended = warped.astype(np.float32) * a3 + out.astype(np.float32) * (1.0 - a3)
    out = blended.clip(0, 255).astype(np.uint8)
    return out


def _mouth_only_mask(
    h: int,
    w: int,
    landmarks: Optional[np.ndarray],
    cx: int,
    cy: int,
    mouth_w: int,
    mouth_h: int,
    *,
    pad_scale: float = 1.05,
    pad_x: float = 1.00,
    pad_up: float = 0.70,
    pad_down: float = 1.25,
    shift_down_px: float = 0.0,
    feather_px: int = 8,
) -> np.ndarray:
    """
    Soft alpha mask covering **lips only** (tracks live mouth pose).

    ``pad_up`` / ``pad_down`` / ``pad_x`` multiply distance from the mouth
    center to each outer-lip landmark (1.0 ≈ natural lip hull). Hard caps
    keep the top at the real upper-lip line (no nose/glasses plate).
    """
    import cv2

    mask = np.zeros((h, w), dtype=np.uint8)
    feather = max(3, min(16, int(feather_px)))
    scale = float(np.clip(pad_scale, 0.7, 1.3))
    px = float(np.clip(pad_x, 0.75, 1.30)) * scale
    # Multipliers on lip geometry. LOWER pad_up → less nose/glasses.
    pu = float(np.clip(pad_up, 0.35, 1.20)) * scale
    pd = float(np.clip(pad_down, 0.70, 1.50)) * scale
    mw = float(max(mouth_w, 16))
    mh_raw = float(max(mouth_h, 6))
    pivot_x = float(cx)
    pivot_y = float(cy) + float(shift_down_px)
    # Tight caps — upper especially (nose/glasses were bleeding in)
    max_up = mw * 0.08
    max_down = mw * 0.26
    max_x = mw * 0.55
    drawn = False
    if landmarks is not None and getattr(landmarks, "size", 0) > 0:
        try:
            pts = np.asarray(landmarks, dtype=np.float32).reshape(-1, 2)
            lip = []
            for idx in OUTER_LIP_IDX:
                if 0 <= int(idx) < pts.shape[0]:
                    lip.append(pts[int(idx)])
            if len(lip) >= 6:
                arr = np.array(lip, dtype=np.float32)
                span = float(np.ptp(arr[:, 0]) + np.ptp(arr[:, 1]))
                if span >= 8.0:
                    lip_top = float(arr[:, 1].min())
                    lip_bot = float(arr[:, 1].max())
                    out_pts = np.empty_like(arr)
                    dx = arr[:, 0] - pivot_x
                    dy = arr[:, 1] - pivot_y
                    out_pts[:, 0] = pivot_x + np.clip(dx * px, -max_x, max_x)
                    above = dy < 0
                    # Never expand above the real upper-lip landmark (+1px slack)
                    out_pts[above, 1] = np.maximum(
                        pivot_y + dy[above] * min(pu, 1.0),
                        lip_top - 1.0,
                    )
                    out_pts[above, 1] = np.maximum(
                        out_pts[above, 1], pivot_y - max_up
                    )
                    out_pts[~above, 1] = pivot_y + np.minimum(
                        dy[~above] * pd, max_down
                    )
                    # Hard cut: nothing above real upper lip
                    out_pts[:, 1] = np.maximum(out_pts[:, 1], lip_top - 1.0)
                    # Soft floor near lower lip
                    out_pts[:, 1] = np.minimum(
                        out_pts[:, 1], max(lip_bot + mw * 0.06, pivot_y + max_down)
                    )
                    hull = cv2.convexHull(out_pts.astype(np.int32))
                    cv2.fillConvexPoly(mask, hull, 255, cv2.LINE_AA)
                    # Pixel hard-cut above upper lip (belt + suspenders)
                    y_cut = int(max(0, math.floor(lip_top - 1.0)))
                    if y_cut > 0:
                        mask[:y_cut, :] = 0
                    drawn = True
        except Exception:
            drawn = False
    if not drawn:
        ax = max(8, int(round(min(max_x, mw * 0.38 * px))))
        ay_up = max(3, int(round(min(max_up, mh_raw * 0.45 * pu))))
        ay_dn = max(6, int(round(min(max_down, mh_raw * 0.85 * pd))))
        cy_draw = int(round(pivot_y + (ay_dn - ay_up) * 0.25))
        ay = max(ay_up, ay_dn)
        cv2.ellipse(
            mask,
            (int(pivot_x), cy_draw),
            (ax, ay),
            0,
            0,
            360,
            255,
            -1,
            cv2.LINE_AA,
        )
        y_cut = int(round(pivot_y - max_up))
        if 0 <= y_cut < h:
            mask[:y_cut, :] = 0

    if int(mask.max()) == 0:
        return np.zeros((h, w), dtype=np.float32)

    dist = cv2.distanceTransform(mask, cv2.DIST_L2, 5)
    ramp = max(2.0, float(feather) * 0.5)
    soft = np.clip(dist / ramp, 0.0, 1.0).astype(np.float32)
    k = max(3, min(9, (feather // 4) * 2 + 1))
    if k % 2 == 0:
        k += 1
    soft = cv2.GaussianBlur(soft, (k, k), 0)
    soft = np.clip(soft, 0.0, 1.0)
    soft[mask == 0] = 0.0
    return soft


def align_and_blend_mouth(
    live_bgr: np.ndarray,
    live_roi: MouthROI,
    template: MouthTemplate,
    *,
    feather_px: int = 8,
    pad_scale: float = 1.05,
    pad_x: float = 1.00,
    pad_up: float = 0.70,
    pad_down: float = 1.25,
    shift_down_px: float = 0.0,
    color_match: bool = True,
    flip_h: bool = False,
    track: bool = True,
    rotate: bool = True,
    pose: Optional[MouthTrackPose] = None,
) -> np.ndarray:
    """
    Clone **closed mouth only** from the template onto the live face.

    Eyes / cheeks / forehead stay live. With ``track=True`` (default), the
    mouth region follows a stable mouth-center pose (scale + optional
    rotation) so head motion keeps lips closed. With ``track=False``, the
    clone stays at the snap pose (does not follow the head).

    ``pose``: optional precomputed / smoothed / held track pose from the
    service; when omitted, derived from ``live_roi`` (or template if not tracking).
    """
    try:
        import cv2
    except Exception:
        return live_bgr
    if live_bgr is None or live_bgr.size == 0:
        return live_bgr
    if template is None or not template.ok:
        return live_bgr

    if flip_h:
        template = template.with_horizontal_flip()

    out = live_bgr.copy()
    h, w = out.shape[:2]
    src = template.image_bgr

    tpl_pose = pose_from_template(template)
    if track:
        # Prefer *current* live Face Mesh every frame (hold/EMA only if mesh lost).
        if getattr(live_roi, "mesh_ok", False):
            live_pose = pose_from_roi(live_roi)
            # Use MediaPipe mouth_cx/cy directly — most responsive head follow
            try:
                live_pose = replace(
                    live_pose,
                    cx=float(live_roi.mouth_cx),
                    cy=float(live_roi.mouth_cy),
                    w=float(live_roi.mouth_w or live_pose.w),
                    h=float(max(live_roi.mouth_h or 0, live_pose.h)),
                    landmarks_xy=getattr(live_roi, "landmarks_xy", live_pose.landmarks_xy),
                )
            except Exception:
                pass
        elif pose is not None:
            live_pose = pose
        else:
            live_pose = pose_from_roi(live_roi)
    else:
        # Static: paste at snap coordinates (ignore live head motion)
        live_pose = tpl_pose

    try:
        tw = float(max(8.0, tpl_pose.w))
        lw = float(max(8.0, live_pose.w))
        # Allow larger scale when snap resolution ≠ live (e.g. 640 tpl → 1280 live)
        s = float(np.clip(lw / tw, 0.55, 2.60)) if track else 1.0
        # Nudge warp slightly down so closed lips land under the nose
        mh = float(max(4.0, live_pose.h, live_pose.w * 0.20))
        ty_bias = float(shift_down_px) + mh * 0.12
        tcx = float(tpl_pose.cx)
        tcy = float(tpl_pose.cy)
        lcx = float(live_pose.cx)
        lcy = float(live_pose.cy) + ty_bias

        if track and rotate:
            d_ang = float(live_pose.angle_deg - tpl_pose.angle_deg)
            # Clamp extreme tilts (detection noise / profile views)
            d_ang = float(np.clip(d_ang, -35.0, 35.0))
            M = cv2.getRotationMatrix2D((tcx, tcy), d_ang, s)
            M[0, 2] += lcx - tcx
            M[1, 2] += lcy - tcy
        else:
            # Scale + translate only (legacy path / track off)
            if track:
                tx = lcx - s * tcx
                ty = lcy - s * tcy
                M = np.array([[s, 0.0, tx], [0.0, s, ty]], dtype=np.float64)
            else:
                M = np.array(
                    [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]], dtype=np.float64
                )
    except Exception:
        return out

    try:
        warped = cv2.warpAffine(
            src,
            M,
            (w, h),
            flags=cv2.INTER_LINEAR,
            borderMode=cv2.BORDER_REFLECT_101,
        )
    except Exception:
        return out

    mask_lm = live_pose.landmarks_xy
    if mask_lm is None and track:
        mask_lm = getattr(live_roi, "landmarks_xy", None)
    if not track:
        mask_lm = getattr(template, "landmarks_xy", None)

    alpha = _mouth_only_mask(
        h,
        w,
        mask_lm,
        int(round(live_pose.cx)),
        int(round(live_pose.cy)),
        int(round(live_pose.w or 40)),
        int(round(live_pose.h or 12)),
        pad_scale=pad_scale,
        pad_x=pad_x,
        pad_up=pad_up,
        pad_down=pad_down,
        shift_down_px=shift_down_px,
        feather_px=feather_px,
    )

    if color_match and float(alpha.max()) > 0.05:
        try:
            core = alpha > 0.55
            if int(core.sum()) > 40:
                live_px = out[core].astype(np.float32)
                warp_px = warped[core].astype(np.float32)
                live_lum = (
                    0.114 * live_px[:, 0]
                    + 0.587 * live_px[:, 1]
                    + 0.299 * live_px[:, 2]
                )
                warp_lum = (
                    0.114 * warp_px[:, 0]
                    + 0.587 * warp_px[:, 1]
                    + 0.299 * warp_px[:, 2]
                )
                d = float(np.clip(live_lum.mean() - warp_lum.mean(), -22.0, 22.0))
                warped = np.clip(warped.astype(np.float32) + d, 0, 255).astype(
                    np.uint8
                )
        except Exception:
            pass

    a3 = alpha[:, :, None]
    blended = warped.astype(np.float32) * a3 + out.astype(np.float32) * (1.0 - a3)
    return blended.clip(0, 255).astype(np.uint8)


def cover_frame_with_closed_image(
    live_bgr: np.ndarray,
    template: MouthTemplate,
    *,
    flip_h: bool = False,
) -> np.ndarray:
    """
    F11: fill the **entire** virtual-cam frame with the closed-mouth photo.

    Unlike F10 (face plate only), live video is fully hidden — letterbox-free
    cover crop (scale to fill, center crop).
    """
    try:
        import cv2
    except Exception:
        return live_bgr
    if live_bgr is None or getattr(live_bgr, "size", 0) == 0:
        return live_bgr
    if template is None or not template.ok:
        return live_bgr
    src = template.image_bgr
    if flip_h:
        try:
            src = template.with_horizontal_flip().image_bgr
        except Exception:
            src = np.ascontiguousarray(src[:, ::-1])
    h, w = live_bgr.shape[:2]
    th, tw = src.shape[:2]
    if th < 2 or tw < 2:
        return live_bgr
    # Cover: scale so both dims >= target, then center-crop
    scale = max(float(w) / float(tw), float(h) / float(th))
    nw = max(w, int(round(tw * scale)))
    nh = max(h, int(round(th * scale)))
    try:
        resized = cv2.resize(src, (nw, nh), interpolation=cv2.INTER_LINEAR)
    except Exception:
        return live_bgr
    x0 = max(0, (nw - w) // 2)
    y0 = max(0, (nh - h) // 2)
    crop = resized[y0 : y0 + h, x0 : x0 + w]
    if crop.shape[0] != h or crop.shape[1] != w:
        try:
            crop = cv2.resize(crop, (w, h), interpolation=cv2.INTER_LINEAR)
        except Exception:
            return live_bgr
    return np.ascontiguousarray(crop)


def open_from_closed_template(
    closed_bgr: np.ndarray,
    live_roi: MouthROI,
    open_amt: float,
) -> np.ndarray:
    """
    From the closed base, open lips gently ∝ open_amt (warp only).

    Operates on a tight lip band. No procedural teeth paint (removed —
    looked poorly positioned / low quality).
    """
    try:
        import cv2
    except Exception:
        return closed_bgr
    if closed_bgr is None or closed_bgr.size == 0 or not live_roi.face_ok:
        return closed_bgr
    amt = float(np.clip(open_amt, 0.0, 1.0))
    if amt < 0.06:
        return closed_bgr

    out = closed_bgr.copy()
    h, w = out.shape[:2]
    cx, cy = int(live_roi.mouth_cx), int(live_roi.mouth_cy)
    lip_w = max(16, int(live_roi.mouth_w or 40))
    lip_h = max(6, int(live_roi.mouth_h or 10))

    # Tight lip-only band (cheeks stay frozen from template)
    half_w = max(12, int(lip_w * 0.5) + 6)
    half_h = max(10, int(lip_h * 2.0 + 6 + amt * 10))
    x0, x1 = max(0, cx - half_w), min(w, cx + half_w)
    y0, y1 = max(0, cy - half_h), min(h, cy + half_h)
    crop = out[y0:y1, x0:x1]
    if crop.size == 0:
        return out
    ch, cw = crop.shape[:2]
    mid = float(np.clip(cy - y0, 0, ch - 1))

    map_x = np.tile(np.arange(cw, dtype=np.float32), (ch, 1))
    ys = np.arange(ch, dtype=np.float32)
    scale = 1.0 + 0.65 * amt
    dy = amt * max(5.0, lip_w * 0.18)
    src_y = mid + (ys - mid) / scale
    src_y = src_y + np.where(ys > mid, dy * 0.4, -dy * 0.22)
    map_y = np.tile(src_y.reshape(-1, 1), (1, cw)).astype(np.float32)
    opened = cv2.remap(
        crop,
        map_x,
        map_y,
        interpolation=cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_REPLICATE,
    )

    yy, xx = np.mgrid[0:ch, 0:cw].astype(np.float32)
    lx = max(8.0, lip_w * 0.45)
    ly = max(5.0, half_h * 0.4)
    a = np.exp(-(((xx - (cx - x0)) / lx) ** 2 + ((yy - mid) / ly) ** 2)).astype(
        np.float32
    )
    a = np.clip(a, 0.0, 1.0)

    # Soft neutral darken in the lip gap only (no red cast, no teeth paint)
    if amt > 0.18:
        ax = max(2.5, lip_w * 0.16)
        ay = max(1.2, 1.2 + amt * min(6.0, lip_w * 0.09))
        ca = np.exp(
            -(((xx - (cx - x0)) / ax) ** 2 + ((yy - mid) / max(ay, 1.0)) ** 2)
        ).astype(np.float32)
        ca = np.clip(ca * (0.08 + 0.14 * amt), 0.0, 0.28)
        opened = (
            (opened.astype(np.float32) * (1.0 - 0.40 * ca[:, :, None]))
            .clip(0, 255)
            .astype(np.uint8)
        )

    a3 = a[:, :, None]
    blended = opened.astype(np.float32) * a3 + crop.astype(np.float32) * (1.0 - a3)
    out[y0:y1, x0:x1] = blended.clip(0, 255).astype(np.uint8)
    return out


class ClosedMouthTemplateStore:
    """Thread-safe holder for the active closed-mouth template."""

    def __init__(self):
        self._lock = threading.Lock()
        self._tpl: Optional[MouthTemplate] = None

    def load_from_config(self, config) -> bool:
        img = (getattr(config, "WEBCAM_CLOSED_MOUTH_IMAGE", "") or "").strip()
        lm = (getattr(config, "WEBCAM_CLOSED_MOUTH_LANDMARKS", "") or "").strip()
        if not img:
            img = str(default_image_path())
        if not lm:
            lm = str(default_landmarks_path())
        tpl = load_template(img, lm)
        with self._lock:
            self._tpl = tpl
        return tpl is not None

    def set(self, tpl: Optional[MouthTemplate]) -> None:
        with self._lock:
            self._tpl = tpl

    def get(self) -> Optional[MouthTemplate]:
        with self._lock:
            return self._tpl

    @property
    def loaded(self) -> bool:
        with self._lock:
            return self._tpl is not None and self._tpl.ok

    def path(self) -> str:
        with self._lock:
            if self._tpl is None:
                return ""
            return self._tpl.path or ""
