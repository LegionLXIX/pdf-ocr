"""Translate a Japanese PDF into English with Claude, as a searchable text layer.

For each page: Surya (local, GPU) finds the text blocks and reads the Japanese; the page image and those
blocks go to Claude, which returns an English translation per block; the English is written into the PDF
as invisible text over the block it translates, and stored inside the file for the viewer's overlay.
The result goes into the book's working copy, "<name> (OCR).pdf" (the same file the OCR button writes;
created from the source if it does not exist yet); the source is never modified. Pages already translated are
never sent (or paid for) twice: finished pages are cached on disk and recorded inside the output file.

Every request goes through the spending guard in budget.py.

    python -m pdfocr.translate "book.pdf" [--pages 1-5] [--model claude-opus-5-5] [--effort low]
                                          [--glossary "text or file"] [--max-spend 3.00] [--yes]
    python -m pdfocr.translate --set-limit 100 [--margin 5] [--period-day 1]
    python -m pdfocr.translate --status
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import math
import os
import sys
import threading
import time
import copy
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import pymupdf
from PIL import Image

from .budget import APP_DIR, Budget, BudgetExceeded, BudgetNotSet, cost_of_usage, price_row, worst_case_cost
from .pdf_ocr import (DEFAULT_DPI, OCR_MARKER, _font_for, isolate_content, page_has_text, part_path_for,
                      render_page, write_invisible_line)
from .pdf_ocr import output_path_for as working_copy_for
from .pdflock import LOCK
from .textutil import clean_text
from .units import build_units, cover, english_turn, inter, merge_reading, turn_unit, union

DEFAULT_MODEL = "claude-opus-5-5"
DEFAULT_EFFORT = "low"        # translation needs little deliberation; raise it in the dialog for hard pages
EFFORTS = ("low", "medium", "high")
MAX_TOKENS = 16000            # per page, thinking included; also the ceiling of what one page can cost
CONCURRENCY = 3               # pages in flight at once
OCR_BATCH = 4
IMAGE_LONG_EDGE = 2576        # the high-resolution limit of current Claude models ...
IMAGE_MAX_TOKENS = 4784       # ... and their cap on visual tokens (28 px patches) per image
EMBED_NAME = "pdfocr-translation.json"
EN_MARKER = "PDFOCR-EN"
CACHE_DIR = APP_DIR / "translation_cache"
KEYRING_SERVICE, KEYRING_USER = "PDF OCR", "anthropic_api_key"

SYSTEM_PROMPT = """You are translating scanned pages of Japanese books into English for a reader who cannot read \
Japanese. The books are art books, mechanical design works, anime production materials, liner notes and magazines.

Each request gives you one page image and the text an OCR pass found on it, split into numbered blocks. Each \
block has its OCR text and its box on the page as [left, top, right, bottom] in thousandths of the page's \
width and height.

For every block, write the English translation.
- The OCR text can contain misread characters. The image is the authority: read the block's region in the \
image and translate what is printed there.
- Translate faithfully, in natural English. Keep every fact, number, unit, model code and name. Do not \
summarise, and do not add notes or explanations of your own.
- Names of people, machines, organisations and places take their established official English spelling where \
one exists (the reader's glossary below, when there is one, wins). Otherwise romanise them.
- A block that is a heading, caption or label stays as short as the original. A block whose lines are separate \
items (a specification table, a list, credits) keeps one item per line, separated by newline characters.
- Text that is already in English or in Latin letters is returned as it is, corrected against the image if \
the OCR misread it.
- If a block is not text (OCR noise, a bare page number, a decorative pattern) or cannot be read at all in \
the image, return an empty string for it.

Return every block id exactly once.

Missed text. When the request asks for it, also look over the image for Japanese text that no block covers: \
handwritten notes, slanted or curved captions, text printed sideways or upside down, labels on drawings, text \
set over artwork. Report each piece in "found", one entry per caption, note or paragraph, with:
- "box": the smallest upright rectangle that contains it, as [left, top, right, bottom] in thousandths of the \
image's width and height. Be exact: the translation is placed by this box.
- "angle": how far its reading direction is turned clockwise from ordinary left-to-right text, in degrees: 0 \
for normal horizontal text, 90 for text that runs top to bottom with its letters lying on their side, about \
20 for a note that slopes down to the right, about 340 for one that slopes up to the right.
- "vertical": true for vertical Japanese text (upright characters stacked top to bottom; its angle is 0).
- "ja": the text as written. "en": its translation, under the rules above.
Leave out text that lies inside a block's box or inside a region listed as already handled, text that is \
already in English, and decoration that only looks like writing. When the request does not ask for missed \
text, or there is none, "found" is an empty list."""

SCHEMA = {
    "type": "object",
    "properties": {
        "blocks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"id": {"type": "integer"}, "en": {"type": "string"}},
                "required": ["id", "en"],
                "additionalProperties": False,
            },
        },
        "found": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"box": {"type": "array", "items": {"type": "integer"}}, "angle": {"type": "integer"},
                               "vertical": {"type": "boolean"}, "ja": {"type": "string"}, "en": {"type": "string"}},
                "required": ["box", "angle", "vertical", "ja", "en"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["blocks", "found"],
    "additionalProperties": False,
}


# ---- credentials ------------------------------------------------------------------------------------
def stored_api_key() -> str | None:
    try:
        import keyring
        return keyring.get_password(KEYRING_SERVICE, KEYRING_USER) or None
    except Exception:
        return None


def store_api_key(key: str | None):
    """Keep the API key in the Windows Credential Manager (never in a file). None removes it."""
    import keyring
    if key:
        keyring.set_password(KEYRING_SERVICE, KEYRING_USER, key.strip())
    else:
        try:
            keyring.delete_password(KEYRING_SERVICE, KEYRING_USER)
        except Exception:
            pass


