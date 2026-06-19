"""OCR text extraction + personal-data detection for KontAKT screening.

Two layers, split so the tricky part is testable on its own:

* ``extract_pages`` — pull *words with positions* out of a PDF: its own text
  layer where present, Tesseract OCR (Danish + English) for scanned / image
  pages. Each word's box is normalised to the page size (0..1, top-left
  origin) so it's independent of render scale / DPI. Needs PyMuPDF (+ Tesseract
  for scanned pages).

* ``find_pii`` — scan those words for the personal data that typically must be
  redacted (CPR numbers, phone numbers, e-mail addresses) and return, for each
  hit, the **rectangles** to redact. Pure logic over the word list — no PyMuPDF
  needed — so the detection and coordinate mapping are unit-testable.

A hit that spans several words (e.g. a CPR split across a line break) yields one
rect *per word*, so redaction covers each piece precisely instead of a single
box bridging two lines. Names and addresses are deliberately not detected.

OCR of scanned pages needs the Tesseract binary on the worker with the Danish
(``dan``) and English (``eng``) language data. Point at it with ``TESSERACT_PATH``
or have ``tesseract`` on PATH; if it's missing, scanned pages are skipped (with
a logged warning) while text-layer pages are still screened.
"""
from __future__ import annotations

import os
import re

# Pages with at least this many characters of embedded text are treated as
# "has a text layer" and not sent through OCR.
_MIN_TEXT_CHARS = 20
# Render scanned pages at this DPI before OCR (legibility vs. speed).
_OCR_DPI = 300


# ---------------------------------------------------------------------------
# Word extraction (PyMuPDF text layer, Tesseract OCR fallback)
# ---------------------------------------------------------------------------


def extract_pages(pdf_path: str, *, ocr_lang: str = "dan+eng", log=None) -> tuple[list[list[dict]], bool, int]:
    """Return ``(pages, ocr_used, ocr_skipped)``.

    ``pages`` is one entry per page: a list of words ``{"text": str,
    "box": (x0, y0, x1, y1)}`` with the box normalised to the page (0..1,
    top-left origin), in reading order.

    ``ocr_skipped`` counts pages that had **no text layer and could not be
    OCR'd** (Tesseract missing or it errored). Those pages went unscreened, so
    the caller must flag the screening as incomplete rather than report a clean
    result — otherwise PII in a scanned/image page is silently missed.
    """
    log = log or (lambda *_: None)
    import fitz  # PyMuPDF — lazy import

    ocr = _load_tesseract(log)
    pages: list[list[dict]] = []
    ocr_used = False
    ocr_skipped = 0
    with fitz.open(pdf_path) as doc:
        for page in doc:
            pw, ph = page.rect.width, page.rect.height
            raw = page.get_text("words", sort=True)  # (x0,y0,x1,y1, word, block, line, wordno)
            char_count = sum(len(w[4]) for w in raw)
            text_words = [
                {"text": w[4], "box": _norm_box(w[0], w[1], w[2], w[3], pw, ph)}
                for w in raw if (w[4] or "").strip()
            ]
            if char_count >= _MIN_TEXT_CHARS:
                pages.append(text_words)
                continue
            # Little/no text layer → the page needs OCR to be screened.
            if ocr is None:
                ocr_skipped += 1     # no Tesseract — this page goes UNscreened
                pages.append(text_words)
                continue
            try:
                pages.append(_ocr_words(ocr, page, ocr_lang))
                ocr_used = True
            except Exception as exc:  # pylint: disable=broad-except
                log(f"OCR fejlede på en side: {exc!r}")
                ocr_skipped += 1
                pages.append(text_words)
    return pages, ocr_used, ocr_skipped


def _norm_box(x0, y0, x1, y1, pw, ph):
    if pw <= 0 or ph <= 0:
        return (0.0, 0.0, 0.0, 0.0)
    return (
        max(0.0, min(1.0, x0 / pw)), max(0.0, min(1.0, y0 / ph)),
        max(0.0, min(1.0, x1 / pw)), max(0.0, min(1.0, y1 / ph)),
    )


def _ocr_words(ocr, page, ocr_lang):
    from PIL import Image  # lazy
    pix = page.get_pixmap(dpi=_OCR_DPI, alpha=False)
    img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    data = ocr.image_to_data(img, lang=ocr_lang, output_type=ocr.Output.DICT)
    iw, ih = pix.width or 1, pix.height or 1
    words = []
    for i in range(len(data["text"])):
        text = (data["text"][i] or "").strip()
        if not text:
            continue
        left, top = data["left"][i], data["top"][i]
        width, height = data["width"][i], data["height"][i]
        words.append({"text": text, "box": _norm_box(left, top, left + width, top + height, iw, ih)})
    return words


