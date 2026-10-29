"""Continuous-scroll PDF view with background page rendering and search highlights."""
from __future__ import annotations

from PySide6.QtCore import QObject, QRectF, QRunnable, Qt, QThreadPool, Signal, QSize, QTimer
from PySide6.QtGui import QBrush, QColor, QImage, QPen, QPixmap, QPainter
from PySide6.QtWidgets import (QApplication, QFileDialog, QGraphicsItem, QGraphicsPixmapItem, QGraphicsRectItem,
                               QGraphicsScene, QGraphicsView, QListWidget, QListWidgetItem, QAbstractItemView, QMenu)
import pymupdf

GAP = 12
HIT_COLOR = QColor(255, 220, 0, 110)
CURRENT_COLOR = QColor(255, 120, 0, 150)
SELECT_COLOR = QColor(50, 120, 255, 90)
AREA_FILL = QColor(50, 120, 255, 40)
AREA_PEN = QColor(30, 90, 220)
AREA_DPI = 300   # resolution of snapshot images copied/saved from a dragged box


class _RenderSignals(QObject):
    done = Signal(int, float, QImage)


class _RenderJob(QRunnable):
    def __init__(self, doc_path: str, page_no: int, zoom: float, signals: _RenderSignals):
        super().__init__()
        self.doc_path, self.page_no, self.zoom, self.signals = doc_path, page_no, zoom, signals

    def run(self):
        try:
            doc = pymupdf.open(self.doc_path)
            pix = doc[self.page_no].get_pixmap(matrix=pymupdf.Matrix(self.zoom, self.zoom), alpha=False)
            img = QImage(pix.samples, pix.width, pix.height, pix.stride, QImage.Format.Format_RGB888).copy()
            doc.close()
            self.signals.done.emit(self.page_no, self.zoom, img)
        except Exception:
            pass