def have_credentials() -> bool:
    return bool(stored_api_key() or os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


def make_client():
    """An Anthropic client using the stored key, else whatever the environment provides.
    Retries are done here, not by the SDK, so every attempt is accounted for."""
    import anthropic
    key = stored_api_key()
    return anthropic.Anthropic(api_key=key, max_retries=0) if key else anthropic.Anthropic(max_retries=0)


def check_credentials(client=None) -> tuple[bool, str]:
    """Free check that the key is accepted (lists models; sends no billable request)."""
    try:
        client = client or make_client()
        next(iter(client.models.list(limit=1)), None)
        return True, "The API key is accepted."
    except Exception as e:
        kind, msg = classify_error(e)
        return False, msg if kind.startswith("fatal") else f"Could not reach the API: {msg}"


# ---- data -------------------------------------------------------------------------------------------
@dataclass
class TranslateResult:
    dst: Path
    output: Path | None = None        # the written file: `dst`, or a temp file to swap in when replace=False
    translated: int = 0               # pages sent to Claude this run
    blocks: int = 0                   # blocks of text translated this run
    found: int = 0                    # pieces of text the translator located itself (the OCR had missed them)
    from_cache: int = 0               # pages reused from the on-disk cache (free)
    already_done: int = 0             # pages the output file already had
    empty: int = 0                    # pages with no text
    failed: list[tuple[int, str]] = field(default_factory=list)
    cost: float = 0.0
    stopped: str = ""                 # "", "cancelled", "budget", "run cap", "credits", "auth", "network", "no limit"
    message: str = ""


def output_path_for(src: Path) -> Path:
    """The translation goes into the book's working copy, "<name> (OCR).pdf": the same file the OCR
    button writes. There is no separate English file, and the original is never modified."""
    return working_copy_for(Path(src))


def load_translation(doc: pymupdf.Document) -> dict | None:
    """The translation stored inside a PDF by this module, or None."""
    try:
        if EMBED_NAME in doc.embfile_names():
            return json.loads(doc.embfile_get(EMBED_NAME))
    except Exception:
        pass
    return None


def save_translation(doc: pymupdf.Document, store: dict):
    """Write the translation store back into the PDF."""
    if EMBED_NAME in doc.embfile_names():
        doc.embfile_del(EMBED_NAME)
    doc.embfile_add(EMBED_NAME, json.dumps(store, ensure_ascii=False).encode("utf-8"))


def forget_pages(doc: pymupdf.Document, dst: Path, pages: list[int]) -> int:
    """Drop the stored translation of `pages` (and their cache entries), so they count as untranslated.
    Returns how many of them had a translation. The invisible English text is removed by the caller."""
    n = 0
    store = load_translation(doc)
    if store:
        for p in pages:
            if store["pages"].pop(str(p), None) is not None:
                n += 1
        if n:
            save_translation(doc, store)
    for p in pages:
        _cache_forget(dst, p)
    return n


def restore_english(doc: pymupdf.Document, pages: list[int]):
    """Write the invisible English of `pages` again from the stored translation (after their text
    layers were taken off for a new OCR pass)."""
    store = load_translation(doc)
    if not store:
        return
    for p in pages:
        entry = store["pages"].get(str(p))
        if not entry or p >= doc.page_count:
            continue
        page = doc[p]
        isolate_content(page)
        for u in entry.get("units", []):
            write_unit_layers(page, u, japanese=u.get("src") == "found")


def write_unit_layers(page: pymupdf.Page, u: dict, japanese: bool = True):
    """The invisible text of one translated unit: its Japanese on its printed lines and its English over
    the block, both lying the way the unit lies on the page (see units.py for "angle")."""
    angle = int(round((u.get("angle") or 0) / 90.0)) * 90 % 360
    derot = page.derotation_matrix
    ja = (u.get("ja") or "").strip()
    if japanese and ja:
        run = (angle + (90 if u.get("vertical") else 0)) % 360
        texts, boxes = ja.split("\n"), u.get("lines") or []
        if len(boxes) != len(texts):
            texts, boxes = [" ".join(texts)], [u["bbox"]]
        for box, text in zip(boxes, texts):
            r = pymupdf.Rect(*box) * derot
            r.normalize()
            write_invisible_line(page, r, text, run)
    en = (u.get("en") or "").strip()
    if en:
        turn = english_turn(u)
        rect = english_rect(u["bbox"], page.rect, en) if turn == 0 else pymupdf.Rect(*u["bbox"])
        write_invisible_box(page, rect, en, turn)


def apply_reading(doc: pymupdf.Document, dst: Path, pno: int, ocr, mat: pymupdf.Matrix) -> tuple[int, int] | None:
    """A translated page was read again by the OCR (its old text layers already taken off). Merge the
    new reading into what the page has and write its text layers. Translations stay; blocks that are new
    wait as "pending" for the next translation run, which sends only those.

    Returns (units kept, units waiting), or None when the page has no stored translation, in which case
    the caller writes an ordinary OCR layer."""
    _cache_forget(dst, pno)
    store = load_translation(doc)
    if not store or str(pno) not in store["pages"]:
        return None
    page = doc[pno]
    entry, kept, waiting = merge_reading(store["pages"][str(pno)], build_units(ocr, ~mat), abs(page.rect))
    store["pages"][str(pno)] = entry
    save_translation(doc, store)
    isolate_content(page)
    to_display, to_page = ~mat, ~mat * page.derotation_matrix
    boxes = [u["bbox"] for u in entry["units"]]
    for block in ocr.blocks:
        min_conf = 0.5 if block.label in ("Picture", "Figure") else 0.0
        for line in block.lines:
            if not line.text or line.confidence < min_conf:
                continue
            c = pymupdf.Rect(*line.bbox) * to_display
            cx, cy = (c.x0 + c.x1) / 2, (c.y0 + c.y1) / 2
            if any(b[0] <= cx <= b[2] and b[1] <= cy <= b[3] for b in boxes):
                continue                      # a kept unit covers this spot and brings its own text
            r = pymupdf.Rect(*line.bbox) * to_page
            r.normalize()
            write_invisible_line(page, r, line.text)
    for u in entry["units"]:
        write_unit_layers(page, u)
    return kept, waiting


def turn_units(doc: pymupdf.Document, dst: Path, page: int, display_map: pymupdf.Matrix, degrees: int = 0) -> bool:
    """A page was turned `degrees` clockwise: its stored translation turns with it and stays in place on
    the page (`display_map` takes a point of the page as it was displayed to where it is displayed now)."""
    _cache_forget(dst, page)     # the cache holds positions from before the turn
    store = load_translation(doc)
    if not store or str(page) not in store["pages"]:
        return False
    entry = store["pages"][str(page)]
    for key in ("units", "pending"):
        entry[key] = [turn_unit(u, display_map, degrees) for u in entry.get(key, [])]
    save_translation(doc, store)
    return True


def english_rect(bbox, page_rect: pymupdf.Rect, text: str) -> pymupdf.Rect:
    """Where a block's English goes: its own box, widened when that box is a narrow vertical column
    (English can't be set in a 20-point-wide strip) and kept on the page."""
    r = pymupdf.Rect(*bbox)
    min_w = 130.0 if len(text) > 30 else 70.0
    if r.width < min_w:
        cx = (r.x0 + r.x1) / 2
        r.x0, r.x1 = cx - min_w / 2, cx + min_w / 2
    if r.height < 9:
        r.y1 = r.y0 + 9
    dx = max(0.0, page_rect.x0 - r.x0) - max(0.0, r.x1 - page_rect.x1)
    dy = max(0.0, page_rect.y0 - r.y0) - max(0.0, r.y1 - page_rect.y1)
    return pymupdf.Rect(r.x0 + dx, r.y0 + dy, r.x1 + dx, r.y1 + dy)


# ---- the request ------------------------------------------------------------------------------------
def image_for_api(im: Image.Image) -> tuple[str, int]:
    """JPEG, base64, sized to the model's native limits (so the API doesn't resample it again).
    Returns (base64 data, visual tokens it will be billed as)."""
    im = im.convert("RGB")
    w, h = im.size
    scale = min(1.0, IMAGE_LONG_EDGE / max(w, h))
    tokens = lambda s: math.ceil(w * s / 28) * math.ceil(h * s / 28)
    while tokens(scale) > IMAGE_MAX_TOKENS:
        scale *= 0.98
    if scale < 1.0:
        im = im.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
    quality = 88
    while True:
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=quality)
        if buf.tell() < 3_500_000 or quality <= 50:   # base64 adds a third; the API limit is 5 MB
            break
        quality -= 8
    return base64.standard_b64encode(buf.getvalue()).decode("ascii"), math.ceil(im.width / 28) * math.ceil(im.height / 28)


