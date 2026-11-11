"""Translation units: the blocks of text on a page, each translated and shown as one piece.

A unit is stored in the PDF as a dict:
    bbox      [x0, y0, x1, y1] in page points, as the page is displayed now
    lines     the boxes of its printed lines, same coordinates (empty when not known)
    ja, en    the original text (lines separated by newlines) and its translation
    vertical  it was read as vertical lines (tall, narrow boxes)
    angle     how far the page has been turned, clockwise, since the unit was read: 0 for text that reads
              the ordinary way on the page as it is now, 90 for text that now runs top to bottom. A unit
              reported by the translator (src "found") carries the angle of the writing itself.
    conf      the OCR's confidence in its reading, 0 to 1
    loose     the box does not hug the text (slanted handwriting, a low-confidence region): the English
              is shown as a label over it, and nothing is painted out
    src       "ocr", or "found" for text the translator located itself

This module has no PDF or network code: it builds units from an OCR result, merges a new reading of a
page with the units it already has, and turns units with their page.
"""
from __future__ import annotations

from dataclasses import dataclass, field

PICTURE_LABELS = ("Picture", "Figure")
MIN_PICTURE_CONF = 0.5      # text found inside pictures is kept only when the OCR is fairly sure of it
LOOSE_CONF = 0.5            # below this a reading is not trusted to mark where the text really is


@dataclass
class Unit:
    """One block of text to translate. `bbox` and `lines` are in page points as displayed."""
    id: int
    bbox: tuple[float, float, float, float]
    ja: str
    vertical: bool = False
    en: str = ""
    lines: list = field(default_factory=list)
    conf: float = 0.0
    angle: int = 0
    loose: bool = False
    src: str = "ocr"

    def to_dict(self) -> dict:
        return {"bbox": [round(v, 2) for v in self.bbox], "lines": [[round(v, 2) for v in l] for l in self.lines],
                "ja": self.ja, "en": self.en, "vertical": self.vertical, "angle": self.angle,
                "conf": round(self.conf, 3), "loose": self.loose, "src": self.src}


# ---- geometry ---------------------------------------------------------------------------------------
def area(b) -> float:
    return max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])


def inter(a, b) -> float:
    return max(0.0, min(a[2], b[2]) - max(a[0], b[0])) * max(0.0, min(a[3], b[3]) - max(a[1], b[1]))


def cover(a, b) -> float:
    """How much of box `a` lies inside box `b`, 0 to 1."""
    return inter(a, b) / max(1e-6, area(a))


def union(boxes) -> tuple[float, float, float, float]:
    return (min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes))


def quarter_turned(u: dict) -> bool:
    return int(u.get("angle") or 0) % 180 == 90


def runs_down(u: dict) -> bool:
    """Do the unit's lines run up and down the page as it is displayed now?"""
    return bool(u.get("vertical")) != quarter_turned(u)


def english_turn(u: dict, narrow: float = 60.0) -> int:
    """The quarter turn (0, 90, 180, 270, clockwise) at which a block's English is set on the page.
    English runs along the block as ordinary text; the one exception is a single narrow column of
    vertical Japanese, where it runs down the column. Both then turn with the page."""
    quarter = int(round(float(u.get("angle") or 0) / 90.0)) * 90 % 360
    b = u["bbox"]
    w, h = b[2] - b[0], b[3] - b[1]
    if quarter in (90, 270):
        w, h = h, w                       # the block's shape when it was read
    column = bool(u.get("vertical")) and h > 1.5 * w and w < narrow
    return (quarter + (90 if column else 0)) % 360


def is_region(u: dict, page_area: float | None = None) -> bool:
    """A stored block whose box marks a region of the page, not the print itself: one without line boxes
    that takes in a large part of the page, or is far too small for its characters."""
    if u.get("lines") or u.get("src") == "found":
        return False
    b = u["bbox"]
    w, h = b[2] - b[0], b[3] - b[1]
    if page_area and w * h > 0.22 * page_area:
        return True
    rows = (u.get("ja") or "").split("\n")
    thick, length = (w / len(rows), h) if runs_down(u) else (h / len(rows), w)
    return not hugs(max(rows, key=len), length, thick)


