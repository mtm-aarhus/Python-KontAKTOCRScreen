"""KontAKT Nova → PDF → SharePoint robot.

Queue-driven, one queue element per document. Nova has no built-in PDF
converter, so for a single Nova document it:
  1. looks up the document (documentUuid + file extension) by document number,
  2. downloads the original file,
  3. converts it to PDF via oomtm.pdf (LibreOffice / Pillow / email-render),
  4. uploads the PDF to the KontAKT SharePoint site (one file per document),
  5. reports status + the SharePoint URL back to KontAKT.

Videos / audio / unconvertible binaries are skipped (status='skipped').

The Nova token, SharePoint context and cached credentials live on the ``Client``
opened in ``reset.open_all`` and are reused across every queue element (the
framework reconnects via ``reset.reset`` on a retry).

Queue payload (set by KontAKT's "Hent filer" trigger):
    {
        "kontakt_case_id": 11,
        "doc_id": 42,
        "source_case_id": "S2024-12345",
        "dok_id": "D2024-9",
        "akt_id": 5,
        "title": "Ansøgning",
        "case_title": "Aktindsigt i miljøsag"
    }

OO config:
    Constant   KMDNovaURL
    Constant   KMDTokenTimestamp
    Credential KMDClientSecret
    Credential KMDAccessToken        — username = token URL, password = cached token
    Constant   KontAKTSharePoint
    Credential SharePointCert / SharePointAPI / KontAKTAPI   (as in GOToPDF)
"""
from OpenOrchestrator.orchestrator_connection.connection import OrchestratorConnection
from OpenOrchestrator.database.queues import QueueElement
import json
import tempfile
from pathlib import Path

import requests

from robot_framework import reset
from oomtm import nova as oomtm_nova
from oomtm import pdf as oomtm_pdf
from oomtm import sharepoint as sp

LIBRARY = "Delte dokumenter"


def process(
    orchestrator_connection: OrchestratorConnection,
    queue_element: QueueElement | None = None,
    client: "reset.Client | None" = None,
) -> None:
    orchestrator_connection.log_trace("Running process.")
    if queue_element is None:
        raise RuntimeError("KontAKTNovaToPDF is queue-driven; no queue_element given.")
    if client is None:  # e.g. a manual run outside the queue framework
        client = reset.open_all(orchestrator_connection)

    payload = json.loads(queue_element.data or "{}")
    case_id = int(payload["kontakt_case_id"])
    doc_id = int(payload["doc_id"])
    source_case_id = str(payload.get("source_case_id") or "").strip()
    dok_id = str(payload["dok_id"]).strip()
    akt_id = payload.get("akt_id")
    title = str(payload.get("title") or "").strip()
    case_title = str(payload.get("case_title") or "").strip()

    orchestrator_connection.log_info(f"NovaToPDF case={case_id} doc={doc_id} dok={dok_id}")
    _callback(orchestrator_connection, client, case_id, doc_id, {"status": "converting"})

    try:
        result = _convert_and_upload(
            orchestrator_connection, client, case_id, source_case_id, dok_id, akt_id, title, case_title,
        )
    except Exception as exc:
        orchestrator_connection.log_info(f"NovaToPDF failed: {exc!r}")
        _callback(orchestrator_connection, client, case_id, doc_id, {"status": "error", "note": str(exc)[:500]})
        raise

    _callback(orchestrator_connection, client, case_id, doc_id, result)
    orchestrator_connection.log_info(f"NovaToPDF done doc={doc_id}: {result.get('status')}")


# ----- Conversion + upload ---------------------------------------------------


