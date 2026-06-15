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
    {"type": "cpr", "value": "010190-1234", "count": 2, "pages": [1, 3]},
    {"type": "telefon", "value": "12 34 56 78", "count": 1, "pages": [2]},
    {"type": "email", "value": "navn@eksempel.dk", "count": 1, "pages": [2]}
  ],
  "pages": 4,
  "ocr_used": true
}
```

On failure it reports `{"status": "error", "note": "…"}`.

## Required configuration

- Constant `KontAKTSharePoint` — SharePoint site URL
- Credential `SharePointCert` — username = certificate thumbprint, password = certificate path
- Credential `SharePointAPI` — username = tenant, password = client id
- Credential `KontAKTAPI` — username = base URL, password = API key

## Requirements on the worker

- **Tesseract** with the Danish (`dan`) and English (`eng`) language data, for OCR of scanned pages. Put `tesseract` on `PATH`, or set the `TESSERACT_PATH` environment variable to the binary.
- If Tesseract is missing, pages that already have a text layer are still screened; scanned pages are skipped with a logged warning (the result's `ocr_used` stays `false`).

## Dependencies

The shared [`oomtm`](https://github.com/mtm-aarhus/oomtm) library (`sharepoint`), plus [PyMuPDF](https://pymupdf.readthedocs.io/) for text/render and [pytesseract](https://pypi.org/project/pytesseract/) for OCR.
