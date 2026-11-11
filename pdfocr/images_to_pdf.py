"""Build a PDF book from page images (a folder of scans, or a hand-ordered list).

    python -m pdfocr.images_to_pdf "<folder>" [-o out.pdf] [--sort name|mtime] [--split] [--ltr]
                                              [--each-subfolder] [--overwrite]

JPEG and PNG files are embedded as they are (no re-encoding, so no quality loss and the PDF is about the
size of the images). Other formats (WebP, BMP, TIFF, still GIF) are converted losslessly to PNG first.
Animated GIFs are skipped. The source images are never modified.
"""
from __future__ import annotations

import argparse
import io
import re
from pathlib import Path
from typing import Callable, Iterable

import pymupdf
from PIL import Image, ImageOps

from .pdflock import LOCK

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff", ".gif"}
DIRECT_EXTS = {".jpg", ".jpeg", ".png"}   # MuPDF embeds these without re-encoding
ASSUMED_DPI = 200       # page size = pixels at this density; equals the OCR render density, so OCR sees native pixels
SPREAD_RATIO = 1.15     # wider than this (width / height) counts as a two-page spread


def natural_key(name: str):
    """Sort key that orders 'page 2' before 'page 10'."""
    return [(0, int(t)) if t.isdigit() else (1, t.lower()) for t in re.split(r"(\d+)", name) if t != ""]


def list_images(folder: Path, sort: str = "name", recursive: bool = False) -> list[Path]:
    folder = Path(folder)
    it = folder.rglob("*") if recursive else folder.iterdir()
    files = [p for p in it if p.is_file() and p.suffix.lower() in IMAGE_EXTS]
    if sort == "mtime":
        files.sort(key=lambda p: (p.stat().st_mtime, natural_key(p.name)))
    else:
        files.sort(key=lambda p: natural_key(str(p.relative_to(folder))))
    return files


def _load(path: Path) -> tuple[dict, int, int] | None:
    """How to embed one image: (insert_image kwargs, width px, height px), or None to skip it."""
    with Image.open(path) as im:
        if getattr(im, "is_animated", False) and getattr(im, "n_frames", 1) > 1:
            return None
        orientation = im.getexif().get(0x0112, 1)
        direct = path.suffix.lower() in DIRECT_EXTS and orientation in (0, 1) and im.mode not in ("P", "PA")
        if direct:
            return {"filename": str(path)}, im.width, im.height
        # needs converting: apply the EXIF rotation and store losslessly
        im = ImageOps.exif_transpose(im)
        if im.mode not in ("RGB", "L", "RGBA"):
            im = im.convert("RGBA" if "A" in im.mode or "transparency" in im.info else "RGB")
        buf = io.BytesIO()
        im.save(buf, format="PNG", optimize=False)
        return {"stream": buf.getvalue()}, im.width, im.height


def images_to_pdf(
    images: Iterable[Path],
    dst: Path,
    split_spreads: bool = False,
    rtl: bool = True,
    dpi: int = ASSUMED_DPI,
    title: str | None = None,
    overwrite: bool = False,
    progress: Callable[[int, int, str], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> tuple[Path, int, list[str]]:
    """Write `images`, in the order given, to `dst`. Returns (path, pages written, skipped file names).

    split_spreads: an image wider than it is tall becomes two pages; `rtl` puts the right half first,
    the reading order of Japanese books.
    """
    images = [Path(p) for p in images]
    dst = Path(dst)
    if dst.exists() and not overwrite:
        raise FileExistsError(f"{dst} already exists")
    scale = 72.0 / dpi
    skipped: list[str] = []
    pages = 0
    with LOCK:
        doc = pymupdf.open()
    try:
        for k, path in enumerate(images):
            if cancelled and cancelled():
                raise InterruptedError("cancelled")
            if progress:
                progress(k, len(images), path.name)
            try:
                loaded = _load(path)
            except Exception as e:
                skipped.append(f"{path.name} ({type(e).__name__})")
                continue
            if loaded is None:
                skipped.append(f"{path.name} (animated)")
                continue
            kwargs, w, h = loaded
            pw, ph = w * scale, h * scale
            with LOCK:
                if split_spreads and w > h * SPREAD_RATIO:
                    # two half-width pages showing the same embedded image, so nothing is re-encoded
                    xref = 0
                    for half in ((1, 0) if rtl else (0, 1)):
                        page = doc.new_page(width=pw / 2, height=ph)
                        rect = pymupdf.Rect(-half * pw / 2, 0, pw - half * pw / 2, ph)
                        if xref:
                            page.insert_image(rect, xref=xref)
                        else:
                            xref = page.insert_image(rect, **kwargs)
                        pages += 1
                else:
                    page = doc.new_page(width=pw, height=ph)
                    page.insert_image(page.rect, **kwargs)
                    pages += 1
        if pages == 0:
            raise ValueError("no usable images")
        with LOCK:
            doc.set_metadata({"title": title or dst.stem, "creator": "PDF OCR (images to PDF)"})
            dst.parent.mkdir(parents=True, exist_ok=True)
            doc.save(dst, deflate=True)
    finally:
        with LOCK:
            doc.close()
    if progress:
        progress(len(images), len(images), "")
    return dst, pages, skipped


def main(argv=None):
    ap = argparse.ArgumentParser(description="Build a PDF from a folder of page images.")
    ap.add_argument("folder", type=Path)
    ap.add_argument("-o", "--output", type=Path, help="output PDF (default: '<folder name>.pdf' beside the folder)")
    ap.add_argument("--sort", choices=["name", "mtime"], default="name",
                    help="page order: by file name (natural) or by modified time")
    ap.add_argument("--split", action="store_true", help="split two-page spreads into single pages")
    ap.add_argument("--ltr", action="store_true",
                    help="with --split: left half first (default is right first, as in Japanese books)")
    ap.add_argument("--each-subfolder", action="store_true", help="one PDF per subfolder instead of one for the folder")
    ap.add_argument("--overwrite", action="store_true")
    args = ap.parse_args(argv)
    try:
        import sys
        sys.stdout.reconfigure(errors="replace")   # file names the console can't show must not stop the run
    except Exception:
        pass

    jobs: list[tuple[Path, Path]] = []
    if args.each_subfolder:
        for sub in sorted((p for p in args.folder.iterdir() if p.is_dir()), key=lambda p: natural_key(p.name)):
            jobs.append((sub, args.folder / f"{sub.name}.pdf"))
    else:
        jobs.append((args.folder, args.output or args.folder.with_name(f"{args.folder.name}.pdf")))
    for folder, out in jobs:
        imgs = list_images(folder, args.sort)
        if not imgs:
            print(f"{folder.name}: no images, skipped")
            continue
        try:
            dst, n, skipped = images_to_pdf(imgs, out, args.split, not args.ltr, overwrite=args.overwrite,
                                            title=folder.name)
        except FileExistsError:
            print(f"{out.name}: already exists (use --overwrite)")
            continue
        print(f"{dst.name}: {n} pages, {dst.stat().st_size / 1e6:.0f} MB" + (f"; skipped {skipped}" if skipped else ""))


if __name__ == "__main__":
    main()
