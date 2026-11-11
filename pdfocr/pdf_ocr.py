"""Make a PDF searchable: OCR pages that have no text and write an invisible text layer.

Usage as a script:
    python -m pdfocr.pdf_ocr "in.pdf" [-o "out.pdf"] [--pages 1-3,7] [--force] [--dpi 200]

The original file is never modified; the default output is "<name> (OCR).pdf" beside it.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path
from typing import Callable, Iterable

import pymupdf
from PIL import Image

from .ocr_engine import OcrEngine, PageOcr, SKIP_LABELS
from .pdflock import LOCK

DEFAULT_DPI = 200
BATCH = 4  # pages per OCR call; the 4090 has room for more but this keeps progress responsive
MIN_TEXT_CHARS = 30  # pages with at least this much real text are left alone
OCR_MARKER = "PDFOCR-done"  # stamped into the PDF keywords once a file has been OCR'd
VERTICAL_ROTATION = -90     # degrees for vertical text; sign checked by the vertical-text test (chars must run downward)


WORK_SUFFIX = " (OCR)"


def output_path_for(src: Path) -> Path:
    """The working copy of a book: "<name> (OCR).pdf" beside the original. It holds the OCR text layer and,
    once translated, the English layer too. A working copy is its own output, so it is updated in place."""
    src = Path(src)
    stem = src.stem
    for old in (" (EN)", WORK_SUFFIX):
        if stem.endswith(old):
            stem = stem[: -len(old)]
    return src.with_name(f"{stem}{WORK_SUFFIX}.pdf")


def part_path_for(dst: Path) -> Path:
    """Where a new version of `dst` is written before it replaces the old one."""
    return dst.with_name(dst.stem + ".part.pdf")


def page_has_text(page: pymupdf.Page) -> bool:
    return len(page.get_text().strip()) >= MIN_TEXT_CHARS


def text_kinds(page: pymupdf.Page) -> tuple[bool, bool]:
    """(has visible text, has invisible text). Invisible text is a layer this app wrote (OCR or English);
    visible text is the document's own."""
    visible = invisible = False
    for span in page.get_texttrace():
        if not any(ch[0] > 32 for ch in span.get("chars", ())):
            continue
        if span.get("type") == 3:
            invisible = True
        else:
            visible = True
    return visible, invisible


def strip_text_layers(page: pymupdf.Page) -> bool:
    """Remove the invisible text this app wrote on a page (the OCR layer and the English layer).
    The scan, drawings and any visible text of the document are left exactly as they are."""
    if not text_kinds(page)[1]:
        return False
    page.add_redact_annot(page.rect)
    page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE, graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
                          text=pymupdf.PDF_REDACT_TEXT_REMOVE_INVISIBLE)
    return True


def is_ocr_done(doc: pymupdf.Document) -> bool:
    return OCR_MARKER in (doc.metadata or {}).get("keywords", "")


def pages_needing_ocr(doc: pymupdf.Document) -> list[int]:
    """Pages without a text layer. A file we already OCR'd is done, even if some pages are pure artwork."""
    if is_ocr_done(doc):
        return []
    return [i for i in range(doc.page_count) if not page_has_text(doc[i])]


def render_page(page: pymupdf.Page, dpi: int) -> tuple[Image.Image, pymupdf.Matrix]:
    """Render the page as displayed (rotation applied). Returns the image and the matrix used."""
    mat = pymupdf.Matrix(dpi / 72, dpi / 72)
    pix = page.get_pixmap(matrix=mat, colorspace=pymupdf.csRGB, alpha=False)
    return Image.frombytes("RGB", (pix.width, pix.height), pix.samples), mat


def isolate_content(page: pymupdf.Page):
    """Make sure text added to the page starts from a clean graphics state. A page whose own content
    leaves a transformation in force (a turned page does, and so do some scanners' files) would otherwise
    pass that transformation on to our text, which then lands somewhere else on the page."""
    if not page.is_wrapped:
        page.wrap_contents()


