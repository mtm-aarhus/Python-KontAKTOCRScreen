"""KontAKT OCR-screening robot.

Queue-driven, one queue element per (PDF) document. For a single document it:

  1. downloads the PDF from SharePoint,
  2. extracts the text — the PDF's own text layer where present, Tesseract OCR
     (Danish + English) for scanned / image-only pages,
  3. scans the text for the personal data that typically must be redacted in an
     aktindsigt — CPR numbers, phone numbers and e-mail addresses (handling CPR
     numbers that are split across a line break),
  4. reports the suggestions back to KontAKT, where a caseworker reviews them.

This robot only *suggests* — it never redacts. Actual redaction happens later,
when the case is prepared for release. Names and addresses are not detected
(too noisy to be useful as suggestions).

Queue payload (set by KontAKT's "OCR-screen" trigger):
    {
        "kontakt_case_id": 11,
        "doc_id": 42,
        "source_case_id": "GEO-2024-000170",
        "dok_id": "8431876",
        "sharepoint_url": "https://.../0001 - 8431876 - Titel.pdf"
    }

Result posted back to KontAKT (per document):
    {
        "status": "screened" | "error",
        "suggestions": [{"type": "cpr"|"telefon"|"email", "value", "count", "pages": [...]}],
        "pages": <int>,
        "ocr_used": <bool>,
        "note": <str, on error/skip>
    }

OO config:
    Constant   KontAKTSharePoint      — SharePoint site URL
    Credential SharePointCert         — username = thumbprint, password = cert path
    Credential SharePointAPI          — username = tenant,     password = client id
    Credential KontAKTAPI             — username = base URL,    password = X-API-Key
OCR of scanned / image-only pages uses Tesseract, which is **auto-installed**
(binary + Danish/English language data) if missing — the same way the
conversion robots auto-install LibreOffice (via ``oomtm.pdf.ensure_tesseract``).
Set ``TESSERACT_PATH`` to use an existing install instead; set
``OOMTM_TESSDATA_BASE_URL`` to change where the language data is fetched from.
"""
from OpenOrchestrator.orchestrator_connection.connection import OrchestratorConnection
from OpenOrchestrator.database.queues import QueueElement
import json
import tempfile
from pathlib import Path
from urllib.parse import unquote, urlparse

import requests

from robot_framework import reset
from robot_framework import screening
from oomtm import sharepoint as sp


def process(
    orchestrator_connection: OrchestratorConnection,
    queue_element: QueueElement | None = None,
    client: "reset.Client | None" = None,
) -> None:
    orchestrator_connection.log_trace("Running process.")
    if queue_element is None:
        raise RuntimeError("KontAKTOCRScreen is queue-driven; no queue_element given.")
    if client is None:  # e.g. a manual run outside the queue framework
        client = reset.open_all(orchestrator_connection)

    payload = json.loads(queue_element.data or "{}")
    case_id = int(payload["kontakt_case_id"])
    doc_id = int(payload["doc_id"])
    dok_id = str(payload.get("dok_id") or "").strip()
    sharepoint_url = str(payload.get("sharepoint_url") or "").strip()

    orchestrator_connection.log_info(f"OCRScreen case={case_id} doc={doc_id} dok={dok_id}")

    try:
        result = _screen(orchestrator_connection, client, dok_id, sharepoint_url)
    except Exception as exc:
        orchestrator_connection.log_info(f"OCRScreen failed: {exc!r}")
        _callback(orchestrator_connection, client, case_id, doc_id, {"status": "error", "note": str(exc)[:500]})
        raise

    _callback(orchestrator_connection, client, case_id, doc_id, result)
    n = len(result.get("suggestions", []))
    orchestrator_connection.log_info(f"OCRScreen done doc={doc_id}: {result.get('status')} ({n} forslag)")


def _screen(orchestrator_connection, client, dok_id, sharepoint_url):
    """Download the PDF, extract its text (with OCR fallback) and detect PII."""
    if not sharepoint_url:
        return {"status": "error", "note": "Dokumentet har ingen SharePoint-fil at screene."}

    with tempfile.TemporaryDirectory() as tmpdir:
        local = Path(tmpdir) / f"{dok_id or 'dokument'}.pdf"
        server_relative = unquote(urlparse(sharepoint_url).path)
        sp.download_file(client.sp_ctx, file_path=server_relative, local_path=str(local))

        pages, ocr_used, ocr_skipped = screening.extract_pages(str(local), log=orchestrator_connection.log_info)
        suggestions = screening.find_pii(pages)

    result = {
        "status": "screened",
        "suggestions": suggestions,
        "pages": len(pages),
        "ocr_used": ocr_used,
        "ocr_skipped": ocr_skipped,
    }
    if ocr_skipped:
        # Some image/scanned pages couldn't be OCR'd — screening is INCOMPLETE.
        # KontAKT shows this as a warning instead of a clean result so the
        # caseworker knows to check the document manually (and an admin knows to
        # install Tesseract with the dan+eng language data on the OCR worker).
        result["ocr_incomplete"] = True
        result["note"] = (
            f"{ocr_skipped} side(r) uden tekstlag kunne ikke OCR-screenes "
            "(Tesseract mangler eller fejlede). Screeningen er ufuldstændig — "
            "gennemgå dokumentet manuelt."
        )
    return result


# ----- KontAKT callback ------------------------------------------------------


def _callback(orchestrator_connection, client, case_id: int, doc_id: int, body: dict) -> None:
    try:
        requests.post(
            f"{client.kontakt_base}/api/v1/cases/{case_id}/documents/{doc_id}/ocr",
            headers={"X-API-Key": client.kontakt_key, "Content-Type": "application/json"},
            json=body, timeout=30,
        )
    except Exception as exc:  # pylint: disable=broad-except
        orchestrator_connection.log_info(f"Callback to KontAKT failed: {exc!r}")
