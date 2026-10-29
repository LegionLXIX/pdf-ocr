"""Full-text index of every PDF the app has seen, for searching across all books (SQLite FTS5)."""
from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import pymupdf

from .pdf_ocr import MIN_TEXT_CHARS, is_ocr_done, output_path_for

APP_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "PDF OCR"
DB_PATH = APP_DIR / "library.db"


@dataclass
class LibraryHit:
    path: str
    page: int        # 0-based
    snippet: str


@dataclass
class DocStatus:
    path: str
    pages: int
    text_pages: int

    @property
    def needs_ocr(self) -> bool:
        return self.text_pages < self.pages


class Library:
    def __init__(self, db_path: Path = DB_PATH):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(db_path, check_same_thread=False)
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS docs(
                id INTEGER PRIMARY KEY, path TEXT UNIQUE, mtime REAL, page_count INT, text_pages INT);
            CREATE VIRTUAL TABLE IF NOT EXISTS pages USING fts5(
                doc_id UNINDEXED, page UNINDEXED, text, tokenize='unicode61 remove_diacritics 2');
            """
        )

    # ---- indexing -----------------------------------------------------------------
    def is_current(self, path: Path) -> bool:
        row = self.db.execute("SELECT mtime FROM docs WHERE path=?", (str(path),)).fetchone()
        return row is not None and abs(row[0] - path.stat().st_mtime) < 1

    def index_pdf(self, path: Path, texts: list[str] | None = None) -> DocStatus:
        """Store the text of every page. `texts` lets the OCR step pass what it already extracted."""
        path = Path(path)
        with pymupdf.open(path) as doc:
            done = is_ocr_done(doc)
            if texts is None:
                texts = [doc[i].get_text() for i in range(doc.page_count)]
        text_pages = len(texts) if done else sum(1 for t in texts if len(t.strip()) >= MIN_TEXT_CHARS)
        with self.db:
            self.db.execute("DELETE FROM pages WHERE doc_id IN (SELECT id FROM docs WHERE path=?)", (str(path),))
            self.db.execute("DELETE FROM docs WHERE path=?", (str(path),))
            cur = self.db.execute(
                "INSERT INTO docs(path, mtime, page_count, text_pages) VALUES(?,?,?,?)",
                (str(path), path.stat().st_mtime, len(texts), text_pages))
            doc_id = cur.lastrowid
            self.db.executemany(
                "INSERT INTO pages(doc_id, page, text) VALUES(?,?,?)",
                [(doc_id, i, t) for i, t in enumerate(texts) if t.strip()])
        return DocStatus(str(path), len(texts), text_pages)

    def remove(self, path: Path):
        with self.db:
            self.db.execute("DELETE FROM pages WHERE doc_id IN (SELECT id FROM docs WHERE path=?)", (str(path),))
            self.db.execute("DELETE FROM docs WHERE path=?", (str(path),))

    def docs(self) -> list[DocStatus]:
        rows = self.db.execute("SELECT path, page_count, text_pages FROM docs ORDER BY path").fetchall()
        return [DocStatus(*r) for r in rows]

    def prune_missing(self) -> int:
        gone = [d.path for d in self.docs() if not Path(d.path).exists()]
        for p in gone:
            self.remove(Path(p))
        return len(gone)

    @staticmethod
    def find_pdfs(folder: Path) -> list[Path]:
        """All PDFs under a folder. When both X.pdf and X (OCR).pdf exist, only the OCR'd copy counts."""
        all_pdfs = sorted(p for p in Path(folder).rglob("*.pdf") if p.is_file())
        have = {str(p).lower() for p in all_pdfs}
        return [p for p in all_pdfs if str(output_path_for(p)).lower() not in have]

    # ---- search -------------------------------------------------------------------
    def search(self, query: str, limit: int = 500) -> list[LibraryHit]:
        q = query.strip()
        if not q:
            return []
        # Phrase match so the user's words are taken literally, same as the in-document search
        fts_q = '"' + q.replace('"', '""') + '"'
        rows = self.db.execute(
            """SELECT d.path, m.page, m.snip FROM
                 (SELECT doc_id, page, snippet(pages, 2, '‹', '›', '…', 14) AS snip
                  FROM pages WHERE pages MATCH ?) m
               JOIN docs d ON d.id = m.doc_id
               ORDER BY d.path, m.page LIMIT ?""",
            (fts_q, limit)).fetchall()
        return [LibraryHit(r[0], r[1], " ".join(r[2].split())) for r in rows]
