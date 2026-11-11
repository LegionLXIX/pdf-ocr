"""Dialogs: build a PDF from images, translation settings and spending, and the translate job."""
from __future__ import annotations

from pathlib import Path

import pymupdf
from PySide6.QtCore import QSettings, QSize, Qt, QTimer
from PySide6.QtGui import QIcon, QImageReader, QPixmap
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDialog, QDoubleSpinBox, QFileDialog,
                               QFormLayout, QFrame, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
                               QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QSpinBox, QVBoxLayout)

from . import translate as tr
from .budget import MODEL_LABELS, Budget
from .images_to_pdf import IMAGE_EXTS, list_images, natural_key
from .pdflock import LOCK
from .workers import ImagesToPdfWorker, TranslateWorker

CONSOLE_KEYS_URL = "https://platform.claude.com/settings/keys"
EFFORT_LABELS = {"low": "Standard", "medium": "Careful (costs more)", "high": "Most careful (costs most)"}


def _line() -> QFrame:
    f = QFrame()
    f.setFrameShape(QFrame.Shape.HLine)
    f.setFrameShadow(QFrame.Shadow.Sunken)
    return f


# ---- images -> PDF ----------------------------------------------------------------------------------
class ImagesDialog(QDialog):
    """Pick images, put them in page order, and write a PDF. `self.result_path` is set on success."""
    THUMB = 56

    def __init__(self, parent=None, start_dir: str = ""):
        super().__init__(parent)
        self.setWindowTitle("New PDF from images")
        self.resize(760, 680)
        self.result_path: str | None = None
        self.worker: ImagesToPdfWorker | None = None
        self._start_dir = start_dir or str(Path.home())
        self._thumb_queue: list[QListWidgetItem] = []

        self.list = QListWidget()
        self.list.setIconSize(QSize(self.THUMB, self.THUMB))
        self.list.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.list.setAlternatingRowColors(True)
        self.count_label = QLabel("No images yet. Add a folder, or drop files here. Drag rows to reorder pages.")

        b_folder, b_files = QPushButton("Add folder…"), QPushButton("Add files…")
        b_up, b_down, b_remove = QPushButton("Move up"), QPushButton("Move down"), QPushButton("Remove")
        b_name, b_date, b_clear = QPushButton("Sort by name"), QPushButton("Sort by date"), QPushButton("Clear")
        b_folder.clicked.connect(self.add_folder)
        b_files.clicked.connect(self.add_files)
        b_up.clicked.connect(lambda: self.move(-1))
        b_down.clicked.connect(lambda: self.move(1))
        b_remove.clicked.connect(self.remove_selected)
        b_name.clicked.connect(lambda: self.sort("name"))
        b_date.clicked.connect(lambda: self.sort("mtime"))
        b_clear.clicked.connect(lambda: (self.list.clear(), self._update_count()))
        side = QVBoxLayout()
        for b in (b_folder, b_files, None, b_up, b_down, b_remove, None, b_name, b_date, b_clear):
            side.addWidget(b) if b else side.addSpacing(12)
        side.addStretch(1)

        self.split = QCheckBox("Split two-page spreads into single pages")
        self.rtl = QCheckBox("Right-hand page first (Japanese books)")
        self.rtl.setChecked(True)
        self.rtl.setEnabled(False)
        self.split.toggled.connect(self.rtl.setEnabled)
        self.out = QLineEdit()
        self.out.setPlaceholderText("Where to save the PDF")
        b_out = QPushButton("Browse…")
        b_out.clicked.connect(self.browse_out)
        out_row = QHBoxLayout()
        out_row.addWidget(QLabel("Save as"))
        out_row.addWidget(self.out, 1)
        out_row.addWidget(b_out)

        self.bar = QProgressBar()
        self.bar.hide()
        self.create_btn = QPushButton("Create PDF")
        self.create_btn.setDefault(True)
        self.create_btn.clicked.connect(self.create)
        self.close_btn = QPushButton("Close")
        self.close_btn.clicked.connect(self.reject)
        buttons = QHBoxLayout()
        buttons.addWidget(self.bar, 1)
        buttons.addStretch(0)
        buttons.addWidget(self.create_btn)
        buttons.addWidget(self.close_btn)

        top = QHBoxLayout()
        top.addWidget(self.list, 1)
        top.addLayout(side)
        lay = QVBoxLayout(self)
        lay.addWidget(self.count_label)
        lay.addLayout(top, 1)
        lay.addWidget(self.split)
        lay.addWidget(self.rtl)
        lay.addLayout(out_row)
        lay.addWidget(QLabel("JPEG and PNG pages are stored as they are, with no loss of quality. The images are not changed."))
        lay.addLayout(buttons)
        self.setAcceptDrops(True)
        self._thumb_timer = QTimer(self)
        self._thumb_timer.setInterval(0)
        self._thumb_timer.timeout.connect(self._load_thumbs)

    # ---- list ---------------------------------------------------------------------
    def paths(self) -> list[Path]:
        return [Path(self.list.item(i).data(Qt.ItemDataRole.UserRole)) for i in range(self.list.count())]

    def add_paths(self, paths: list[Path]):
        have = {str(p).lower() for p in self.paths()}
        for p in paths:
            if p.suffix.lower() not in IMAGE_EXTS or str(p).lower() in have:
                continue
            it = QListWidgetItem(p.name)
            it.setData(Qt.ItemDataRole.UserRole, str(p))
            it.setToolTip(str(p))
            it.setSizeHint(QSize(0, self.THUMB + 8))
            self.list.addItem(it)
            self._thumb_queue.append(it)
        self._update_count()
        self._thumb_timer.start()

    def _load_thumbs(self):
        for _ in range(6):
            if not self._thumb_queue:
                self._thumb_timer.stop()
                return
            it = self._thumb_queue.pop(0)
            try:
                reader = QImageReader(it.data(Qt.ItemDataRole.UserRole))
                reader.setAutoTransform(True)
                size = reader.size()
                if size.isValid():
                    reader.setScaledSize(size.scaled(self.THUMB * 2, self.THUMB * 2, Qt.AspectRatioMode.KeepAspectRatio))
                img = reader.read()
                if not img.isNull():
                    it.setIcon(QIcon(QPixmap.fromImage(img)))
            except RuntimeError:
                pass   # the item was removed while its thumbnail was queued

    def _update_count(self):
        n = self.list.count()
        self.count_label.setText(f"{n} page image{'s' if n != 1 else ''}, in this order. Drag rows to reorder."
                                 if n else "No images yet. Add a folder, or drop files here. Drag rows to reorder pages.")

    def add_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Folder of page images", self._start_dir)
        if not folder:
            return
        self._start_dir = folder
        f = Path(folder)
        imgs = list_images(f) or list_images(f, recursive=True)
        self.add_paths(imgs)
        if not self.out.text():
            self.out.setText(str(f.with_name(f.name + ".pdf")))
        if not imgs:
            QMessageBox.information(self, self.windowTitle(), "No images were found in that folder.")

    def add_files(self):
        exts = " ".join(f"*{e}" for e in sorted(IMAGE_EXTS))
        files, _ = QFileDialog.getOpenFileNames(self, "Page images", self._start_dir, f"Images ({exts})")
        if files:
            self._start_dir = str(Path(files[0]).parent)
            self.add_paths(sorted((Path(f) for f in files), key=lambda p: natural_key(p.name)))
            if not self.out.text():
                d = Path(files[0]).parent
                self.out.setText(str(d.with_name(d.name + ".pdf")))

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):
        paths: list[Path] = []
        for u in e.mimeData().urls():
            p = Path(u.toLocalFile())
            if p.is_dir():
                paths.extend(list_images(p) or list_images(p, recursive=True))
                if not self.out.text():
                    self.out.setText(str(p.with_name(p.name + ".pdf")))
            else:
                paths.append(p)
        self.add_paths(paths)

    def move(self, delta: int):
        rows = sorted({i.row() for i in self.list.selectedIndexes()}, reverse=delta > 0)
        for r in rows:
            t = r + delta
            if 0 <= t < self.list.count() and not self.list.item(t).isSelected():
                it = self.list.takeItem(r)
                self.list.insertItem(t, it)
                it.setSelected(True)

    def remove_selected(self):
        for it in self.list.selectedItems():
            if it in self._thumb_queue:
                self._thumb_queue.remove(it)
            self.list.takeItem(self.list.row(it))
        self._update_count()

    def sort(self, how: str):
        paths = self.paths()
        key = (lambda p: (p.stat().st_mtime, natural_key(p.name))) if how == "mtime" else (lambda p: natural_key(p.name))
        order = {str(p): k for k, p in enumerate(sorted(paths, key=key))}
        items = [self.list.takeItem(0) for _ in range(self.list.count())]
        for it in sorted(items, key=lambda it: order[it.data(Qt.ItemDataRole.UserRole)]):
            self.list.addItem(it)

    def browse_out(self):
        path, _ = QFileDialog.getSaveFileName(self, "Save PDF as", self.out.text() or self._start_dir, "PDF (*.pdf)")
        if path:
            self.out.setText(path)

    # ---- create -------------------------------------------------------------------
    def create(self):
        paths = self.paths()
        if not paths:
            QMessageBox.information(self, self.windowTitle(), "Add some images first.")
            return
        if not self.out.text().strip():
            self.browse_out()
            if not self.out.text().strip():
                return
        dst = Path(self.out.text().strip())
        if dst.suffix.lower() != ".pdf":
            dst = dst.with_suffix(".pdf")
        overwrite = False
        if dst.exists():
            if QMessageBox.question(self, self.windowTitle(), f"{dst.name} already exists. Replace it?") != QMessageBox.StandardButton.Yes:
                return
            overwrite = True
        self.create_btn.setEnabled(False)
        self.bar.show()
        self.worker = ImagesToPdfWorker(paths, dst, self.split.isChecked(), self.rtl.isChecked(), overwrite)
        self.worker.progress.connect(lambda d, t, m: (self.bar.setRange(0, t), self.bar.setValue(d)))
        self.worker.done.connect(self._done)
        self.worker.failed.connect(self._failed)
        self.worker.start()

    def _done(self, out: str, pages: int, skipped: list):
        self.result_path = out
        if skipped:
            QMessageBox.information(self, self.windowTitle(), f"Wrote {pages} pages. Skipped: " + ", ".join(skipped))
        self.accept()

    def _failed(self, msg: str):
        self.create_btn.setEnabled(True)
        self.bar.hide()
        QMessageBox.warning(self, self.windowTitle(), msg)

    def reject(self):
        if self.worker and self.worker.isRunning():
            self.worker.cancel()
            self.worker.wait(5000)
        super().reject()


