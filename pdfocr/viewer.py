"""Continuous-scroll PDF view with background page rendering and search highlights."""
from __future__ import annotations

from PySide6.QtCore import QObject, QRectF, QRunnable, Qt, QThreadPool, Signal, QSize, QTimer
from PySide6.QtGui import QBrush, QColor, QFont, QImage, QPen, QPixmap, QPainter
from PySide6.QtWidgets import (QApplication, QFileDialog, QGraphicsItem, QGraphicsPixmapItem, QGraphicsRectItem,
                               QGraphicsScene, QGraphicsTextItem, QGraphicsView, QListWidget, QListWidgetItem,
                               QAbstractItemView, QMenu)
import pymupdf

from .pdflock import LOCK
from .textutil import clean_text
from .translate import english_rect

OVERLAY_FILL = QColor(255, 255, 255, 240)
OVERLAY_PEN = QColor(70, 130, 200, 200)


def _has_cjk(s: str) -> bool:
    return any(ch >= "⺀" for ch in s)

GAP = 12
HIT_COLOR = QColor(255, 220, 0, 110)
CURRENT_COLOR = QColor(255, 120, 0, 150)
SELECT_COLOR = QColor(50, 120, 255, 90)
AREA_FILL = QColor(50, 120, 255, 40)
AREA_PEN = QColor(30, 90, 220)
AREA_DPI = 300   # resolution of snapshot images copied/saved from a dragged box


class _RenderSignals(QObject):
    done = Signal(int, float, QImage, object)   # page, zoom, image, text colours (None unless text was erased)