# A request shows the model one region of a page. The region is turned when its blocks lie turned on the
# page, so that the text to translate is always the right way up in the image.
SPLIT_PX = 4400      # a region longer than this (at 200 dpi) is sent in pieces: at the API's image size its
PIECE_PX = 3000      # small print would otherwise be too reduced to read


@dataclass
class Job:
    """One request: translate `units` (blocks waiting on a page) as seen in `region`, and, when `sweep`
    is set, report text in the region that no block covers."""
    page: int
    region: tuple[float, float, float, float]      # page points, as displayed
    turn: int                                      # the image is turned back by this much (0, 90, 180, 270)
    units: list[dict]
    known: list[list[float]]                       # boxes already handled, for the missed-text pass
    sweep: bool

    def key(self, model: str, effort: str, glossary: str) -> str:
        blob = json.dumps([model, effort, glossary, [round(v, 1) for v in self.region], self.turn, self.sweep,
                           [[u["ja"], [round(v, 1) for v in u["bbox"]]] for u in self.units],
                           sorted([round(v) for v in b] for b in self.known)], ensure_ascii=False)
        return f"p{self.page:05d}_j{hashlib.sha1(blob.encode('utf-8')).hexdigest()[:12]}"


def _turned(turn: int, u: float, v: float) -> tuple[float, float]:
    """A point of a region (0-1 across and down) in the image after the image was turned back by `turn`."""
    return {0: (u, v), 90: (v, 1 - u), 180: (1 - u, 1 - v), 270: (1 - v, u)}[turn]


def box_to_image(b, region, turn: int) -> list[int]:
    """A box in page points as [left, top, right, bottom] in thousandths of the request image."""
    w, h = region[2] - region[0], region[3] - region[1]
    pts = [_turned(turn, (x - region[0]) / w, (y - region[1]) / h) for x, y in ((b[0], b[1]), (b[2], b[3]))]
    xs, ys = sorted(p[0] for p in pts), sorted(p[1] for p in pts)
    clamp = lambda v: max(0, min(1000, round(1000 * v)))
    return [clamp(xs[0]), clamp(ys[0]), clamp(xs[1]), clamp(ys[1])]


def box_from_image(box, region, turn: int) -> list[float]:
    """The inverse of box_to_image."""
    w, h = region[2] - region[0], region[3] - region[1]
    back = (360 - turn) % 360
    pts = [_turned(back, x / 1000.0, y / 1000.0) for x, y in ((box[0], box[1]), (box[2], box[3]))]
    xs, ys = sorted(p[0] for p in pts), sorted(p[1] for p in pts)
    return [region[0] + xs[0] * w, region[1] + ys[0] * h, region[0] + xs[1] * w, region[1] + ys[1] * h]


def _pieces(region) -> list[tuple[float, float, float, float]]:
    """A region as one piece, or as overlapping pieces when it is too large to send whole."""
    px = DEFAULT_DPI / 72.0

    def spans(a: float, b: float) -> list[tuple[float, float]]:
        length = (b - a) * px
        if length <= SPLIT_PX:
            return [(a, b)]
        n = math.ceil(length / PIECE_PX)
        size = (b - a) / n
        lap = size * 0.06
        return [(max(a, a + k * size - lap), min(b, a + (k + 1) * size + lap)) for k in range(n)]

    return [(x0, y0, x1, y1) for y0, y1 in spans(region[1], region[3]) for x0, x1 in spans(region[0], region[2])]


def _home(u: dict, pieces: list) -> int:
    """The piece a block belongs to: the one its centre sits deepest inside."""
    cx, cy = (u["bbox"][0] + u["bbox"][2]) / 2, (u["bbox"][1] + u["bbox"][3]) / 2
    depth = lambda p: min(cx - p[0], p[2] - cx, cy - p[1], p[3] - cy)
    return max(range(len(pieces)), key=lambda k: depth(pieces[k]))