class PdfView(QGraphicsView):
    """Shows every page of a document stacked vertically. Pages render lazily off the GUI thread."""
    page_changed = Signal(int)       # current page (0-based) as the user scrolls
    zoom_changed = Signal(float)
    selection_changed = Signal(int)  # number of selected words
    copied = Signal(int)             # words copied to the clipboard
    area_copied = Signal(int, int)   # snapshot image copied to the clipboard (width, height in px)
    area_saved = Signal(str)         # snapshot image saved to this path

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setScene(QGraphicsScene(self))
        self.setRenderHints(QPainter.RenderHint.SmoothPixmapTransform)
        self.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
        self.setBackgroundBrush(QBrush(QColor(60, 60, 60)))
        self.setViewportUpdateMode(QGraphicsView.ViewportUpdateMode.SmartViewportUpdate)
        # text selection state
        self._words: dict[int, list[tuple]] = {}     # page -> get_text("words") tuples
        self._selecting = False
        self._anchor: tuple[int, int] | None = None   # (page, word index)
        self._selection: list[tuple[int, int]] = []   # (page, word index) in reading order
        self._sel_items: list[QGraphicsRectItem] = []
        # area (snapshot) tool state
        self._area_anchor: tuple[int, float, float] | None = None   # page, x, y (page points)
        self._area: tuple[int, pymupdf.Rect] | None = None           # finished box
        self._area_item: QGraphicsRectItem | None = None
        self.tool = "text"
        self.set_tool("text")
        self.doc: pymupdf.Document | None = None
        self.doc_path = ""
        self.zoom = 1.0
        self.fit_mode: str | None = None   # 'width' / 'page' keep fitting as the window resizes
        self.page_items: list[QGraphicsPixmapItem] = []
        self.page_rects: list[QRectF] = []   # scene rects of pages at current zoom
        self.rendered: dict[int, float] = {}  # page -> zoom it was last rendered at
        self.pending: set[int] = set()
        self.hit_items: list[QGraphicsRectItem] = []
        self.signals = _RenderSignals()
        self.signals.done.connect(self._on_rendered)
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(3)
        self._current_page = 0
        self.verticalScrollBar().valueChanged.connect(self._on_scroll)
        self._render_timer = QTimer(self)
        self._render_timer.setSingleShot(True)
        self._render_timer.setInterval(60)
        self._render_timer.timeout.connect(self._render_visible)

    # ---- document -----------------------------------------------------------------
    def set_document(self, doc: pymupdf.Document | None, path: str = ""):
        self.scene().clear()
        self.page_items, self.page_rects, self.hit_items = [], [], []
        self.rendered, self.pending = {}, set()
        self._words, self._selection, self._sel_items, self._anchor = {}, [], [], None
        self._area, self._area_item, self._area_anchor = None, None, None
        self.doc, self.doc_path = doc, path
        if doc is None:
            return
        self._layout_pages()
        self.verticalScrollBar().setValue(0)
        self._render_timer.start()

    def _layout_pages(self):
        scene = self.scene()
        for item in self.page_items:
            scene.removeItem(item)
        self.page_items, self.page_rects = [], []
        y = GAP
        max_w = 0
        for i in range(self.doc.page_count):
            r = self.doc[i].rect
            w, h = r.width * self.zoom, r.height * self.zoom
            item = QGraphicsPixmapItem()
            item.setOffset(0, 0)
            item.setPos(0, y)
            item.setZValue(0)
            scene.addItem(item)
            self.page_items.append(item)
            self.page_rects.append(QRectF(0, y, w, h))
            y += h + GAP
            max_w = max(max_w, w)
        for i, rect in enumerate(self.page_rects):
            self.page_items[i].setPos((max_w - rect.width()) / 2, rect.y())
            rect.moveLeft((max_w - rect.width()) / 2)
            # grey placeholder so unrendered pages are visible
            ph = QPixmap(int(rect.width()), int(rect.height()))
            ph.fill(QColor(235, 235, 235))
            self.page_items[i].setPixmap(ph)
        scene.setSceneRect(0, 0, max_w, y)
        self.rendered = {}

    # ---- rendering ----------------------------------------------------------------
    def _visible_pages(self) -> list[int]:
        vis = self.mapToScene(self.viewport().rect()).boundingRect()
        vis.adjust(0, -vis.height(), 0, vis.height())  # one screen above and below
        return [i for i, r in enumerate(self.page_rects) if r.intersects(vis)]

    def _render_visible(self):
        if not self.doc:
            return
        for i in self._visible_pages():
            if self.rendered.get(i) == self.zoom or i in self.pending:
                continue
            self.pending.add(i)
            self.pool.start(_RenderJob(self.doc_path, i, self.zoom, self.signals))

    def _on_rendered(self, page_no: int, zoom: float, img: QImage):
        self.pending.discard(page_no)
        if zoom != self.zoom or page_no >= len(self.page_items):
            return
        self.page_items[page_no].setPixmap(QPixmap.fromImage(img))
        self.rendered[page_no] = zoom

    def _on_scroll(self, _value):
        self._render_timer.start()
        p = self.current_page()
        if p != self._current_page:
            self._current_page = p
            self.page_changed.emit(p)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if self.fit_mode:
            self._reapply_fit()
        self._render_timer.start()

    # ---- navigation ---------------------------------------------------------------
    def current_page(self) -> int:
        if not self.page_rects:
            return 0
        vis = self.mapToScene(self.viewport().rect()).boundingRect()
        probe = vis.top() + vis.height() * 0.35
        for i, r in enumerate(self.page_rects):
            if r.bottom() >= probe:
                return i
        return len(self.page_rects) - 1

    def go_to_page(self, page_no: int, rect: pymupdf.Rect | None = None):
        if not self.page_rects or not 0 <= page_no < len(self.page_rects):
            return
        pr = self.page_rects[page_no]
        if rect is None:
            self.verticalScrollBar().setValue(int(pr.y() - GAP))
        else:
            y = pr.y() + rect.y0 * self.zoom
            self.verticalScrollBar().setValue(int(y - self.viewport().height() * 0.3))
            x = pr.x() + rect.x0 * self.zoom
            self.horizontalScrollBar().setValue(int(x - self.viewport().width() * 0.3))
        self._render_timer.start()

    # ---- zoom ---------------------------------------------------------------------
    def set_zoom(self, zoom: float, fit: str | None = None):
        """fit: None for a manual zoom, or 'width' / 'page' so the zoom follows window resizes."""
        self.fit_mode = fit
        zoom = max(0.2, min(6.0, zoom))
        if not self.doc or abs(zoom - self.zoom) < 1e-3:
            return
        page = self.current_page()
        frac = 0.0
        if self.page_rects:
            pr = self.page_rects[page]
            top = self.mapToScene(self.viewport().rect()).boundingRect().top()
            frac = (top - pr.y()) / max(1.0, pr.height())
        self.zoom = zoom
        self._layout_pages()
        self._relayout_hits()
        self._draw_selection()
        self._draw_area()
        pr = self.page_rects[page]
        self.verticalScrollBar().setValue(int(pr.y() + frac * pr.height()))
        self.zoom_changed.emit(zoom)
        self._render_timer.start()

    def _fit_page_size(self) -> tuple[float, float]:
        """Size in points of the page to fit: the current one (pages can differ in size)."""
        r = self.doc[self.current_page()].rect
        return r.width, r.height

    def fit_width(self):
        if not self.doc:
            return
        w, _ = self._fit_page_size()
        self.set_zoom((self.viewport().width() - 2 * GAP) / w, fit="width")

    def fit_page(self):
        """Whole page visible: the smaller of the width and height fits."""
        if not self.doc:
            return
        w, h = self._fit_page_size()
        zw = (self.viewport().width() - 2 * GAP) / w
        zh = (self.viewport().height() - 2 * GAP) / h
        self.set_zoom(min(zw, zh), fit="page")

    def _reapply_fit(self):
        if getattr(self, "_refitting", False):
            return   # a re-fit can resize the viewport (scrollbars); don't recurse
        self._refitting = True
        try:
            if self.fit_mode == "width":
                self.fit_width()
            elif self.fit_mode == "page":
                self.fit_page()
        finally:
            self._refitting = False

    def wheelEvent(self, e):
        if e.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.set_zoom(self.zoom * (1.1 if e.angleDelta().y() > 0 else 1 / 1.1))
            e.accept()
        else:
            super().wheelEvent(e)

    # ---- highlights ---------------------------------------------------------------
    def set_hits(self, hits: list[tuple[int, pymupdf.Rect]], current: int = -1):
        """hits: (page, rect in page points). `current` indexes the one to colour differently."""
        for it in self.hit_items:
            self.scene().removeItem(it)
        self.hit_items = []
        self._hits = hits
        for k, (p, r) in enumerate(hits):
            item = QGraphicsRectItem()
            item.setBrush(QBrush(CURRENT_COLOR if k == current else HIT_COLOR))
            item.setPen(QPen(Qt.PenStyle.NoPen))
            item.setZValue(5)
            item.setData(0, p)
            item.setData(1, (r.x0, r.y0, r.x1, r.y1))
            self.scene().addItem(item)
            self.hit_items.append(item)
        self._relayout_hits()

    def set_current_hit(self, current: int):
        for k, it in enumerate(self.hit_items):
            it.setBrush(QBrush(CURRENT_COLOR if k == current else HIT_COLOR))

    def _relayout_hits(self):
        for it in self.hit_items:
            p = it.data(0)
            x0, y0, x1, y1 = it.data(1)
            if p < len(self.page_rects):
                pr = self.page_rects[p]
                it.setRect(pr.x() + x0 * self.zoom, pr.y() + y0 * self.zoom,
                           (x1 - x0) * self.zoom, (y1 - y0) * self.zoom)

    # ---- tools --------------------------------------------------------------------
    def set_tool(self, tool: str):
        """'text': left-drag selects words. 'area': left-drag draws a box to copy as an image.
        'hand': left-drag pans."""
        self.tool = tool
        self.select_mode = tool == "text"
        if tool != "text":
            self.clear_selection()
        if tool != "area":
            self.clear_area()
        if tool == "hand":
            self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
            self.viewport().unsetCursor()
        else:
            self.setDragMode(QGraphicsView.DragMode.NoDrag)
            self.viewport().setCursor(Qt.CursorShape.IBeamCursor if tool == "text" else Qt.CursorShape.CrossCursor)

    def set_select_mode(self, select: bool):
        self.set_tool("text" if select else "hand")

    # ---- area / snapshot tool -----------------------------------------------------
    def _area_point(self, view_pos, page: int | None = None) -> tuple[int, float, float] | None:
        """Page-clamped point; when `page` is given the point is clamped onto that page."""
        sp = self.mapToScene(view_pos)
        if page is None:
            return self._locate(sp)
        pr = self.page_rects[page]
        x = min(max(sp.x() - pr.x(), 0.0), pr.width()) / self.zoom
        y = min(max(sp.y() - pr.y(), 0.0), pr.height()) / self.zoom
        return page, x, y

    def _set_area(self, page: int, x0: float, y0: float, x1: float, y1: float):
        r = pymupdf.Rect(min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))
        self._area = (page, r)
        self._draw_area()

    def _draw_area(self):
        if self._area_item is not None:
            self.scene().removeItem(self._area_item)
            self._area_item = None
        if self._area is None or not self.page_rects:
            return
        page, r = self._area
        pr = self.page_rects[page]
        it = QGraphicsRectItem(pr.x() + r.x0 * self.zoom, pr.y() + r.y0 * self.zoom,
                               r.width * self.zoom, r.height * self.zoom)
        it.setBrush(QBrush(AREA_FILL))
        pen = QPen(AREA_PEN, 1.5, Qt.PenStyle.DashLine)
        pen.setCosmetic(True)
        it.setPen(pen)
        it.setZValue(7)
        self.scene().addItem(it)
        self._area_item = it

    def clear_area(self):
        self._area, self._area_anchor = None, None
        self._draw_area()

    def has_area(self) -> bool:
        return self._area is not None and self._area[1].width > 1 and self._area[1].height > 1

    def area_image(self, dpi: int = AREA_DPI) -> QImage | None:
        """Render the boxed region of the page as an image."""
        if not self.has_area():
            return None
        page, r = self._area
        pix = self.doc[page].get_pixmap(matrix=pymupdf.Matrix(dpi / 72, dpi / 72), clip=r, alpha=False)
        return QImage(pix.samples, pix.width, pix.height, pix.stride, QImage.Format.Format_RGB888).copy()

    def copy_area_image(self) -> bool:
        img = self.area_image()
        if img is None:
            return False
        QApplication.clipboard().setImage(img)
        self.area_copied.emit(img.width(), img.height())
        return True

    def save_area_image(self):
        img = self.area_image()
        if img is None:
            return
        page = self._area[0]
        path, _ = QFileDialog.getSaveFileName(self, "Save image", f"page {page + 1} snapshot.png",
                                              "PNG image (*.png);;JPEG image (*.jpg)")
        if path:
            img.save(path)
            self.area_saved.emit(path)

    def area_text(self) -> str:
        if not self.has_area():
            return ""
        page, r = self._area
        return self.doc[page].get_text("text", clip=r).strip()

    def copy_area_text(self) -> int:
        text = self.area_text()
        if not text:
            return 0
        QApplication.clipboard().setText(text)
        n = len(text.split())
        self.copied.emit(n)
        return n

    def _page_words(self, page: int) -> list[tuple]:
        if page not in self._words:
            words = self.doc[page].get_text("words") if self.doc else []
            # reading order: block, line, word
            self._words[page] = sorted(words, key=lambda w: (w[5], w[6], w[7]))
        return self._words[page]

    def _locate(self, scene_pos) -> tuple[int, float, float] | None:
        """Scene point -> (page, x, y) in page points; clamps to the nearest page vertically."""
        if not self.page_rects:
            return None
        y = scene_pos.y()
        page = next((i for i, r in enumerate(self.page_rects) if y < r.bottom() + GAP / 2), len(self.page_rects) - 1)
        pr = self.page_rects[page]
        px = min(max(scene_pos.x() - pr.x(), 0.0), pr.width()) / self.zoom
        py = min(max(y - pr.y(), 0.0), pr.height()) / self.zoom
        return page, px, py

    def _word_at(self, page: int, x: float, y: float) -> int | None:
        """Index of the word nearest a point: on the same line if there is one, else the nearest line."""
        words = self._page_words(page)
        if not words:
            return None
        on_line = [(abs((w[0] + w[2]) / 2 - x), i) for i, w in enumerate(words) if w[1] <= y <= w[3]]
        if on_line:
            # prefer a word that actually contains x; otherwise the closest on that line
            inside = [(d, i) for d, i in on_line if words[i][0] <= x <= words[i][2]]
            return min(inside or on_line)[1]
        return min(((abs((w[1] + w[3]) / 2 - y), abs((w[0] + w[2]) / 2 - x), i) for i, w in enumerate(words)))[2]

    def _hit_test(self, view_pos) -> tuple[int, int] | None:
        loc = self._locate(self.mapToScene(view_pos))
        if loc is None:
            return None
        page, x, y = loc
        k = self._word_at(page, x, y)
        return None if k is None else (page, k)

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton and self.doc:
            if self.tool == "text":
                self.clear_selection()
                self._anchor = self._hit_test(e.position().toPoint())
                self._selecting = self._anchor is not None
                e.accept()
                return
            if self.tool == "area":
                self.clear_area()
                self._area_anchor = self._area_point(e.position().toPoint())
                e.accept()
                return
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        if self._selecting:
            cur = self._hit_test(e.position().toPoint())
            if cur is not None:
                self._select_range(self._anchor, cur)
            e.accept()
            return
        if self._area_anchor is not None:
            page, ax, ay = self._area_anchor
            _, x, y = self._area_point(e.position().toPoint(), page)   # box stays on the page it started on
            self._set_area(page, ax, ay, x, y)
            e.accept()
            return
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):
        if self._selecting:
            self._selecting = False
            e.accept()
            return
        if self._area_anchor is not None:
            self._area_anchor = None
            if self.has_area():
                self.copy_area_image()   # a finished box goes straight to the clipboard
            else:
                self.clear_area()
            e.accept()
            return
        super().mouseReleaseEvent(e)

    def mouseDoubleClickEvent(self, e):
        if self.select_mode and e.button() == Qt.MouseButton.LeftButton and self.doc:
            hit = self._hit_test(e.position().toPoint())
            if hit:
                self._select_range(hit, hit)
            e.accept()
            return
        super().mouseDoubleClickEvent(e)

    def keyPressEvent(self, e):
        if e.key() == Qt.Key.Key_Escape and (self._selection or self._area):
            self.clear_selection()
            self.clear_area()
            e.accept()
            return
        super().keyPressEvent(e)

    def contextMenuEvent(self, e):
        if not self.doc:
            return
        menu = QMenu(self)
        if self.has_area():
            menu.addAction("Copy image", self.copy_area_image)
            menu.addAction("Save image as…", self.save_area_image)
            a_at = menu.addAction("Copy text in box", self.copy_area_text)
            a_at.setEnabled(bool(self.area_text()))
            menu.addAction("Clear box", self.clear_area)
            menu.addSeparator()
        a_copy = menu.addAction("Copy")
        a_copy.setEnabled(bool(self._selection))
        a_copy.triggered.connect(self.copy_selection)
        loc = self._locate(self.mapToScene(e.pos()))
        a_page = menu.addAction("Select all on this page")
        a_page.setEnabled(loc is not None)
        a_page.triggered.connect(lambda: self.select_page(loc[0]))
        a_clear = menu.addAction("Clear selection")
        a_clear.setEnabled(bool(self._selection))
        a_clear.triggered.connect(self.clear_selection)
        menu.exec(e.globalPos())

    def _select_range(self, a: tuple[int, int], b: tuple[int, int]):
        if a > b:
            a, b = b, a
        sel = []
        for p in range(a[0], b[0] + 1):
            n = len(self._page_words(p))
            start = a[1] if p == a[0] else 0
            end = b[1] if p == b[0] else n - 1
            sel.extend((p, k) for k in range(start, end + 1))
        self._selection = sel
        self._draw_selection()
        self.selection_changed.emit(len(sel))

    def select_page(self, page: int):
        n = len(self._page_words(page))
        if n:
            self._select_range((page, 0), (page, n - 1))

    def clear_selection(self):
        if self._selection or self._sel_items:
            self._selection = []
            self._draw_selection()
            self.selection_changed.emit(0)

    def _draw_selection(self):
        for it in self._sel_items:
            self.scene().removeItem(it)
        self._sel_items = []
        # one rectangle per (page, block, line) so the highlight reads as lines of text
        spans: dict[tuple, list[float]] = {}
        for p, k in self._selection:
            w = self._page_words(p)[k]
            key = (p, w[5], w[6])
            r = spans.get(key)
            if r is None:
                spans[key] = [w[0], w[1], w[2], w[3]]
            else:
                r[0], r[1], r[2], r[3] = min(r[0], w[0]), min(r[1], w[1]), max(r[2], w[2]), max(r[3], w[3])
        for (p, _b, _l), (x0, y0, x1, y1) in spans.items():
            pr = self.page_rects[p]
            it = QGraphicsRectItem(pr.x() + x0 * self.zoom, pr.y() + y0 * self.zoom,
                                   (x1 - x0) * self.zoom, (y1 - y0) * self.zoom)
            it.setBrush(QBrush(SELECT_COLOR))
            it.setPen(QPen(Qt.PenStyle.NoPen))
            it.setZValue(6)
            self.scene().addItem(it)
            self._sel_items.append(it)

    def selected_text(self) -> str:
        out, last = [], None
        for p, k in self._selection:
            w = self._page_words(p)[k]
            key = (p, w[5], w[6])
            if last is not None:
                out.append(" " if key == last else "\n")
            out.append(w[4])
            last = key
        return "".join(out)

    def copy_selection(self) -> int:
        """Ctrl+C: text selection if there is one, else the snapshot box as an image."""
        text = self.selected_text()
        if not text:
            if self.has_area():
                self.copy_area_image()
            return 0
        QApplication.clipboard().setText(text)
        n = len(self._selection)
        self.copied.emit(n)
        return n


