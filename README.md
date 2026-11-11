# PDF OCR

<img src="docs/icon.png" width="96" align="right" alt="PDF OCR icon">

A Windows PDF viewer with GPU OCR (Surya) and full-text search, built for scanned books such as old
RPG and wargame rulebooks: pages where Tesseract misses the stippled headings and stat tables.

- Opens any PDF; continuous scroll, thumbnails, zoom, fit width / fit page.
- **Make searchable (OCR)**: finds pages without a text layer, OCRs them on the GPU with Surya's layout
  model, and writes an invisible text layer into a copy (`<name> (OCR).pdf`). The original is never touched.
- Search within a document with highlighted hits, or across every book in the library (SQLite FTS5).
- Select and copy text, or drag a box to copy any region of a page as a 300 dpi image.
- **Point at what was missed**: drag a box around part of a page and have just that part read again,
  enlarged, and translated.
- **New PDF from images**: turn a folder of page scans into a PDF, losslessly.
- **Rotate pages** that were scanned sideways, and read them again the right way up.
- **Japanese**: OCR reads Japanese, horizontal and vertical, into a searchable layer.
- **Translate to English** (optional, uses the Claude API): an English text layer over the Japanese,
  searchable, and shown set into the page in place of the Japanese, with a hard spending limit.

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

- **Viewer**: continuous scroll, Ctrl+wheel or +/− to zoom, thumbnails on the left, page box.
  ←/→ and PgUp/PgDn step a page, ↑/↓ scroll, Home/End jump to the first/last page (click the page
  first so it has keyboard focus). Middle-click starts autoscroll: move the mouse away from the anchor
  to scroll, faster the farther you go; click again (or release after dragging) to stop. Pages render at the screen's real pixel density, so they stay sharp
  on HiDPI / scaled displays.
  A document opens at *Fit page* (whole page visible, Ctrl+0); *Fit width* is Ctrl+1. Both keep
  re-fitting as you resize the window until you zoom manually.
- **Text selection**: the default tool is *Select text* (T): drag over words, double-click a word,
  right-click > *Select all on this page*, Ctrl+A for the current page. Ctrl+C copies (lines are kept,
  words joined with spaces). *Hand* (H) switches the left button to panning. On OCR'd scans the selection
  follows the invisible text layer, so word boundaries are approximate.
- **Select area** (A): drag a box on a page and it is copied to the clipboard as a 300 dpi image the
  moment you let go (paste into anything that takes images). Right-click the box for *Save image as…*,
  *Copy text in box* or *Clear box*; Ctrl+C copies the image again; Esc clears it. A row of buttons under
  the box reads or translates what is in it (see *One part of a page* under Translating).
- **Search (Ctrl+F, F3 / Shift+F3)**: finds every occurrence in the open document, lists them with context,
  highlights them on the page. Pressing Enter again on the same text steps to the next hit (wrapping);
  changing the text starts a fresh search. "All books in library" searches every indexed PDF instead;
  clicking a hit opens that book at that page.
- **Make searchable (OCR)**: finds pages with no text layer, renders them at 200 dpi, runs Surya on the GPU
  (layout model keeps columns and tables in reading order), and writes an invisible word-positioned text
  layer. The result is saved as `<name> (OCR).pdf` beside the original. **The original is never modified.**
  Pages that already have text are left alone, so a partly digital PDF only gets its scanned pages OCR'd.
  `Library > Re-OCR this document` forces every page: old OCR layers are taken off first, and a
  translation already in the file is kept.
- **Library** (`%LOCALAPPDATA%\PDF OCR\library.db`): every text PDF you open and every OCR output is indexed
  (SQLite FTS5). `Library > Add folder…` indexes a whole folder tree and offers to OCR the scans in it.
  When both `X.pdf` and `X (OCR).pdf` exist, only the OCR'd one is used.
- **New PDF from images** (`File` menu): add a folder or files, drag the rows into page order (or sort by
  name or date), optionally split two-page spreads (right-hand page first for Japanese books), and save.
  JPEG and PNG pages are embedded byte for byte, so nothing is re-compressed; WebP, BMP, TIFF and still
  GIFs are converted losslessly; animated GIFs are skipped. Command line:
  `.venv\Scripts\python.exe -m pdfocr.images_to_pdf "<folder>" [-o out.pdf] [--sort name|mtime] [--split] [--each-subfolder]`