# ---- translation settings and spending --------------------------------------------------------------
def budget_line(budget: Budget) -> str:
    st = budget.status()
    period = f"{st.period_start:%b %d} to {st.period_end:%b %d}"
    if st.limit is None:
        return f"No monthly limit set yet. Nothing can be sent until you set one.  (Period: {period})"
    s = f"This period ({period}): ${st.spent:.2f} spent of ${st.limit:.2f}"
    if st.pending > 0.005:
        s += f", ${st.pending:.2f} reserved"
    s += f", ${st.margin:.2f} kept back. Available: ${st.available:.2f}."
    return s


class TranslateSettingsDialog(QDialog):
    def __init__(self, parent=None, budget: Budget | None = None):
        super().__init__(parent)
        self.setWindowTitle("Translation settings and spending")
        self.setMinimumWidth(620)
        self.budget = budget or Budget()
        s = self.budget.settings()

        self.key_state = QLabel()
        self.key_edit = QLineEdit()
        self.key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.key_edit.setPlaceholderText("Paste a Claude API key (sk-ant-…) to save or replace it")
        b_save, b_remove, b_test = QPushButton("Save key"), QPushButton("Remove key"), QPushButton("Test key (free)")
        b_save.clicked.connect(self.save_key)
        b_remove.clicked.connect(self.remove_key)
        b_test.clicked.connect(self.test_key)
        key_row = QHBoxLayout()
        key_row.addWidget(self.key_edit, 1)
        key_row.addWidget(b_save)
        key_row.addWidget(b_remove)
        key_row.addWidget(b_test)
        key_help = QLabel(
            f'Create a key in the Claude Console: <a href="{CONSOLE_KEYS_URL}">{CONSOLE_KEYS_URL}</a>. '
            "It is kept in the Windows Credential Manager, not in a file.")
        key_help.setOpenExternalLinks(True)
        key_help.setWordWrap(True)

        self.limit = QDoubleSpinBox()
        self.limit.setRange(0, 100000)
        self.limit.setDecimals(2)
        self.limit.setPrefix("$ ")
        self.limit.setSpecialValueText("not set")
        self.limit.setValue(s["monthly_limit_usd"] or 0)
        self.margin = QDoubleSpinBox()
        self.margin.setRange(0, 100000)
        self.margin.setDecimals(2)
        self.margin.setPrefix("$ ")
        self.margin.setValue(s["margin_usd"])
        self.day = QSpinBox()
        self.day.setRange(1, 28)
        self.day.setValue(s["period_start_day"])
        form = QFormLayout()
        form.addRow("Monthly API limit", self.limit)
        form.addRow("Keep unspent (safety margin)", self.margin)
        form.addRow("Allowance renews on day of month", self.day)
        note = QLabel(
            "The app counts the cost of every request it sends and will not send one that could take this "
            "period's total over the limit minus the margin. Set the limit to the API credits your plan gives "
            "you each month (or less). The API cannot tell this app your real balance, so it only knows about "
            "spending made here. For a second, independent stop: in the Console's Billing page leave "
            "auto-reload off, and Anthropic refuses requests once the credits are gone.")
        note.setWordWrap(True)

        self.status = QLabel()
        self.status.setWordWrap(True)
        self.unconfirmed = QLabel()
        self.unconfirmed.setWordWrap(True)
        b_clear = QPushButton("Clear unconfirmed reservations")
        b_clear.setToolTip("Requests whose connection dropped are counted at their worst-case cost. "
                           "After checking the real figure in the Console, clear them here.")
        b_clear.clicked.connect(self.clear_unconfirmed)
        b_ok, b_cancel = QPushButton("Save"), QPushButton("Close")
        b_ok.setDefault(True)
        b_ok.clicked.connect(self.save)
        b_cancel.clicked.connect(self.reject)
        buttons = QHBoxLayout()
        buttons.addWidget(b_clear)
        buttons.addStretch(1)
        buttons.addWidget(b_ok)
        buttons.addWidget(b_cancel)

        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("<b>Claude API key</b>"))
        lay.addWidget(self.key_state)
        lay.addLayout(key_row)
        lay.addWidget(key_help)
        lay.addWidget(_line())
        lay.addWidget(QLabel("<b>Spending limit</b>"))
        lay.addLayout(form)
        lay.addWidget(note)
        lay.addWidget(_line())
        lay.addWidget(QLabel("<b>Spending</b>"))
        lay.addWidget(self.status)
        lay.addWidget(self.unconfirmed)
        lay.addLayout(buttons)
        self.refresh()

    def refresh(self):
        if tr.stored_api_key():
            self.key_state.setText("A key is saved.")
        elif tr.have_credentials():
            self.key_state.setText("No key saved here; using the one in this PC's environment.")
        else:
            self.key_state.setText("No key yet.")
        self.status.setText(budget_line(self.budget))
        st = self.budget.status()
        n = len([e for e in self.budget.entries() if e["state"] == "final"])
        self.unconfirmed.setText(
            f"{n} request(s) paid for this period."
            + (f" ${st.unconfirmed:.2f} is held for requests whose outcome is unknown." if st.unconfirmed > 0.005 else ""))

    def save_key(self):
        key = self.key_edit.text().strip()
        if not key:
            return
        tr.store_api_key(key)
        self.key_edit.clear()
        self.refresh()

    def remove_key(self):
        tr.store_api_key(None)
        self.refresh()

    def test_key(self):
        if self.key_edit.text().strip():
            self.save_key()
        if not tr.have_credentials():
            QMessageBox.information(self, self.windowTitle(), "There is no key to test yet.")
            return
        ok, msg = tr.check_credentials()
        (QMessageBox.information if ok else QMessageBox.warning)(
            self, self.windowTitle(),
            msg + ("\n\nThis check is free. Whether the account has credits only shows on the first translated page."
                   if ok else ""))

    def clear_unconfirmed(self):
        freed = self.budget.clear_unconfirmed()
        QMessageBox.information(self, self.windowTitle(), f"Cleared ${freed:.2f} of unconfirmed reservations.")
        self.refresh()

    def save(self):
        if self.key_edit.text().strip():
            self.save_key()
        self.budget.set_settings(monthly_limit_usd=self.limit.value() or None, margin_usd=self.margin.value(),
                                 period_start_day=self.day.value())
        self.accept()


