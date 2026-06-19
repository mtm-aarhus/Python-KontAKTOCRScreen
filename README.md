# Python-KontAKTOCRScreen

Screens a single PDF document for personal data and suggests what to redact, for the **KontAKT** aktindsigt (FOI request) system.

KontAKT triggers this once per document after a case's files have been transferred to SharePoint and the caseworker starts OCR-screening. It only **suggests** — it never changes the document.

## What it does

For one document:

1. Downloads the PDF from SharePoint.
2. Extracts the text — from the PDF's own text layer where it has one, and via **Tesseract OCR (Danish + English)** for scanned / image-only pages.
3. Scans the text for the personal data that typically must be redacted:
   - **CPR numbers** — including numbers split across a line break,
   - **phone numbers** (Danish, `+45` or spaced pairs),
   - **e-mail addresses**.
   Each hit also records its **location(s) on the page** — one rectangle per
   word it covers (so a CPR split across a line gives two boxes) — so the
   redaction editor can pre-place a box over it.
4. Reports the suggestions back to KontAKT, where a caseworker reviews them.

Names and addresses are deliberately **not** detected — they're too noisy to be useful as automatic suggestions. The results are heuristic hints for a human, not an authoritative redaction list.

## Input (one document)

| Field | Meaning |
|-------|---------|
| `kontakt_case_id` | KontAKT case id |
| `doc_id` | KontAKT document id (used for the result callback) |
| `source_case_id` | GO/Nova case number |
| `dok_id` | Source document id |
| `sharepoint_url` | URL of the PDF to screen |

## Output

A callback to KontAKT, per document:

```json
{
  "status": "screened",
  "suggestions": [
    {"type": "cpr", "value": "010190-1234", "count": 2, "pages": [1, 3],
     "rects": [
       {"page": 1, "x0": 0.22, "y0": 0.10, "x1": 0.40, "y1": 0.12},
       {"page": 3, "x0": 0.15, "y0": 0.55, "x1": 0.33, "y1": 0.57}
     ]},
    {"type": "telefon", "value": "12 34 56 78", "count": 1, "pages": [2],
     "rects": [{"page": 2, "x0": 0.20, "y0": 0.30, "x1": 0.39, "y1": 0.32}]}
  ],
  "pages": 4,
  "ocr_used": true
}
```

Rect coordinates are fractions of the page (0–1, top-left origin), so they map onto the page at any render scale.

On failure it reports `{"status": "error", "note": "…"}`.

## Required configuration

- Constant `KontAKTSharePoint` — SharePoint site URL
- Credential `SharePointCert` — username = certificate thumbprint, password = certificate path
- Credential `SharePointAPI` — username = tenant, password = client id
- Credential `KontAKTAPI` — username = base URL, password = API key

## Requirements on the worker

- **Tesseract** (with Danish + English language data) is used to OCR scanned / image-only pages. It is **auto-installed if missing** — the binary via `winget`/Chocolatey and the `dan`+`eng` language data downloaded to a user folder — the same way the conversion robots auto-install LibreOffice. Set `TESSERACT_PATH` to use an existing install instead, or `OOMTM_TESSDATA_BASE_URL` to change the language-data source (defaults to `tessdata_fast`).
- Downloads go through the **OS trust store** (so they work behind a corporate TLS-inspection proxy). If a page can't be OCR'd (Tesseract truly unavailable), that page is left unscreened and the document is reported as **incomplete** (a ⚠ badge in KontAKT) rather than falsely "clean".

## Dependencies

The shared [`oomtm`](https://github.com/mtm-aarhus/oomtm) library (`sharepoint`), plus [PyMuPDF](https://pymupdf.readthedocs.io/) for text/render and [pytesseract](https://pypi.org/project/pytesseract/) for OCR.
