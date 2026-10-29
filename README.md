# PDF OCR

<img src="docs/icon.png" width="96" align="right" alt="PDF OCR icon">

A Windows PDF viewer with GPU OCR (Surya) and full-text search, built for scanned books such as old
RPG and wargame rulebooks: pages where Tesseract misses the stippled headings and stat tables.

- Opens any PDF; continuous scroll, thumbnails, zoom, fit width / fit page.
- **Make searchable (OCR)**: finds pages without a text layer, OCRs them on the GPU with Surya's layout
  model, and writes an invisible text layer into a copy (`<name> (OCR).pdf`). The original is never touched.
- Search within a document with highlighted hits, or across every book in the library (SQLite FTS5).
- Select and copy text, or drag a box to copy any region of a page as a 300 dpi image.

## Setup (fresh machine)

Requirements: Windows 10/11, an NVIDIA GPU (Surya is slow on CPU), Python 3.12.

```
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe make_icon.py
.venv\Scripts\python.exe register.py     # optional: lets Explorer open PDFs with it
```

The first OCR downloads Surya's models (~3 GB) from Hugging Face. `requirements.txt` pins Surya 0.17.1
and transformers 4.56.2 on purpose; see *Environment* below.

## Run

Double-click `PDF OCR.bat`, or drop a PDF onto it. From a shell (inside this folder):

```
.venv\Scripts\pythonw.exe main.py "some book.pdf"
```

Command-line OCR without the window (handy for batches):

```
.venv\Scripts\python.exe -m pdfocr.pdf_ocr "book.pdf" [--pages 1-10] [--force] [-o out.pdf]
```

**Open PDFs with it from Explorer:** `python register.py` compiles `pdfocr.exe` (a tiny native launcher,
`launcher.cs`, built with the C# compiler that ships with .NET Framework, carrying the icon) and registers
it as an application for `.pdf` with an App Paths entry, for the current user only. Windows 11 runs such a
default as `pdfocr.exe "<file>"` and ignores anything else in the registry, which is why the launcher is a
real exe rather than a renamed `pythonw.exe`. Windows doesn't let a program make itself the default, so pick
it once: Settings > Apps > Default apps > type `.pdf` > *Choose an app on your PC* > `pdfocr.exe` in this folder.
`python register.py --remove` undoes it. Re-run `register.py` if this folder ever moves.
The icon is built from `icon_source.png` by `make_icon.py` (square crop, rounded corners, green border)
into `icon.ico` / `icon.png`. Explorer caches icons by file path, so after changing the artwork give it
a new `ICON_NAME` in `make_icon.py` and run `register.py` again, or the old icon keeps showing.

## How it works

- **Viewer**: continuous scroll, Ctrl+wheel or +/− to zoom, thumbnails on the left, PgUp/PgDn, page box.
  *Fit width* (Ctrl+1) and *Fit page* (Ctrl+0, whole page visible) keep re-fitting as you resize the
  window until you zoom manually.
- **Text selection**: the default tool is *Select text* (T): drag over words, double-click a word,
  right-click > *Select all on this page*, Ctrl+A for the current page. Ctrl+C copies (lines are kept,
  words joined with spaces). *Hand* (H) switches the left button to panning. On OCR'd scans the selection
  follows the invisible text layer, so word boundaries are approximate.
- **Select area** (A): drag a box on a page and it is copied to the clipboard as a 300 dpi image the
  moment you let go (paste into anything that takes images). Right-click the box for *Save image as…*,
  *Copy text in box* or *Clear box*; Ctrl+C copies the image again; Esc clears it.
- **Search (Ctrl+F, F3 / Shift+F3)**: finds every occurrence in the open document, lists them with context,
  highlights them on the page. Pressing Enter again on the same text steps to the next hit (wrapping);
  changing the text starts a fresh search. "All books in library" searches every indexed PDF instead;
  clicking a hit opens that book at that page.
- **Make searchable (OCR)**: finds pages with no text layer, renders them at 200 dpi, runs Surya on the GPU
  (layout model keeps columns and tables in reading order), and writes an invisible word-positioned text
  layer. The result is saved as `<name> (OCR).pdf` beside the original. **The original is never modified.**
  Pages that already have text are left alone, so a partly digital PDF only gets its scanned pages OCR'd.
  `Library > Re-OCR this document` forces every page.
- **Library** (`%LOCALAPPDATA%\PDF OCR\library.db`): every text PDF you open and every OCR output is indexed
  (SQLite FTS5). `Library > Add folder…` indexes a whole folder tree and offers to OCR the scans in it.
  When both `X.pdf` and `X (OCR).pdf` exist, only the OCR'd one is used.

## Speed and quality

On the RTX 4090, Surya does about 3–4 s per page one at a time, less in batches, so a 300-page book takes
roughly 10–15 minutes. Models (~3 GB) download from Hugging Face on first use and load in ~7 s per session.

Tested on the Rogue Trader rulebook (150 dpi scans): Surya read the stippled section headings and all the
stat-table numbers that Tesseract missed. Weak spots: fraction glyphs (¼, ½) and stylised drop caps.

## Environment

Python 3.12 venv in `.venv` (see `requirements.txt`). Pinned on purpose:
- `surya-ocr==0.17.1` — the last version that runs in-process with PyTorch. 0.20+ switched to a
  vision-language model that needs Docker/vLLM or a llama.cpp server on Windows.
- `transformers==4.56.2` — newer versions break Surya 0.17's model config.
- `torch==2.11.0+cu128` — CUDA build.

Model cache: `%USERPROFILE%\.cache\huggingface` (or `datalab` cache) — safe to delete; re-downloads on next OCR.
