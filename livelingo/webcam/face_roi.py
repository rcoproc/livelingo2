"""
MediaPipe Face Mesh → outer/inner mouth mask with feathered blending.

Designed for low latency: one FaceMesh instance per worker thread, ROI-only
inference compositing (full-frame landmark once per frame is still cheap at 30 FPS).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, List, Optional, Tuple

import numpy as np

# MediaPipe Face Mesh lip indices (outer + inner rings).
# Ref: https://github.com/google/mediapipe/blob/master/mediapipe/modules/face_geometry/data/canonical_face_model_uv_visualization.png
OUTER_LIP_IDX = (
    61,
    146,
    91,
    181,
    84,
    17,
    314,
    405,
    321,
    375,
    291,
    308,
    324,
    318,
    402,
    317,
    14,
    87,
    178,
    88,
    95,
)
INNER_LIP_IDX = (
    78,
    95,
    88,
    178,
    87,
    14,
    317,
    402,
    318,
    324,
    308,
    415,
    310,
    311,
    312,
    13,
    82,
    81,
    80,
    191,
)
# Compact hull used for bbox expand
MOUTH_ALL_IDX = tuple(dict.fromkeys(OUTER_LIP_IDX + INNER_LIP_IDX + (0, 17, 269, 39)))


# MediaPipe indices for mouth center (upper/lower inner lip).
_LIP_UPPER = 13
_LIP_LOWER = 14
_LIP_LEFT = 61
_LIP_RIGHT = 291
# Upper / lower outer arcs (for seal + open geometry)
UPPER_OUTER_LIP = (61, 185, 40, 39, 37, 0, 267, 269, 270, 409, 291)
LOWER_OUTER_LIP = (146, 91, 181, 84, 17, 314, 405, 321, 375, 291)
UPPER_INNER_LIP = (78, 191, 80, 81, 82, 13, 312, 311, 310, 415, 308)
LOWER_INNER_LIP = (95, 88, 178, 87, 14, 317, 402, 318, 324, 308)


@dataclass
class MouthROI:
    """Cropped mouth region + geometry to paste back into full frame."""

    crop_bgr: np.ndarray
    x0: int
    y0: int
    x1: int
    y1: int
    mask_full: np.ndarray  # float32 HxW full-frame soft mask 0..1
    landmarks_xy: np.ndarray  # Nx2 float pixels
    face_ok: bool
    # Precise lip center (pixels); used for open-mouth paint + markers.
    mouth_cx: int = 0
    mouth_cy: int = 0
    mouth_w: int = 0  # approx lip width px
    mouth_h: int = 0  # approx closed lip height px


class FaceMouthROI:
    """
    Extract mouth ROI and soft mask from a BGR frame.

    If mediapipe is missing, ``process`` returns face_ok=False and passthrough
    geometry so the pipeline can still emit raw webcam frames.
    """

    def __init__(
        self,
        max_faces: int = 1,
        refine_landmarks: bool = True,
        min_detection_confidence: float = 0.5,
        min_tracking_confidence: float = 0.5,
        pad_ratio: float = 0.35,
        feather_px: int = 9,
    ):
        self.pad_ratio = float(pad_ratio)
        self.feather_px = max(1, int(feather_px))
        self._max_faces = int(max_faces)
        self._refine_landmarks = bool(refine_landmarks)
        self._min_detection_confidence = float(min_detection_confidence)
        self._min_tracking_confidence = float(min_tracking_confidence)
        # Lazy-load mediapipe/cv2 on first process() — avoids hard crash on some
        # Windows/Python builds when importing mediapipe at construction time
        # (COM/matplotlib teardown), and keeps heuristic tests import-free.
        self._mp = None
        self._mesh = None
        self._cv2 = None
        self._err: Optional[str] = None
        self._init_attempted = False

    def _ensure_mesh(self) -> None:
        if self._init_attempted:
            return
        self._init_attempted = True
        try:
            import cv2  # noqa: F401
            import mediapipe as mp

            self._cv2 = cv2
            self._mp = mp
            self._mesh = mp.solutions.face_mesh.FaceMesh(
                static_image_mode=False,
                max_num_faces=self._max_faces,
                refine_landmarks=self._refine_landmarks,
                min_detection_confidence=self._min_detection_confidence,
                min_tracking_confidence=self._min_tracking_confidence,
            )
        except Exception as exc:
            self._err = str(exc)
            self._mesh = None

    @property
    def available(self) -> bool:
        self._ensure_mesh()
        return self._mesh is not None

    @property
    def error(self) -> Optional[str]:
        self._ensure_mesh()
        return self._err

    def close(self) -> None:
        try:
            if self._mesh is not None:
                self._mesh.close()
        except Exception:
            pass
        self._mesh = None

    def _heuristic_mouth_roi(self, frame_bgr: np.ndarray) -> MouthROI:
        """
        Lower-center box when MediaPipe misses face.

        Still enables amplitude lip morph so Teams sees *some* mouth motion
        even without landmarks (demo path).
        """
        h, w = frame_bgr.shape[:2]
        # Mouth-ish band: horizontal center, lower third of frame
        x0 = int(w * 0.28)
        x1 = int(w * 0.72)
        y0 = int(h * 0.55)
        y1 = int(h * 0.88)
        x0, y0 = max(0, x0), max(0, y0)
        x1, y1 = min(w, max(x0 + 8, x1)), min(h, max(y0 + 8, y1))
        crop = frame_bgr[y0:y1, x0:x1].copy()
        mask = np.zeros((h, w), dtype=np.float32)
        # Soft elliptical mask in the box
        try:
            import cv2

            mh, mw = y1 - y0, x1 - x0
            local = np.zeros((mh, mw), dtype=np.uint8)
            cv2.ellipse(
                local,
                (mw // 2, mh // 2),
                (max(4, mw // 2 - 2), max(4, mh // 3)),
                0,
                0,
                360,
                255,
                -1,
            )
            blur = max(3, self.feather_px * 2 + 1)
            if blur % 2 == 0:
                blur += 1
            soft = cv2.GaussianBlur(local, (blur, blur), 0).astype(np.float32) / 255.0
            mask[y0:y1, x0:x1] = soft
        except Exception:
            mask[y0:y1, x0:x1] = 1.0
        cx = (x0 + x1) // 2
        cy = (y0 + y1) // 2
        mw = max(8, (x1 - x0) // 2)
        mh = max(4, (y1 - y0) // 5)
        # Synthetic landmarks so snap/template affine still works without MediaPipe
        try:
            from .mouth_template import synthetic_landmarks_from_box

            lm = synthetic_landmarks_from_box(h, w, cx, cy, mw, mh)
        except Exception:
            lm = np.zeros((0, 2), dtype=np.float32)
        return MouthROI(
            crop_bgr=crop,
            x0=x0,
            y0=y0,
            x1=x1,
            y1=y1,
            mask_full=mask,
            landmarks_xy=lm,
            face_ok=True,  # treat as usable ROI for engine/blend
            mouth_cx=cx,
            mouth_cy=cy,
            mouth_w=mw,
            mouth_h=mh,
        )

    def process(self, frame_bgr: np.ndarray) -> MouthROI:
        h, w = frame_bgr.shape[:2]
        self._ensure_mesh()
        if self._mesh is None or self._cv2 is None or self._mp is None:
            return self._heuristic_mouth_roi(frame_bgr)

        cv2 = self._cv2
        rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
        res = self._mesh.process(rgb)
        if not res.multi_face_landmarks:
            return self._heuristic_mouth_roi(frame_bgr)

        lm = res.multi_face_landmarks[0].landmark
        pts = np.array(
            [[p.x * w, p.y * h] for p in lm],
            dtype=np.float32,
        )
        mouth_pts = pts[list(MOUTH_ALL_IDX)]
        x0, y0 = mouth_pts.min(axis=0)
        x1, y1 = mouth_pts.max(axis=0)
        bw = max(8.0, x1 - x0)
        bh = max(8.0, y1 - y0)
        pad_x = bw * self.pad_ratio
        pad_y = bh * self.pad_ratio
        x0i = int(max(0, np.floor(x0 - pad_x)))
        y0i = int(max(0, np.floor(y0 - pad_y)))
        x1i = int(min(w, np.ceil(x1 + pad_x)))
        y1i = int(min(h, np.ceil(y1 + pad_y)))
        if x1i <= x0i + 4 or y1i <= y0i + 4:
            return self._heuristic_mouth_roi(frame_bgr)

        crop = frame_bgr[y0i:y1i, x0i:x1i].copy()
        mask = self._build_soft_mask(h, w, pts, outer_idx=OUTER_LIP_IDX)
        # Precise lip center from MediaPipe landmarks (not padded bbox center)
        try:
            lu = pts[_LIP_UPPER]
            ll = pts[_LIP_LOWER]
            left = pts[_LIP_LEFT]
            right = pts[_LIP_RIGHT]
            mcx = int(round((lu[0] + ll[0] + left[0] + right[0]) * 0.25))
            mcy = int(round((lu[1] + ll[1]) * 0.5))
            mw = int(max(12.0, abs(right[0] - left[0])))
            mh = int(max(4.0, abs(ll[1] - lu[1])))
        except Exception:
            mcx = (x0i + x1i) // 2
            mcy = (y0i + y1i) // 2
            mw = max(12, (x1i - x0i) // 2)
            mh = max(4, (y1i - y0i) // 5)
        return MouthROI(
            crop_bgr=crop,
            x0=x0i,
            y0=y0i,
            x1=x1i,
            y1=y1i,
            mask_full=mask,
            landmarks_xy=pts[:, :2],
            face_ok=True,
            mouth_cx=mcx,
            mouth_cy=mcy,
            mouth_w=mw,
            mouth_h=mh,
        )

    def _build_soft_mask(
        self,
        h: int,
        w: int,
        pts: np.ndarray,
        outer_idx: Tuple[int, ...],
    ) -> np.ndarray:
        cv2 = self._cv2
        mask = np.zeros((h, w), dtype=np.uint8)
        poly = pts[list(outer_idx)].astype(np.int32)
        cv2.fillConvexPoly(mask, cv2.convexHull(poly), 255)
        # Slight dilate so lips are fully covered
        k = max(3, self.feather_px // 2 * 2 + 1)
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k))
        mask = cv2.dilate(mask, kernel, iterations=1)
        # Feather edges
        blur = max(3, self.feather_px * 2 + 1)
        if blur % 2 == 0:
            blur += 1
        soft = cv2.GaussianBlur(mask, (blur, blur), 0)
        return (soft.astype(np.float32) / 255.0).clip(0.0, 1.0)

    @staticmethod
    def blend_mouth(
        frame_bgr: np.ndarray,
        mouth_bgr: np.ndarray,
        roi: MouthROI,
        *,
        use_soft_mask: bool = True,
    ) -> np.ndarray:
        """
        Paste ``mouth_bgr`` into frame. Soft lip mask by default; set
        ``use_soft_mask=False`` for a full ROI paste (amplitude demo).
        """
        out = frame_bgr.copy()
        if not roi.face_ok:
            return out
        x0, y0, x1, y1 = roi.x0, roi.y0, roi.x1, roi.y1
        region = out[y0:y1, x0:x1]
        mh, mw = region.shape[:2]
        if mouth_bgr.shape[0] != mh or mouth_bgr.shape[1] != mw:
            try:
                import cv2

                mouth_bgr = cv2.resize(mouth_bgr, (mw, mh), interpolation=cv2.INTER_LINEAR)
            except Exception:
                return out
        if use_soft_mask:
            alpha = roi.mask_full[y0:y1, x0:x1]
            if alpha.ndim == 2:
                alpha3 = alpha[:, :, None]
            else:
                alpha3 = alpha
            blended = (
                mouth_bgr.astype(np.float32) * alpha3
                + region.astype(np.float32) * (1.0 - alpha3)
            )
            out[y0:y1, x0:x1] = blended.clip(0, 255).astype(np.uint8)
        else:
            out[y0:y1, x0:x1] = mouth_bgr
        return out

    @staticmethod
    def _mouth_center(roi: MouthROI) -> Tuple[int, int, int, int]:
        """Return (cx, cy, lip_w, lip_h) preferring landmark-based center."""
        cx = int(roi.mouth_cx or ((roi.x0 + roi.x1) // 2))
        cy = int(roi.mouth_cy or ((roi.y0 + roi.y1) // 2))
        mw = int(roi.mouth_w or max(12, (roi.x1 - roi.x0) // 2))
        mh = int(roi.mouth_h or max(4, (roi.y1 - roi.y0) // 5))
        return cx, cy, mw, mh

    @staticmethod
    def _lip_polylines(pts: np.ndarray):
        """
        Upper/lower outer lip polylines sorted by x.
        Returns (upper Nx2, lower Nx2) or (None, None).
        """
        if pts is None or pts.shape[0] < 292:
            return None, None
        try:
            up_idx = [i for i in UPPER_OUTER_LIP if i < pts.shape[0]]
            lo_idx = [i for i in LOWER_OUTER_LIP if i < pts.shape[0]]
            if len(up_idx) < 3 or len(lo_idx) < 3:
                # Fallback: inner lips
                up_idx = [i for i in UPPER_INNER_LIP if i < pts.shape[0]]
                lo_idx = [i for i in LOWER_INNER_LIP if i < pts.shape[0]]
            if len(up_idx) < 3 or len(lo_idx) < 3:
                return None, None
            upper = pts[up_idx].astype(np.float32)
            lower = pts[lo_idx].astype(np.float32)
            upper = upper[np.argsort(upper[:, 0])]
            lower = lower[np.argsort(lower[:, 0])]
            # Dedup x
            def _dedup(arr):
                xs, ys = [], []
                for x, y in arr:
                    if xs and abs(x - xs[-1]) < 0.5:
                        ys[-1] = 0.5 * (ys[-1] + y)
                    else:
                        xs.append(float(x))
                        ys.append(float(y))
                return np.asarray(xs, dtype=np.float32), np.asarray(ys, dtype=np.float32)

            return _dedup(upper), _dedup(lower)
        except Exception:
            return None, None

    @staticmethod
    def _interp_y(xs: np.ndarray, ys: np.ndarray, x: float) -> float:
        if xs is None or len(xs) == 0:
            return 0.0
        if x <= xs[0]:
            return float(ys[0])
        if x >= xs[-1]:
            return float(ys[-1])
        return float(np.interp(x, xs, ys))

    @staticmethod
    def force_mouth_closed(
        frame_bgr: np.ndarray,
        roi: MouthROI,
    ) -> np.ndarray:
        """
        Close mouth by sealing the *gap between lips only* (column-wise).

        Keeps real lip texture — no big blur/inpaint blob.
        """
        try:
            import cv2  # noqa: F401
        except Exception:
            return frame_bgr
        if frame_bgr is None or frame_bgr.size == 0 or not roi.face_ok:
            return frame_bgr

        out = frame_bgr.copy()
        h_img, w_img = out.shape[:2]
        pts = roi.landmarks_xy
        cx, cy, lip_w, lip_h = FaceMouthROI._mouth_center(roi)
        upper, lower = FaceMouthROI._lip_polylines(pts)

        if upper is None:
            # Minimal fallback: only if we see a dark slit near mouth center
            return FaceMouthROI._force_closed_fallback(out, cx, cy, lip_w, lip_h)

        uxs, uys = upper
        lxs, lys = lower
        x_left = int(max(0, min(uxs[0], lxs[0]) - 1))
        x_right = int(min(w_img - 1, max(uxs[-1], lxs[-1]) + 1))
        if x_right <= x_left + 2:
            return out

        for x in range(x_left, x_right + 1):
            yu = FaceMouthROI._interp_y(uxs, uys, float(x))
            yl = FaceMouthROI._interp_y(lxs, lys, float(x))
            if yl < yu:
                yu, yl = yl, yu
            gap = yl - yu
            if gap < 1.5:
                continue  # already closed at this column
            yu_i = int(np.floor(yu))
            yl_i = int(np.ceil(yl))
            yu_i = max(1, min(h_img - 2, yu_i))
            yl_i = max(1, min(h_img - 2, yl_i))
            if yl_i <= yu_i:
                continue
            # Sample lip tissue just outside the gap (keep texture)
            up_col = out[max(0, yu_i - 1), x].astype(np.float32)
            if yu_i >= 2:
                up_col = 0.6 * up_col + 0.4 * out[yu_i - 2, x].astype(np.float32)
            lo_col = out[min(h_img - 1, yl_i + 1), x].astype(np.float32)
            if yl_i + 2 < h_img:
                lo_col = 0.6 * lo_col + 0.4 * out[yl_i + 2, x].astype(np.float32)
            mid = 0.5 * (yu + yl)
            # Contact line a bit darker (natural closed crease)
            crease = 0.55 * up_col + 0.45 * lo_col
            crease = crease * 0.88  # slight shadow at seal

            for y in range(yu_i, yl_i + 1):
                # Map gap onto thin closed band around mid
                t = (y - yu) / max(gap, 1e-3)  # 0 at upper, 1 at lower
                # Collapse toward mid: upper half → upper lip color, lower → lower
                if t < 0.5:
                    # blend upper lip into crease near mid
                    k = t * 2.0  # 0..1 in upper half
                    col = (1.0 - k * 0.65) * up_col + (k * 0.65) * crease
                else:
                    k = (t - 0.5) * 2.0
                    col = (1.0 - k) * crease + k * lo_col
                out[y, x] = col.clip(0, 255).astype(np.uint8)

        return out

    @staticmethod
    def _force_closed_fallback(
        frame_bgr: np.ndarray,
        cx: int,
        cy: int,
        lip_w: int,
        lip_h: int,
    ) -> np.ndarray:
        """No landmarks: thin horizontal seal only (no face blur)."""
        try:
            import cv2
        except Exception:
            return frame_bgr
        out = frame_bgr.copy()
        h, w = out.shape[:2]
        half_w = max(8, int(lip_w * 0.45))
        half_h = max(3, min(10, int(max(lip_h, 4) * 0.6 + 2)))
        x0, x1 = max(0, cx - half_w), min(w, cx + half_w)
        y0, y1 = max(0, cy - half_h), min(h, cy + half_h)
        if x1 <= x0 or y1 <= y0:
            return out
        # Sample colors above/below thin band
        y_up = max(0, y0 - 1)
        y_lo = min(h - 1, y1)
        for x in range(x0, x1):
            up = out[y_up, x].astype(np.float32)
            lo = out[y_lo, x].astype(np.float32)
            mid = (y0 + y1) * 0.5
            for y in range(y0, y1):
                t = (y - y0) / max(1, y1 - y0)
                if t < 0.5:
                    col = (1 - t * 1.2) * up + (t * 1.2) * (0.5 * (up + lo))
                else:
                    col = (2 - 2 * t) * (0.5 * (up + lo)) + (2 * t - 1) * lo
                out[y, x] = np.clip(col, 0, 255).astype(np.uint8)
        return out

    @staticmethod
    def animate_speaking(
        base_bgr: np.ndarray,
        roi: MouthROI,
        open_amt: float,
    ) -> np.ndarray:
        """
        Natural open/close: part lips with a *thin* cavity slit ∝ open_amt.

        Not a giant dark oval — upper/lower lip bands stay visible.
        """
        try:
            import cv2  # noqa: F401
        except Exception:
            return base_bgr
        if base_bgr is None or base_bgr.size == 0 or not roi.face_ok:
            return base_bgr
        amt = float(np.clip(open_amt, 0.0, 1.0))
        if amt < 0.05:
            return base_bgr

        out = base_bgr.copy()
        h_img, w_img = out.shape[:2]
        pts = roi.landmarks_xy
        cx, cy, lip_w, lip_h = FaceMouthROI._mouth_center(roi)
        upper, lower = FaceMouthROI._lip_polylines(pts)

        # Max open height in px (readable but not a black blob)
        max_open = max(6.0, min(22.0, lip_w * 0.28))
        open_px = amt * max_open

        if upper is None:
            return FaceMouthROI._animate_speaking_fallback(
                out, cx, cy, lip_w, open_px, amt
            )

        uxs, uys = upper
        lxs, lys = lower
        x_left = int(max(0, min(uxs[0], lxs[0])))
        x_right = int(min(w_img - 1, max(uxs[-1], lxs[-1])))
        if x_right <= x_left + 2:
            return out

        # Work on a tight crop for speed
        pad = int(open_px + 6)
        y_min = max(0, int(min(uys.min(), lys.min()) - pad))
        y_max = min(h_img - 1, int(max(uys.max(), lys.max()) + pad))
        # Build output column by column
        src = base_bgr
        for x in range(x_left, x_right + 1):
            yu = FaceMouthROI._interp_y(uxs, uys, float(x))
            yl = FaceMouthROI._interp_y(lxs, lys, float(x))
            if yl < yu:
                yu, yl = yl, yu
            mid = 0.5 * (yu + yl)
            # Horizontal falloff near mouth corners (less open at edges)
            edge = 0.0
            span = max(1.0, float(x_right - x_left))
            t_edge = min(x - x_left, x_right - x) / span
            edge_scale = float(np.clip(t_edge * 4.0, 0.0, 1.0))  # 0 at corners
            gap = open_px * edge_scale
            if gap < 0.8:
                continue
            half = 0.5 * gap
            # New lip edges after opening
            yu2 = mid - half
            yl2 = mid + half
            # How much to shift upper content up / lower down from original mid
            # Sample: for y above yu2 use pixels from original upper lip band
            # for y below yl2 use lower lip band; between = cavity

            # Colors for cavity from original mid line (neutral darken — no red cast)
            my = int(np.clip(round(mid), 0, h_img - 1))
            base_c = src[my, x].astype(np.float32)
            # Uniform scale keeps hue; old [0.35,0.28,0.32] crushed G → lipstick red
            cavity = base_c * 0.32
            cavity = np.clip(cavity, 8, 80)

            y0 = max(y_min, int(np.floor(yu2 - 4)))
            y1 = min(y_max, int(np.ceil(yl2 + 4)))
            for y in range(y0, y1 + 1):
                if y < yu2:
                    # Upper lip: sample from original upper side
                    # map [yu2-4, yu2] -> [yu-4, yu]
                    src_y = yu - (yu2 - y)
                    sy = int(np.clip(round(src_y), 0, h_img - 1))
                    out[y, x] = src[sy, x]
                elif y > yl2:
                    src_y = yl + (y - yl2)
                    sy = int(np.clip(round(src_y), 0, h_img - 1))
                    out[y, x] = src[sy, x]
                else:
                    # Inside opening: soft cavity, darker at center
                    if gap < 1e-3:
                        continue
                    u = (y - yu2) / gap  # 0..1
                    # Soft vertical vignette (edges blend to lips)
                    edge_a = float(np.clip(min(u, 1.0 - u) * 3.0, 0.0, 1.0))
                    # Sample near original lips for edge blend
                    if u < 0.5:
                        lip = src[int(np.clip(round(yu), 0, h_img - 1)), x].astype(
                            np.float32
                        )
                    else:
                        lip = src[int(np.clip(round(yl), 0, h_img - 1)), x].astype(
                            np.float32
                        )
                    col = (1.0 - edge_a) * lip + edge_a * cavity
                    out[y, x] = col.clip(0, 255).astype(np.uint8)

        return out

    @staticmethod
    def _animate_speaking_fallback(
        frame_bgr: np.ndarray,
        cx: int,
        cy: int,
        lip_w: int,
        open_px: float,
        amt: float,
    ) -> np.ndarray:
        """Elliptical slit without landmarks — kept small and soft."""
        try:
            import cv2
        except Exception:
            return frame_bgr
        out = frame_bgr.copy()
        h, w = out.shape[:2]
        ax = max(4, int(lip_w * 0.38))
        ay = max(1, int(open_px * 0.55))
        if ay < 1:
            return out
        # Soft cavity only (no solid fill on whole face)
        overlay = out.copy()
        dark = (int(30 + 10 * (1 - amt)), int(22 + 8 * (1 - amt)), int(35 + 10 * (1 - amt)))
        cv2.ellipse(overlay, (cx, cy), (ax, ay), 0, 0, 360, dark, -1, cv2.LINE_AA)
        # Soft mask
        mask = np.zeros((h, w), dtype=np.float32)
        cv2.ellipse(mask, (cx, cy), (ax, ay), 0, 0, 360, 1.0, -1, cv2.LINE_AA)
        mask = cv2.GaussianBlur(mask, (5, 5), 0)
        mask = np.clip(mask * (0.4 + 0.45 * amt), 0.0, 0.75)
        a3 = mask[:, :, None]
        out = (overlay.astype(np.float32) * a3 + out.astype(np.float32) * (1.0 - a3)).clip(
            0, 255
        ).astype(np.uint8)
        # Slight vertical expand of a tight band
        half_w = ax + 4
        half_h = max(8, int(ay * 3 + 6))
        x0, x1 = max(0, cx - half_w), min(w, cx + half_w)
        y0, y1 = max(0, cy - half_h), min(h, cy + half_h)
        crop = out[y0:y1, x0:x1]
        if crop.size == 0:
            return out
        ch, cw = crop.shape[:2]
        mid = float(cy - y0)
        map_x = np.tile(np.arange(cw, dtype=np.float32), (ch, 1))
        ys = np.arange(ch, dtype=np.float32)
        scale = 1.0 + 0.55 * amt
        src_y = mid + (ys - mid) / scale
        map_y = np.tile(src_y.reshape(-1, 1), (1, cw)).astype(np.float32)
        warped = cv2.remap(
            crop, map_x, map_y, interpolation=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE
        )
        yy, xx = np.mgrid[0:ch, 0:cw].astype(np.float32)
        a = np.exp(
            -(((xx - (cx - x0)) / max(6, ax * 1.1)) ** 2 + ((yy - mid) / max(4, half_h * 0.5)) ** 2)
        ).astype(np.float32)
        a = np.clip(a * 0.85, 0, 1)
        out[y0:y1, x0:x1] = (
            warped.astype(np.float32) * a[:, :, None]
            + crop.astype(np.float32) * (1.0 - a[:, :, None])
        ).clip(0, 255).astype(np.uint8)
        return out

    @staticmethod
    def paint_mouth_open(
        frame_bgr: np.ndarray,
        roi: MouthROI,
        open_amt: float,
    ) -> np.ndarray:
        """Deprecated cartoon cavity — no-op. Use animate_speaking instead."""
        return frame_bgr

    @staticmethod
    def draw_sync_marker(
        frame_bgr: np.ndarray,
        roi: MouthROI,
        open_amt: float,
        active: bool,
    ) -> np.ndarray:
        """
        Markers **on the mouth** (not on the cheek):

        - Cyan ROI box around lip bbox
        - Crosshair / ring at true lip center
        - Horizontal energy bar **under** the chin (height∝open)
        - Label SYNC/idle above the lips
        """
        try:
            import cv2
        except Exception:
            return frame_bgr
        if frame_bgr is None or frame_bgr.size == 0:
            return frame_bgr
        out = frame_bgr
        h, w = out.shape[:2]
        if roi is None or not roi.face_ok:
            return out

        cx, cy, lip_w, lip_h = FaceMouthROI._mouth_center(roi)
        # Tight box around actual lips (not huge padded ROI)
        half_w = max(10, lip_w // 2 + 6)
        half_h = max(8, int(lip_h * 2 + 8 + (open_amt * 12 if active else 0)))
        bx0 = max(0, cx - half_w)
        bx1 = min(w - 1, cx + half_w)
        by0 = max(0, cy - half_h)
        by1 = min(h - 1, cy + half_h)

        amt = float(np.clip(open_amt, 0.0, 1.0)) if active else 0.0
        box_col = (0, 255, 180) if active else (140, 140, 140)
        cv2.rectangle(out, (bx0, by0), (bx1, by1), box_col, 1, cv2.LINE_AA)

        # Crosshair on lip center
        arm = max(6, lip_w // 6)
        cross = (0, 255, 80) if active else (90, 90, 90)
        cv2.line(out, (cx - arm, cy), (cx + arm, cy), cross, 1, cv2.LINE_AA)
        cv2.line(out, (cx, cy - arm), (cx, cy + arm), cross, 1, cv2.LINE_AA)
        cv2.circle(out, (cx, cy), 3, cross, -1, cv2.LINE_AA)

        # Horizontal energy bar just below the lip box
        bar_y0 = min(h - 10, by1 + 4)
        bar_y1 = min(h - 4, bar_y0 + 8)
        bar_x0 = bx0
        bar_x1 = bx1
        cv2.rectangle(out, (bar_x0, bar_y0), (bar_x1, bar_y1), (60, 60, 60), 1, cv2.LINE_AA)
        if active and amt > 0.02:
            fill_w = max(2, int((bar_x1 - bar_x0) * amt))
            color = (40, int(180 + 75 * amt), int(80 + 175 * amt))
            cv2.rectangle(
                out,
                (bar_x0 + 1, bar_y0 + 1),
                (bar_x0 + fill_w, bar_y1 - 1),
                color,
                -1,
                cv2.LINE_AA,
            )
            # Expanding ring on mouth
            r = max(5, int(6 + 16 * amt))
            cv2.circle(out, (cx, cy), r, (0, 255, 120), 2, cv2.LINE_AA)
            label = f"SYNC {int(amt * 100)}"
            col = (0, 255, 180)
        else:
            label = "idle"
            col = (150, 150, 150)

        ty = max(14, by0 - 6)
        cv2.putText(
            out,
            label,
            (max(4, bx0), ty),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.5,
            col,
            1,
            cv2.LINE_AA,
        )
        return out
