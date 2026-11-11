"""Surya OCR wrapper: page image -> ordered blocks of lines of words.

Models load lazily on first use (about 7 s on the 4090). Coordinates in the
results are pixels of the image that was passed in.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

# Keep HF/torch from writing noise to the console
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

from PIL import Image

# Layout labels whose text is not worth keeping in a search layer
SKIP_LABELS = {"Picture", "Figure"}


@dataclass
class Word:
    text: str
    bbox: tuple[float, float, float, float]  # x0, y0, x1, y1 in image pixels
    confidence: float = 0.0


@dataclass
class Line:
    text: str
    bbox: tuple[float, float, float, float]
    words: list[Word] = field(default_factory=list)
    confidence: float = 0.0


@dataclass
class Block:
    label: str
    bbox: tuple[float, float, float, float]
    lines: list[Line] = field(default_factory=list)


@dataclass
class PageOcr:
    width: int
    height: int
    blocks: list[Block] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "\n\n".join("\n".join(l.text for l in b.lines) for b in self.blocks if b.lines)

    @property
    def line_count(self) -> int:
        return sum(len(b.lines) for b in self.blocks)


_TAG_RE = re.compile(r"</?(b|i|u|sup|sub|math|br)\s*/?>|\\text\{([^}]*)\}")


def clean_text(s: str) -> str:
    """Drop the inline markup Surya emits (<b>, <i>, <math>, \\text{}) and tidy spaces."""
    s = _TAG_RE.sub(lambda m: m.group(2) or "", s)
    return " ".join(s.split())


def _poly_bbox(poly) -> tuple[float, float, float, float]:
    xs = [p[0] for p in poly]
    ys = [p[1] for p in poly]
    return (min(xs), min(ys), max(xs), max(ys))


class OcrEngine:
    def __init__(self, use_layout: bool = True):
        self.use_layout = use_layout
        self._rec = None
        self._det = None
        self._layout = None

    @property
    def loaded(self) -> bool:
        return self._rec is not None

    def load(self, progress=None):
        if self.loaded:
            return
        if progress:
            progress("Loading OCR models…")
        from surya.foundation import FoundationPredictor
        from surya.recognition import RecognitionPredictor
        from surya.detection import DetectionPredictor
        from surya.settings import settings

        foundation = FoundationPredictor()
        self._rec = RecognitionPredictor(foundation)
        self._det = DetectionPredictor()
        if self.use_layout:
            layout_foundation = FoundationPredictor(checkpoint=settings.LAYOUT_MODEL_CHECKPOINT)
            from surya.layout import LayoutPredictor
            self._layout = LayoutPredictor(layout_foundation)
        for p in (self._rec, self._det, self._layout):
            if p is not None:
                p.disable_tqdm = True

    def ocr_images(self, images: list[Image.Image]) -> list[PageOcr]:
        """OCR a batch of page images. Batching several pages per call is much faster than one at a time."""
        self.load()
        images = [im.convert("RGB") if im.mode != "RGB" else im for im in images]
        # No return_words: Surya 0.17's char/word boxes all collapse to one box per line
        ocr_results = self._rec(images, det_predictor=self._det, math_mode=False)
        layout_results = self._layout(images) if self._layout else [None] * len(images)
        return [self._assemble(im, o, l) for im, o, l in zip(images, ocr_results, layout_results)]

    def ocr_pages(self, images: list[Image.Image]) -> list[PageOcr]:
        """OCR page images; a very large page is read a second time in overlapping tiles.

        The text detector works on a reduced copy of the image, so on a page several times the usual
        size (a fold-out, a row of cards scanned as one page) small text is lost. Each tile is read at
        full size; lines it finds that the whole-page pass missed are added, and a line the whole-page
        pass read badly gives way to a tile's clearly better reading of the same spot."""
        pages = self.ocr_images(images)
        for page, im in zip(pages, images):
            tiles = tile_boxes(im.size)
            if len(tiles) < 2:
                continue
            results = []
            for k in range(0, len(tiles), 4):
                results += self.ocr_images([im.crop(t) for t in tiles[k:k + 4]])
            add_tile_lines(page, tiles, results)
        return pages

    def _assemble(self, image: Image.Image, ocr, layout) -> PageOcr:
        w, h = image.size
        lines: list[Line] = []
        for tl in ocr.text_lines:
            text = clean_text(tl.text)
            if not text:
                continue
            lines.append(Line(text, _poly_bbox(tl.polygon), [], tl.confidence or 0))

        page = PageOcr(w, h)
        if layout is None or not layout.bboxes:
            page.blocks = [Block("Text", (0, 0, w, h), _reading_order(lines))]
            return page

        # Layout polygons are in the model's processed-image space; rescale to ours
        lw, lh = layout.image_bbox[2], layout.image_bbox[3]
        sx, sy = w / lw, h / lh
        blocks = []
        for lb in sorted(layout.bboxes, key=lambda b: b.position):
            x0, y0, x1, y1 = _poly_bbox(lb.polygon)
            blocks.append(Block(lb.label, (x0 * sx, y0 * sy, x1 * sx, y1 * sy)))

        # Assign each line to the block with the most overlap of the line's area; a line no block
        # covers goes to the nearest block if one is close, else to a leftovers block at the end.
        leftovers = Block("Text", (0, 0, w, h))
        for ln in lines:
            best, best_ov = None, 0.0
            lx0, ly0, lx1, ly1 = ln.bbox
            larea = max(1.0, (lx1 - lx0) * (ly1 - ly0))
            for b in blocks:
                bx0, by0, bx1, by1 = b.bbox
                ov = max(0, min(lx1, bx1) - max(lx0, bx0)) * max(0, min(ly1, by1) - max(ly0, by0)) / larea
                if ov > best_ov:
                    best, best_ov = b, ov
            if best is None or best_ov <= 0.3:
                cx, cy, lh = (lx0 + lx1) / 2, (ly0 + ly1) / 2, ly1 - ly0
                dist, near = min(
                    ((max(b.bbox[0] - cx, 0, cx - b.bbox[2]) ** 2
                      + max(b.bbox[1] - cy, 0, cy - b.bbox[3]) ** 2) ** 0.5, k, b)
                    for k, b in enumerate(blocks))[::2]
                best = near if dist < 2 * lh else leftovers
            best.lines.append(ln)

        out = []
        for b in blocks + [leftovers]:
            if b.lines:
                b.lines = _reading_order(b.lines)
                out.append(b)
        page.blocks = out
        return page