def _load_tesseract(log):
    """Return the pytesseract module configured to find the binary, or None."""
    try:
        import pytesseract
    except ImportError:
        log("pytesseract ikke installeret — scannede sider bliver ikke OCR-screenet.")
        return None
    cmd = os.getenv("TESSERACT_PATH")
    if cmd:
        pytesseract.pytesseract.tesseract_cmd = cmd   # honour a manual setup as-is
    else:
        # Auto-install the binary + dan/eng language data if missing (the same way
        # the conversion robot auto-installs LibreOffice) and point at the data.
        try:
            from oomtm import pdf as _oopdf
            pytesseract.pytesseract.tesseract_cmd = _oopdf.ensure_tesseract(log=log)
        except Exception as exc:  # pylint: disable=broad-except
            log(f"Tesseract kunne ikke klargøres automatisk ({exc}) — scannede sider "
                "bliver ikke OCR-screenet. Sæt evt. TESSERACT_PATH.")
            return None
    try:
        pytesseract.get_tesseract_version()
    except Exception as exc:  # pylint: disable=broad-except
        log(f"Tesseract ikke fundet ({exc}) — scannede sider bliver ikke OCR-screenet. "
            "Installér Tesseract (med dan+eng), eller sæt TESSERACT_PATH.")
        return None
    return pytesseract


# ---------------------------------------------------------------------------
# Personal-data detection (pure — operates on the word list from extract_pages)
# ---------------------------------------------------------------------------

# CPR: DDMMYY + 4-digit serial. The separator may be a hyphen, a space, a soft
# hyphen and/or a word break — CPR numbers are often split across lines — so we
# allow up to 3 separator chars between the date part and the serial.
_CPR_RE = re.compile(r"(?<!\d)(\d{2})(\d{2})(\d{2})[\s­\-]{0,3}(\d{4})(?!\d)")

# Danish phone: 8 digits, written with a +45 prefix or as space-separated pairs.
# A bare 8-digit run is too ambiguous (act numbers, amounts) to flag.
_PHONE_RE = re.compile(
    r"(?<!\d)(?:"
    r"\+45[\s]?\d{2}[\s]?\d{2}[\s]?\d{2}[\s]?\d{2}"   # +45 12 34 56 78 / +4512345678
    r"|\d{2}\s\d{2}\s\d{2}\s\d{2}"                     # 12 34 56 78
    r")(?!\d)"
)

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


def _valid_cpr_date(dd: str, mm: str) -> bool:
    day, month = int(dd), int(mm)
    return 1 <= day <= 31 and 1 <= month <= 12


def _join_words(words: list[dict]) -> tuple[str, list[int]]:
    """Join a page's words into one string (single-space separated) and a
    parallel list mapping each character to its word index (-1 for separators)."""
    parts: list[str] = []
    char_word: list[int] = []
    for wi, w in enumerate(words):
        if wi:
            parts.append(" ")
            char_word.append(-1)
        text = w["text"]
        parts.append(text)
        char_word.extend([wi] * len(text))
    return "".join(parts), char_word


def _match_rects(words, char_word, start, end, page) -> list[dict]:
    """The boxes of every word the match [start, end) touches (one rect/word)."""
    idxs = sorted({char_word[i] for i in range(start, min(end, len(char_word))) if char_word[i] >= 0})
    rects = []
    for wi in idxs:
        x0, y0, x1, y1 = words[wi]["box"]
        rects.append({
            "page": page,
            "x0": round(x0, 5), "y0": round(y0, 5),
            "x1": round(x1, 5), "y1": round(y1, 5),
        })
    return rects


def find_pii(pages: list[list[dict]]) -> list[dict]:
    """Detect CPR numbers, phone numbers and e-mail addresses across the pages'
    words. Returns de-duplicated suggestions::

        {"type": "cpr"|"telefon"|"email", "value": str, "count": int,
         "pages": [int], "rects": [{"page", "x0", "y0", "x1", "y1"}, ...]}

    Page numbers are 1-based; rect coords are 0..1 of the page (top-left origin).
    These are heuristic hints for a caseworker to review and redact — not an
    authoritative list.
    """
    found: dict = {}

    def add(kind, key, value, page, rects):
        entry = found.get((kind, key))
        if entry is None:
            found[(kind, key)] = {
                "type": kind, "value": value, "count": 1,
                "pages": [page], "rects": list(rects),
            }
        else:
            entry["count"] += 1
            if page not in entry["pages"]:
                entry["pages"].append(page)
            entry["rects"].extend(rects)

    for idx, words in enumerate(pages, start=1):
        if not words:
            continue
        joined, char_word = _join_words(words)

        cpr_spans = []
        for m in _CPR_RE.finditer(joined):
            if not _valid_cpr_date(m.group(1), m.group(2)):
                continue
            cpr_spans.append((m.start(), m.end()))
            clean = f"{m.group(1)}{m.group(2)}{m.group(3)}-{m.group(4)}"
            add("cpr", clean, clean, idx, _match_rects(words, char_word, m.start(), m.end(), idx))

        for m in _EMAIL_RE.finditer(joined):
            value = m.group(0)
            add("email", value.lower(), value, idx, _match_rects(words, char_word, m.start(), m.end(), idx))

        for m in _PHONE_RE.finditer(joined):
            if any(start <= m.start() < end for start, end in cpr_spans):
                continue
            digits = re.sub(r"\D", "", m.group(0))
            if len(digits) == 10 and digits.startswith("45"):
                digits = digits[2:]  # drop the +45 country code
            if len(digits) != 8:
                continue
            grouped = " ".join(digits[i:i + 2] for i in range(0, 8, 2))
            add("telefon", digits, grouped, idx, _match_rects(words, char_word, m.start(), m.end(), idx))

    return list(found.values())
