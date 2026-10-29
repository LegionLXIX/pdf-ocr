"""PDF OCR main window: viewer + search + OCR + library."""
from __future__ import annotations

import ctypes
import sys
from pathlib import Path

import pymupdf
from PySide6.QtCore import QSettings, Qt, QTimer, QSize
from PySide6.QtGui import QAction, QActionGroup, QKeySequence, QIcon
from PySide6.QtWidgets import (QApplication, QDialog, QDockWidget, QFileDialog, QHBoxLayout, QLabel, QLineEdit,
                               QListWidget, QListWidgetItem, QMainWindow, QMessageBox, QProgressBar, QPushButton,
                               QRadioButton, QSpinBox, QToolBar, QVBoxLayout, QWidget, QStyle, QFrame, QTextBrowser)

from .library import Library, LibraryHit
from .pdf_ocr import output_path_for, pages_needing_ocr
from .viewer import PdfView, ThumbnailList
from .workers import DocSearchWorker, IndexWorker, OcrWorker

APP_NAME = "PDF OCR"
APP_ID = "PDFOCR.App"  # AppUserModelID: taskbar shows our icon, not Python's (must match register.py)
ICON_PATH = Path(__file__).resolve().parent.parent / "icon.ico"
MAX_RECENT = 12


class ProgressDialog(QDialog):
    """Modal-ish progress with a cancel button; the worker is cancelled, not killed."""

    def __init__(self, title: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(460)
        self.setWindowFlag(Qt.WindowType.WindowCloseButtonHint, False)
        self.label = QLabel("Starting…")
        self.bar = QProgressBar()
        self.bar.setRange(0, 0)
        self.cancel_btn = QPushButton("Cancel")
        lay = QVBoxLayout(self)
        lay.addWidget(self.label)
        lay.addWidget(self.bar)
        lay.addWidget(self.cancel_btn, alignment=Qt.AlignmentFlag.AlignRight)

    def update(self, done: int, total: int, msg: str):
        self.label.setText(msg)
        self.bar.setRange(0, max(total, 0))
        self.bar.setValue(done)


class SearchPanel(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.edit = QLineEdit()
        self.edit.setPlaceholderText("Search text…  (Enter)")
        self.edit.setClearButtonEnabled(True)
        self.this_doc = QRadioButton("This document")
        self.all_books = QRadioButton("All books in library")
        self.this_doc.setChecked(True)
        self.count = QLabel("")
        self.results = QListWidget()
        self.results.setWordWrap(True)
        self.results.setAlternatingRowColors(True)
        self.prev = QPushButton("◀ Prev")
        self.next = QPushButton("Next ▶")
        nav = QHBoxLayout()
        nav.addWidget(self.prev)
        nav.addWidget(self.next)
        scope = QHBoxLayout()
        scope.addWidget(self.this_doc)
        scope.addWidget(self.all_books)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)
        lay.addWidget(self.edit)
        lay.addLayout(scope)
        lay.addWidget(self.count)
        lay.addWidget(self.results, 1)
        lay.addLayout(nav)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.resize(1500, 950)
        self.settings = QSettings("PDF OCR", "PDF OCR")
        self.library = Library()
        self.doc: pymupdf.Document | None = None
        self.doc_path = ""
        self.hits: list[tuple[int, pymupdf.Rect]] = []
        self.current_hit = -1
        self.search_worker: DocSearchWorker | None = None
        self.ocr_worker: OcrWorker | None = None
        self.index_worker: IndexWorker | None = None
        self.pending_search = ""

        self.view = PdfView()
        self.setCentralWidget(self._with_infobar(self.view))
        self.view.page_changed.connect(self._on_page_changed)
        self.view.zoom_changed.connect(lambda z: self.zoom_label.setText(f"{z * 100:.0f}%"))
        self.view.selection_changed.connect(
            lambda n: self.statusBar().showMessage(f"{n} word{'s' if n != 1 else ''} selected — Ctrl+C to copy", 4000) if n else None)
        self.view.copied.connect(lambda n: self.statusBar().showMessage(f"Copied {n} word{'s' if n != 1 else ''}", 4000))
        self.view.area_copied.connect(lambda w, h: self.statusBar().showMessage(
            f"Copied image {w}×{h} px to the clipboard — right-click the box to save it or copy its text", 6000))
        self.view.area_saved.connect(lambda p: self.statusBar().showMessage(f"Saved {p}", 6000))

        self.thumbs = ThumbnailList()
        self.thumbs.currentRowChanged.connect(self._on_thumb_clicked)
        dock_t = QDockWidget("Pages", self)
        dock_t.setWidget(self.thumbs)
        dock_t.setMinimumWidth(170)
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, dock_t)

        self.search = SearchPanel()
        self._last_query = None   # Enter on an unchanged query steps to the next hit instead of re-searching
        self.search.edit.returnPressed.connect(self.run_search)
        self.search.edit.textEdited.connect(lambda _: setattr(self, "_last_query", None))
        self.search.results.currentRowChanged.connect(self._on_result_selected)
        self.search.results.itemActivated.connect(lambda _: self._on_result_selected(self.search.results.currentRow()))
        self.search.prev.clicked.connect(lambda: self.step_hit(-1))
        self.search.next.clicked.connect(lambda: self.step_hit(1))
        self.search.this_doc.toggled.connect(lambda _: self.run_search(fresh=True) if self.search.edit.text() else None)
        dock_s = QDockWidget("Search", self)
        dock_s.setWidget(self.search)
        dock_s.setMinimumWidth(320)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, dock_s)

        self._build_toolbar()
        self._build_menu()
        self.statusBar().showMessage("Open a PDF to begin  (Ctrl+O)")
        self.setAcceptDrops(True)

    # ---- UI construction -----------------------------------------------------------
    def _with_infobar(self, central: QWidget) -> QWidget:
        wrap = QWidget()
        lay = QVBoxLayout(wrap)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self.infobar = QFrame()
        self.infobar.setStyleSheet("QFrame{background:#fff3c4;border-bottom:1px solid #d9b94a} QLabel{color:#000}")
        il = QHBoxLayout(self.infobar)
        il.setContentsMargins(10, 6, 10, 6)
        self.info_label = QLabel("")
        self.info_btn = QPushButton("Make searchable (OCR)")
        self.info_btn.clicked.connect(self.ocr_current)
        il.addWidget(self.info_label, 1)
        il.addWidget(self.info_btn)
        self.infobar.hide()
        lay.addWidget(self.infobar)
        lay.addWidget(central, 1)
        return wrap

    def _build_toolbar(self):
        tb = QToolBar("Main")
        tb.setIconSize(QSize(20, 20))
        tb.setMovable(False)
        self.addToolBar(tb)
        st = self.style()

        a_open = QAction(st.standardIcon(QStyle.StandardPixmap.SP_DialogOpenButton), "Open…", self)
        a_open.setShortcut(QKeySequence.StandardKey.Open)
        a_open.triggered.connect(self.open_dialog)
        tb.addAction(a_open)
        tb.addSeparator()

        a_prev = QAction(st.standardIcon(QStyle.StandardPixmap.SP_ArrowUp), "Previous page", self)
        a_prev.setShortcut("PgUp")
        a_prev.triggered.connect(lambda: self.view.go_to_page(self.view.current_page() - 1))
        a_next = QAction(st.standardIcon(QStyle.StandardPixmap.SP_ArrowDown), "Next page", self)
        a_next.setShortcut("PgDown")
        a_next.triggered.connect(lambda: self.view.go_to_page(self.view.current_page() + 1))
        tb.addAction(a_prev)
        self.page_spin = QSpinBox()
        self.page_spin.setRange(1, 1)
        self.page_spin.setFixedWidth(70)
        self.page_spin.editingFinished.connect(lambda: self.view.go_to_page(self.page_spin.value() - 1))
        self.page_total = QLabel(" / 0 ")
        tb.addWidget(self.page_spin)
        tb.addWidget(self.page_total)
        tb.addAction(a_next)
        tb.addSeparator()

        a_zout = QAction("−", self)
        a_zout.setShortcut(QKeySequence.StandardKey.ZoomOut)
        a_zout.triggered.connect(lambda: self.view.set_zoom(self.view.zoom / 1.2))
        a_zin = QAction("+", self)
        a_zin.setShortcut(QKeySequence.StandardKey.ZoomIn)
        a_zin.triggered.connect(lambda: self.view.set_zoom(self.view.zoom * 1.2))
        a_fit = QAction("Fit width", self)
        a_fit.setShortcut("Ctrl+1")
        a_fit.setToolTip("Zoom so the page fills the width (Ctrl+1); keeps fitting when the window resizes")
        a_fit.triggered.connect(self.view.fit_width)
        a_fitpage = QAction("Fit page", self)
        a_fitpage.setShortcut("Ctrl+0")
        a_fitpage.setToolTip("Zoom so the whole page is visible (Ctrl+0); keeps fitting when the window resizes")
        a_fitpage.triggered.connect(self.view.fit_page)
        self.zoom_label = QLabel("100%")
        self.zoom_label.setFixedWidth(48)
        tb.addAction(a_zout)
        tb.addWidget(self.zoom_label)
        tb.addAction(a_zin)
        tb.addAction(a_fit)
        tb.addAction(a_fitpage)
        tb.addSeparator()

        # mouse tool: select text (default) or pan
        tools = QActionGroup(self)
        tools.setExclusive(True)
        self.a_select = QAction("Select text", self, checkable=True, checked=True)
        self.a_select.setToolTip("Drag to select text; Ctrl+C copies (T)")
        self.a_select.setShortcut("T")
        self.a_area = QAction("Select area", self, checkable=True)
        self.a_area.setToolTip("Drag a box; it is copied as an image. Right-click the box for more (A)")
        self.a_area.setShortcut("A")
        self.a_hand = QAction("Hand", self, checkable=True)
        self.a_hand.setToolTip("Drag to pan (H)")
        self.a_hand.setShortcut("H")
        for a, name in ((self.a_select, "text"), (self.a_area, "area"), (self.a_hand, "hand")):
            tools.addAction(a)
            a.triggered.connect(lambda _=False, n=name: self.view.set_tool(n))
            tb.addAction(a)
        tb.addSeparator()

        self.a_ocr = QAction("Make searchable (OCR)", self)
        self.a_ocr.triggered.connect(self.ocr_current)
        tb.addAction(self.a_ocr)
        spacer = QWidget()
        spacer.setSizePolicy(spacer.sizePolicy().horizontalPolicy().Expanding, spacer.sizePolicy().verticalPolicy())
        tb.addWidget(spacer)
        a_fnext = QAction("Find next", self)
        a_fnext.setShortcut("F3")
        a_fnext.triggered.connect(lambda: self.step_hit(1))
        self.addAction(a_fnext)
        a_fprev = QAction("Find previous", self)
        a_fprev.setShortcut("Shift+F3")
        a_fprev.triggered.connect(lambda: self.step_hit(-1))
        self.addAction(a_fprev)

    def _build_menu(self):
        m_file = self.menuBar().addMenu("&File")
        m_file.addAction("&Open…", QKeySequence.StandardKey.Open, self.open_dialog)
        self.m_recent = m_file.addMenu("Open &recent")
        self._refresh_recent_menu()
        m_file.addSeparator()
        m_file.addAction("E&xit", QKeySequence.StandardKey.Quit, self.close)

        m_edit = self.menuBar().addMenu("&Edit")
        m_edit.addAction("&Copy", QKeySequence.StandardKey.Copy, self.view.copy_selection)
        m_edit.addAction("Select &all on current page", QKeySequence.StandardKey.SelectAll,
                         lambda: self.view.select_page(self.view.current_page()))
        m_edit.addAction("Clear selection", "Esc", lambda: (self.view.clear_selection(), self.view.clear_area()))
        m_edit.addSeparator()
        m_edit.addAction("Save selected area as image…", self.view.save_area_image)
        m_edit.addAction("Copy text in selected area", self.view.copy_area_text)
        m_edit.addSeparator()
        m_edit.addAction("&Find…", QKeySequence.StandardKey.Find,
                         lambda: (self.search.edit.setFocus(), self.search.edit.selectAll()))

        m_lib = self.menuBar().addMenu("&Library")
        m_lib.addAction("&Add folder to library…", self.add_folder)
        m_lib.addAction("&OCR everything that needs it", self.ocr_library)
        m_lib.addAction("&Show library…", self.show_library)
        m_lib.addSeparator()
        m_lib.addAction("Re-&OCR this document (all pages)", lambda: self.ocr_current(force=True))

        m_help = self.menuBar().addMenu("&Help")
        m_help.addAction("&About", lambda: QMessageBox.about(
            self, APP_NAME,
            "PDF viewer with GPU OCR (Surya) and full-text search.\n\n"
            "OCR writes an invisible text layer into a copy named '<name> (OCR).pdf' next to the original; "
            "the original is never changed. Every OCR'd or text PDF is added to the library so "
            "'All books' search finds it."))

    # ---- opening ------------------------------------------------------------------
    def open_dialog(self):
        start = self.settings.value("last_dir", str(Path.home()))
        path, _ = QFileDialog.getOpenFileName(self, "Open PDF", start, "PDF files (*.pdf)")
        if path:
            self.open_pdf(path)

    def open_pdf(self, path: str, page: int = 0, search: str = ""):
        p = Path(path)
        if not p.exists():
            QMessageBox.warning(self, APP_NAME, f"File not found:\n{p}")
            return
        try:
            doc = pymupdf.open(p)
        except Exception as e:
            QMessageBox.critical(self, APP_NAME, f"Could not open PDF:\n{e}")
            return
        # Prefer the OCR'd sibling of a scan the user double-clicked
        ocr_sibling = output_path_for(p)
        if ocr_sibling.exists() and p != ocr_sibling and pages_needing_ocr(doc):
            doc.close()
            doc = pymupdf.open(ocr_sibling)
            p = ocr_sibling
            self.statusBar().showMessage(f"Opened the OCR'd copy: {p.name}", 6000)
        if self.doc:
            self.doc.close()
        self.doc, self.doc_path = doc, str(p)
        self.setWindowTitle(f"{p.name} — {APP_NAME}")
        self.settings.setValue("last_dir", str(p.parent))
        self._push_recent(str(p))
        self.view.set_document(doc, str(p))
        self.thumbs.set_document(doc, str(p))
        self.page_spin.setRange(1, doc.page_count)
        self.page_total.setText(f" / {doc.page_count} ")
        self.hits, self.current_hit = [], -1
        self.search.results.clear()
        self.search.count.setText("")
        QTimer.singleShot(0, self.view.fit_width)
        if page:
            QTimer.singleShot(50, lambda: self.view.go_to_page(page))
        missing = pages_needing_ocr(doc)
        if missing:
            n = len(missing)
            self.info_label.setText(
                f"{n} of {doc.page_count} pages have no text layer, so they can't be searched yet.")
            self.infobar.show()
        else:
            self.infobar.hide()
            if not self.library.is_current(p):
                try:
                    self.library.index_pdf(p)
                except Exception:
                    pass
        self.statusBar().showMessage(f"{p}  ·  {doc.page_count} pages", 8000)
        if search:
            self.search.edit.setText(search)
            self.search.this_doc.setChecked(True)
            self.run_search(fresh=True)

    def dragEnterEvent(self, e):
        if any(u.toLocalFile().lower().endswith(".pdf") for u in e.mimeData().urls()):
            e.acceptProposedAction()

    def dropEvent(self, e):
        for u in e.mimeData().urls():
            f = u.toLocalFile()
            if f.lower().endswith(".pdf"):
                self.open_pdf(f)
                break

    def _push_recent(self, path: str):
        recent = [r for r in (self.settings.value("recent", []) or []) if r != path]
        recent.insert(0, path)
        self.settings.setValue("recent", recent[:MAX_RECENT])
        self._refresh_recent_menu()

    def _refresh_recent_menu(self):
        self.m_recent.clear()
        for r in self.settings.value("recent", []) or []:
            self.m_recent.addAction(Path(r).name, lambda r=r: self.open_pdf(r))

    # ---- navigation ---------------------------------------------------------------
    def _on_page_changed(self, p: int):
        self.page_spin.blockSignals(True)
        self.page_spin.setValue(p + 1)
        self.page_spin.blockSignals(False)
        self.thumbs.blockSignals(True)
        self.thumbs.setCurrentRow(p)
        self.thumbs.blockSignals(False)

    def _on_thumb_clicked(self, row: int):
        if row >= 0:
            self.view.go_to_page(row)

    # ---- search -------------------------------------------------------------------
    def run_search(self, fresh: bool = False):
        q = self.search.edit.text().strip()
        if not fresh and q and q == self._last_query and self.search.results.count():
            self.step_hit(1)   # same query again: cycle to the next result
            return
        self._last_query = q
        self.search.results.clear()
        self.hits, self.current_hit = [], -1
        self.view.set_hits([])
        if not q:
            self.search.count.setText("")
            return
        if self.search.all_books.isChecked():
            self._library_search(q)
            return
        if not self.doc:
            self.search.count.setText("Open a document first, or search all books.")
            return
        if self.search_worker and self.search_worker.isRunning():
            self.search_worker.cancel()
            self.search_worker.wait()
        self.search.count.setText("Searching…")
        self.search_worker = DocSearchWorker(self.doc_path, q)
        self.search_worker.hit.connect(self._on_hit)
        self.search_worker.done.connect(self._on_search_done)
        self.search_worker.start()

    def _on_hit(self, page: int, rect, ctx: str):
        self.hits.append((page, rect))
        item = QListWidgetItem(f"p. {page + 1}   {ctx}")
        item.setData(Qt.ItemDataRole.UserRole, len(self.hits) - 1)
        self.search.results.addItem(item)
        if len(self.hits) == 1:
            self.view.set_hits(self.hits, 0)
            self.search.results.setCurrentRow(0)

    def _on_search_done(self, count: int):
        self.view.set_hits(self.hits, self.current_hit)
        if count == 0:
            msg = "No matches."
            if self.doc and pages_needing_ocr(self.doc):
                msg += "  Some pages have no text layer — run OCR."
            self.search.count.setText(msg)
        else:
            self.search.count.setText(f"{count} match{'es' if count != 1 else ''}")

    def _on_result_selected(self, row: int):
        if row < 0:
            return
        item = self.search.results.item(row)
        data = item.data(Qt.ItemDataRole.UserRole)
        if isinstance(data, LibraryHit):
            if data.path != self.doc_path:
                self.open_pdf(data.path, page=data.page, search=self.search.edit.text())
                self.search.all_books.setChecked(True)  # keep scope; open_pdf re-ran doc search already
            else:
                self.view.go_to_page(data.page)
            return
        self.current_hit = data
        page, rect = self.hits[data]
        self.view.set_current_hit(data)
        self.view.go_to_page(page, rect)

    def step_hit(self, delta: int):
        n = self.search.results.count()
        if n == 0:
            return
        row = self.search.results.currentRow()
        for _ in range(n):   # skip the book-name header rows of a library search
            row = (row + delta) % n
            if self.search.results.item(row).flags():
                break
        self.search.results.setCurrentRow(row)

    def _library_search(self, q: str):
        hits = self.library.search(q)
        self.search.count.setText(f"{len(hits)} page{'s' if len(hits) != 1 else ''} across "
                                  f"{len({h.path for h in hits})} book(s)" if hits else "No matches in the library.")
        last = None
        for h in hits:
            if h.path != last:
                hdr = QListWidgetItem(Path(h.path).stem)
                hdr.setFlags(Qt.ItemFlag.NoItemFlags)
                f = hdr.font()
                f.setBold(True)
                hdr.setFont(f)
                self.search.results.addItem(hdr)
                last = h.path
            item = QListWidgetItem(f"   p. {h.page + 1}   {h.snippet}")
            item.setData(Qt.ItemDataRole.UserRole, h)
            self.search.results.addItem(item)

    # ---- OCR ----------------------------------------------------------------------
    def ocr_current(self, force: bool = False):
        if not self.doc:
            return
        src = Path(self.doc_path)
        missing = pages_needing_ocr(self.doc)
        if not missing and not force:
            QMessageBox.information(self, APP_NAME, "Every page already has a text layer.")
            return
        self._start_ocr([src], force=force)

    def _start_ocr(self, paths: list[Path], force: bool = False):
        if self.ocr_worker and self.ocr_worker.isRunning():
            QMessageBox.information(self, APP_NAME, "An OCR job is already running.")
            return
        dlg = ProgressDialog("OCR", self)
        self.ocr_worker = OcrWorker(paths, self.library, force=force)
        self.ocr_worker.progress.connect(
            lambda p, d, t, m: dlg.update(d, t, f"{Path(p).name}\n{m}"))
        self.ocr_worker.file_done.connect(self._on_ocr_file_done)
        self.ocr_worker.file_failed.connect(
            lambda p, err: QMessageBox.warning(self, APP_NAME, f"OCR failed for\n{p}\n\n{err}"))
        self.ocr_worker.finished_all.connect(dlg.accept)
        dlg.cancel_btn.clicked.connect(lambda: (self.ocr_worker.cancel(), dlg.label.setText("Cancelling after this batch…")))
        self.ocr_worker.start()
        dlg.exec()

    def _on_ocr_file_done(self, src: str, out: str, n: int):
        self.statusBar().showMessage(f"OCR'd {n} pages → {Path(out).name}", 10000)
        if src == self.doc_path or out == self.doc_path:
            q = self.search.edit.text()
            self.open_pdf(out, page=self.view.current_page(), search=q)

    # ---- library ------------------------------------------------------------------
    def add_folder(self):
        start = self.settings.value("last_lib_dir", self.settings.value("last_dir", str(Path.home())))
        folder = QFileDialog.getExistingDirectory(self, "Add folder of PDFs to library", start)
        if not folder:
            return
        self.settings.setValue("last_lib_dir", folder)
        dlg = ProgressDialog("Indexing", self)
        self.index_worker = IndexWorker(Path(folder), self.library)
        self.index_worker.progress.connect(lambda d, t, m: dlg.update(d, t, m or "Done"))
        self.index_worker.done.connect(lambda need: (dlg.accept(), self._after_index(need)))
        dlg.cancel_btn.clicked.connect(self.index_worker.cancel)
        self.index_worker.start()
        dlg.exec()

    def _after_index(self, need: list[str]):
        docs = self.library.docs()
        if not need:
            QMessageBox.information(self, APP_NAME, f"Library has {len(docs)} PDFs. All of them are searchable.")
            return
        r = QMessageBox.question(
            self, APP_NAME,
            f"Library has {len(docs)} PDFs. {len(need)} of them have pages without text.\n\n"
            f"OCR them now? Each becomes a '<name> (OCR).pdf' beside the original.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if r == QMessageBox.StandardButton.Yes:
            self._start_ocr([Path(p) for p in need])

    def ocr_library(self):
        need = [Path(d.path) for d in self.library.docs() if d.needs_ocr and Path(d.path).exists()]
        if not need:
            QMessageBox.information(self, APP_NAME, "Nothing in the library needs OCR.")
            return
        self._start_ocr(need)

    def show_library(self):
        self.library.prune_missing()
        docs = self.library.docs()
        dlg = QDialog(self)
        dlg.setWindowTitle("Library")
        dlg.resize(760, 520)
        tb = QTextBrowser()
        rows = "".join(
            f"<tr><td>{d.path}</td><td align=right>{d.pages}</td>"
            f"<td>{'needs OCR' if d.needs_ocr else 'searchable'}</td></tr>" for d in docs)
        tb.setHtml(f"<p>{len(docs)} PDFs indexed. Double-click a path in the Search panel to open it.</p>"
                   f"<table cellpadding=3><tr><th align=left>File</th><th>Pages</th><th></th></tr>{rows}</table>")
        lay = QVBoxLayout(dlg)
        lay.addWidget(tb)
        dlg.exec()

    def closeEvent(self, e):
        for w in (self.search_worker, self.ocr_worker, self.index_worker):
            if w and w.isRunning():
                w.cancel()
                w.wait(5000)
        super().closeEvent(e)


def run(argv: list[str] | None = None):
    argv = sys.argv if argv is None else argv
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
    except Exception:
        pass
    app = QApplication(argv)
    app.setApplicationName(APP_NAME)
    app.setStyle("Fusion")
    if ICON_PATH.exists():
        app.setWindowIcon(QIcon(str(ICON_PATH)))
    win = MainWindow()
    win.show()
    files = [a for a in argv[1:] if a.lower().endswith(".pdf")]
    if files:
        QTimer.singleShot(0, lambda: win.open_pdf(files[0]))

    def bring_to_front():
        # Launched through a file association the window can start minimized / behind Explorer;
        # insist on a normal, foreground window.
        win.setWindowState((win.windowState() & ~Qt.WindowState.WindowMinimized) | Qt.WindowState.WindowActive)
        win.showNormal()
        win.raise_()
        win.activateWindow()
        try:
            ctypes.windll.user32.SetForegroundWindow(int(win.winId()))
        except Exception:
            pass

    bring_to_front()
    QTimer.singleShot(150, bring_to_front)
    QTimer.singleShot(1200, bring_to_front)
    return app.exec()