- **Rotate pages** (`Page` menu, or right-click a page): *Rotate page right* (Ctrl+R) and *left*
  (Ctrl+Shift+R) turn the page you are on; *Rotate several pages…* takes a range such as `9-15`. The turn
  is saved straight away in the book's working copy, `<name> (OCR).pdf` (made from the original if there
  is none yet); the original is never modified and the scan is not re-compressed. The turn is written
  into the page itself, so a turned page behaves like any other page afterwards. A page's text layers
  and its translation turn with it: every translated block stays on its spot of the page and is shown
  lying turned, as the print now lies. That is what you want for a page with content facing different
  ways, such as a row of cards scanned as one page with one card sideways: turn the page to read that
  card, and the rest keeps its English. A notice then offers **Read again (OCR)** for the pages you
  turned. Reading again merges: translations that are there stay, text that is the right way up now and
  was not read before is added as blocks waiting for translation, and where an old sideways reading and
  the new one cover the same spot, the more reliable reading wins (and keeps the English when the words
  are the same). `Page > Read this page again (OCR)` does the same for any page. Translate afterwards
  sends only the blocks that are waiting. Command line:
  `.venv\Scripts\python.exe -m pdfocr.pages "book.pdf" --rotate 9-15 [--left | --half]`
- **Japanese OCR**: nothing to switch on. Surya reads Japanese as it reads English; lines with kana or
  kanji are written with a CJK font and vertical lines run top to bottom, so Japanese search lands on
  the right spot. Text found inside pictures (callouts on diagrams) is kept when the OCR is confident.

## Translating to English

`Translate > Translate to English…` adds an English layer to a Japanese PDF and saves it as
the book's working copy, `<name> (OCR).pdf`, beside the source. That is the same file the OCR button
writes: one working copy per book holds the OCR layer and the English. It is created from the source
if it does not exist yet, and updated in place after that. The source is never modified.

How it works, per page: Surya finds the text blocks and reads the Japanese locally; the page image and
those blocks go to Claude, which reads the image to correct OCR mistakes and returns an English
translation for each block; the English is written as invisible text over the block it translates and
stored inside the PDF. So English searches (in the document and across the library) land on the right
place. The **English** toolbar button (E) shows the translation set into the page: the Japanese is
painted out with the page's own background (a median fill around each block, `pdfocr/erase.py`), and the
English is drawn where it was, in the original's colour and as near its size as fits; headings and labels
stay on one line and paragraphs share a type size. Only the view changes; the PDF's page images are
untouched, and switching English off shows the page as scanned. Painting out only works where the print
can be told from what is behind it. Over a drawing, or for heavy display lettering, the printed lines get
a backing in the colour behind them and the English is set over that. The English always stays inside the
bounds of the text it translates: it shrinks, or wraps onto a second line, before it would run past its
block, so nothing is laid over the artwork around the text. Handwriting, and text the translator located
by eye (below), has only a rough region for a box; its English is set inside that region at the page's
ordinary text size, turned to the slope of the writing, with a backing no larger than the English itself.
English wraps at whole words, text printed at one size reads at one size, and a block that would run into
its neighbour shrinks. `Translate > Show the English in plain boxes` is the plainer
alternative. With English on you can drag inside a block to select and copy it; with it off, selection
works on the Japanese.

**What is read, and what is sent.** The unit of translation is a paragraph: neighbouring lines of one
size (`pdfocr/units.py`), each stored in the PDF with its line boxes, its text, its English and the way
it lies on the page. A very large page (a fold-out, several cards scanned as one) is read by the OCR a
second time in overlapping tiles, because the text detector works on a reduced copy of the page and loses
small print on such pages. A run sends only what is missing: all blocks of a page not seen before, or,
after a page was read again, just the new ones, with a crop of the page around them (turned upright when
the blocks lie turned).

