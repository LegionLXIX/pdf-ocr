"""Search-result list items: a page badge, a two-line snippet with the match highlighted, and
book-name headers for library-wide searches."""
from __future__ import annotations

import html
import re

from PySide6.QtCore import QRectF, QSize, Qt
from PySide6.QtGui import QAbstractTextDocumentLayout, QColor, QPainter, QPen, QTextDocument
from PySide6.QtWidgets import QStyle, QStyledItemDelegate

PAGE_ROLE = Qt.ItemDataRole.UserRole + 1      # 0-based page number
SNIPPET_ROLE = Qt.ItemDataRole.UserRole + 2   # snippet text; ‹ › may mark the match (library search)
HEADER_ROLE = Qt.ItemDataRole.UserRole + 3    # True for a book-name header row

MARK_BG = "#f2c744"
MARK_FG = "#1a1a1a"
PAD = 8
GAP = 8
LINES = 2


def _mark(text: str, query: str) -> str:
    """HTML for a snippet with the matched words highlighted."""
    if "‹" in text and "›" in text:
        parts = re.split("([‹›])", text)
        out, on = [], False
        for p in parts:
            if p == "‹":
                on = True
            elif p == "›":
                on = False
            elif p:
                esc = html.escape(p)
                out.append(f'<span style="background:{MARK_BG};color:{MARK_FG}">{esc}</span>' if on else esc)
        return "".join(out)
    esc = html.escape(text)
    if query:
        pat = re.compile(re.escape(html.escape(query)), re.IGNORECASE)
        esc = pat.sub(lambda m: f'<span style="background:{MARK_BG};color:{MARK_FG}">{m.group(0)}</span>', esc)
    return esc


class ResultDelegate(QStyledItemDelegate):
    def __init__(self, parent, query_getter):
        super().__init__(parent)
        self._query = query_getter
        self._doc = QTextDocument()
        self._doc.setDocumentMargin(0)

    # ---- geometry -----------------------------------------------------------------
    def sizeHint(self, option, index) -> QSize:
        fm = option.fontMetrics
        if index.data(HEADER_ROLE):
            return QSize(option.rect.width(), fm.height() + 2 * PAD)
        return QSize(option.rect.width(), LINES * fm.lineSpacing() + 2 * PAD)

    def _badge_width(self, option, index) -> int:
        return option.fontMetrics.horizontalAdvance(f"p. {index.data(PAGE_ROLE) + 1}") + 14

    # ---- painting -----------------------------------------------------------------
    def paint(self, painter: QPainter, option, index):
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = option.rect
        pal = option.palette
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        base_text = pal.text().color()
        muted = QColor(base_text)
        muted.setAlpha(140)

        if index.data(HEADER_ROLE):
            bg = QColor(pal.window().color())
            bg = bg.lighter(125) if bg.lightness() < 128 else bg.darker(104)
            painter.fillRect(r, bg)
            f = option.font
            f.setBold(True)
            painter.setFont(f)
            painter.setPen(base_text)
            painter.drawText(r.adjusted(PAD, 0, -PAD, 0), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                             option.fontMetrics.elidedText(index.data(Qt.ItemDataRole.DisplayRole),
                                                           Qt.TextElideMode.ElideRight, r.width() - 2 * PAD))
            painter.restore()
            return

        if selected:
            painter.fillRect(r, pal.highlight())
        elif option.state & QStyle.StateFlag.State_MouseOver:
            hover = QColor(pal.highlight().color())
            hover.setAlpha(40)
            painter.fillRect(r, hover)

        # page badge
        fm = option.fontMetrics
        bw = self._badge_width(option, index)
        bh = fm.height() + 4
        badge = QRectF(r.x() + PAD, r.y() + PAD, bw, bh)
        badge_bg = QColor(pal.highlightedText().color()) if selected else QColor(base_text)
        badge_bg.setAlpha(45 if selected else 28)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(badge_bg)
        painter.drawRoundedRect(badge, 4, 4)
        painter.setPen(pal.highlightedText().color() if selected else base_text)
        painter.drawText(badge, Qt.AlignmentFlag.AlignCenter, f"p. {index.data(PAGE_ROLE) + 1}")

        # snippet, up to LINES lines, clipped
        text_x = int(badge.right()) + GAP
        text_w = r.right() - PAD - text_x
        doc = self._doc
        doc.setDefaultFont(option.font)
        color = pal.highlightedText().color().name() if selected else base_text.name()
        doc.setHtml(f'<span style="color:{color}">{_mark(index.data(SNIPPET_ROLE) or "", self._query())}</span>')
        doc.setTextWidth(text_w)
        painter.translate(text_x, r.y() + PAD)
        painter.setClipRect(QRectF(0, 0, text_w, LINES * fm.lineSpacing()))
        ctx = QAbstractTextDocumentLayout.PaintContext()
        doc.documentLayout().draw(painter, ctx)
        painter.restore()

        # hairline separator
        painter.save()
        pen = QPen(muted)
        pen.setWidthF(0.6)
        painter.setPen(pen)
        painter.drawLine(r.x() + PAD, r.bottom(), r.right() - PAD, r.bottom())
        painter.restore()
