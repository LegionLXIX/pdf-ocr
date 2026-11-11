"""Page edits that are saved into a book's working copy: turning pages a quarter turn at a time.

    python -m pdfocr.pages "book.pdf" --rotate 9-15 [--left | --half]

The original file is never modified. Like OCR and translation, the change goes into "<name> (OCR).pdf"
(created from the original if it does not exist yet).
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path

import pymupdf

from .pdf_ocr import output_path_for, part_path_for
from .pdflock import LOCK


def rotate_pages(src: Path, pages: list[int], degrees: int, replace: bool = True) -> tuple[Path, Path, list[int]]:
    """Turn `pages` (0-based) of the book by `degrees` clockwise (a multiple of 90).

    The turn is written into the page itself, not left as a viewing flag, so the page behaves like any
    upright page afterwards: OCR, the text layers, search and the English overlay need no special cases.
    The scan is not re-encoded. Text layers and stored translation boxes turn with the page.

    Returns (working copy, file written, pages turned). replace=False leaves the new version in its
    ".part.pdf" file for a caller that has the document open."""
    from . import translate as tr
    src = Path(src)
    degrees %= 360
    dst = output_path_for(src)
    base = dst if dst.exists() else src
    target = part_path_for(dst)
    with LOCK:
        doc = pymupdf.open(base)
        try:
            turned = []
            for p in sorted(set(pages)):
                if not (0 <= p < doc.page_count) or degrees == 0:
                    continue
                page = doc[p]
                was = page.derotation_matrix                 # displayed -> unrotated, before
                page.set_rotation((page.rotation + degrees) % 360)
                display_map = was * page.rotation_matrix     # displayed before -> displayed now
                page.remove_rotation()                       # write the turn into the page content
                page.wrap_contents()                         # ... and keep it from reaching text added later
                tr.turn_units(doc, dst, p, display_map)
                turned.append(p)
            if not turned:
                return dst, base, []
            dst.parent.mkdir(parents=True, exist_ok=True)
            doc.save(target, garbage=1, deflate=True)
        finally:
            doc.close()
        if replace:
            os.replace(target, dst)
            target = dst
    return dst, target, turned


def main(argv=None):
    from .pdf_ocr import _parse_pages
    ap = argparse.ArgumentParser(description="Turn pages of a PDF; the result goes into its working copy.")
    ap.add_argument("pdf", type=Path)
    ap.add_argument("--rotate", required=True, metavar="PAGES", help="1-based pages, e.g. 9-15 or 3,7")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--left", action="store_true", help="a quarter turn anticlockwise (default: clockwise)")
    g.add_argument("--half", action="store_true", help="a half turn")
    args = ap.parse_args(argv)
    with pymupdf.open(args.pdf) as d:
        count = d.page_count
    dst, _, turned = rotate_pages(args.pdf, _parse_pages(args.rotate, count), 270 if args.left else 180 if args.half else 90)
    print(f"Turned {len(turned)} page(s) -> {dst}")


if __name__ == "__main__":
    main()
