"""OCR text extraction + personal-data detection for KontAKT screening.

Pure, framework-agnostic helpers used by ``process.py``:

* ``extract_text`` — pull text out of a PDF: its own text layer where present,
  Tesseract OCR (Danish + English) for scanned / image-only pages.
* ``find_pii``     — scan that text for the personal data that typically must be
  redacted in an aktindsigt: CPR numbers, phone numbers and e-mail addresses.
  Names and addresses are deliberately NOT detected (too noisy to be useful).

OCR of scanned pages needs the Tesseract binary on the worker, with the Danish
(``dan``) and English (``eng``) language data installed. Point at it with the
``TESSERACT_PATH`` env var, or have ``tesseract`` on PATH. If Tesseract is
unavailable, scanned pages are skipped (text-layer pages are still screened)
and the caller learns via the returned ``ocr_used`` flag and a logged warning.
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
# Text extraction
# ---------------------------------------------------------------------------


def extract_text(pdf_path: str, *, ocr_lang: str = "dan+eng", log=None) -> tuple[list[str], bool]:
    """Return ``(pages, ocr_used)`` — the text of each page, and whether OCR ran.

    A page's embedded text layer is used when it has one; otherwise the page is
    rendered to an image and run through Tesseract (when available).
    """
    log = log or (lambda *_: None)
    import fitz  # PyMuPDF — lazy import

    ocr = _load_tesseract(log)
    pages: list[str] = []
    ocr_used = False
    with fitz.open(pdf_path) as doc:
        for page in doc:
            text = page.get_text("text") or ""
            if len(text.strip()) >= _MIN_TEXT_CHARS or ocr is None:
                pages.append(text)
                continue
            try:
                image = _render_page(page)
                ocr_text = ocr.image_to_string(image, lang=ocr_lang)
                ocr_used = True
                pages.append(ocr_text or text)
            except Exception as exc:  # pylint: disable=broad-except
                log(f"OCR fejlede på en side: {exc!r}")
                pages.append(text)
    return pages, ocr_used


def _render_page(page):
    from PIL import Image  # lazy
    pix = page.get_pixmap(dpi=_OCR_DPI, alpha=False)
    return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)


def _load_tesseract(log):
    """Return the pytesseract module configured to find the binary, or None."""
    try:
        import pytesseract
    except ImportError:
        log("pytesseract ikke installeret — scannede sider bliver ikke OCR-screenet.")
        return None
    cmd = os.getenv("TESSERACT_PATH")
    if cmd:
        pytesseract.pytesseract.tesseract_cmd = cmd
    try:
        pytesseract.get_tesseract_version()
    except Exception as exc:  # pylint: disable=broad-except
        log(f"Tesseract ikke fundet ({exc}) — scannede sider bliver ikke OCR-screenet. "
            "Installér Tesseract (med dan+eng), eller sæt TESSERACT_PATH.")
        return None
    return pytesseract


# ---------------------------------------------------------------------------
# Personal-data detection
# ---------------------------------------------------------------------------

# CPR: DDMMYY + 4-digit serial. The separator may be a hyphen, a space, a soft
# hyphen and/or a line break — CPR numbers are often split across lines — so we
# allow up to 3 separator chars between the date part and the serial.
_CPR_RE = re.compile(r"(?<!\d)(\d{2})(\d{2})(\d{2})[\s­\-]{0,3}(\d{4})(?!\d)")

# Danish phone: 8 digits, written either with a +45 prefix or as space-separated
# pairs ("12 34 56 78"). A bare 8-digit run is too ambiguous (act numbers,
# amounts, dates) to flag, so we require one of those two shapes.
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


def find_pii(pages: list[str]) -> list[dict]:
    """Scan page texts for CPR numbers, phone numbers and e-mail addresses.

    Returns a de-duplicated list of suggestions, each::

        {"type": "cpr"|"telefon"|"email", "value": str, "count": int, "pages": [int]}

    Page numbers are 1-based. These are heuristic suggestions for a caseworker
    to review — not an authoritative redaction list.
    """
    found: dict = {}

    def add(kind, key, value, page):
        entry = found.get((kind, key))
        if entry is None:
            found[(kind, key)] = {"type": kind, "value": value, "count": 1, "pages": [page]}
        else:
            entry["count"] += 1
            if page not in entry["pages"]:
                entry["pages"].append(page)

    for idx, raw in enumerate(pages, start=1):
        text = raw or ""

        cpr_spans = []
        for m in _CPR_RE.finditer(text):
            if not _valid_cpr_date(m.group(1), m.group(2)):
                continue
            cpr_spans.append((m.start(), m.end()))
            clean = f"{m.group(1)}{m.group(2)}{m.group(3)}-{m.group(4)}"
            add("cpr", clean, clean, idx)

        for m in _EMAIL_RE.finditer(text):
            value = m.group(0)
            add("email", value.lower(), value, idx)

        for m in _PHONE_RE.finditer(text):
            # Skip phone matches that fall inside a CPR (its first 8 digits).
            if any(start <= m.start() < end for start, end in cpr_spans):
                continue
            digits = re.sub(r"\D", "", m.group(0))
            if len(digits) == 10 and digits.startswith("45"):
                digits = digits[2:]  # drop the +45 country code
            if len(digits) != 8:
                continue
            grouped = " ".join(digits[i:i + 2] for i in range(0, 8, 2))
            add("telefon", digits, grouped, idx)

    return list(found.values())
