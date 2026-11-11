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

Return every block id exactly once."""

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
        }
    },
    "required": ["blocks"],
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
class Unit:
    """One block of text to translate. `bbox` is in page points as displayed."""
    id: int
    bbox: tuple[float, float, float, float]
    ja: str
    vertical: bool = False
    en: str = ""


@dataclass
class TranslateResult:
    dst: Path
    output: Path | None = None        # the written file: `dst`, or a temp file to swap in when replace=False
    translated: int = 0               # pages sent to Claude this run
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
            if u.get("en", "").strip():
                write_invisible_box(page, english_rect(u["bbox"], page.rect, u["en"]), u["en"])


def turn_units(doc: pymupdf.Document, dst: Path, page: int, display_map: pymupdf.Matrix) -> bool:
    """A page was turned: move its stored translation boxes with it (`display_map` takes a point of the
    page as it was displayed to where it is displayed now). A stored page with no blocks is dropped."""
    _cache_forget(dst, page)     # the cache holds positions from before the turn
    store = load_translation(doc)
    if not store or str(page) not in store["pages"]:
        return False
    units = store["pages"][str(page)].get("units", [])
    if not units:
        del store["pages"][str(page)]
    for u in units:
        r = pymupdf.Rect(*u["bbox"]) * display_map
        r.normalize()
        u["bbox"] = [r.x0, r.y0, r.x1, r.y1]
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


# ---- OCR blocks -> units ----------------------------------------------------------------------------
def _is_text(s: str) -> bool:
    return any(ch.isalpha() for ch in s)


def build_units(ocr, to_page: pymupdf.Matrix) -> list[Unit]:
    """Turn OCR layout blocks into translation units: a paragraph block is one unit; text sitting in a
    picture, a table or outside any block is one unit per line (labels and cells translate separately)."""
    units: list[Unit] = []

    def add(lines):
        lines = [l for l in lines if l.text and _is_text(l.text)]
        if not lines:
            return
        vert = [(l.bbox[3] - l.bbox[1]) > 1.5 * (l.bbox[2] - l.bbox[0]) and len(l.text) >= 2 for l in lines]
        vertical = sum(vert) * 2 > len(lines)
        if vertical:   # columns read right to left
            lines = sorted(lines, key=lambda l: (-l.bbox[0], l.bbox[1]))
        x0 = min(l.bbox[0] for l in lines); y0 = min(l.bbox[1] for l in lines)
        x1 = max(l.bbox[2] for l in lines); y1 = max(l.bbox[3] for l in lines)
        r = pymupdf.Rect(x0, y0, x1, y1) * to_page
        r.normalize()
        units.append(Unit(len(units), (r.x0, r.y0, r.x1, r.y1), "\n".join(l.text for l in lines), vertical))

    for block in ocr.blocks:
        whole_page = (block.bbox[2] - block.bbox[0]) >= ocr.width * 0.98 and (block.bbox[3] - block.bbox[1]) >= ocr.height * 0.98
        lines = [l for l in block.lines if l.confidence >= (0.5 if block.label in ("Picture", "Figure") else 0.0)]
        if whole_page or block.label in ("Picture", "Figure", "Table"):
            for l in lines:
                add([l])
        else:
            add(lines)
    return units


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


def build_request(model: str, effort: str, glossary: str, image_b64: str, units: list[Unit],
                  page_rect: pymupdf.Rect) -> dict:
    system = SYSTEM_PROMPT
    if glossary.strip():
        system += "\n\nReader's glossary and context:\n" + glossary.strip()
    blocks = []
    for u in units:
        x0, y0, x1, y1 = u.bbox
        box = [round(1000 * (x0 - page_rect.x0) / page_rect.width), round(1000 * (y0 - page_rect.y0) / page_rect.height),
               round(1000 * (x1 - page_rect.x0) / page_rect.width), round(1000 * (y1 - page_rect.y0) / page_rect.height)]
        blocks.append({"id": u.id, "box": box, "ocr": u.ja, **({"vertical": True} if u.vertical else {})})
    return {
        "model": model,
        "max_tokens": MAX_TOKENS,
        "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        "output_config": {"effort": effort, "format": {"type": "json_schema", "schema": SCHEMA}},
        "messages": [{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": image_b64}},
            {"type": "text", "text": "OCR blocks for this page:\n" + json.dumps(blocks, ensure_ascii=False)},
        ]}],
    }


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


def _cache_get(dst: Path, page: int) -> dict | None:
    try:
        return json.loads((_cache_dir(dst) / f"p{page:05d}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _cache_forget(dst: Path, page: int):
    try:
        (_cache_dir(dst) / f"p{page:05d}.json").unlink()
    except OSError:
        pass


def _cache_put(dst: Path, page: int, entry: dict):
    d = _cache_dir(dst)
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / f"p{page:05d}.tmp"
    tmp.write_text(json.dumps(entry, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, d / f"p{page:05d}.json")


# ---- writing the PDF --------------------------------------------------------------------------------
def write_invisible_box(page: pymupdf.Page, rect_display: pymupdf.Rect, text: str):
    """Invisible English text filling a box, at the largest size that fits (so search hits land on it)."""
    r = pymupdf.Rect(rect_display) * page.derotation_matrix
    r.normalize()
    font = _font_for(text) if any(ord(ch) > 0xFF for ch in text) else pymupdf.Font("helv")
    size = min(11.0, max(3.0, r.height * 0.8))
    while True:
        tw = pymupdf.TextWriter(page.rect)
        try:
            overflow = tw.fill_textbox(r, text, font=font, fontsize=size, warn=None)
        except Exception:
            overflow = [text]
        if not overflow:
            tw.write_text(page, render_mode=3)
            return
        if size <= 2.5:
            break
        size *= 0.86
    # too much text for the box even at the smallest size: one stretched line still makes it searchable
    write_invisible_line(page, r, " ".join(text.split()))


def assemble(base: Path, out: Path, new_pages: dict[int, dict], meta: dict):
    """Write `out` = `base` plus the text layers and stored translation of `new_pages` (page -> cache entry)."""
    with LOCK:
        doc = pymupdf.open(base)
        try:
            store = load_translation(doc) or {"version": 1, "source_language": "ja", "target_language": "en", "pages": {}}
            for pno, entry in sorted(new_pages.items()):
                if str(pno) in store["pages"] or pno >= doc.page_count:
                    continue
                page = doc[pno]
                isolate_content(page)
                if not page_has_text(page):   # a bare scan: give it the Japanese layer too
                    for x0, y0, x1, y1, text in entry.get("ocr_lines", []):
                        write_invisible_line(page, pymupdf.Rect(x0, y0, x1, y1), text)
                units = [u for u in entry.get("units", []) if u.get("en", "").strip()]
                for u in units:
                    write_invisible_box(page, english_rect(u["bbox"], page.rect, u["en"]), u["en"])
                store["pages"][str(pno)] = {"model": entry.get("model", ""), "units": units}
            store.update(meta)
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
def estimate_cost(doc: pymupdf.Document, pages: list[int], model: str, budget: Budget) -> tuple[float, float]:
    """A rough (low, high) cost for translating `pages`. From this app's own history when there is some,
    otherwise from page size. The spending guard, not this estimate, is what limits spending."""
    if not pages:
        return 0.0, 0.0
    avg = budget.average_cost(model)
    if avg is not None:
        return len(pages) * avg * 0.7, len(pages) * avg * 1.4
    p = price_row(model)
    total = 0.0
    with LOCK:
        sizes = [(doc[i].rect.width, doc[i].rect.height) for i in pages]
    for w, h in sizes:
        mp = (w * DEFAULT_DPI / 72) * (h * DEFAULT_DPI / 72) / 1e6     # megapixels at OCR density
        dense = min(1.0, mp / 6.5)                                       # a full spread is ~6.5 MP
        in_tok = min(IMAGE_MAX_TOKENS, mp * 1e6 / 784) + 900 + 2500 * dense
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
) -> TranslateResult:
    """Translate `pages` (0-based; default all) of `src` into its working copy, "<name> (OCR).pdf".

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
        done_in_file = {int(k) for k in (load_translation(doc) or {"pages": {}})["pages"]}
    try:
        wanted = [p for p in (pages if pages is not None else range(page_count)) if 0 <= p < page_count]
        res.already_done = sum(1 for p in wanted if p in done_in_file)
        todo = [p for p in wanted if p not in done_in_file]
        new_pages: dict[int, dict] = {}
        to_send: list[int] = []
        for p in todo:
            entry = _cache_get(dst, p)
            if entry and entry.get("status") in ("ok", "empty"):
                new_pages[p] = entry
                res.from_cache += 1
            else:
                to_send.append(p)

        stop = threading.Event()
        lock = threading.Lock()
        state = {"run_spent": 0.0, "run_pending": 0.0, "net_failures": 0, "done": 0}
        total = len(to_send)

        def report(msg: str):
            if progress:
                progress(state["done"], total, msg, res.cost)

        def halt(reason: str, message: str):
            with lock:
                if not res.stopped:
                    res.stopped, res.message = reason, message
            stop.set()

        def work(pno: int, units: list[Unit], request: dict, rid: str, worst: float, entry: dict):
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
            problem = ""
            if resp.stop_reason == "refusal":
                problem = "the model declined this page"
            elif resp.stop_reason == "max_tokens":
                problem = "the page's translation did not fit in one response"
            else:
                try:
                    text = next(b.text for b in resp.content if b.type == "text")
                    by_id = {int(b["id"]): str(b["en"]) for b in json.loads(text)["blocks"]}
                    for u in units:
                        u.en = clean_text(by_id.get(u.id, "")).strip()
                except Exception as e:
                    problem = f"unreadable response ({type(e).__name__})"
            with lock:
                state["done"] += 1
                if problem:
                    res.failed.append((pno, problem))
                else:
                    entry.update(status="ok", model=resp.model or request["model"], cost=cost,
                                 units=[{"bbox": list(u.bbox), "ja": u.ja, "en": u.en, "vertical": u.vertical} for u in units])
                    _cache_put(dst, pno, entry)
                    new_pages[pno] = entry
                    res.translated += 1
            report(f"Page {pno + 1}: " + (problem or f"translated (${cost:.3f})"))

        if to_send:
            if budget.status().limit is None:
                halt("no limit", "Set a monthly API limit first (Translate > Settings).")
            else:
                client = client or make_client()
                if engine is None:
                    from .ocr_engine import OcrEngine
                    engine = OcrEngine()
                report("Loading OCR models…")
                engine.load()
        with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
            for start in range(0, len(to_send), OCR_BATCH):
                if stop.is_set():
                    break
                if cancelled and cancelled():
                    halt("cancelled", "Cancelled.")
                    break
                batch = to_send[start:start + OCR_BATCH]
                with LOCK:
                    rendered = [render_page(doc[p], DEFAULT_DPI) for p in batch]
                    had_text = [page_has_text(doc[p]) for p in batch]
                    rects = [(doc[p].rect, doc[p].derotation_matrix) for p in batch]
                ocr_results = engine.ocr_images([im for im, _ in rendered])
                futures = []
                for pno, (im, mat), ocr, has_text, (prect, derot) in zip(batch, rendered, ocr_results, had_text, rects):
                    if stop.is_set():
                        break
                    units = build_units(ocr, ~mat)
                    entry = {"page": pno, "status": "empty", "units": [], "ocr_lines": []}
                    if not has_text:   # unrotated page space, ready for write_invisible_line
                        for b in ocr.blocks:
                            for l in b.lines:
                                if l.text and l.confidence >= (0.5 if b.label in ("Picture", "Figure") else 0.0):
                                    r = pymupdf.Rect(*l.bbox) * (~mat * derot)
                                    r.normalize()
                                    entry["ocr_lines"].append([r.x0, r.y0, r.x1, r.y1, l.text])
                    if not units:
                        _cache_put(dst, pno, entry)
                        new_pages[pno] = entry
                        with lock:
                            res.empty += 1
                            state["done"] += 1
                        report(f"Page {pno + 1}: no text")
                        continue
                    image_b64, image_tokens = image_for_api(im)
                    request = build_request(model, effort, glossary, image_b64, units, prect)
                    worst = worst_case_cost(model, input_token_bound(request, image_tokens), MAX_TOKENS)

                    def try_reserve():
                        with lock:
                            over = run_cap is not None and state["run_spent"] + state["run_pending"] + worst > run_cap + 1e-9
                        if over:
                            return None, ("run cap", f"Stopped at this run's spending cap of ${run_cap:.2f}.")
                        try:
                            return budget.reserve(worst, model, f"{dst.name} p{pno + 1}"), None
                        except BudgetNotSet as e:
                            return None, ("no limit", str(e))
                        except BudgetExceeded as e:
                            return None, ("budget", "Stopped: " + str(e))

                    rid, why = try_reserve()
                    if rid is None and why[0] in ("budget", "run cap") and futures:
                        # pages in flight hold worst-case reservations; let them finish and settle to
                        # their real cost, then see whether this page fits after all
                        for f in futures:
                            f.result()
                        futures = []
                        rid, why = try_reserve()
                    if rid is None:
                        halt(*why)
                        break
                    with lock:
                        state["run_pending"] += worst
                    futures.append(pool.submit(work, pno, units, request, rid, worst, entry))
                for f in futures:
                    f.result()

        if new_pages:
            report("Writing the PDF…")
            tmp = part_path_for(dst)
            assemble(base, tmp, new_pages, {"model": model})
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
            parts = [f"{res.translated} page(s) translated"]
            if res.from_cache:
                parts.append(f"{res.from_cache} from cache")
            if res.empty:
                parts.append(f"{res.empty} without text")
            if res.failed:
                parts.append(f"{len(res.failed)} failed")
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
        low, high = estimate_cost(d, pages, args.model, budget)
    _print_status(budget)
    print(f"{args.pdf.name}: {len(pages)} page(s) with {args.model} at {args.effort} effort, roughly ${low:.2f} to ${high:.2f}"
          + (f", capped at ${args.max_spend:.2f} for this run" if args.max_spend is not None else ""))
    if not args.yes and input("Start? [y/N] ").strip().lower() != "y":
        return
    t0 = time.time()
    res = translate_pdf(args.pdf, pages, args.model, args.effort, glossary, budget=budget, run_cap=args.max_spend,
                        progress=lambda d, t, m, c: print(f"[{d}/{t}] ${c:.2f}  {m}"))
    print(res.message)
    for pno, why in res.failed:
        print(f"  page {pno + 1}: {why}")
    if res.output:
        print(f"-> {res.output}  ({time.time() - t0:.0f} s)")
    _print_status(budget)


if __name__ == "__main__":
    main()
