"""
Burn-in subtitles for virtual camera frames (OBS / Teams sees pixels only).

Draws TARGET (translated) text on a semi-transparent bar at the bottom of a BGR frame.
"""

from __future__ import annotations

from typing import List, Optional, Sequence, Tuple

import numpy as np


def _wrap_text_lines(
    text: str,
    max_width_px: int,
    font,
    font_scale: float,
    thickness: int,
    get_text_size,
    max_lines: int = 3,
) -> List[str]:
    """Word-wrap ``text`` to fit ``max_width_px`` (OpenCV getTextSize)."""
    text = " ".join((text or "").split())
    if not text:
        return []
    max_lines = max(1, int(max_lines or 1))
    words = text.split(" ")
    lines: List[str] = []
    cur = ""
    for w in words:
        trial = w if not cur else f"{cur} {w}"
        (tw, _th), _ = get_text_size(trial, font, font_scale, thickness)
        if tw <= max_width_px or not cur:
            cur = trial
            if tw > max_width_px and not lines:
                # Single long token: hard-cut later if still too wide
                cur = trial
        else:
            lines.append(cur)
            cur = w
            if len(lines) >= max_lines:
                break
    if cur and len(lines) < max_lines:
        lines.append(cur)
    elif cur and lines:
        # Overflow: ellipsis on last line
        last = lines[-1]
        ell = "…"
        while last:
            (tw, _), _ = get_text_size(last + ell, font, font_scale, thickness)
            if tw <= max_width_px:
                lines[-1] = last + ell
                break
            last = last[:-1]
        else:
            lines[-1] = ell
    # Clamp any remaining oversize lines
    out: List[str] = []
    for line in lines[:max_lines]:
        (tw, _), _ = get_text_size(line, font, font_scale, thickness)
        if tw <= max_width_px:
            out.append(line)
            continue
        cut = line
        while cut:
            (tw, _), _ = get_text_size(cut, font, font_scale, thickness)
            if tw <= max_width_px:
                out.append(cut)
                break
            cut = cut[:-1]
        if not cut:
            out.append("…")
    return out


def draw_subtitle_burnin(
    frame_bgr: np.ndarray,
    text: str,
    *,
    max_lines: int = 2,
    max_width_frac: float = 0.94,
    font_scale: float = 0.0,
    thickness: int = 0,
    margin_bottom: int = 2,
    pad_x: int = 12,
    pad_y: int = 6,
    bar_alpha: float = 0.48,
    text_bgr: Tuple[int, int, int] = (255, 255, 255),
    bar_bgr: Tuple[int, int, int] = (0, 0, 0),
    mirror_h: bool = True,
    blur_px: int = 21,
) -> np.ndarray:
    """
    Overlay TARGET subtitle flush on the **footer** of a BGR frame.

    - Band height fits **current** lines only (not a tall empty block).
    - Sits on the bottom edge (small ``margin_bottom``).
    - Background = **frosted video** (Gaussian blur of the live strip) + light
      dark veil — not a solid black bar.
    - Each frame redraws from clean live pixels → caption is replaced, not stacked.

    ``mirror_h``: pre-flip band for Teams/OBS selfie mirror.
    """
    if frame_bgr is None or getattr(frame_bgr, "size", 0) == 0:
        return frame_bgr
    text = " ".join((text or "").split())
    if not text:
        return frame_bgr

    try:
        import cv2
    except Exception:
        return frame_bgr

    out = np.ascontiguousarray(frame_bgr.copy())

    h, w = int(out.shape[0]), int(out.shape[1])
    if h < 32 or w < 64:
        return out

    font = cv2.FONT_HERSHEY_SIMPLEX
    if font_scale is None or float(font_scale) <= 0:
        font_scale = max(0.42, min(1.15, w / 1200.0))
    else:
        font_scale = float(font_scale)
    if thickness is None or int(thickness) <= 0:
        thickness = 2 if font_scale >= 0.7 else 1
    else:
        thickness = max(1, int(thickness))

    max_lines = max(1, int(max_lines or 1))
    max_width_px = max(40, int(w * max(0.5, min(0.98, float(max_width_frac or 0.94)))))
    lines = _wrap_text_lines(
        text,
        max_width_px,
        font,
        font_scale,
        thickness,
        cv2.getTextSize,
        max_lines=max_lines,
    )
    if not lines:
        return out

    line_sizes: List[Tuple[int, int]] = []
    for line in lines:
        (tw, th), baseline = cv2.getTextSize(line, font, font_scale, thickness)
        line_sizes.append((tw, th + baseline))
    line_h = max(s[1] for s in line_sizes) + 3
    n_lines = len(lines)
    # Tight band: only as tall as this caption (footer, not mid-screen)
    pad_y = max(2, int(pad_y))
    band_h = int(line_h * n_lines + 2 * pad_y)
    band_h = max(band_h, line_h + 2 * pad_y)

    mb = max(0, int(margin_bottom))
    # Flush to image bottom (y2 = last row)
    y2 = h - mb
    y1 = max(0, y2 - band_h)
    x1, x2 = 0, w
    if y2 <= y1:
        return out

    roi = out[y1:y2, x1:x2]
    # Frosted glass: blur live video under the caption strip
    bp = int(blur_px or 0)
    if bp >= 3:
        k = bp if bp % 2 == 1 else bp + 1
        k = max(3, min(k, 61))
        # Kernel must be odd and < region size
        kh = min(k, max(3, (roi.shape[0] // 2) * 2 - 1))
        kw = min(k, max(3, (roi.shape[1] // 2) * 2 - 1))
        if kh % 2 == 0:
            kh = max(3, kh - 1)
        if kw % 2 == 0:
            kw = max(3, kw - 1)
        try:
            frosted = cv2.GaussianBlur(roi, (kw, kh), 0)
        except Exception:
            frosted = roi.copy()
    else:
        frosted = roi.copy()

    alpha = max(0.0, min(0.85, float(bar_alpha)))  # keep see-through video look
    veil = np.empty_like(frosted)
    veil[:] = bar_bgr
    canvas = (
        frosted.astype(np.float32) * (1.0 - alpha) + veil.astype(np.float32) * alpha
    ).astype(np.uint8)

    # Text centered horizontally, padded from top of tight band
    band_w = x2 - x1
    ty = pad_y + line_sizes[0][1]
    for i, line in enumerate(lines):
        tw, _ = line_sizes[i]
        tx = max(0, (band_w - tw) // 2)
        cv2.putText(
            canvas,
            line,
            (tx + 1, ty + 1),
            font,
            font_scale,
            (0, 0, 0),
            thickness + 1,
            cv2.LINE_AA,
        )
        cv2.putText(
            canvas,
            line,
            (tx, ty),
            font,
            font_scale,
            text_bgr,
            thickness,
            cv2.LINE_AA,
        )
        ty += line_h

    if mirror_h:
        canvas = cv2.flip(canvas, 1)

    out[y1:y2, x1:x2] = canvas
    return out


__all__ = ["draw_subtitle_burnin", "_wrap_text_lines"]
