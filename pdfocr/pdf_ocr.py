"""Make a PDF searchable: OCR pages that have no text and write an invisible text layer.

Usage as a script:
    python -m pdfocr.pdf_ocr "in.pdf" [-o "out.pdf"] [--pages 1-3,7] [--force] [--dpi 200]

The original file is never modified; the default output is "<name> (OCR).pdf" beside it.
"""
from __future__ import annotations

import argparse
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


def output_path_for(src: Path) -> Path:
    return src.with_name(f"{src.stem} (OCR).pdf")


def page_has_text(page: pymupdf.Page) -> bool:
    return len(page.get_text().strip()) >= MIN_TEXT_CHARS


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


def write_text_layer(page: pymupdf.Page, ocr: PageOcr, mat: pymupdf.Matrix) -> int:
    """Insert OCR words as invisible text positioned over the scan. Returns word count."""
    # pixel -> page (rotated display) -> unrotated page space, which insert_text uses
    inv = ~mat * page.derotation_matrix
    font = pymupdf.Font("helv")
    n = 0
    for block in ocr.blocks:
        if block.label in SKIP_LABELS:
            continue
        for line in block.lines:
            lr = pymupdf.Rect(*line.bbox) * inv
            lr.normalize()
            if lr.is_empty or lr.height < 1:
                continue
            if not line.text:
                continue
            # Surya 0.17 gives no usable per-word boxes, so each line is written as one string
            # stretched to the line's width; words then land roughly where they are on the scan.
            fontsize = lr.height * 0.85
            natural = font.text_length(line.text, fontsize=fontsize)
            if natural <= 0:
                continue
            scale = max(0.3, min(3.0, lr.width / natural))
            origin = pymupdf.Point(lr.x0, lr.y1 - lr.height * 0.22)
            tw = pymupdf.TextWriter(page.rect)
            tw.append(origin, line.text, font=font, fontsize=fontsize)
            morph = (origin, pymupdf.Matrix(scale, 1)) if abs(scale - 1) > 0.02 else None
            tw.write_text(page, render_mode=3, morph=morph)  # 3 = invisible
            n += len(line.text.split())
    return n


def ocr_pdf(
    src: Path,
    dst: Path | None = None,
    pages: Iterable[int] | None = None,
    force: bool = False,
    dpi: int = DEFAULT_DPI,
    engine: OcrEngine | None = None,
    progress: Callable[[int, int, str], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> tuple[Path, int, list[str]]:
    """OCR `src` into `dst`. Returns (output path, pages OCR'd, page texts in page order for indexing)."""
    src = Path(src)
    dst = Path(dst) if dst else output_path_for(src)
    engine = engine or OcrEngine()
    with LOCK:
        doc = pymupdf.open(src)
        todo = list(pages) if pages is not None else list(range(doc.page_count))
        if not force:
            todo = [i for i in todo if not page_has_text(doc[i])]
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
        if todo:
            meta = dict(doc.metadata or {})
            kw = meta.get("keywords") or ""
            if OCR_MARKER not in kw:
                meta["keywords"] = (kw + " " + OCR_MARKER).strip()
            doc.set_metadata({k: v for k, v in meta.items() if k not in ("format", "encryption")})
            dst.parent.mkdir(parents=True, exist_ok=True)
            doc.save(dst, garbage=1, deflate=True)
        doc.close()
    return dst, len(todo), texts


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
