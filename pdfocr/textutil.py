"""Cleaning for text pulled out of PDFs.

Digital PDFs often contain glyphs with no Unicode mapping (dot leaders in a table of contents, dingbats,
icon fonts); MuPDF returns U+FFFD for each of those, which shows up as "����" in snippets and copied text.
"""
import re

# replacement characters, private-use glyphs, and C0 control characters other than tab/newline
_JUNK = re.compile(r"[�-\x00-\x08\x0b\x0c\x0e-\x1f]+")


def clean_text(s: str) -> str:
    return _JUNK.sub("", s)


def clean_line(s: str) -> str:
    """One-line form for snippets: junk removed and whitespace collapsed."""
    return " ".join(clean_text(s).split())