**Text the OCR cannot read.** Surya does not read slanted handwriting, and misses some text set over
artwork. With *Also look for text the OCR could not read* ticked (the default), each page is shown to the
translator once with everything already handled marked off, and it reports any other Japanese it can see:
where it is, how it slopes, what it says, and the translation. Those pieces become labels on the page.
This is one extra request per page (several for a very large page), made once; the dialog counts them
in its estimate. Its boxes are placed by eye and are less exact than the OCR's.

**One part of a page.** When a caption, a note or a whole corner of a page was missed or read badly,
choose **Select area** (A) and drag a box around it. Three buttons appear under the box (they are also on
the box's right-click menu, and in the `Translate` and `Page` menus):

- **Translate box** reads what is in the box again with the OCR, enlarged (a small box is read at up to
  450 dpi, so small print comes out far better than in a whole-page pass), and sends that one picture to
  the translator, which translates the blocks and also reports handwriting and slanted text the OCR
  cannot read. One request, under the same spending limit as everything else; the button shows about what
  it costs.
- **Read box (OCR)** does the reading only. It is free and nothing is sent. Blocks whose text comes out
  the same keep their English; blocks that are new wait for the next translation run.
- **Sideways…** is for text that lies on its side or upside down in the box. It shows the box, you turn
  the picture until the text reads the right way up, and then translate or read it. The page itself is not
  turned: the English is set along the text as it lies on the page.

The new reading takes the place of the blocks that lie mostly inside the box (a block the box cuts
through is read whole); everything else on the page stays exactly as it is, and the text layer is
rewritten only where the box was. If nothing is found in the box, nothing is changed. The view stays
where it was, so several spots on a page can be done one after another. On a page that has not been
translated yet, *Translate box* reads the whole page first, so the rest of it still counts as waiting
for the normal Translate run.

A glossary box lets you name the book and fix how names and terms are spelled; it is sent with every
page so the translation stays consistent.

**What it needs.** A Claude API key from <https://platform.claude.com/settings/keys>, pasted once into
`Translate > API key, limit and spending…`. It is stored in the Windows Credential Manager, never in a
file. API use is billed by Anthropic separately from chat; some plans include monthly API credits.

**The spending limit.** Nothing is sent until you enter a monthly limit. The API has no call that reports
a credit balance to an ordinary key, so the app keeps its own ledger (`data\spend.json` in the app folder)
and applies one rule before every request:

    spent this period + worst case of requests in flight + worst case of this request
        <= monthly limit - safety margin

Worst case is the request's input (bounded locally: an image's 28-pixel patches plus one token per byte
of text) at the input price plus the full `max_tokens` at the output price, so a response cannot cost
more than was reserved. After each response the real cost, from the response's own token counts,
replaces the reservation. A request whose connection drops stays booked at worst case until you clear it
in the settings dialog. A run can also be given its own smaller cap. Prices are in `pdfocr/budget.py`
(checked against Anthropic's pricing page on 2026-11-10) and can be overridden in
`data\prices.json`.

The ledger lives in the app folder rather than under `%LOCALAPPDATA%` so that every way of running the
app shares one book: sandboxed tools get a private copy of files they write under AppData, which split
the ledger in two when it lived there (old entries are imported automatically on start).

The ledger only knows about spending made through this app. For an independent stop, leave auto-reload
off in the Console's Billing page: Anthropic then refuses requests once the credits are gone, and the app
stops cleanly when it sees that.

**Never paying twice.** Each answer is cached on disk as soon as it comes back and recorded inside the
working copy. Stopping, hitting the limit or a crash loses nothing; running again sends only what is
still missing. You can translate a few pages first to see the quality and the real
cost per page (the estimate in the dialog uses your own history once there is some).

Models: Claude Opus 5.5 by default, with Sonnet 5.5 (half the price) and Haiku 5.5 (cheapest) in the
dialog. Command line:

```
.venv\Scripts\python.exe -m pdfocr.translate --set-limit 100 --margin 5
.venv\Scripts\python.exe -m pdfocr.translate "book.pdf" --pages 1-5 --max-spend 2
.venv\Scripts\python.exe -m pdfocr.translate --status
```

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