def _snap(angle) -> int:
    return int(round((angle or 0) / 90.0)) * 90 % 360


def page_needs(entry: dict | None, sweep: bool) -> bool:
    """Is there anything left to send for a page? A page not yet in the store needs everything."""
    return entry is None or bool(entry.get("pending")) or (sweep and not entry.get("swept"))


def plan_jobs(pno: int, page_rect, entry: dict, sweep: bool) -> list[Job]:
    """The requests a page needs: its waiting blocks, grouped by the way they lie on the page, and (with
    `sweep`, once per page) a look over the whole page for text the OCR missed."""
    rect = tuple(page_rect)
    jobs: list[Job] = []
    by_angle: dict[int, list[dict]] = {}
    for u in entry.get("pending", []):
        by_angle.setdefault(_snap(u.get("angle")), []).append(u)
    if sweep and not entry.get("swept"):
        upright = by_angle.pop(0, [])
        handled = [u["bbox"] for u in entry.get("units", [])] + [u["bbox"] for us in by_angle.values() for u in us]
        pieces = _pieces(rect)
        home = {id(u): _home(u, pieces) for u in upright}
        for k, piece in enumerate(pieces):
            mine = [u for u in upright if home[id(u)] == k]
            others = [u["bbox"] for u in upright if home[id(u)] != k]
            jobs.append(Job(pno, piece, 0, mine, [b for b in handled + others if inter(b, piece) > 0], True))
    for angle, us in by_angle.items():
        box = union([u["bbox"] for u in us])
        # some of the page around the blocks, so the model sees what they belong to
        pad = max(24.0, 0.12 * max(box[2] - box[0], box[3] - box[1]))
        need_w, need_h = max(0.0, 260 - (box[2] - box[0])) / 2, max(0.0, 260 - (box[3] - box[1])) / 2
        region = (max(rect[0], box[0] - pad - need_w), max(rect[1], box[1] - pad - need_h),
                  min(rect[2], box[2] + pad + need_w), min(rect[3], box[3] + pad + need_h))
        pieces = _pieces(region)
        home = {id(u): _home(u, pieces) for u in us}
        for k, piece in enumerate(pieces):
            mine = [u for u in us if home[id(u)] == k]
            if mine:
                jobs.append(Job(pno, piece, angle, mine, [], False))
    return jobs


def render_region(page: pymupdf.Page, region, turn: int) -> Image.Image:
    """The picture of a region for a request: sharp enough for the API's image size, and turned back so
    that text lying at `turn` degrees reads upright."""
    clip = pymupdf.Rect(*region)
    dpi = max(100.0, min(300.0, IMAGE_LONG_EDGE * 72.0 / max(clip.width, clip.height, 1.0)))
    pix = page.get_pixmap(matrix=pymupdf.Matrix(dpi / 72, dpi / 72), clip=clip, colorspace=pymupdf.csRGB, alpha=False)
    im = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    if turn:   # PIL turns anticlockwise, which undoes a clockwise turn of the text
        im = im.transpose({90: Image.Transpose.ROTATE_90, 180: Image.Transpose.ROTATE_180, 270: Image.Transpose.ROTATE_270}[turn])
    return im


def build_request(model: str, effort: str, glossary: str, image_b64: str, job: Job) -> dict:
    system = SYSTEM_PROMPT
    if glossary.strip():
        system += "\n\nReader's glossary and context:\n" + glossary.strip()
    blocks = [{"id": k, "box": box_to_image(u["bbox"], job.region, job.turn), "ocr": u["ja"],
               **({"vertical": True} if u.get("vertical") else {})} for k, u in enumerate(job.units)]
    text = "OCR blocks for this image:\n" + json.dumps(blocks, ensure_ascii=False)
    if job.sweep:
        known = [box_to_image(b, job.region, job.turn) for b in job.known]
        text += ("\n\nAlso report missed text in \"found\". Regions already handled (report nothing that lies "
                 "inside them):\n" + json.dumps(known))
    else:
        text += "\n\nThis request does not ask for missed text: return an empty \"found\" list."
    return {
        "model": model,
        "max_tokens": MAX_TOKENS,
        "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        "output_config": {"effort": effort, "format": {"type": "json_schema", "schema": SCHEMA}},
        "messages": [{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": image_b64}},
            {"type": "text", "text": text},
        ]}],
    }


def read_response(resp, job: Job) -> dict:
    """What a response says, as plain data: {"en": {block number: English}, "found": [units in page points]}."""
    text = next(b.text for b in resp.content if b.type == "text")
    data = json.loads(text)
    en = {str(int(b["id"])): clean_text(str(b["en"])).strip() for b in data["blocks"]}
    found = []
    for f in data.get("found") or []:
        box = [int(v) for v in f.get("box") or []]
        english = clean_text(str(f.get("en") or "")).strip()
        if len(box) != 4 or not english or not job.sweep:
            continue
        box = [max(0, min(1000, v)) for v in box]
        if box[2] - box[0] < 3 or box[3] - box[1] < 3:
            continue
        found.append({"bbox": [round(v, 2) for v in box_from_image(box, job.region, job.turn)], "lines": [],
                      "ja": clean_text(str(f.get("ja") or "")).strip(), "en": english, "vertical": bool(f.get("vertical")),
                      "angle": (job.turn + int(f.get("angle") or 0)) % 360, "conf": 0.0, "loose": True, "src": "found"})
    return {"en": en, "found": found}


def input_token_bound(request: dict, image_tokens: int) -> int:
    """An upper bound on the request's input tokens without a network call: a token is never shorter
    than one byte of UTF-8, and an image costs its 28-pixel patches. Plus room for message framing
    and the output schema."""
    text_bytes = len(request["system"][0]["text"].encode("utf-8")) + len(json.dumps(SCHEMA))
    for part in request["messages"][0]["content"]:
        if part["type"] == "text":
            text_bytes += len(part["text"].encode("utf-8"))
    return image_tokens + text_bytes + 1500