def _convert_and_upload(orchestrator_connection, client, case_id, source_case_id, dok_id, akt_id, title, case_title):
    nova_url = client.nova_url
    token = client.token

    info = oomtm_nova.lookup_document(
        token=token, base_url=nova_url, document_number=dok_id, case_number=source_case_id,
    )
    if not info:
        return {"status": "error", "note": f"Dokument {dok_id} ikke fundet i Nova."}
    document_uuid = info.get("documentUuid")
    ext = (info.get("fileExtension") or "").lower().lstrip(".")
    if not document_uuid:
        return {"status": "error", "note": f"Dokument {dok_id} mangler documentUuid i Nova."}

    with tempfile.TemporaryDirectory() as tmpdir:
        work = Path(tmpdir)
        src = work / f"{dok_id}.{ext or 'bin'}"
        oomtm_nova.download_file(token=token, base_url=nova_url, document_uuid=document_uuid, local_path=str(src))

        if ext == "pdf":
            upload_path, upload_ext, status, note = src, "pdf", "ready", ""
        elif oomtm_pdf.classify(ext) == "skip":
            upload_path, upload_ext, status, note = src, (ext or "bin"), "uploaded_original", (
                f"Filtypen .{ext} kan ikke konverteres til PDF — uploadet som original "
                "(bliver ikke OCR-screenet)."
            )
        else:
            pdf_path, cstatus, cnote = oomtm_pdf.convert_to_pdf(
                src, ext, work, auto_install=True, log=orchestrator_connection.log_info,
            )
            if cstatus == "ready" and pdf_path is not None:
                upload_path, upload_ext, status, note = pdf_path, "pdf", "ready", ""
            else:
                upload_path, upload_ext, status, note = src, (ext or "bin"), "uploaded_original", (
                    cnote or "Kunne ikke konverteres til PDF — original uploadet (bliver ikke OCR-screenet)."
                )

        result = _upload_final(
            orchestrator_connection, client, case_id, source_case_id, akt_id, dok_id, title,
            case_title, upload_path, upload_ext,
        )
        result["status"] = status
        if note:
            result["note"] = note
        return result


def _upload_final(orchestrator_connection, client, case_id, source_case_id, akt_id, dok_id,
                  title, case_title, upload_path, upload_ext):
    """Upload ``upload_path`` (a PDF or an unconvertible original) into the case's
    SharePoint folder and return the callback payload (sans status)."""
    ctx, site_url = client.sp_ctx, client.sp_site_url
    overmappe = sp.sanitize_segment(f"{case_id} - {case_title}")[:120].strip() or str(case_id)
    undermappe = sp.sanitize_segment(source_case_id)[:80].strip() or "ukendt-sag"

    base_path = sp.site_root_path(site_url) + "/" + LIBRARY + "/"
    akt = akt_id if akt_id is not None else 0
    safe_title = sp.sanitize_title(title)
    safe_title = sp.truncate_title(
        safe_title, base_path=base_path, overmappe=overmappe, undermappe=undermappe,
        akt_id=akt, dok_id=dok_id,
    )
    filename = sp.build_filename(akt, dok_id, safe_title, upload_ext)

    final = upload_path.parent / filename
    if upload_path != final:
        upload_path.replace(final)

    sha = oomtm_pdf.sha256_file(final)
    size = final.stat().st_size
    file_path = sp.upload_to_case_folder(
        ctx, site_url=site_url, library=LIBRARY,
        overmappe=overmappe, undermappe=undermappe, local_file=str(final),
    )
    return {
        "sharepoint_url": sp.file_browser_url(site_url, file_path),
        "file_name": filename,
        "file_size_bytes": size,
        "sha256": sha.hex(),
    }


# ----- KontAKT callback ------------------------------------------------------


def _callback(orchestrator_connection, client, case_id: int, doc_id: int, body: dict) -> None:
    try:
        requests.post(
            f"{client.kontakt_base}/api/v1/cases/{case_id}/documents/{doc_id}/file",
            headers={"X-API-Key": client.kontakt_key, "Content-Type": "application/json"},
            json=body, timeout=30,
        )
    except Exception as exc:  # pylint: disable=broad-except
        orchestrator_connection.log_info(f"Callback to KontAKT failed: {exc!r}")
