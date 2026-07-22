"""
Closed-mouth photo template: capture once, align + blend while mic listens.

Stores a full-frame (or ROI-padded) BGR image + MediaPipe landmarks, then
warps the mouth region onto the live face with a soft lip mask.

This is the high-quality idle path — real lip texture instead of procedural
seal/blur.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

from .face_roi import (
    OUTER_LIP_IDX,
    _LIP_LEFT,
    _LIP_LOWER,
    _LIP_RIGHT,
    _LIP_UPPER,
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
    a = np.exp(
        -(((xx - (cx - x0)) / lx) ** 2 + ((yy - mid) / ly) ** 2)
    ).astype(np.float32)
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
            opened.astype(np.float32) * (1.0 - 0.40 * ca[:, :, None])
        ).clip(0, 255).astype(np.uint8)

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