def classify_error(e: Exception) -> tuple[str, str]:
    """('retry' | 'page' | 'unconfirmed' | 'fatal:credits' | 'fatal:auth', message) for an API failure.

    retry: not billed, worth another attempt (rate limit, overload, server error).
    page: this request was rejected (not billed); other pages may still work.
    unconfirmed: no answer arrived, so it may or may not have been billed.
    fatal: nothing more can be sent (no credits, or the key isn't accepted)."""
    status = getattr(e, "status_code", None)
    msg = str(getattr(e, "message", "") or e)
    low = msg.lower()
    if status == 402 or getattr(e, "type", "") == "billing_error" or "credit balance" in low:
        return "fatal:credits", "Anthropic reports no API credits left on this account. " + msg
    if status in (401, 403) or "could not resolve authentication" in low:
        return "fatal:auth", "The API key was not accepted (check it in Translate > Settings). " + msg
    if status in (408, 409, 429) or (status is not None and status >= 500):
        return "retry", msg
    if status is not None and 400 <= status < 500:
        return "page", msg
    return "unconfirmed", msg or type(e).__name__


def call_claude(client, request: dict, stop: threading.Event, attempts: int = 4):
    """Send one request, retrying only failures that are not billed. Raises the last error otherwise."""
    delay = 4.0
    for attempt in range(attempts):
        try:
            return client.messages.create(**request)
        except Exception as e:
            kind, _ = classify_error(e)
            if kind != "retry" or attempt == attempts - 1 or stop.is_set():
                raise
            wait = delay
            try:
                wait = max(wait, float(e.response.headers.get("retry-after", 0)))
            except Exception:
                pass
            stop.wait(min(wait, 90.0))
            delay *= 2


# ---- cache ------------------------------------------------------------------------------------------
def _cache_dir(dst: Path) -> Path:
    key = hashlib.sha1(str(dst.resolve()).lower().encode("utf-8")).hexdigest()[:16]
    return CACHE_DIR / key


