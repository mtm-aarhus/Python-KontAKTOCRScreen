"""KontAKT OCR-screening robot.

Queue-driven, one queue element per (PDF) document. For a single document it:

  1. downloads the PDF from KontAKT's local file store (GET .../content),
  2. extracts the text — the PDF's own text layer where present, Tesseract OCR
     (Danish + English) for scanned / image-only pages,
  3. scans the text for what the sag's screening rules ask for — the fixed
     patterns ticked on (CPR, telefon, e-mail, CVR, kontonummer, handling numbers
     split across a line break) plus the words and phrases on the relevant lists,
  4. reports the suggestions back to KontAKT, where a caseworker reviews them.

The rules are fetched from KontAKT per sag (``…/screening-rules``) and compiled
there, so what the caseworker was shown when they added a word is exactly what
runs here. Which lists apply follows from the sag — the caseworker on it plus
that person's teams — not from whoever pressed Screen, so a re-run finds the same
things. If KontAKT can't be asked, the three usual patterns apply.

This robot only *suggests* — it never redacts. Actual redaction happens later,
when the case is prepared for release. Names and addresses are not offered as a
fixed pattern (without a register to check against it is guesswork), but a name
can of course be typed in as a word rule.

Queue payload (set by KontAKT's "OCR-screen" trigger):
    {
        "kontakt_case_id": 11,
        "doc_id": 42,
        "source_case_id": "GEO-2024-000170",
        "dok_id": "8431876"
    }

Result posted back to KontAKT (per document):
    {
        "status": "screened" | "error",
        "suggestions": [{"type", "label", "value", "count", "pages": [...], "rects": [...]}],
        "pages": <int>,
        "ocr_used": <bool>,
        "note": <str, on error/skip>
    }

OO config:
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
from robot_framework.exceptions import CaseDeleted
from robot_framework import screening


# ----- Deleted in KontAKT ----------------------------------------------------


def _check_gone(resp) -> None:
    """Stop cleanly if what this queue element is about was deleted in KontAKT.

    KontAKT answers HTTP 410 with ``{"deleted": "case"|"reference"|"document"}``
    when the caseworker deleted the KontAKT case, the sag/mappe or the document
    while this element waited in the queue. Not an error and not retryable, so
    the queue framework marks the element done and takes the next one.
    """
    if resp is None or resp.status_code != 410:
        return
    try:
        body = resp.json() or {}
    except ValueError:
        body = {}
    if body.get("deleted"):
        raise CaseDeleted(body.get("note") or f"{body['deleted']} deleted in KontAKT")


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
    source_case_id = str(payload.get("source_case_id") or "").strip()

    orchestrator_connection.log_info(f"OCRScreen case={case_id} doc={doc_id} dok={dok_id}")

    try:
        result = _screen(orchestrator_connection, client, case_id, doc_id, dok_id, source_case_id)
    except Exception as exc:
        orchestrator_connection.log_info(f"OCRScreen failed: {exc!r}")
        _callback(orchestrator_connection, client, case_id, doc_id, {"status": "error", "note": str(exc)[:500]})
        raise

    _callback(orchestrator_connection, client, case_id, doc_id, result)
    n = len(result.get("suggestions", []))
    orchestrator_connection.log_info(f"OCRScreen done doc={doc_id}: {result.get('status')} ({n} forslag)")


def _fetch_content(client, case_id, doc_id, local_path) -> bool:
    """Stream a document's stored bytes from KontAKT to ``local_path``. Returns
    False if the file isn't in the store (404)."""
    r = requests.get(
        f"{client.kontakt_base}/api/v1/cases/{case_id}/documents/{doc_id}/content",
        headers={"X-API-Key": client.kontakt_key}, timeout=300, stream=True,
    )
    _check_gone(r)
    if r.status_code == 404:
        return False
    r.raise_for_status()
    with open(local_path, "wb") as fh:
        for chunk in r.iter_content(1 << 20):
            if chunk:
                fh.write(chunk)
    return True


def _fetch_rules(orchestrator_connection, client, case_id, doc_id, source_case_id):
    """The screening rules for the sag this document belongs to.

    Compiled by KontAKT (app/screening_rules.py) so the caseworker's "matcher
    eksempelvis også …" cannot drift from what actually runs. Fetched per run per
    sag, and at run time rather than from the queue payload — a word added while
    the element waited in the queue still counts.

    On any failure the screening falls back to ``screening.DEFAULT_SPEC`` (CPR,
    telefon, e-mail): finding the usual three beats finding nothing.
    """
    key = source_case_id or f"doc:{doc_id}"
    if key in client.screening_specs:
        return client.screening_specs[key]
    spec = None
    try:
        r = requests.get(
            f"{client.kontakt_base}/api/v1/cases/{case_id}/documents/{doc_id}/screening-rules",
            headers={"X-API-Key": client.kontakt_key}, timeout=30,
        )
        _check_gone(r)
        if r.status_code == 404:
            orchestrator_connection.log_info(
                "KontAKT kender ikke screeningsregler endnu — bruger standardmønstrene.")
        else:
            r.raise_for_status()
            spec = r.json()
    except CaseDeleted:
        raise
    except Exception as exc:  # pylint: disable=broad-except
        orchestrator_connection.log_info(
            f"Kunne ikke hente screeningsregler ({exc!r}) — bruger standardmønstrene.")
    if spec:
        who = ", ".join(s.get("label") or "?" for s in spec.get("scopes") or [])
        orchestrator_connection.log_info(
            f"Screeningsregler for {key}: {len(spec.get('patterns') or [])} mønstre"
            + (f" ({who})" if who else ""))
    client.screening_specs[key] = spec
    return spec


def _screen(orchestrator_connection, client, case_id, doc_id, dok_id, source_case_id=""):
    """Fetch the PDF from KontAKT's file store, extract its text (with OCR
    fallback) and detect what the sag's screening rules ask for."""
    spec = _fetch_rules(orchestrator_connection, client, case_id, doc_id, source_case_id)
    with tempfile.TemporaryDirectory() as tmpdir:
        local = Path(tmpdir) / f"{dok_id or 'dokument'}.pdf"
        if not _fetch_content(client, case_id, doc_id, local):
            return {"status": "error", "note": "Dokumentet har ingen fil at screene."}

        pages, ocr_used, ocr_skipped = screening.extract_pages(str(local), log=orchestrator_connection.log_info)
        suggestions = screening.find_pii(pages, spec, log=orchestrator_connection.log_info)

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
        resp = requests.post(
            f"{client.kontakt_base}/api/v1/cases/{case_id}/documents/{doc_id}/ocr",
            headers={"X-API-Key": client.kontakt_key, "Content-Type": "application/json"},
            json=body, timeout=30,
        )
    except Exception as exc:  # pylint: disable=broad-except
        orchestrator_connection.log_info(f"Callback to KontAKT failed: {exc!r}")
        return
    # Outside the except: a network blip stays harmless, but "deleted in KontAKT"
    # must reach the framework instead of being swallowed as a broad Exception.
    _check_gone(resp)
