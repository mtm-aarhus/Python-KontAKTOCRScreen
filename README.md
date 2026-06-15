# Python-KontAKTNovaToPDF

Converts a single **KMD Nova** document to PDF and uploads it to SharePoint, for the **KontAKT** aktindsigt (FOI request) system. The Nova counterpart to `Python-KontAKTGOToPDF`.

KontAKT triggers this once per document when a caseworker transfers a case's files to SharePoint.

## What it does

For one Nova document:

1. Looks up the document (its `documentUuid` and file extension) by document number.
2. Downloads the original file.
3. Converts it to PDF via the shared [`oomtm`](https://github.com/mtm-aarhus/oomtm) library (LibreOffice / Pillow for images / e-mail rendering). Nova has no built-in converter, so all conversion happens here.
4. Uploads the PDF to the KontAKT SharePoint site — one file per document.
5. Reports the result (status + SharePoint URL) back to KontAKT.

Files that can't be converted are uploaded **as their original**; video / audio / unconvertible binaries are skipped.

## SharePoint layout

```
{site}/Delte dokumenter/{kontakt-sag-id} - {sagstitel}/{Nova-sagsnummer}/{aktnr} - {doknr} - {titel}.pdf
```

## Input (one document)

| Field | Meaning |
|-------|---------|
| `kontakt_case_id` | KontAKT case id |
| `doc_id` | KontAKT document id (used for the result callback) |
| `source_case_id` | Nova case number |
| `dok_id` | Nova document number |
| `akt_id` | Act number (zero-padded in the filename) |
| `title` | Document title |
| `case_title` | KontAKT case title (used for the folder name) |

## Output

The PDF (or unconverted original) in SharePoint, plus a callback to KontAKT with the SharePoint URL, file name, size and SHA-256.

## Required configuration

- Constant `KMDNovaURL` — Nova API base URL
- Constant `KMDTokenTimestamp` — cached token issue time (updated automatically)
- Credential `KMDClientSecret` — KMD OAuth2 client secret
- Credential `KMDAccessToken` — username = token URL, password = cached bearer token (updated automatically)
- Constant `KontAKTSharePoint` — SharePoint site URL (library: *Delte dokumenter*)
- Credential `SharePointCert` — username = certificate thumbprint, password = certificate path
- Credential `SharePointAPI` — username = tenant, password = client id
- Credential `KontAKTAPI` — username = base URL, password = API key

## Dependencies

The shared [`oomtm`](https://github.com/mtm-aarhus/oomtm) library (`nova`, `pdf`, `sharepoint`). PDF conversion auto-installs LibreOffice on the worker if it's missing (no admin required).
