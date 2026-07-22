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
        try:
            import cv2
        except Exception:
            return self
        img = cv2.flip(self.image_bgr, 1)
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


def _lower_face_mask(
    h: int,
    w: int,
    landmarks: Optional[np.ndarray],
    cx: int,
    cy: int,
    mouth_w: int,
    mouth_h: int,
    *,
    region_scale: float = 2.4,
    feather_px: int = 40,
) -> np.ndarray:
    """
    Boxy plate centered on the **mouth** (cheeks + chin).

    Primary geometry uses mouth_cx/cy/w/h from the live ROI (always valid).
    Landmarks only *expand* the box if they look consistent — never shrink
    the plate off the mouth (that bug left alpha=0 on the lips).
    """
    import cv2

    mask = np.zeros((h, w), dtype=np.uint8)
    scale = max(1.2, float(region_scale))
    mw = max(28, int(mouth_w or 40))
    mh = max(12, int(mouth_h or 12))
    feather = max(10, int(feather_px))
    cx = int(np.clip(cx, 0, w - 1))
    cy = int(np.clip(cy, 0, h - 1))

    # --- Base plate: tall enough to cover under-nose → chin (not a thin strip) ---
    # Use mouth WIDTH as vertical scale too — mouth_h alone is often ~30px (too short)
    half_w = max(48.0, mw * 0.95 * scale)
    half_up = max(28.0, mw * 0.35 * scale)  # up toward nose base
    half_dn = max(55.0, mw * 0.55 * scale + mh * 2.0)  # down to chin
    x0 = float(cx) - half_w
    x1 = float(cx) + half_w
    y0 = float(cy) - half_up
    y1 = float(cy) + half_dn

    # Expand with outer-lip ring if near mouth (width/height refine)
    if landmarks is not None and landmarks.shape[0] > max(OUTER_LIP_IDX):
        try:
            outer = landmarks[list(OUTER_LIP_IDX)].astype(np.float32)
            near = []
            for px, py in outer:
                if abs(px - cx) < half_w * 1.5 and abs(py - cy) < half_dn * 1.4:
                    near.append((px, py))
            if len(near) >= 4:
                arr = np.asarray(near, dtype=np.float32)
                lx0, lx1 = float(arr[:, 0].min()), float(arr[:, 0].max())
                ly0, ly1 = float(arr[:, 1].min()), float(arr[:, 1].max())
                pad_x = max(12.0, (lx1 - lx0) * 0.4)
                pad_up = max(18.0, (ly1 - ly0) * 0.9)
                pad_dn = max(28.0, (ly1 - ly0) * 1.6)
                x0 = min(x0, lx0 - pad_x)
                x1 = max(x1, lx1 + pad_x)
                y0 = min(y0, ly0 - pad_up)
                y1 = max(y1, ly1 + pad_dn)
        except Exception:
            pass

    # Keep plate on lower face only (not hair)
    y0 = max(y0, h * 0.28)
    y0 = min(y0, float(cy) - 16.0)  # always some solid above lips
    y1 = max(y1, float(cy) + 40.0)

    xi0 = int(max(0, np.floor(x0)))
    yi0 = int(max(0, np.floor(y0)))
    xi1 = int(min(w, np.ceil(x1)))
    yi1 = int(min(h, np.ceil(y1)))
    if xi1 <= xi0 + 4 or yi1 <= yi0 + 4:
        # Last resort: fixed box on mouth
        xi0 = max(0, cx - 60)
        xi1 = min(w, cx + 60)
        yi0 = max(0, cy - 30)
        yi1 = min(h, cy + 70)

    cv2.rectangle(mask, (xi0, yi0), (xi1, yi1), 255, -1)
    corner = max(3, min(10, int(min(xi1 - xi0, yi1 - yi0) * 0.05)))
    if corner > 2:
        k = cv2.getStructuringElement(
            cv2.MORPH_ELLIPSE, (corner * 2 + 1, corner * 2 + 1)
        )
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, k)

    if int(mask.max()) == 0:
        return np.zeros((h, w), dtype=np.float32)

    dist = cv2.distanceTransform(mask, cv2.DIST_L2, 5)
    ramp = max(4.0, float(feather) * 0.32)
    soft = np.clip(dist / ramp, 0.0, 1.0).astype(np.float32)
    k = max(3, min(9, (feather // 8) * 2 + 1))
    if k % 2 == 0:
        k += 1
    soft = cv2.GaussianBlur(soft, (k, k), 0)
    soft = np.clip(soft, 0.0, 1.0)
    soft[mask == 0] = 0.0
    # Guarantee mouth center is fully frozen
    if 0 <= cy < h and 0 <= cx < w and soft[cy, cx] < 0.85:
        soft[cy, cx] = 1.0
        # small solid disk on mouth
        cv2.circle(soft, (cx, cy), max(12, mw // 4), 1.0, -1)
        soft = np.clip(soft, 0.0, 1.0)
        soft[mask == 0] = 0.0
    return soft


def align_and_blend(
    live_bgr: np.ndarray,
    live_roi: MouthROI,
    template: MouthTemplate,
    *,
    feather_px: int = 36,
    region_scale: float = 2.4,
    color_match: bool = True,
    flip_h: bool = False,
) -> np.ndarray:
    """
    Freeze a *large* closed-mouth plate from the photo onto the live frame.

    Soft edges blend into the live background so the patch doesn't look
    pasted. Core region stays fully from the photo (hides speaking motion).

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

    # Uniform scale from mouth WIDTH only (mouth_h is tiny → was squashing ~50%).
    # Skip multi-point affine: bad jaw landmarks compress the face vertically.
    M = None
    try:
        tw = float(template.mouth_w or 40)
        lw = float(live_roi.mouth_w or 40)
        s = float(np.clip(lw / max(8.0, tw), 0.85, 1.25))
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

    # Color match: core mean + edge ring (reduces halo at feather boundary)
    if color_match and float(alpha.max()) > 0.05:
        try:
            core = alpha > 0.65
            ring = (alpha > 0.12) & (alpha < 0.55)
            if int(core.sum()) > 80:
                live_mean = out[core].astype(np.float32).mean(axis=0)
                warp_mean = warped[core].astype(np.float32).mean(axis=0)
                scale = (live_mean + 1.0) / (warp_mean + 1.0)
                scale = np.clip(scale, 0.82, 1.22)
                warped_f = warped.astype(np.float32) * scale.reshape(1, 1, 3)
                # Nudge ring toward live skin tone
                if int(ring.sum()) > 40:
                    live_r = out[ring].astype(np.float32).mean(axis=0)
                    warp_r = warped_f[ring].mean(axis=0)
                    delta = (live_r - warp_r) * 0.45
                    # Apply delta only where alpha is mid (edges)
                    edge_w = np.clip((0.55 - np.abs(alpha - 0.35)) / 0.35, 0.0, 1.0)
                    warped_f = warped_f + delta.reshape(1, 1, 3) * edge_w[:, :, None]
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
    From the frozen closed plate, open lips gently ∝ open_amt.

    Operates only on a tight lip band *inside* the large frozen region so
    cheeks/chin stay from the photo.
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
    dy = amt * max(5.0, lip_w * 0.15)
    src_y = mid + (ys - mid) / scale
    src_y = src_y + np.where(ys > mid, dy * 0.4, -dy * 0.2)
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

    if amt > 0.2:
        ax = max(2.5, lip_w * 0.18)
        ay = max(1.0, 1.0 + amt * min(6.0, lip_w * 0.08))
        ca = np.exp(
            -(((xx - (cx - x0)) / ax) ** 2 + ((yy - mid) / max(ay, 1.0)) ** 2)
        ).astype(np.float32)
        ca = np.clip(ca * (0.12 + 0.2 * amt), 0.0, 0.35)
        opened = (
            opened.astype(np.float32) * (1.0 - 0.5 * ca[:, :, None])
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