class _RenderJob(QRunnable):
    """Render one page at `zoom` logical scale for a screen with device pixel ratio `dpr`.
    The image is rendered at zoom*dpr and tagged with the ratio, so it is sharp on HiDPI screens.

    erase: [(box in page points, line height in points)] of text blocks to paint out of the picture,
    for showing a translation in their place; the colours of the removed text come back with the image."""

    def __init__(self, doc_path: str, page_no: int, zoom: float, signals: _RenderSignals, dpr: float = 1.0,
                 erase: list | None = None):
        super().__init__()
        self.doc_path, self.page_no, self.zoom, self.signals, self.dpr = doc_path, page_no, zoom, signals, dpr
        self.erase = erase

    def run(self):
        try:
            s = self.zoom * self.dpr
            colors = None
            with LOCK:
                doc = pymupdf.open(self.doc_path)
                pix = doc[self.page_no].get_pixmap(matrix=pymupdf.Matrix(s, s), alpha=False)
                if self.erase:
                    import numpy as np
                    arr = np.frombuffer(pix.samples, np.uint8).reshape(pix.height, pix.stride)[:, :pix.width * 3]
                    arr = np.array(arr).reshape(pix.height, pix.width, 3)   # a writable copy; the pixmap buffer is read-only
                else:
                    img = QImage(pix.samples, pix.width, pix.height, pix.stride, QImage.Format.Format_RGB888).copy()
                doc.close()
            if self.erase:   # the pixel work happens outside the lock
                from .erase import erase_units
                colors = erase_units(arr, [(tuple(v * s for v in box), line * s) for box, line in self.erase])
                img = QImage(arr.data, arr.shape[1], arr.shape[0], arr.shape[1] * 3, QImage.Format.Format_RGB888).copy()
            img.setDevicePixelRatio(self.dpr)
            self.signals.done.emit(self.page_no, self.zoom, img, colors)
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
    rotate_requested = Signal(int, int)   # page, degrees clockwise (from the right-click menu)
    reread_requested = Signal(int)        # page to OCR again

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setScene(QGraphicsScene(self))
        self.setRenderHints(QPainter.RenderHint.SmoothPixmapTransform)
        self.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
        self.setBackgroundBrush(QBrush(QColor(60, 60, 60)))
        self.setViewportUpdateMode(QGraphicsView.ViewportUpdateMode.SmartViewportUpdate)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)   # arrow / page keys work once the view is clicked
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
        # middle-click autoscroll (Windows style): click to start, move away from the anchor to scroll,
        # faster the farther you go; click again (or release after a drag) to stop
        self._auto_anchor = None            # QPointF in viewport coords while autoscrolling
        self._auto_pos = None
        self._auto_moved = False
        self._auto_timer = QTimer(self)
        self._auto_timer.setInterval(16)
        self._auto_timer.timeout.connect(self._auto_tick)
        self.viewport().setMouseTracking(True)   # move events without a button held, for sticky mode
        self.tool = "text"
        self.set_tool("text")
        self.doc: pymupdf.Document | None = None
        self.doc_path = ""
        self.zoom = 1.0
        self.fit_mode: str | None = None   # 'width' / 'page' keep fitting as the window resizes
        self.show_english = False                         # draw the stored translation over the page
        self.overlay_boxes = False                        # True: plain white boxes; False: set into the page
        self._unit_colors: dict[int, list] = {}           # page -> colour of each erased block's text
        self._translation: dict[int, list[dict]] = {}     # page -> translated units stored in the PDF
        self._overlay_items: dict[int, list[QGraphicsRectItem]] = {}
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
        self._translation, self._overlay_items, self._unit_colors = {}, {}, {}
        self.doc, self.doc_path = doc, path
        if doc is None:
            return
        self._layout_pages()
        self.verticalScrollBar().setValue(0)
        self._render_timer.start()

    def _layout_pages(self):
        scene = self.scene()
        self._clear_overlays()
        for item in self.page_items:
            scene.removeItem(item)
        self.page_items, self.page_rects = [], []
        y = GAP
        max_w = 0
        with LOCK:
            rects = [self.doc[i].rect for i in range(self.doc.page_count)]
        for i, r in enumerate(rects):
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
        dpr = self.devicePixelRatioF()
        visible = self._visible_pages()
        for i in visible:
            spec = self._erase_spec(i)
            if self.rendered.get(i) == (self.zoom, dpr, bool(spec)) or i in self.pending:
                continue
            self.pending.add(i)
            self.pool.start(_RenderJob(self.doc_path, i, self.zoom, self.signals, dpr, spec))
        if self.show_english:
            for i in visible:
                # in-place English waits for its page picture (it needs the erased text's colours)
                ready = self.overlay_boxes or self.rendered.get(i) == (self.zoom, dpr, bool(self._erase_spec(i)))
                if i not in self._overlay_items and ready:
                    self._make_overlay(i)

    def _english_units(self, page: int) -> list[dict]:
        return [u for u in self._translation.get(page) or [] if (u.get("en") or "").strip()]

    @staticmethod
    def _line_pts(u: dict) -> float:
        """Height of one line of a block's original text (width of one column when vertical), in points."""
        b = u["bbox"]
        n = max(1, (u.get("ja") or "").count("\n") + 1)
        return ((b[2] - b[0]) if u.get("vertical") else (b[3] - b[1])) / n

    def _erase_spec(self, page: int) -> list | None:
        """What to paint out of a page's picture: its translated blocks, when English is shown in place."""
        if not self.show_english or self.overlay_boxes:
            return None
        return [(tuple(u["bbox"]), self._line_pts(u)) for u in self._english_units(page)] or None

    # ---- English translation overlay ----------------------------------------------
    def set_translation(self, pages: dict[int, list[dict]] | None):
        """pages: page number -> units ({"bbox": [x0, y0, x1, y1] in page points, "en": text}) stored in the PDF."""
        self._clear_overlays()
        self._translation = pages or {}
        self._words = {}
        self._render_timer.start()

    def has_translation(self) -> bool:
        return bool(self._translation)

    def set_show_english(self, on: bool):
        """Show the stored English over the page. Selection and copy then work on the English text."""
        if on == self.show_english:
            return
        self.show_english = on
        self.clear_selection()
        self._words = {}
        self._clear_overlays()
        self.rendered = {}          # pages are redrawn with or without their Japanese
        self._render_timer.start()

    def set_overlay_boxes(self, boxes: bool):
        """True: translation in plain white boxes over the page. False: set into the page in place."""
        if boxes == self.overlay_boxes:
            return
        self.overlay_boxes = boxes
        self._clear_overlays()
        self.rendered = {}
        self._render_timer.start()

    def _clear_overlays(self):
        for items in self._overlay_items.values():
            for it in items:
                self.scene().removeItem(it)   # removes the text child with it
        self._overlay_items = {}

    def _make_overlay(self, page: int):
        for it in self._overlay_items.get(page, []):
            self.scene().removeItem(it)
        units = self._english_units(page)
        self._overlay_items[page] = items = []
        if not units or page >= len(self.page_rects):
            return
        pr, z = self.page_rects[page], self.zoom
        if not self.overlay_boxes:
            self._make_inplace_overlay(page, units, items)
            return
        with LOCK:
            page_rect = self.doc[page].rect
        for u in units:
            en = u["en"].strip()
            r = english_rect(u["bbox"], page_rect, en)
            x, y, w, h = pr.x() + r.x0 * z, pr.y() + r.y0 * z, r.width * z, r.height * z
            box = QGraphicsRectItem()
            box.setBrush(QBrush(OVERLAY_FILL))
            pen = QPen(OVERLAY_PEN, 1)
            pen.setCosmetic(True)
            box.setPen(pen)
            box.setZValue(4)
            text = QGraphicsTextItem(box)
            text.setDefaultTextColor(QColor(15, 15, 15))
            text.document().setDocumentMargin(1.5)
            text.setPlainText(en)
            text.setTextWidth(max(10.0, w - 2))
            text.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse
                                         | Qt.TextInteractionFlag.TextSelectableByKeyboard)
            # largest type that fits the box, down to a floor; past that the box grows instead
            size, floor = min(12.5 * z, max(6.0 * z, h * 0.8)), 6.0 * z
            font = QFont(self.font())
            while True:
                font.setPixelSize(max(1, int(round(size))))
                text.setFont(font)
                th = text.boundingRect().height()
                if th <= h or size <= floor:
                    break
                size = max(floor, size * 0.92)
            box.setRect(x, y, w, max(h, th))
            text.setPos(x + 1, y)
            box.text_item = text   # keep the Python object alive; without this the child is collected
            self.scene().addItem(box)
            items.append(box)

    def _make_inplace_overlay(self, page: int, units: list[dict], items: list):
        """English set where the Japanese was: same place, the original's colour, as near its size as fits.
        The Japanese itself has been painted out of the page picture by the render job."""
        pr, z = self.page_rects[page], self.zoom
        colors = self._unit_colors.get(page) or []
        floor = 4.5 * z
        laid = []   # per block: geometry, the text item, and the type size that fits it
        for k, u in enumerate(units):
            en = u["en"].strip()
            b = u["bbox"]
            x, y, w, h = pr.x() + b[0] * z, pr.y() + b[1] * z, (b[2] - b[0]) * z, (b[3] - b[1]) * z
            box = QGraphicsRectItem(x, y, w, h)          # invisible; it carries the text and takes the clicks
            box.setPen(QPen(Qt.PenStyle.NoPen))
            box.setZValue(4)
            text = QGraphicsTextItem(box)
            text.setDefaultTextColor(QColor(*colors[k]) if k < len(colors) else QColor(20, 20, 20))
            text.document().setDocumentMargin(0)
            text.setPlainText(en)
            text.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse
                                         | Qt.TextInteractionFlag.TextSelectableByKeyboard)
            # a narrow vertical column: run the English down it, as the original runs
            down = bool(u.get("vertical")) and h > 1.5 * w and w < 60 * z
            length, room = (h, w) if down else (w, h)
            # a one-line original (a heading, a label) stays on one line: English is often longer than
            # its box, so it may run on to the right, up to the page edge, before it has to shrink
            one_line = not down and "\n" not in (u.get("ja") or "") and "\n" not in en
            reach = max(length, min(length * 2.4, pr.right() - x - 2 * z)) if one_line else length
            font = QFont(self.font())

            def fits(size: float) -> bool:
                font.setPixelSize(max(1, int(round(size))))
                text.setFont(font)
                if one_line:
                    text.setTextWidth(-1)
                    return text.boundingRect().width() <= reach + 1
                text.setTextWidth(max(10.0, length))
                return text.boundingRect().height() <= room + 1

            # start at the original's type size and shrink until the translation fits its block
            size = min(self._line_pts(u) * z * 0.82, 48.0 * z)
            while not fits(size) and size > floor:
                size = max(floor, size * 0.93)
            if one_line and not fits(size):     # still too long at the smallest size: let it wrap after all
                one_line = False
            laid.append({"u": u, "box": box, "text": text, "font": font, "size": size, "one_line": one_line,
                         "down": down, "length": length, "geom": (x, y, w, h), "en": en})

        # Paragraphs printed at one size should read at one size: blocks of running text whose originals
        # had about the same line height all take the smallest size any of them needed.
        body = sorted((d for d in laid if not d["one_line"] and not d["down"]), key=lambda d: self._line_pts(d["u"]))
        group: list[dict] = []
        for d in body + [None]:
            if d is not None and (not group or self._line_pts(d["u"]) <= self._line_pts(group[0]["u"]) * 1.18):
                group.append(d)
                continue
            if len(group) > 1:
                common = min(g["size"] for g in group)
                for g in group:
                    g["size"] = max(common, g["size"] * 0.8)   # but never shrink one block by more than a fifth
            group = [d] if d is not None else []

        for d in laid:
            box, text, font = d["box"], d["text"], d["font"]
            x, y, w, h = d["geom"]
            font.setPixelSize(max(1, int(round(d["size"]))))
            text.setFont(font)
            text.setTextWidth(-1 if d["one_line"] else max(10.0, d["length"]))
            th = text.boundingRect().height()
            if d["down"]:
                text.setRotation(90)                      # local +x now points down the page
                text.setPos(x + w - max(0.0, (w - th) / 2), y)
            else:
                centred = d["one_line"] or (th < h * 0.6 and "\n" not in d["en"])
                text.setPos(x, y + max(0.0, (h - th) / 2) if centred else y)
            box.text_item = text   # keep the Python object alive; without this the child is collected
            self.scene().addItem(box)
            items.append(box)

    def _overlay_text_at(self, view_pos) -> QGraphicsTextItem | None:
        it = self.itemAt(view_pos)
        if isinstance(it, QGraphicsTextItem):
            return it
        return getattr(it, "text_item", None)

    def _overlay_selection(self) -> str:
        fi = self.scene().focusItem()
        if isinstance(fi, QGraphicsTextItem) and fi.textCursor().hasSelection():
            return fi.textCursor().selectedText().replace(" ", "\n").replace(" ", "\n")
        return ""

    def _drop_overlay_focus(self):
        fi = self.scene().focusItem()
        if isinstance(fi, QGraphicsTextItem):
            c = fi.textCursor()
            c.clearSelection()
            fi.setTextCursor(c)
            self.scene().setFocusItem(None)

    def _on_rendered(self, page_no: int, zoom: float, img: QImage, colors=None):
        self.pending.discard(page_no)
        if zoom != self.zoom or page_no >= len(self.page_items):
            self._render_timer.start()   # a picture for an old zoom; make sure the current one gets drawn
            return
        erased = colors is not None
        if erased != bool(self._erase_spec(page_no)):
            self._render_timer.start()   # English was switched on or off while this was being drawn
            return
        self.page_items[page_no].setPixmap(QPixmap.fromImage(img))   # keeps the image's device pixel ratio
        self.rendered[page_no] = (zoom, img.devicePixelRatio(), erased)
        if erased:
            self._unit_colors[page_no] = colors
            self._make_overlay(page_no)
        if img.devicePixelRatio() != self.devicePixelRatioF():
            self._render_timer.start()   # window moved to a screen with a different scale; redo

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
            self._center_on_page(page_no)
        else:
            y = pr.y() + rect.y0 * self.zoom
            self.verticalScrollBar().setValue(int(y - self.viewport().height() * 0.3))
            x = pr.x() + rect.x0 * self.zoom
            self.horizontalScrollBar().setValue(int(x - self.viewport().width() * 0.3))
        self._render_timer.start()

    def _center_on_page(self, page_no: int):
        """In a book whose pages differ in width the scene is as wide as the widest one, so a narrower
        page can sit off to one side. Bring it to the middle when all of it fits across the view."""
        pr = self.page_rects[page_no]
        if pr.width() <= self.viewport().width():
            self.horizontalScrollBar().setValue(int(pr.center().x() - self.viewport().width() / 2))

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
        self._center_on_page(page)
        self.zoom_changed.emit(zoom)
        self._render_timer.start()

    def _fit_page_size(self) -> tuple[float, float]:
        """Size in points of the page to fit: the current one (pages can differ in size)."""
        with LOCK:
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
        if self._auto_anchor is not None:
            self._auto_stop()
        if e.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.set_zoom(self.zoom * (1.1 if e.angleDelta().y() > 0 else 1 / 1.1))
            e.accept()
        else:
            super().wheelEvent(e)

    # ---- highlights ---------------------------------------------------------------
    def set_hits(self, hits: list[tuple[int, list]], current: int = -1):
        """hits: (page, [rects in page points]); a match that wraps lines has several rects.
        `current` indexes the hit to colour differently."""
        for it in self.hit_items:
            self.scene().removeItem(it)
        self.hit_items = []
        self._hits = hits
        for k, (p, rects) in enumerate(hits):
            for r in rects:
                item = QGraphicsRectItem()
                item.setBrush(QBrush(CURRENT_COLOR if k == current else HIT_COLOR))
                item.setPen(QPen(Qt.PenStyle.NoPen))
                item.setZValue(5)
                item.setData(0, p)
                item.setData(1, (r.x0, r.y0, r.x1, r.y1))
                item.setData(2, k)
                self.scene().addItem(item)
                self.hit_items.append(item)
        self._relayout_hits()

    def set_current_hit(self, current: int):
        for it in self.hit_items:
            it.setBrush(QBrush(CURRENT_COLOR if it.data(2) == current else HIT_COLOR))

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
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag if tool == "hand" else QGraphicsView.DragMode.NoDrag)
        self._apply_cursor()

    def _apply_cursor(self):
        if self._auto_anchor is not None:
            self.viewport().setCursor(Qt.CursorShape.SizeAllCursor)
        elif self.tool == "hand":
            self.viewport().unsetCursor()
        else:
            self.viewport().setCursor(Qt.CursorShape.IBeamCursor if self.tool == "text" else Qt.CursorShape.CrossCursor)

    # ---- middle-click autoscroll --------------------------------------------------
    AUTO_DEAD_ZONE = 10      # px around the anchor where nothing scrolls
    AUTO_GAIN = 0.08         # px per tick per px of distance beyond the dead zone

    def _auto_start(self, pos):
        self._auto_anchor = pos
        self._auto_pos = pos
        self._auto_moved = False
        self._auto_timer.start()
        self._apply_cursor()

    def _auto_stop(self):
        self._auto_timer.stop()
        self._auto_anchor = None
        self._auto_pos = None
        self._apply_cursor()

    def _auto_tick(self):
        if self._auto_anchor is None or self._auto_pos is None:
            return
        for d, bar in ((self._auto_pos.y() - self._auto_anchor.y(), self.verticalScrollBar()),
                       (self._auto_pos.x() - self._auto_anchor.x(), self.horizontalScrollBar())):
            if abs(d) <= self.AUTO_DEAD_ZONE:
                continue
            step = (abs(d) - self.AUTO_DEAD_ZONE) * self.AUTO_GAIN
            step = step * step / 10 + step   # gentle near the anchor, quick far away
            bar.setValue(int(bar.value() + (step if d > 0 else -step)))

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
        with LOCK:
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
        with LOCK:
            return clean_text(self.doc[page].get_text("text", clip=r)).strip()

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
            with LOCK:
                raw = self.doc[page].get_text("words") if self.doc else []
            # drop unmappable glyphs (dot leaders etc.) and words that were nothing but those
            words = []
            # A translated page carries two text layers on top of each other. With the English shown,
            # selection works on the English (drop Japanese words); otherwise drop the English layer's
            # words: Latin words inside a translated block that occur in that block's translation.
            units = self._translation.get(page) or []
            en_boxes = []
            if units and not self.show_english:
                with LOCK:
                    page_rect = self.doc[page].rect
                for u in units:
                    en = (u.get("en") or "").strip()
                    if en:
                        en_boxes.append((english_rect(u["bbox"], page_rect, en) + (-2, -2, 2, 2), set(en.split())))
            for w in raw:
                t = clean_text(w[4])
                if not t:
                    continue
                if units and self.show_english and _has_cjk(t):
                    continue
                if en_boxes and not _has_cjk(t):
                    cx, cy = (w[0] + w[2]) / 2, (w[1] + w[3]) / 2
                    if any(r.x0 <= cx <= r.x1 and r.y0 <= cy <= r.y1 and t in toks for r, toks in en_boxes):
                        continue
                words.append((w[0], w[1], w[2], w[3], t, w[5], w[6], w[7]))
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
        self.setFocus()   # so the arrow keys drive the page view after a click, not the search box
        if self._auto_anchor is not None:   # any click ends autoscroll
            self._auto_stop()
            e.accept()
            return
        if e.button() == Qt.MouseButton.MiddleButton and self.doc:
            self._auto_start(e.position())
            e.accept()
            return
        if e.button() == Qt.MouseButton.LeftButton and self.doc:
            if self.tool == "text" and self.show_english and self._overlay_text_at(e.position().toPoint()):
                # inside an English box: let the box's own text handle the drag-selection
                self.clear_selection()
                QGraphicsView.mousePressEvent(self, e)
                return
            self._drop_overlay_focus()
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
        if self._auto_anchor is not None:
            self._auto_pos = e.position()
            if (self._auto_pos - self._auto_anchor).manhattanLength() > self.AUTO_DEAD_ZONE:
                self._auto_moved = True
            e.accept()
            return
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
        if e.button() == Qt.MouseButton.MiddleButton and self._auto_anchor is not None:
            if self._auto_moved:
                self._auto_stop()   # held and dragged: stop on release
            # a plain click stays in autoscroll until the next click
            e.accept()
            return
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
        k = e.key()
        if k == Qt.Key.Key_Escape and self._auto_anchor is not None:
            self._auto_stop()
            e.accept()
            return
        if k == Qt.Key.Key_Escape and (self._selection or self._area):
            self.clear_selection()
            self.clear_area()
            e.accept()
            return
        # Left/Right (and PageUp/PageDown) step whole pages; Up/Down keep Qt's smooth line scrolling
        page_keys = {Qt.Key.Key_Left: -1, Qt.Key.Key_PageUp: -1, Qt.Key.Key_Right: 1, Qt.Key.Key_PageDown: 1}
        if k in page_keys and self.doc:
            self.go_to_page(self.current_page() + page_keys[k])
            e.accept()
            return
        if k == Qt.Key.Key_Home and self.doc:
            self.go_to_page(0)
            e.accept()
            return
        if k == Qt.Key.Key_End and self.doc:
            self.go_to_page(self.doc.page_count - 1)
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
        if loc is not None:
            menu.addSeparator()
            menu.addAction("Rotate page right", lambda: self.rotate_requested.emit(loc[0], 90))
            menu.addAction("Rotate page left", lambda: self.rotate_requested.emit(loc[0], 270))
            menu.addAction("Read this page again (OCR)", lambda: self.reread_requested.emit(loc[0]))
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
        overlay = self._overlay_selection()
        if overlay:
            QApplication.clipboard().setText(overlay)
            n = len(overlay.split())
            self.copied.emit(n)
            return n
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
        with LOCK:
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
            self.pool.start(_RenderJob(self._path, i, self._zoom, self.signals, self.devicePixelRatioF()))

    def _on_rendered(self, page_no: int, zoom: float, img: QImage, _colors=None):
        if zoom != self._zoom or page_no >= self.count():
            return
        self.item(page_no).setIcon(QPixmap.fromImage(img))