# ---- the translate job ------------------------------------------------------------------------------
class TranslateDialog(QDialog):
    """Choose pages and options, see the estimate against the budget, run the job.
    `self.result` holds the TranslateResult once a run has finished."""

    def __init__(self, parent, doc: pymupdf.Document, doc_path: str, current_page: int, budget: Budget | None = None):
        super().__init__(parent)
        self.setWindowTitle("Translate to English")
        self.setMinimumWidth(680)
        self.doc, self.doc_path, self.budget = doc, doc_path, budget or Budget()
        self.settings = QSettings("PDF OCR", "PDF OCR")
        self.worker: TranslateWorker | None = None
        self.result = None
        with LOCK:
            self.page_count = doc.page_count
            self.done_pages = {int(k) for k in (tr.load_translation(doc) or {"pages": {}})["pages"]}

        self.pages = QLineEdit()
        self.pages.setPlaceholderText(f"all {self.page_count} pages   (or e.g. 1-5, 12)")
        b_cur = QPushButton(f"This page ({current_page + 1})")
        b_cur.clicked.connect(lambda: self.pages.setText(str(current_page + 1)))
        b_all = QPushButton("All")
        b_all.clicked.connect(lambda: self.pages.setText(""))
        pages_row = QHBoxLayout()
        pages_row.addWidget(self.pages, 1)
        pages_row.addWidget(b_cur)
        pages_row.addWidget(b_all)

        self.model = QComboBox()
        for mid, label in MODEL_LABELS.items():
            self.model.addItem(label, mid)
        self.model.setCurrentIndex(max(0, self.model.findData(self.settings.value("tr_model", tr.DEFAULT_MODEL))))
        self.effort = QComboBox()
        for eid, label in EFFORT_LABELS.items():
            self.effort.addItem(label, eid)
        self.effort.setCurrentIndex(max(0, self.effort.findData(self.settings.value("tr_effort", tr.DEFAULT_EFFORT))))
        self.cap = QDoubleSpinBox()
        self.cap.setRange(0, 100000)
        self.cap.setDecimals(2)
        self.cap.setPrefix("$ ")
        self.cap.setSpecialValueText("no extra cap")
        self.cap.setToolTip("Stop this run once it has spent this much, even if the monthly limit allows more.")
        self.glossary = QPlainTextEdit(self.settings.value("tr_glossary", ""))
        self.glossary.setPlaceholderText(
            "Optional. What the book is, and names or terms to spell a certain way, one per line.\n"
            "The same text is used for every page, so the translation stays consistent.")
        self.glossary.setFixedHeight(96)

        form = QFormLayout()
        form.addRow("Pages", pages_row)
        form.addRow("Model", self.model)
        form.addRow("Care", self.effort)
        form.addRow("Spend at most, this run", self.cap)
        form.addRow("Glossary and context", self.glossary)

        self.estimate = QLabel()
        self.estimate.setWordWrap(True)
        self.budget_label = QLabel()
        self.budget_label.setWordWrap(True)
        self.bar = QProgressBar()
        self.bar.hide()
        self.log = QListWidget()
        self.log.setFixedHeight(130)
        self.log.hide()
        self.start_btn = QPushButton("Start")
        self.start_btn.setDefault(True)
        self.start_btn.clicked.connect(self.start)
        self.settings_btn = QPushButton("Key and limit…")
        self.settings_btn.clicked.connect(self.open_settings)
        self.close_btn = QPushButton("Close")
        self.close_btn.clicked.connect(self.reject)
        buttons = QHBoxLayout()
        buttons.addWidget(self.settings_btn)
        buttons.addStretch(1)
        buttons.addWidget(self.start_btn)
        buttons.addWidget(self.close_btn)

        lay = QVBoxLayout(self)
        lay.addWidget(QLabel(f"<b>{Path(doc_path).name}</b>  →  {tr.output_path_for(Path(doc_path)).name}"))
        lay.addLayout(form)
        lay.addWidget(_line())
        lay.addWidget(self.estimate)
        lay.addWidget(self.budget_label)
        lay.addWidget(self.bar)
        lay.addWidget(self.log)
        lay.addLayout(buttons)
        for w in (self.pages.textChanged, self.model.currentIndexChanged, self.effort.currentIndexChanged):
            w.connect(self.refresh)
        self.refresh()

    # ---- state --------------------------------------------------------------------
    def selected_pages(self) -> list[int] | None:
        """0-based pages from the box; None when the text can't be read."""
        spec = self.pages.text().strip()
        if not spec:
            return list(range(self.page_count))
        try:
            pages = sorted({p for p in tr._parse_pages(spec, self.page_count) if 0 <= p < self.page_count})
        except ValueError:
            return None
        return pages or None

    def refresh(self):
        pages = self.selected_pages()
        model = self.model.currentData()
        ready = True
        if pages is None:
            self.estimate.setText("Page range not understood. Use forms like 1-5, 12.")
            ready = False
        else:
            todo = [p for p in pages if p not in self.done_pages]
            low, high = tr.estimate_cost(self.doc, todo, model, self.budget)
            scale = {"low": 1.0, "medium": 1.6, "high": 2.4}[self.effort.currentData()]
            based = "your recent pages" if self.budget.average_cost(model) is not None else "page size, before any page has been run"
            done = len(pages) - len(todo)
            self.estimate.setText(
                f"{len(todo)} page(s) to translate" + (f" ({done} already done, free)" if done else "")
                + (f": roughly ${low * scale:.2f} to ${high * scale:.2f}, estimated from {based}. "
                   "The limit below is enforced whatever the estimate says." if todo else "."))
            if not todo:
                ready = False
        self.budget_label.setText(budget_line(self.budget))
        st = self.budget.status()
        problems = []
        if not tr.have_credentials():
            problems.append("no API key")
        if st.limit is None:
            problems.append("no monthly limit")
        if problems:
            self.budget_label.setText(self.budget_label.text() + "  Missing: " + " and ".join(problems)
                                      + ". Use “Key and limit…”.")
            ready = False
        self.start_btn.setEnabled(ready and not (self.worker and self.worker.isRunning()))

    def open_settings(self):
        TranslateSettingsDialog(self, self.budget).exec()
        self.refresh()

    # ---- run ----------------------------------------------------------------------
    def start(self):
        pages = self.selected_pages()
        if pages is None:
            return
        self.settings.setValue("tr_model", self.model.currentData())
        self.settings.setValue("tr_effort", self.effort.currentData())
        self.settings.setValue("tr_glossary", self.glossary.toPlainText())
        self.worker = TranslateWorker(Path(self.doc_path), pages, self.model.currentData(), self.effort.currentData(),
                                      self.glossary.toPlainText(), self.cap.value() or None, self.budget)
        self.worker.progress.connect(self._progress)
        self.worker.done.connect(self._done)
        self.worker.failed.connect(self._failed)
        self.log.clear()
        self.log.show()
        self.bar.show()
        self.bar.setRange(0, 0)
        self.start_btn.setEnabled(False)
        self.close_btn.setText("Stop")
        for w in (self.pages, self.model, self.effort, self.cap, self.glossary, self.settings_btn):
            w.setEnabled(False)
        self.worker.start()

    def _progress(self, done: int, total: int, msg: str, cost: float):
        self.bar.setRange(0, max(total, 1))
        self.bar.setValue(done)
        self.log.addItem(f"{msg}     total ${cost:.2f}")
        self.log.scrollToBottom()
        self.budget_label.setText(budget_line(self.budget))

    def _finish(self):
        self.bar.hide()
        self.close_btn.setText("Close")
        for w in (self.pages, self.model, self.effort, self.cap, self.glossary, self.settings_btn):
            w.setEnabled(True)

    def _done(self, res):
        self.result = res
        self._finish()
        self.log.addItem(res.message)
        for pno, why in res.failed[:20]:
            self.log.addItem(f"   page {pno + 1}: {why}")
        self.log.scrollToBottom()
        self.budget_label.setText(budget_line(self.budget))
        if res.output is not None and (res.translated or res.from_cache or res.empty):
            self.accept()          # the window swaps the new file in and shows the summary
        else:
            self.estimate.setText(res.message)
            self.refresh()

    def _failed(self, msg: str):
        self._finish()
        self.log.addItem("Failed: " + msg)
        self.refresh()

    def reject(self):
        if self.worker and self.worker.isRunning():
            self.worker.cancel()
            self.close_btn.setEnabled(False)
            self.close_btn.setText("Stopping…")
            self.log.addItem("Stopping after the pages already sent…")
            QTimer.singleShot(200, self._wait_stop)
            return
        super().reject()

    def _wait_stop(self):
        if self.worker and self.worker.isRunning():
            QTimer.singleShot(200, self._wait_stop)
            return
        self.close_btn.setEnabled(True)