def write_text_layer(page: pymupdf.Page, ocr: PageOcr, mat: pymupdf.Matrix) -> int:
    """Insert OCR words as invisible text positioned over the scan. Returns word count."""
    isolate_content(page)
    # pixel -> page (rotated display) -> unrotated page space, which insert_text uses
    inv = ~mat * page.derotation_matrix
    n = 0
    for block in ocr.blocks:
        # text found inside pictures (callouts on diagrams) is kept unless the OCR was unsure of it
        min_conf = 0.5 if block.label in SKIP_LABELS else 0.0
        for line in block.lines:
            if not line.text or line.confidence < min_conf:
                continue
            lr = pymupdf.Rect(*line.bbox) * inv
            lr.normalize()
            if write_invisible_line(page, lr, line.text):
                n += len(line.text.split())
    return n


_FONTS: dict[str, pymupdf.Font] = {}


def _font_for(text: str) -> pymupdf.Font:
    """Helvetica for Latin text; MuPDF's built-in CJK font once a line has kana, kanji or full-width forms."""
    name = "japan" if any(ch >= "⺀" for ch in text) else "helv"
    if name not in _FONTS:
        _FONTS[name] = pymupdf.Font(name)
    return _FONTS[name]


def is_vertical(rect: pymupdf.Rect, text: str) -> bool:
    """A tall, narrow line of two or more characters: vertical Japanese text."""
    return len(text) >= 2 and rect.height > 1.5 * rect.width


def write_invisible_line(page: pymupdf.Page, lr: pymupdf.Rect, text: str) -> bool:
    """Write one line of invisible text stretched over `lr` (unrotated page space).

    Surya 0.17 gives no usable per-word boxes, so a line is one string stretched to the line's length;
    words then land roughly where they are on the scan. Vertical lines are written top to bottom.
    """
    if lr.is_empty or lr.height < 1 or lr.width < 1:
        return False
    font = _font_for(text)
    tw = pymupdf.TextWriter(page.rect)
    if is_vertical(lr, text):
        fontsize = lr.width * 0.85
        natural = font.text_length(text, fontsize=fontsize)
        if natural <= 0:
            return False
        scale = max(0.3, min(3.0, lr.height / natural))
        # rotate a quarter turn clockwise about the top of the column: the text then runs downward
        origin = pymupdf.Point(lr.x0 + lr.width * 0.22, lr.y0)
        tw.append(origin, text, font=font, fontsize=fontsize)
        tw.write_text(page, render_mode=3, morph=(origin, pymupdf.Matrix(scale, 1) * pymupdf.Matrix(VERTICAL_ROTATION)))
        return True
    fontsize = lr.height * 0.85
    natural = font.text_length(text, fontsize=fontsize)
    if natural <= 0:
        return False
    scale = max(0.3, min(3.0, lr.width / natural))
    origin = pymupdf.Point(lr.x0, lr.y1 - lr.height * 0.22)
    tw.append(origin, text, font=font, fontsize=fontsize)
    morph = (origin, pymupdf.Matrix(scale, 1)) if abs(scale - 1) > 0.02 else None
    tw.write_text(page, render_mode=3, morph=morph)  # 3 = invisible
    return True


