"""Background threads: OCR jobs, document search, folder indexing."""
from __future__ import annotations

import re
from pathlib import Path

import pymupdf
from PySide6.QtCore import QThread, Signal

from .library import Library
from .ocr_engine import OcrEngine
from .pdf_ocr import ocr_pdf, output_path_for, page_has_text
from .pdflock import LOCK
from .textutil import clean_line

_engine: OcrEngine | None = None


def shared_engine() -> OcrEngine:
    """One engine per process so the models load once (~7 s, ~3 GB VRAM)."""
    global _engine
    if _engine is None:
        _engine = OcrEngine()
    return _engine


class OcrWorker(QThread):
    """OCR one or more PDFs in sequence. Emits progress for the current file and a result per file."""
    progress = Signal(str, int, int, str)   # path, done, total, message
    file_done = Signal(str, str, int)       # src path, output path, pages OCR'd
    file_failed = Signal(str, str)          # src path, error
    finished_all = Signal()

    def __init__(self, paths: list[Path], library: Library | None, dpi: int = 200, force: bool = False):
        super().__init__()
        self.paths, self.library, self.dpi, self.force = [Path(p) for p in paths], library, dpi, force
        self._cancel = False

    def cancel(self):
        self._cancel = True

    def run(self):
        engine = shared_engine()
        for src in self.paths:
            if self._cancel:
                break
            try:
                out, n, texts = ocr_pdf(
                    src, output_path_for(src), force=self.force, dpi=self.dpi, engine=engine,
                    progress=lambda d, t, m, s=src: self.progress.emit(str(s), d, t, m),
                    cancelled=lambda: self._cancel)
                if self.library is not None and n:
                    self.library.index_pdf(out, texts)
                self.file_done.emit(str(src), str(out), n)
            except InterruptedError:
                break
            except Exception as e:  # keep going with the next file
                self.file_failed.emit(str(src), f"{type(e).__name__}: {e}")
        self.finished_all.emit()


class DocSearchWorker(QThread):
    """Find every occurrence of `query` in an open document."""
    hit = Signal(int, object, str)   # page, list of rects (one per line the match spans), context
    done = Signal(int)

    def __init__(self, path: str, query: str):
        super().__init__()
        self.path, self.query = path, query
        self._cancel = False

    def cancel(self):
        self._cancel = True

    def run(self):
        count = 0
        q = self.query
        ql = q.lower()
        with LOCK:
            doc = pymupdf.open(self.path)
        with doc:
            for i in range(doc.page_count):
                if self._cancel:
                    break
                with LOCK:   # one page at a time, so the renderer and GUI thread get turns in between
                    page = doc[i]
                    rects = page.search_for(q)
                    text = clean_line(page.get_text()) if rects else ""
                if not rects:
                    continue
                tl = text.lower()
                positions = [m.start() for m in re.finditer(re.escape(ql), tl)]
                for k, group in zip(positions, _group_rects(rects, len(positions))):
                    # start at a word boundary a little before the match, keep it to about two lines
                    start = max(0, k - 40)
                    if start > 0:
                        sp = text.rfind(" ", start, k)
                        start = sp + 1 if sp >= 0 else start
                    end = k + len(q) + 70
                    ctx = ("…" if start > 0 else "") + text[start:end] + ("…" if end < len(text) else "")
                    self.hit.emit(i, group, ctx)
                    count += 1
        self.done.emit(count)


def _group_rects(rects: list, n: int) -> list[list]:
    """Group MuPDF's per-line hit rectangles into `n` matches. A match that wraps onto the next line
    comes back as two short rects; merge the `extra` most likely pairs (next line down, smallest
    combined width) so each match keeps all of its highlight boxes."""
    if n <= 0:
        return []
    if len(rects) <= n:
        groups = [[r] for r in rects]
        return groups + [groups[-1]] * (n - len(groups)) if groups else [[]] * n
    extra = len(rects) - n
    cands = []
    for j in range(len(rects) - 1):
        a, b = rects[j], rects[j + 1]
        if b.y0 >= a.y0 + 0.5 * a.height:   # b is on a lower line than a
            cands.append((a.width + b.width, j))
    merge = set()
    for _, j in sorted(cands):
        if len(merge) >= extra:
            break
        if j - 1 not in merge and j + 1 not in merge:
            merge.add(j)
    groups, j = [], 0
    while j < len(rects):
        if j in merge:
            groups.append([rects[j], rects[j + 1]])
            j += 2
        else:
            groups.append([rects[j]])
            j += 1
    while len(groups) > n:          # couldn't pair enough: fold leftovers into their neighbour
        groups[-2].extend(groups.pop())
    return groups


class IndexWorker(QThread):
    """Index every PDF under a folder; report which ones still need OCR."""
    progress = Signal(int, int, str)
    done = Signal(list)   # paths that need OCR

    def __init__(self, folder: Path, library: Library):
        super().__init__()
        self.folder, self.library = Path(folder), library
        self._cancel = False

    def cancel(self):
        self._cancel = True

    def run(self):
        pdfs = Library.find_pdfs(self.folder)
        need = []
        for k, p in enumerate(pdfs):
            if self._cancel:
                break
            self.progress.emit(k, len(pdfs), p.name)
            try:
                if not self.library.is_current(p):
                    status = self.library.index_pdf(p)
                else:
                    status = next((d for d in self.library.docs() if d.path == str(p)), None)
                if status is not None and status.needs_ocr:
                    need.append(str(p))
            except Exception:
                continue
        self.progress.emit(len(pdfs), len(pdfs), "")
        self.done.emit(need)
