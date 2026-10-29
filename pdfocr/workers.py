"""Background threads: OCR jobs, document search, folder indexing."""
from __future__ import annotations

from pathlib import Path

import pymupdf
from PySide6.QtCore import QThread, Signal

from .library import Library
from .ocr_engine import OcrEngine
from .pdf_ocr import ocr_pdf, output_path_for, page_has_text

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
    hit = Signal(int, object, str)   # page, rect, context
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
        with pymupdf.open(self.path) as doc:
            for i in range(doc.page_count):
                if self._cancel:
                    break
                page = doc[i]
                rects = page.search_for(q)
                if not rects:
                    continue
                text = " ".join(page.get_text().split())
                tl = text.lower()
                pos = 0
                for r in rects:
                    k = tl.find(ql, pos)
                    if k < 0:
                        k = tl.find(ql)
                    if k >= 0:
                        ctx = text[max(0, k - 50): k + len(q) + 60]
                        pos = k + len(q)
                    else:
                        ctx = ""
                    self.hit.emit(i, r, ("…" if k > 50 else "") + ctx + "…")
                    count += 1
        self.done.emit(count)


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