def line_thickness(u: dict, box=None) -> float:
    """Height of one printed line of the unit (its width, where the lines run up and down), in points."""
    down = runs_down(u)
    if box is None and u.get("lines"):
        ts = sorted((l[2] - l[0]) if down else (l[3] - l[1]) for l in u["lines"])
        return ts[len(ts) // 2]
    b = box or u["bbox"]
    if box is not None:
        return (b[2] - b[0]) if down else (b[3] - b[1])
    n = max(1, (u.get("ja") or "").count("\n") + 1)
    return ((b[2] - b[0]) if down else (b[3] - b[1])) / n


def hugs(text: str, length: float, thick: float) -> bool:
    """Does a box `length` long and `thick` high fit one printed line of `text`? A line is about as long
    as its characters set side by side (a full-width character is as wide as the line is high, a Latin
    letter about half that). A box much too short for its characters holds several lines, or slanted
    writing: it marks a region, not the print itself."""
    n = sum(1.0 if ch > "\u2e7f" else 0.5 for ch in text if not ch.isspace())
    return n < 3 or n <= 1.7 * length / max(thick, 1e-6)


# ---- OCR result -> units ----------------------------------------------------------------------------
def _is_text(s: str) -> bool:
    return any(ch.isalpha() for ch in s)


class _L:
    __slots__ = ("box", "text", "conf", "vertical", "thick", "length", "loose")

    def __init__(self, box, text, conf):
        self.box, self.text, self.conf = tuple(box), text, conf
        w, h = box[2] - box[0], box[3] - box[1]
        self.vertical = len(text) >= 2 and h > 1.5 * w
        self.thick, self.length = (w, h) if self.vertical else (h, w)
        self.loose = conf < LOOSE_CONF or not hugs(text, self.length, self.thick)


def _linked(a: _L, b: _L, max_gap: float) -> bool:
    """Are two lines neighbours in one paragraph: same direction and size, one right after the other?"""
    if a.loose or b.loose or a.vertical != b.vertical:
        return False
    t = min(a.thick, b.thick)
    if max(a.thick, b.thick) > 1.4 * t:
        return False
    if a.vertical:                       # columns, read right to left
        if a.box[0] < b.box[0]:
            a, b = b, a
        gap = a.box[0] - b.box[2]
        along = min(a.box[3], b.box[3]) - max(a.box[1], b.box[1])
        return -0.6 * t <= gap <= max_gap * t and along >= 0.25 * min(a.length, b.length)
    if a.box[1] > b.box[1]:
        a, b = b, a
    gap = b.box[1] - a.box[3]
    along = min(a.box[2], b.box[2]) - max(a.box[0], b.box[0])
    return -0.6 * t <= gap <= max_gap * t and along >= 0.25 * min(a.length, b.length)


def _paragraphs(lines: list[_L], max_gap: float) -> list[list[_L]]:
    parent = list(range(len(lines)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(len(lines)):
        for j in range(i + 1, len(lines)):
            if _linked(lines[i], lines[j], max_gap):
                parent[find(i)] = find(j)
    groups: dict[int, list[_L]] = {}
    for i, l in enumerate(lines):
        groups.setdefault(find(i), []).append(l)
    out = []
    for g in groups.values():
        g.sort(key=(lambda l: (-l.box[0], l.box[1])) if g[0].vertical else (lambda l: (l.box[1], l.box[0])))
        out.append(g)
    out.sort(key=lambda g: (min(l.box[1] for l in g), min(l.box[0] for l in g)))
    return out


def build_units(ocr, to_page) -> list[Unit]:
    """Turn an OCR result into translation units: neighbouring lines of one size form a paragraph, and a
    paragraph is one unit. `to_page` maps image pixels to page points (a pymupdf.Matrix).

    Grouping goes by where the lines are, not by the layout model's blocks alone: a picture full of
    captions, or a page the layout model took for one block, still comes out as its separate paragraphs."""
    import pymupdf
    units: list[Unit] = []
    for block in ocr.blocks:
        picture = block.label in PICTURE_LABELS
        whole = (block.bbox[2] - block.bbox[0]) >= ocr.width * 0.98 and (block.bbox[3] - block.bbox[1]) >= ocr.height * 0.98
        lines = []
        for l in block.lines:
            if not l.text or not _is_text(l.text) or l.confidence < (MIN_PICTURE_CONF if picture else 0.0):
                continue
            r = pymupdf.Rect(*l.bbox) * to_page
            r.normalize()
            lines.append(_L((r.x0, r.y0, r.x1, r.y1), l.text, float(l.confidence)))
        # running text set in a block of its own may be double spaced; captions on a picture are not
        for g in _paragraphs(lines, 0.9 if (picture or whole) else 1.5):
            chars = sum(len(l.text) for l in g) or 1
            units.append(Unit(len(units), union([l.box for l in g]), "\n".join(l.text for l in g), g[0].vertical,
                              lines=[list(l.box) for l in g], conf=sum(l.conf * len(l.text) for l in g) / chars,
                              loose=g[0].loose))
    return units


# ---- merging a new reading with what a page already has ---------------------------------------------
def _bigrams(s: str) -> set[str]:
    s = "".join(s.split())
    return {s[i:i + 2] for i in range(len(s) - 1)} or set(s)


def same_text(new: str, old: str) -> float:
    """How much of `new` is found in `old` (0 to 1), whatever the order of the lines."""
    a = _bigrams(new)
    return len(a & _bigrams(old)) / len(a) if a else 0.0


def trust(u: dict) -> float:
    """How far a reading can be relied on when two readings of one spot disagree. Tall, narrow lines are
    either vertical Japanese or ordinary text lying on its side, and the second is read badly; so a
    reading made of such lines counts for less than one made of ordinary horizontal lines."""
    c = u.get("conf")
    c = 0.9 if c is None else float(c)         # units stored before confidences were kept
    return c - (0.3 if u.get("vertical") else 0.0)


def merge_reading(entry: dict | None, new_units: list[Unit], page_area: float | None = None) -> tuple[dict, int, int]:
    """Bring a new OCR reading of a page into its stored entry. Returns (entry, units kept, units to translate).

    Translations that are already there stay. A newly read block is added (as "pending", waiting to be
    translated) when nothing translated covers it. Where both cover the same spot and say the same thing,
    the translation is kept. Where they disagree, the new reading takes the place of the old one only
    when it is clearly the more reliable of the two, as when a page was read sideways the first time.
    A stored block that only marks a region (see is_region) also gives way to a properly read block
    on the same spot: its English could never be set where the print is."""
    entry = dict(entry or {})
    kept = [dict(u) for u in entry.get("units", [])]
    old_pending = [dict(u) for u in entry.get("pending", [])]
    pending: list[dict] = []
    chars = lambda s: len("".join((s or "").split()))
    waiting = [n.to_dict() for n in new_units]
    # Removing one stored block can change the verdict on a neighbour that lost to it, so blocks that
    # were turned away are looked at again until a round changes nothing.
    for _ in range(4):
        again, changed = [], False
        for nd in waiting:
            over = [k for k in kept if cover(nd["bbox"], k["bbox"]) > 0.3 or cover(k["bbox"], nd["bbox"]) > 0.5]
            if not over:
                pending.append(nd)
                changed = True
                continue
            regions = not nd["loose"] and all(is_region(k, page_area) for k in over)
            same = same_text(nd["ja"], "\n".join(k.get("ja") or "" for k in over))
            ours = all(k.get("src") != "found" for k in over)
            better = regions or (ours and trust(nd) >= max(trust(k) for k in over) + 0.1)
            # a sideways line is sometimes read as a scatter of short scraps; a confident reading of the
            # whole line that holds far more text than the scraps under it is the real one
            if (not better and ours and not nd["loose"] and nd["conf"] >= 0.85
                    and chars(nd["ja"]) >= 2 * sum(chars(k.get("ja")) for k in over)
                    and sum(inter(nd["bbox"], k["bbox"]) for k in over) < 0.5 * area(nd["bbox"])):
                better = True
            # one old block and one new block on the same spot with the same words: the new one can simply
            # take over the English. Worth doing when its reading is the better one, and also when the old
            # block has no line boxes (stored by the first version of the app) and the new one does.
            pair = (len(over) == 1 and not nd["loose"] and (over[0].get("en") or "").strip() and over[0].get("src") != "found"
                    and cover(over[0]["bbox"], nd["bbox"]) > 0.6)
            if pair and ((better and same >= 0.5 and not regions)
                         or (same >= 0.8 and not over[0].get("lines") and cover(nd["bbox"], over[0]["bbox"]) > 0.6)):
                nd["en"] = over[0]["en"]
                kept = [nd if k is over[0] else k for k in kept]
                changed = True
                continue
            if not better:
                again.append(nd)                           # what is there stands, for now
                continue
            kept = [k for k in kept if not any(k is o for o in over)]
            pending.append(nd)
            changed = True
        waiting = again
        if not changed or not waiting:
            break
    # blocks that were waiting from an earlier reading: the new reading replaces those it covers
    for p in old_pending:
        if not any(cover(p["bbox"], q["bbox"]) > 0.3 or cover(q["bbox"], p["bbox"]) > 0.5 for q in pending + kept):
            pending.append(p)
    entry["units"], entry["pending"] = kept, pending
    entry["swept"] = False                             # worth another look for text the OCR cannot read
    return entry, len(kept), len(pending)


# ---- turning with the page --------------------------------------------------------------------------
def turn_unit(u: dict, display_map, degrees: int) -> dict:
    """A page was turned `degrees` clockwise; `display_map` takes a point of the page as it was displayed
    to where it is now. The unit keeps its text and moves, with its line boxes, to the same spot of the
    turned page; its angle records that it now lies turned."""
    import pymupdf

    def moved(b):
        r = pymupdf.Rect(*b) * display_map
        r.normalize()
        return [round(r.x0, 2), round(r.y0, 2), round(r.x1, 2), round(r.y1, 2)]

    u = dict(u)
    u["bbox"] = moved(u["bbox"])
    u["lines"] = [moved(l) for l in u.get("lines") or []]
    u["angle"] = (int(u.get("angle") or 0) + degrees) % 360
    return u