class ThumbnailList(QListWidget):
    """Page thumbnails; rendered in the background after a document opens."""
    THUMB_W = 120

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setViewMode(QListWidget.ViewMode.IconMode)
        self.setIconSize(QSize(self.THUMB_W, int(self.THUMB_W * 1.4)))
        self.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.setMovement(QListWidget.Movement.Static)
        self.setSpacing(6)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setUniformItemSizes(True)
        self.signals = _RenderSignals()
        self.signals.done.connect(self._on_rendered)
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(2)
        self._zoom = 0.0
        self._path = ""

    def set_document(self, doc: pymupdf.Document | None, path: str):
        self.pool.clear()
        self.clear()
        self._path = path
        if doc is None:
            return
        self._zoom = self.THUMB_W / max(1.0, doc[0].rect.width)
        blank = QPixmap(self.THUMB_W, int(self.THUMB_W * 1.4))
        blank.fill(QColor(220, 220, 220))
        for i in range(doc.page_count):
            it = QListWidgetItem(blank, str(i + 1))
            it.setTextAlignment(Qt.AlignmentFlag.AlignHCenter)
            self.addItem(it)
        self.verticalScrollBar().valueChanged.connect(self._render_visible)
        self.doItemsLayout()
        QTimer.singleShot(50, self._render_visible)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        QTimer.singleShot(50, self._render_visible)

    def _render_visible(self):
        if not self._path:
            return
        vp = self.viewport().rect().adjusted(0, -400, 0, 400)  # a little above and below too
        for i in range(self.count()):
            it = self.item(i)
            r = self.visualItemRect(it)
            if it.data(Qt.ItemDataRole.UserRole) or r.isNull() or not r.intersects(vp):
                continue
            it.setData(Qt.ItemDataRole.UserRole, True)
            self.pool.start(_RenderJob(self._path, i, self._zoom, self.signals))

    def _on_rendered(self, page_no: int, zoom: float, img: QImage):
        if zoom != self._zoom or page_no >= self.count():
            return
        self.item(page_no).setIcon(QPixmap.fromImage(img))
