"""OCR text extraction + personal-data detection for KontAKT screening.

Two layers, split so the tricky part is testable on its own:

* ``extract_pages`` — pull *words with positions* out of a PDF: its own text
  layer where present, Tesseract OCR (Danish + English) for scanned / image
  pages. Each word's box is normalised to the page size (0..1, top-left
  origin) so it's independent of render scale / DPI. Needs PyMuPDF (+ Tesseract
  for scanned pages).

* ``find_pii`` — scan those words for what the sag's screening rules ask for, and
  return, for each hit, the **rectangles** to redact. Pure logic over the word
  list — no PyMuPDF needed — so the detection and coordinate mapping are
  unit-testable.

The rules come from KontAKT as a compiled spec (see ``DEFAULT_SPEC`` for the
shape, and ``app/screening_rules.py`` for where they are built): the fixed
patterns a caseworker ticked on, plus the words and phrases on the lists that
apply to that sag. The patterns are built there so the caseworker's "matcher
eksempelvis også …" cannot drift from what runs here; the *validation* of a hit
(a CPR's date must be real, a CVR's control digit must add up) stays here,
because that is logic rather than text. Without a spec the three usual patterns
apply, so an upgrade in either order still screens.

A hit that spans several words (e.g. a CPR split across a line break) yields one
rect *per word*, so redaction covers each piece precisely instead of a single
box bridging two lines. Names and addresses are not offered as a fixed pattern —
without a register to check against it is guesswork — but a name can of course be
typed in as a word rule.

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
_OCR_DPI = 450
# Screenshots/forms embedded as images are better treated as one text block than
# with Tesseract's default fully automatic page segmentation.
_OCR_CONFIG = "--psm 6"


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
    data = ocr.image_to_data(img, lang=ocr_lang, output_type=ocr.Output.DICT, config=_OCR_CONFIG)
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


_DAYS_IN_MONTH = (31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)


def _is_leap_year(year: int) -> bool:
    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)


def _cpr_birth_year(yy: int, serial7: int) -> int:
    """Decode the 4-digit birth year from YY + the serial's first digit, per the
    official CPR century table — needed so 29 February is only accepted in a real
    leap year (the year, not just YY, decides the century)."""
    if serial7 <= 3:
        return 1900 + yy
    if serial7 in (4, 9):
        return 2000 + yy if yy <= 36 else 1900 + yy
    return 2000 + yy if yy <= 57 else 1800 + yy   # serial7 in 5..8


def _valid_cpr_date(dd: str, mm: str, yy: str, serial: str) -> bool:
    """True only if DDMMYY is a real calendar date (correct days-per-month and
    leap years). A genuine CPR always has a valid birth date, so this rejects
    only impossible coincidental matches (e.g. 31-04, 29-02 in a non-leap year)."""
    day, month, year2 = int(dd), int(mm), int(yy)
    if not (1 <= month <= 12):
        return False
    serial7 = int(serial[0]) if serial[:1].isdigit() else 0
    year = _cpr_birth_year(year2, serial7)
    max_day = 29 if (month == 2 and _is_leap_year(year)) else _DAYS_IN_MONTH[month - 1]
    return 1 <= day <= max_day


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


# ---------------------------------------------------------------------------
# Validering af de faste mønstre
#
# A pattern says where to look; a validator says whether what was found counts.
# The patterns come from KontAKT (app/screening_rules.py) so the caseworker's
# "matcher eksempelvis også …" can't drift from what runs here; the checks stay
# here because they are logic, not text. The spec names one by ``validate``.
#
# A validator returns (display_value, dedupe_key) or None to reject the match.
# ---------------------------------------------------------------------------


def _v_cpr(m):
    if len(m.groups()) < 4:
        return None
    if not _valid_cpr_date(m.group(1), m.group(2), m.group(3), m.group(4)):
        return None
    clean = f"{m.group(1)}{m.group(2)}{m.group(3)}-{m.group(4)}"
    return clean, clean


def _v_telefon(m):
    digits = re.sub(r"\D", "", m.group(0))
    if len(digits) == 10 and digits.startswith("45"):
        digits = digits[2:]                     # drop the +45 country code
    if len(digits) != 8:
        return None
    return " ".join(digits[i:i + 2] for i in range(0, 8, 2)), digits


def _v_cvr(m):
    """Modulus 11 over the first seven digits — a CVR number's control digit.

    Eight digits is a common shape in a case (dates, amounts, act numbers), so
    without this check the suggestion list would fill up with them.
    """
    digits = re.sub(r"\D", "", m.group(0))
    if len(digits) != 8:
        return None
    weights = (2, 7, 6, 5, 4, 3, 2, 1)
    if sum(int(d) * w for d, w in zip(digits, weights)) % 11 != 0:
        return None
    return digits, digits


def _v_konto(m):
    """Registreringsnummer + kontonummer, written the same way however it was
    found, so "1234-567890" and "1234 567890" are one suggestion."""
    digits = re.sub(r"\D", "", m.group(0))
    if len(digits) < 10:
        return None
    return f"{digits[:4]}-{digits[4:]}", digits


_VALIDATORS = {"cpr": _v_cpr, "telefon": _v_telefon, "cvr": _v_cvr, "konto": _v_konto}

# What to screen for when KontAKT didn't say — an older KontAKT that doesn't serve
# the rules endpoint yet. Exactly what this robot looked for before the lists
# existed, so an upgrade in either order still screens.
DEFAULT_SPEC = {
    "version": 1,
    "scopes": [],
    "patterns": [
        {"type": "cpr", "kind": "builtin", "label": "CPR-numre",
         "regex": _CPR_RE.pattern, "validate": "cpr", "numeric": True},
        {"type": "telefon", "kind": "builtin", "label": "Telefonnumre",
         "regex": _PHONE_RE.pattern, "validate": "telefon", "numeric": True},
        {"type": "email", "kind": "builtin", "label": "E-mailadresser",
         "regex": _EMAIL_RE.pattern, "validate": None, "numeric": False},
    ],
}


def _compile_spec(spec, log):
    """(pattern dict, compiled regex, validator) per usable pattern.

    A pattern that won't compile is skipped with a logged warning rather than
    failing the whole screening: one broken rule must not stop the other twenty.
    """
    out = []
    for p in (spec or {}).get("patterns") or []:
        raw = p.get("regex") or ""
        if not raw:
            continue
        try:
            rx = re.compile(raw)
        except re.error as exc:
            log(f"Screeningsmønster kunne ikke bruges ({p.get('label')}): {exc}")
            continue
        name = p.get("validate")
        if name and name not in _VALIDATORS:
            # A pattern from a newer KontAKT whose check this robot doesn't know:
            # suggest a little too much rather than silently nothing.
            log(f"Ukendt validering {name!r} for {p.get('label')} — bruger mønsteret uden.")
        out.append((p, rx, _VALIDATORS.get(name)))
    return out


def find_pii(pages: list[list[dict]], spec: dict | None = None, log=None) -> list[dict]:
    """Detect what the case's screening rules ask for, across the pages' words.

    ``spec`` is what KontAKT's ``…/screening-rules`` endpoint returned: the fixed
    patterns ticked on for this sag, plus the words and phrases from the relevant
    lists. Without one, DEFAULT_SPEC (CPR, telefon, e-mail) applies.

    Returns de-duplicated suggestions::

        {"type": "cpr"|"telefon"|"email"|"cvr"|"konto"|"ord"|"frase",
         "label": str, "value": str, "count": int, "pages": [int],
         "rects": [{"page", "x0", "y0", "x1", "y1"}, ...]}

    Page numbers are 1-based; rect coords are 0..1 of the page (top-left origin).
    These are heuristic hints for a caseworker to review and redact — not an
    authoritative list.
    """
    log = log or (lambda *_: None)
    patterns = _compile_spec(spec or DEFAULT_SPEC, log)
    found: dict = {}

    def add(pattern, key, value, page, rects):
        kind = pattern.get("type") or "ord"
        entry = found.get((kind, key))
        if entry is None:
            found[(kind, key)] = {
                "type": kind, "label": pattern.get("label") or kind,
                "value": value, "count": 1,
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
        # The numeric patterns overlap each other — a CPR number is also eight
        # digits next to four — so the first one that accepts a stretch of digits
        # keeps it. Patterns arrive in KontAKT's order, CPR first.
        taken: list[tuple[int, int]] = []

        for pattern, rx, validate in patterns:
            numeric = bool(pattern.get("numeric"))
            for m in rx.finditer(joined):
                if numeric and any(s <= m.start() < e for s, e in taken):
                    continue
                if validate is not None:
                    checked = validate(m)
                    if checked is None:
                        continue
                    value, key = checked
                else:
                    value = m.group(0)
                    key = value.casefold()
                if numeric:
                    taken.append((m.start(), m.end()))
                add(pattern, key, value, idx,
                    _match_rects(words, char_word, m.start(), m.end(), idx))

    return list(found.values())
