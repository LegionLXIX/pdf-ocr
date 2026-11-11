"""Paint printed text out of a rendered page, so a translation can be set in its place.

For each printed line the local background is estimated with a median filter wider than the strokes (text
covers well under half of any such neighbourhood, so the median is the paper, the tint or the artwork
behind it), the text pixels are the ones that differ from it, and those are replaced by the background
with a feathered edge. The colour of the removed text is returned so the translation can match it.

That only works where the text can be told from what is behind it. Over a drawing, or for heavy display
lettering, "what differs from the background" takes in the artwork too and painting it out leaves a
smear. Such a block is left untouched and reported as needing a plate: the viewer then sets the English
on a backing panel over it instead.
"""
from __future__ import annotations

import cv2
import numpy as np


def _odd(n: float, lo: int, hi: int) -> int:
    k = int(max(lo, min(hi, n)))
    return k if k % 2 else k + 1


class _Line:
    """The analysis of one line box: where its text is, and whether it can be painted out cleanly."""

    def __init__(self, arr: np.ndarray, box, line_px: float):
        H, W = arr.shape[:2]
        pad = int(max(3, line_px * 0.2))
        self.x0, self.y0 = max(0, int(box[0]) - pad), max(0, int(box[1]) - pad)
        self.x1, self.y1 = min(W, int(np.ceil(box[2])) + pad), min(H, int(np.ceil(box[3])) + pad)
        self.ok = self.x1 - self.x0 >= 3 and self.y1 - self.y0 >= 3
        self.line_px = line_px
        self.found = False
        self.clean = True
        self.fg = None
        self.bg_mean, self.bg_lum = np.array([255.0, 255.0, 255.0]), 255.0
        if not self.ok:
            return
        crop = self.crop = np.ascontiguousarray(arr[self.y0:self.y1, self.x0:self.x1])
        # background: median over a window about one line tall
        bg = self.bg = cv2.medianBlur(crop, _odd(line_px * 1.2, 9, 99))
        gray = cv2.cvtColor(crop, cv2.COLOR_RGB2GRAY).astype(np.int16)
        bg_gray = cv2.cvtColor(bg, cv2.COLOR_RGB2GRAY).astype(np.int16)
        diff = gray - bg_gray
        strength = np.abs(diff)
        thresh = max(22.0, 0.42 * float(np.percentile(strength, 99.5)))
        dark, light = diff < -thresh, diff > thresh
        n_dark, n_light = int(dark.sum()), int(light.sum())
        self.mask = dark if n_dark >= n_light else light
        self.bg_mean = bg.reshape(-1, 3).mean(axis=0)
        self.bg_lum = float(bg_gray.mean())
        coverage = float(self.mask.mean())
        if coverage < 0.003:
            return                                        # nothing printed here that we can see
        self.found = True
        vals = strength[self.mask]
        core = self.mask & (strength >= np.percentile(vals, 65))
        self.fg = crop[core].mean(axis=0) if core.any() else None
        # Print is strokes of one polarity covering a modest share of its line. A drawing has both darker
        # and lighter detail, heavy lettering covers most of its box, and a busy picture gives a
        # background estimate that is itself full of structure.
        self.coverage = coverage
        self.both = min(n_dark, n_light) / max(1, max(n_dark, n_light))
        self.texture = float(cv2.Laplacian(bg_gray.astype(np.float32), cv2.CV_32F).std())
        # ... and print is thin: half a stroke is a small part of the line's height, where the shapes of a
        # drawing (or of poster lettering) are broad
        dist = cv2.distanceTransform(self.mask.astype(np.uint8), cv2.DIST_L2, 3)
        self.stroke = float(np.percentile(dist[self.mask], 97)) / max(1.0, line_px)
        self.clean = (coverage <= 0.34 and not (self.both > 0.45 and coverage > 0.06) and self.texture < 9.0
                      and self.stroke <= 0.13)

    def paint(self, arr: np.ndarray):
        if not (self.ok and self.found):
            return
        crop, bg, line_px = self.crop, self.bg, self.line_px
        # cover the anti-aliased fringe, then blend the background in with a soft edge
        r = int(max(1, round(line_px * 0.07)))
        grown = cv2.dilate(self.mask.astype(np.uint8), cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * r + 1, 2 * r + 1)))
        alpha = cv2.GaussianBlur(grown.astype(np.float32), (0, 0), sigmaX=max(0.7, r * 0.7))
        alpha = np.clip(alpha * 1.8, 0.0, 1.0)[..., None]
        # a second, wider median taken with the text already thinned gives a cleaner fill under dense text
        first = (crop * (1 - alpha) + bg * alpha).astype(np.uint8)
        fill = cv2.medianBlur(first, _odd(line_px * 0.6, 5, 51))
        arr[self.y0:self.y1, self.x0:self.x1] = (crop * (1 - alpha) + fill * alpha).astype(np.uint8)


def erase_unit(arr: np.ndarray, boxes: list, line_px: float) -> dict:
    """Paint out the text of one block: `boxes` are its printed lines (x0, y0, x1, y1 in pixels) and
    `line_px` the height of one line (the width of one column, for lines that run up and down).

    Returns {"color": the text's colour, "bg": the colour behind it, "plate": True when the block was
    left as it is because it cannot be painted out cleanly}."""
    lines = [l for l in (_Line(arr, b, line_px) for b in boxes) if l.ok]
    if not lines:
        return {"color": (20, 20, 20), "bg": (255, 255, 255), "plate": False}
    bg = np.mean([l.bg_mean for l in lines], axis=0)
    lum = float(np.mean([l.bg_lum for l in lines]))
    contrast = (245, 245, 245) if lum < 128 else (20, 20, 20)
    fgs = [l.fg for l in lines if l.fg is not None]
    color = tuple(int(v) for v in np.median(fgs, axis=0)) if fgs else contrast
    out = {"color": color, "bg": tuple(int(v) for v in bg), "plate": False}
    if not all(l.clean for l in lines):
        out["plate"] = True
        return out
    for l in lines:
        l.paint(arr)
    return out


def erase_text(arr: np.ndarray, box: tuple[float, float, float, float], line_px: float) -> tuple[int, int, int]:
    """Paint out one box whatever is in it; returns the text's colour. (The plain operation; the viewer
    uses erase_unit, which first checks that the result will be clean.)"""
    l = _Line(arr, box, line_px)
    l.paint(arr)
    if l.ok and l.fg is not None:
        return tuple(int(v) for v in l.fg)
    return (245, 245, 245) if (l.ok and l.bg_lum < 128) else (20, 20, 20)
