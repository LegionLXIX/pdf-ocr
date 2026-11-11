"""Paint printed text out of a rendered page, so a translation can be set in its place.

For each text block the local background is estimated with a median filter wider than the strokes (text
covers well under half of any such neighbourhood, so the median is the paper, the tint or the artwork
behind it), the text pixels are the ones that differ from it, and those are replaced by the background
with a feathered edge. The colour of the removed text is returned so the translation can match it.
"""
from __future__ import annotations

import cv2
import numpy as np


def _odd(n: float, lo: int, hi: int) -> int:
    k = int(max(lo, min(hi, n)))
    return k if k % 2 else k + 1


def erase_text(arr: np.ndarray, box: tuple[float, float, float, float], line_px: float) -> tuple[int, int, int]:
    """Remove the text inside `box` (x0, y0, x1, y1 in pixels) of an RGB image, in place.

    line_px: height of one line of the text (width of one column, for vertical text), in pixels.
    Returns the text's colour; when no text is found, a colour that contrasts with the background.
    """
    H, W = arr.shape[:2]
    pad = int(max(3, line_px * 0.2))
    x0, y0 = max(0, int(box[0]) - pad), max(0, int(box[1]) - pad)
    x1, y1 = min(W, int(np.ceil(box[2])) + pad), min(H, int(np.ceil(box[3])) + pad)
    if x1 - x0 < 3 or y1 - y0 < 3:
        return (0, 0, 0)
    crop = np.ascontiguousarray(arr[y0:y1, x0:x1])
    # background: median over a window about one line tall
    bg = cv2.medianBlur(crop, _odd(line_px * 1.2, 9, 99))
    gray = cv2.cvtColor(crop, cv2.COLOR_RGB2GRAY).astype(np.int16)
    bg_gray = cv2.cvtColor(bg, cv2.COLOR_RGB2GRAY).astype(np.int16)
    diff = gray - bg_gray
    strength = np.abs(diff)
    thresh = max(22.0, 0.42 * float(np.percentile(strength, 99.5)))
    dark, light = diff < -thresh, diff > thresh
    mask = dark if dark.sum() >= light.sum() else light
    bg_lum = float(bg_gray.mean())
    contrast = (245, 245, 245) if bg_lum < 128 else (20, 20, 20)
    if mask.mean() < 0.003:
        return contrast                                   # nothing printed here that we can see
    # the text colour: the most strongly differing third of the text pixels (their cores, not the edges)
    vals = strength[mask]
    core = mask & (strength >= np.percentile(vals, 65))
    fg = crop[core].mean(axis=0) if core.any() else np.array(contrast, dtype=np.float64)
    # cover the anti-aliased fringe, then blend the background in with a soft edge
    r = int(max(1, round(line_px * 0.07)))
    grown = cv2.dilate(mask.astype(np.uint8), cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * r + 1, 2 * r + 1)))
    alpha = cv2.GaussianBlur(grown.astype(np.float32), (0, 0), sigmaX=max(0.7, r * 0.7))
    alpha = np.clip(alpha * 1.8, 0.0, 1.0)[..., None]
    # a second, wider median taken with the text already thinned gives a cleaner fill under dense text
    first = (crop * (1 - alpha) + bg * alpha).astype(np.uint8)
    fill = cv2.medianBlur(first, _odd(line_px * 0.6, 5, 51))
    arr[y0:y1, x0:x1] = (crop * (1 - alpha) + fill * alpha).astype(np.uint8)
    return (int(fg[0]), int(fg[1]), int(fg[2]))


def erase_units(arr: np.ndarray, units: list[tuple[tuple[float, float, float, float], float]]) -> list[tuple[int, int, int]]:
    """Erase several blocks: `units` is [(box in pixels, line height in pixels)]. Returns their text colours."""
    return [erase_text(arr, box, line_px) for box, line_px in units]