def _reading_order(lines: list[Line]) -> list[Line]:
    """Top-to-bottom; lines that overlap vertically (table cells, key/value pairs) go left-to-right."""
    rows: list[tuple[list[Line], float, float]] = []   # (lines, y0, y1)
    for ln in sorted(lines, key=lambda l: l.bbox[1]):
        y0, y1 = ln.bbox[1], ln.bbox[3]
        if rows:
            row, ry0, ry1 = rows[-1]
            overlap = min(y1, ry1) - max(y0, ry0)
            if overlap > 0.5 * min(y1 - y0, ry1 - ry0):
                row.append(ln)
                rows[-1] = (row, min(ry0, y0), max(ry1, y1))
                continue
        rows.append(([ln], y0, y1))
    return [ln for row, _, _ in rows for ln in sorted(row, key=lambda l: l.bbox[0])]


# ---- large pages: reading in tiles ----------------------------------------------------------------------
TILE = 2400        # tile side in pixels: about what the detector sees a normal page at
BIG = 3200         # pages with a side longer than this are also read in tiles


def _spans(length: int) -> list[tuple[int, int]]:
    """Tile positions along one side: half-overlapping, so any line up to half a tile long lies whole
    inside at least one tile."""
    if length <= BIG:
        return [(0, length)]
    step = TILE // 2
    starts = list(range(0, length - TILE, step)) + [length - TILE]
    return [(s, s + TILE) for s in starts]


def tile_boxes(size: tuple[int, int]) -> list[tuple[int, int, int, int]]:
    w, h = size
    if max(w, h) <= BIG:
        return []
    return [(x0, y0, x1, y1) for y0, y1 in _spans(h) for x0, x1 in _spans(w)]


def _cover(a, b) -> float:
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    return ix * iy / max(1e-6, (a[2] - a[0]) * (a[3] - a[1]))


def add_tile_lines(page: PageOcr, tiles: list, results: list[PageOcr], edge: int = 8):
    """Merge the tiles' readings into the whole-page result (see OcrEngine.ocr_pages)."""
    have = [(ln, b) for b in page.blocks for ln in b.lines]
    for (tx0, ty0, tx1, ty1), res in zip(tiles, results):
        for b in res.blocks:
            fresh = []
            for ln in b.lines:
                x0, y0, x1, y1 = ln.bbox
                # a line touching an inner edge of its tile is cut off there; another tile has it whole
                if ((x0 < edge and tx0 > 0) or (y0 < edge and ty0 > 0)
                        or (x1 > (tx1 - tx0) - edge and tx1 < page.width) or (y1 > (ty1 - ty0) - edge and ty1 < page.height)):
                    continue
                box = (x0 + tx0, y0 + ty0, x1 + tx0, y1 + ty0)
                clash = [(o, ob) for o, ob in have if _cover(box, o.bbox) > 0.5 or _cover(o.bbox, box) > 0.5]
                if clash and not all(o.confidence + 0.15 < ln.confidence for o, _ in clash):
                    continue
                for o, ob in clash:                    # a worse reading of the same spot gives way
                    if o in ob.lines:
                        ob.lines.remove(o)
                    have = [(p, pb) for p, pb in have if p is not o]
                moved = Line(ln.text, box, [], ln.confidence)
                fresh.append(moved)
            if fresh:
                bx0, by0, bx1, by1 = b.bbox
                nb = Block(b.label, (bx0 + tx0, by0 + ty0, bx1 + tx0, by1 + ty0), fresh)
                page.blocks.append(nb)
                have += [(ln, nb) for ln in fresh]
    page.blocks = [b for b in page.blocks if b.lines]