def ocr_pdf(
    src: Path,
    dst: Path | None = None,
    pages: Iterable[int] | None = None,
    force: bool = False,
    dpi: int = DEFAULT_DPI,
    engine: OcrEngine | None = None,
    progress: Callable[[int, int, str], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
    replace: bool = True,
    fresh: bool = False,
) -> tuple[Path, int, list[str]]:
    """OCR `src` into `dst`. Returns (output path, pages OCR'd, page texts in page order for indexing).

    force reads pages again even when they have a text layer: the old layer is taken off first, and a
    page's English is written back from the translation stored in the file. fresh (with force) is for a
    page whose old reading was wrong, such as one that was sideways: its translation is dropped as well,
    so the page can be translated again from the new reading.

    When `src` is already a working copy (dst == src) the new version is written beside it and then
    replaces it. replace=False leaves that new version in its ".part.pdf" file and returns that path,
    for a caller that has the document open and must let go of it first."""
    src = Path(src)
    dst = Path(dst) if dst else output_path_for(src)
    in_place = dst.resolve() == src.resolve()
    target = part_path_for(dst) if in_place else dst
    engine = engine or OcrEngine()
    with LOCK:
        doc = pymupdf.open(src)
        todo = list(pages) if pages is not None else list(range(doc.page_count))
        todo = [i for i in todo if 0 <= i < doc.page_count]
        stripped: list[int] = []
        if not force:
            todo = [i for i in todo if not page_has_text(doc[i])]
        else:
            keep = []
            for i in todo:
                visible, invisible = text_kinds(doc[i])
                if visible and invisible and not fresh:
                    continue          # the document's own text plus a layer of ours: nothing to redo
                if strip_text_layers(doc[i]):
                    stripped.append(i)
                keep.append(i)
            todo = keep
        texts = [""] * doc.page_count
        for i in range(doc.page_count):
            if i not in todo:
                texts[i] = doc[i].get_text()

    if progress:
        progress(0, len(todo), "Loading OCR models…")
    engine.load()
    done = 0
    for start in range(0, len(todo), BATCH):
        if cancelled and cancelled():
            with LOCK:
                doc.close()
            raise InterruptedError("OCR cancelled")
        batch = todo[start:start + BATCH]
        with LOCK:
            rendered = [render_page(doc[i], dpi) for i in batch]
        results = engine.ocr_images([im for im, _ in rendered])   # GPU work, outside the lock
        with LOCK:
            for i, (im, mat), res in zip(batch, rendered, results):
                write_text_layer(doc[i], res, mat)
                texts[i] = res.text
                done += 1
        if progress:
            progress(done, len(todo), f"Page {batch[-1] + 1} of {doc.page_count}")

    with LOCK:
        if force and todo:
            from . import translate as tr   # imported here: translate imports this module
            if fresh:
                tr.forget_pages(doc, dst, todo)
            else:
                tr.restore_english(doc, stripped)
        if todo:
            meta = dict(doc.metadata or {})
            kw = meta.get("keywords") or ""
            # the stamp means "nothing left for the OCR button": not true after a pass over chosen pages
            # of a book whose other pages are still bare scans
            whole = pages is None or all(i in todo or page_has_text(doc[i]) for i in range(doc.page_count))
            if whole and OCR_MARKER not in kw:
                meta["keywords"] = (kw + " " + OCR_MARKER).strip()
            doc.set_metadata({k: v for k, v in meta.items() if k not in ("format", "encryption")})
            dst.parent.mkdir(parents=True, exist_ok=True)
            doc.save(target, garbage=1, deflate=True)
        doc.close()
        if todo and in_place and replace:
            os.replace(target, dst)
            target = dst
    return (target if todo else dst), len(todo), texts


def _parse_pages(spec: str, count: int) -> list[int]:
    out = []
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-")
            out.extend(range(int(a) - 1, min(int(b), count)))
        elif part:
            out.append(int(part) - 1)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("pdf", type=Path)
    ap.add_argument("-o", "--output", type=Path)
    ap.add_argument("--pages", help="1-based pages, e.g. 1-3,7 (default: all pages without text)")
    ap.add_argument("--force", action="store_true", help="OCR even pages that already have text")
    ap.add_argument("--dpi", type=int, default=DEFAULT_DPI)
    args = ap.parse_args(argv)

    with pymupdf.open(args.pdf) as d:
        count = d.page_count
    pages = _parse_pages(args.pages, count) if args.pages else None
    t0 = time.time()

    def prog(done, total, msg):
        print(f"\r[{done}/{total}] {msg}        ", end="", flush=True)

    out, n, _ = ocr_pdf(args.pdf, args.output, pages, args.force, args.dpi, progress=prog)
    print(f"\nOCR'd {n} pages in {time.time() - t0:.0f} s -> {out}")


if __name__ == "__main__":
    main()