def _cache_get(dst: Path, key) -> dict | None:
    """A cached result: `key` is a page number (a whole page, as this app stored them at first), or a name
    such as a job's key or "p00012_read" (the blocks the OCR found on a page not yet in the file)."""
    name = f"p{key:05d}" if isinstance(key, int) else key
    try:
        return json.loads((_cache_dir(dst) / f"{name}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _cache_forget(dst: Path, page: int):
    """Drop everything cached for a page (its positions changed, or it was read again)."""
    try:
        for f in _cache_dir(dst).glob(f"p{page:05d}*.json"):
            f.unlink()
    except OSError:
        pass


def _cache_put(dst: Path, key, entry: dict):
    name = f"p{key:05d}" if isinstance(key, int) else key
    d = _cache_dir(dst)
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / f"{name}.tmp"
    tmp.write_text(json.dumps(entry, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, d / f"{name}.json")


# ---- writing the PDF --------------------------------------------------------------------------------
def write_invisible_box(page: pymupdf.Page, rect_display: pymupdf.Rect, text: str, angle: int = 0):
    """Invisible English text filling a box, at the largest size that fits (so search hits land on it).
    angle: 90, 180 or 270 when the block lies turned on the page; the text is then set turned with it."""
    r = pymupdf.Rect(rect_display) * page.derotation_matrix
    r.normalize()
    angle %= 360
    centre = pymupdf.Point((r.x0 + r.x1) / 2, (r.y0 + r.y1) / 2)
    lay = r
    if angle in (90, 270):     # set it in a box of the block's size before the turn, then turn that about the middle
        lay = pymupdf.Rect(centre.x - r.height / 2, centre.y - r.width / 2, centre.x + r.height / 2, centre.y + r.width / 2)
    morph = (centre, pymupdf.Matrix(-angle)) if angle else None
    font = _font_for(text) if any(ord(ch) > 0xFF for ch in text) else pymupdf.Font("helv")
    size = min(11.0, max(3.0, lay.height * 0.8))
    longest = max(text.split(), key=len, default="")
    while True:
        tw = pymupdf.TextWriter(page.rect)
        try:
            if font.text_length(longest, fontsize=size) > lay.width:
                overflow = [text]      # a word would be broken in two, and search would no longer find it
            else:
                overflow = tw.fill_textbox(lay, text, font=font, fontsize=size, warn=None)
        except Exception:
            overflow = [text]
        if not overflow:
            tw.write_text(page, render_mode=3, morph=morph)
            return
        if size <= 2.5:
            break
        size *= 0.86
    # too much text for the box even at the smallest size: one stretched line still makes it searchable
    write_invisible_line(page, r, " ".join(text.split()), angle)


def assemble(base: Path, out: Path, pages: dict[int, dict], meta: dict):
    """Write `out` = `base` with the pages in `pages` brought up to date. Each value holds the page's new
    stored "entry", the "new_units" whose invisible text has to be written, and for a bare scan the
    "ocr_lines" of its Japanese layer."""
    with LOCK:
        doc = pymupdf.open(base)
        try:
            store = load_translation(doc) or {"version": 2, "source_language": "ja", "target_language": "en", "pages": {}}
            for pno, upd in sorted(pages.items()):
                if pno >= doc.page_count:
                    continue
                page = doc[pno]
                isolate_content(page)
                if upd.get("ocr_lines") and not page_has_text(page):   # a bare scan: give it the Japanese layer too
                    for x0, y0, x1, y1, text in upd["ocr_lines"]:
                        write_invisible_line(page, pymupdf.Rect(x0, y0, x1, y1), text)
                for u in upd.get("new_units", []):
                    write_unit_layers(page, u, japanese=u.get("src") == "found")
                store["pages"][str(pno)] = upd["entry"]
            store.update(meta)
            store["version"] = 2
            save_translation(doc, store)
            md = dict(doc.metadata or {})
            kw = set((md.get("keywords") or "").split())
            kw.add(EN_MARKER)
            if all(str(i) in store["pages"] or page_has_text(doc[i]) for i in range(doc.page_count)):
                kw.add(OCR_MARKER)   # nothing left for the OCR button to do
            md["keywords"] = " ".join(sorted(kw))
            doc.set_metadata({k: v for k, v in md.items() if k not in ("format", "encryption")})
            out.parent.mkdir(parents=True, exist_ok=True)
            doc.save(out, garbage=1, deflate=True)
        finally:
            doc.close()


# ---- estimate ---------------------------------------------------------------------------------------
def pages_to_do(doc: pymupdf.Document, pages: list[int], sweep: bool) -> list[int]:
    """The pages among `pages` that still have something to send."""
    with LOCK:
        store = (load_translation(doc) or {"pages": {}})["pages"]
    return [p for p in pages if page_needs(store.get(str(p)), sweep)]


def request_sizes(doc: pymupdf.Document, pages: list[int], sweep: bool) -> list[tuple[float, float]]:
    """(width, height) in points of the region each request for `pages` would show."""
    out = []
    with LOCK:
        store = (load_translation(doc) or {"pages": {}})["pages"]
        for p in pages:
            rect = doc[p].rect
            entry = store.get(str(p))
            if entry is None:            # not read yet: the whole page, in pieces when it is very large
                regions = _pieces(tuple(rect)) if sweep else [tuple(rect)]
            else:
                regions = [j.region for j in plan_jobs(p, rect, entry, sweep)]
            out += [(r[2] - r[0], r[3] - r[1]) for r in regions]
    return out


def estimate_cost(doc: pymupdf.Document, pages: list[int], model: str, budget: Budget, sweep: bool = True) -> tuple[float, float]:
    """A rough (low, high) cost for translating `pages`. From this app's own history when there is some,
    otherwise from the size of what is sent. The spending guard, not this estimate, is what limits spending."""
    sizes = request_sizes(doc, pages, sweep)
    if not sizes:
        return 0.0, 0.0
    avg = budget.average_cost(model)
    if avg is not None:
        return len(sizes) * avg * 0.7, len(sizes) * avg * 1.5
    p = price_row(model)
    total = 0.0
    for w, h in sizes:
        mp = (w * DEFAULT_DPI / 72) * (h * DEFAULT_DPI / 72) / 1e6     # megapixels at OCR density
        dense = min(1.0, mp / 6.5)
        in_tok = min(IMAGE_MAX_TOKENS, mp * 1e6 / 784) + 1500 + 2500 * dense
        out_tok = 500 + 3000 * dense
        total += (in_tok * p["in"] + out_tok * p["out"]) / 1e6
    return total * 0.6, total * 1.7


# ---- the job ----------------------------------------------------------------------------------------
def translate_pdf(
    src: Path,
    pages: list[int] | None = None,
    model: str = DEFAULT_MODEL,
    effort: str = DEFAULT_EFFORT,
    glossary: str = "",
    client=None,
    budget: Budget | None = None,
    engine=None,
    run_cap: float | None = None,
    replace: bool = True,
    progress: Callable[[int, int, str, float], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
    sweep: bool = True,
) -> TranslateResult:
    """Translate `pages` (0-based; default all) of `src` into its working copy, "<name> (OCR).pdf".

    Only what is missing is sent: the blocks of a page that have no translation yet (all of them, for a
    page not seen before; after a page was read again, only the new ones), and, with `sweep`, one look
    per page for text the OCR could not read (handwriting, slanted or sideways text).

    run_cap: spend at most this many dollars in this run, on top of the monthly limit.
    replace=False leaves the result in a temp file (`result.output`) for the caller to swap in, for when
    the destination is open in the viewer.
    """
    src = Path(src)
    dst = output_path_for(src)
    budget = budget or Budget()
    res = TranslateResult(dst=dst)
    # build on the working copy when the book has one (it carries the OCR layer and any pages already
    # translated); otherwise start from the original, which is only read
    base = dst if dst.exists() else src
    with LOCK:
        doc = pymupdf.open(base)
        page_count = doc.page_count
        stored = (load_translation(doc) or {"pages": {}})["pages"]
    try:
        wanted = [p for p in (pages if pages is not None else range(page_count)) if 0 <= p < page_count]
        entries: dict[int, dict] = {}        # working copies of the pages this run touches
        ocr_lines: dict[int, list] = {}      # Japanese layer for pages that are bare scans
        new_units: dict[int, list] = {}      # units whose invisible text has to be written
        dirty: set[int] = set()
        to_read: list[int] = []
        for p in wanted:
            e = stored.get(str(p))
            if e is not None:
                if page_needs(e, sweep):
                    entries[p] = copy.deepcopy(e)
                else:
                    res.already_done += 1
                continue
            whole = _cache_get(dst, p)       # a whole page, cached by an earlier version of this app
            if whole and whole.get("status") in ("ok", "empty"):
                units = [dict(u, src="ocr") for u in whole.get("units", []) if (u.get("en") or "").strip()]
                entries[p] = {"model": whole.get("model", ""), "units": units, "pending": [], "swept": False}
                ocr_lines[p], new_units[p] = whole.get("ocr_lines", []), list(units)
                dirty.add(p)
                res.from_cache += 1
                continue
            read = _cache_get(dst, f"p{p:05d}_read")     # read by the OCR in a run that did not finish
            if read:
                entries[p] = {"units": [], "pending": read["pending"], "swept": False}
                ocr_lines[p] = read.get("ocr_lines", [])
            else:
                to_read.append(p)

        stop = threading.Event()
        lock = threading.Lock()
        state = {"run_spent": 0.0, "run_pending": 0.0, "net_failures": 0, "done": 0, "total": 0}
        paid_pages: set[int] = set()
        failed_sweep: set[int] = set()

        def report(msg: str):
            if progress:
                progress(state["done"], state["total"], msg, res.cost)

        def halt(reason: str, message: str):
            with lock:
                if not res.stopped:
                    res.stopped, res.message = reason, message
            stop.set()

        def take(job: Job, result: dict):
            """Put a request's answer into its page: translated blocks leave the waiting list."""
            entry = entries[job.page]
            answered = {id(u) for u in job.units}
            fresh = []
            for k, u in enumerate(job.units):
                u["en"] = result["en"].get(str(k), "")
                if u["en"].strip():
                    fresh.append(u)
            entry["pending"] = [u for u in entry.get("pending", []) if id(u) not in answered]
            n_found = 0
            for f in result.get("found", []):
                everything = entry.get("units", []) + entry["pending"] + fresh
                if any(cover(f["bbox"], q["bbox"]) > 0.5 or cover(q["bbox"], f["bbox"]) > 0.7 for q in everything):
                    continue                 # inside something already handled: the same text seen twice
                fresh.append(dict(f))
                n_found += 1
            entry.setdefault("units", []).extend(fresh)
            new_units.setdefault(job.page, []).extend(fresh)
            dirty.add(job.page)
            res.blocks += len(fresh) - n_found
            res.found += n_found

        def work(job: Job, key: str, request: dict, rid: str, worst: float):
            pno = job.page
            try:
                resp = call_claude(client, request, stop)
            except Exception as e:
                kind, msg = classify_error(e)
                with lock:
                    state["run_pending"] -= worst
                if kind == "unconfirmed":
                    budget.mark_unconfirmed(rid, msg[:120])
                    with lock:
                        state["run_spent"] += worst      # count it against this run too
                        state["net_failures"] += 1
                        many = state["net_failures"] >= 2
                    if many:
                        halt("network", "Stopped: the connection to the API failed twice. " + msg[:200])
                else:
                    budget.release(rid)                   # rejected before running: not billed
                    if kind.startswith("fatal"):
                        halt(kind.split(":")[1], msg[:300])
                with lock:
                    res.failed.append((pno, msg[:160]))
                    failed_sweep.add(pno)
                    state["done"] += 1
                report(f"Page {pno + 1}: failed")
                return
            usage = resp.usage
            cost = cost_of_usage(resp.model or request["model"], usage)
            budget.settle(rid, cost, {k: getattr(usage, k, 0) or 0 for k in
                                      ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")})
            with lock:
                state["run_pending"] -= worst
                state["run_spent"] += cost
                state["net_failures"] = 0
                res.cost += cost
            problem, result = "", None
            if resp.stop_reason == "refusal":
                problem = "the model declined this page"
            elif resp.stop_reason == "max_tokens":
                problem = "the translation did not fit in one response"
            else:
                try:
                    result = read_response(resp, job)
                except Exception as e:
                    problem = f"unreadable response ({type(e).__name__})"
            with lock:
                state["done"] += 1
                if problem:
                    res.failed.append((pno, problem))
                    failed_sweep.add(pno)
                else:
                    _cache_put(dst, key, dict(result, status="ok", model=resp.model or request["model"], cost=cost))
                    take(job, result)
                    entries[pno]["model"] = resp.model or request["model"]
                    paid_pages.add(pno)
            report(f"Page {pno + 1}: " + (problem or f"{len(job.units)} block(s)"
                                          + (f", {len(result['found'])} more found" if result["found"] else "")
                                          + f" (${cost:.3f})"))

        work_to_do = bool(to_read) or any(page_needs(e, sweep) for e in entries.values())
        if work_to_do and budget.status().limit is None:
            halt("no limit", "Set a monthly API limit first (Translate > Settings).")
        elif work_to_do:
            client = client or make_client()

        # pages the file has not seen: read them with the OCR first (local, free)
        if to_read and not stop.is_set():
            if engine is None:
                from .ocr_engine import OcrEngine
                engine = OcrEngine()
            report("Loading OCR models…")
            engine.load()
            for start in range(0, len(to_read), OCR_BATCH):
                if cancelled and cancelled():
                    halt("cancelled", "Cancelled.")
                    break
                batch = to_read[start:start + OCR_BATCH]
                report(f"Reading page {batch[0] + 1}" + (f" to {batch[-1] + 1}" if len(batch) > 1 else "") + "…")
                with LOCK:
                    rendered = [render_page(doc[p], DEFAULT_DPI) for p in batch]
                    had_text = [page_has_text(doc[p]) for p in batch]
                    derots = [doc[p].derotation_matrix for p in batch]
                for pno, (im, mat), ocr, has_text, derot in zip(batch, rendered, engine.ocr_pages([im for im, _ in rendered]), had_text, derots):
                    lines = []
                    if not has_text:   # unrotated page space, ready for write_invisible_line
                        for b in ocr.blocks:
                            for l in b.lines:
                                if l.text and l.confidence >= (0.5 if b.label in ("Picture", "Figure") else 0.0):
                                    r = pymupdf.Rect(*l.bbox) * (~mat * derot)
                                    r.normalize()
                                    lines.append([r.x0, r.y0, r.x1, r.y1, l.text])
                    pending = [u.to_dict() for u in build_units(ocr, ~mat)]
                    _cache_put(dst, f"p{pno:05d}_read", {"pending": pending, "ocr_lines": lines})
                    entries[pno] = {"units": [], "pending": pending, "swept": False}
                    ocr_lines[pno] = lines
                    dirty.add(pno)
                    if not pending:
                        res.empty += 1

        # the requests
        jobs: list[Job] = []
        if not stop.is_set():
            with LOCK:
                rects = {p: doc[p].rect for p in entries}
            for p in sorted(entries):
                jobs += plan_jobs(p, rects[p], entries[p], sweep)
        state["total"] = len(jobs)
        swept_pages = {j.page for j in jobs if j.sweep}
        with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
            futures = []
            for job in jobs:
                if stop.is_set():
                    break
                if cancelled and cancelled():
                    halt("cancelled", "Cancelled.")
                    break
                key = job.key(model, effort, glossary)
                cached = _cache_get(dst, key)
                if cached and cached.get("status") == "ok":      # paid for in a run that did not finish
                    with lock:
                        take(job, cached)
                        state["done"] += 1
                    report(f"Page {job.page + 1}: from the cache")
                    continue
                with LOCK:
                    im = render_region(doc[job.page], job.region, job.turn)
                image_b64, image_tokens = image_for_api(im)
                request = build_request(model, effort, glossary, image_b64, job)
                worst = worst_case_cost(model, input_token_bound(request, image_tokens), MAX_TOKENS)

                def try_reserve():
                    with lock:
                        over = run_cap is not None and state["run_spent"] + state["run_pending"] + worst > run_cap + 1e-9
                    if over:
                        return None, ("run cap", f"Stopped at this run's spending cap of ${run_cap:.2f}.")
                    try:
                        return budget.reserve(worst, model, f"{dst.name} p{job.page + 1}"), None
                    except BudgetNotSet as e:
                        return None, ("no limit", str(e))
                    except BudgetExceeded as e:
                        return None, ("budget", "Stopped: " + str(e))

                rid, why = try_reserve()
                if rid is None and why[0] in ("budget", "run cap") and futures:
                    # requests in flight hold worst-case reservations; let them finish and settle to
                    # their real cost, then see whether this one fits after all
                    for f in futures:
                        f.result()
                    futures = []
                    rid, why = try_reserve()
                if rid is None:
                    halt(*why)
                    break
                with lock:
                    state["run_pending"] += worst
                futures.append(pool.submit(work, job, key, request, rid, worst))
            for f in futures:
                f.result()

        # a page counts as looked over once every request of its missed-text pass has come back
        ran = {j.page for j in jobs}
        for p in swept_pages:
            still = any(j.sweep and j.page == p and _cache_get(dst, j.key(model, effort, glossary)) is None for j in jobs)
            if not still and p not in failed_sweep:
                entries[p]["swept"] = True
                dirty.add(p)
        res.translated = len(paid_pages)

        if dirty:
            report("Writing the PDF…")
            tmp = part_path_for(dst)
            assemble(base, tmp, {p: {"entry": {k: entries[p].get(k) for k in ("model", "units", "pending", "swept")},
                                     "new_units": new_units.get(p, []), "ocr_lines": ocr_lines.get(p)}
                                 for p in dirty}, {"model": model})
            if replace:
                with LOCK:
                    doc.close()
                    os.replace(tmp, dst)
                res.output = dst
            else:
                res.output = tmp
        elif dst.exists():
            res.output = dst
        if not res.message:
            parts = [f"{res.blocks} block(s) translated on {res.translated} page(s)"]
            if res.found:
                parts.append(f"{res.found} more found that the OCR had missed")
            if res.from_cache:
                parts.append(f"{res.from_cache} page(s) from the cache")
            if res.empty:
                parts.append(f"{res.empty} page(s) where the OCR found no text")
            if res.failed:
                parts.append(f"{len(res.failed)} request(s) failed")
            res.message = ", ".join(parts) + f". Cost ${res.cost:.2f}."
        return res
    finally:
        with LOCK:
            if not doc.is_closed:
                doc.close()


# ---- command line -----------------------------------------------------------------------------------
def _parse_pages(spec: str, count: int) -> list[int]:
    out: list[int] = []
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-")
            out.extend(range(int(a) - 1, min(int(b), count)))
        elif part:
            out.append(int(part) - 1)
    return out


def _print_status(budget: Budget):
    st = budget.status()
    limit = "not set" if st.limit is None else f"${st.limit:.2f}"
    print(f"Period {st.period_start:%Y-%m-%d} to {st.period_end:%Y-%m-%d}: limit {limit}, margin ${st.margin:.2f}, "
          f"spent ${st.spent:.2f}, reserved ${st.pending:.2f} (unconfirmed ${st.unconfirmed:.2f}), "
          f"available ${st.available:.2f}")


def main(argv=None):
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="Translate a Japanese PDF into English with Claude, within a spending limit.")
    ap.add_argument("pdf", type=Path, nargs="?")
    ap.add_argument("--pages", help="1-based pages, e.g. 1-5,9 (default: all)")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--effort", default=DEFAULT_EFFORT, choices=EFFORTS)
    ap.add_argument("--glossary", default="", help="names and terms to use, as text or a path to a text file")
    ap.add_argument("--max-spend", type=float, help="spend at most this many dollars in this run")
    ap.add_argument("--yes", action="store_true", help="start without asking")
    ap.add_argument("--no-sweep", action="store_true",
                    help="translate only what the OCR read; do not ask for text it missed (handwriting, slanted text)")
    ap.add_argument("--set-limit", type=float, help="set the monthly API limit in dollars")
    ap.add_argument("--margin", type=float, help="dollars of the limit to keep unspent")
    ap.add_argument("--period-day", type=int, help="day of the month the allowance renews (1-28)")
    ap.add_argument("--status", action="store_true", help="show spending for the current period")
    args = ap.parse_args(argv)

    budget = Budget()
    if args.set_limit is not None or args.margin is not None or args.period_day is not None:
        budget.set_settings(**{k: v for k, v in (("monthly_limit_usd", args.set_limit), ("margin_usd", args.margin),
                                                 ("period_start_day", args.period_day)) if v is not None})
    if args.status or args.pdf is None:
        _print_status(budget)
        return
    glossary = args.glossary
    if glossary and Path(glossary).is_file():
        glossary = Path(glossary).read_text(encoding="utf-8")
    with LOCK, pymupdf.open(args.pdf) as d:
        count = d.page_count
        pages = _parse_pages(args.pages, count) if args.pages else list(range(count))
        low, high = estimate_cost(d, pages, args.model, budget, not args.no_sweep)
    _print_status(budget)
    print(f"{args.pdf.name}: {len(pages)} page(s) with {args.model} at {args.effort} effort, roughly ${low:.2f} to ${high:.2f}"
          + (f", capped at ${args.max_spend:.2f} for this run" if args.max_spend is not None else ""))
    if not args.yes and input("Start? [y/N] ").strip().lower() != "y":
        return
    t0 = time.time()
    res = translate_pdf(args.pdf, pages, args.model, args.effort, glossary, budget=budget, run_cap=args.max_spend,
                        sweep=not args.no_sweep,
                        progress=lambda d, t, m, c: print(f"[{d}/{t}] ${c:.2f}  {m}"))
    print(res.message)
    for pno, why in res.failed:
        print(f"  page {pno + 1}: {why}")
    if res.output:
        print(f"-> {res.output}  ({time.time() - t0:.0f} s)")
    _print_status(budget)


if __name__ == "__main__":
    main()
