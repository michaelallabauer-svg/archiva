"""Small HTTP client for the external PDFStampede service."""

from __future__ import annotations

import json
import mimetypes
import uuid
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from archiva.config import Settings
from archiva.models import Document, DocumentType
from archiva.metadata_validation import metadata_from_json


class PdfStampedeError(RuntimeError):
    """Raised when PDFStampede cannot be reached or returns an error."""


def pdf_stampede_enabled(settings: Settings) -> bool:
    return bool(getattr(settings.pdf_stampede, "enabled", False))


def list_pdf_stampede_templates(settings: Settings) -> list[dict[str, Any]]:
    """Return template summaries from PDFStampede, or an empty list if disabled/unavailable."""
    if not pdf_stampede_enabled(settings):
        return []
    url = f"{str(settings.pdf_stampede.base_url).rstrip('/')}/pdf-stamp-templates"
    request = Request(url, method="GET", headers={"Accept": "application/json"})
    try:
        with urlopen(request, timeout=settings.pdf_stampede.timeout_seconds) as response:  # noqa: S310 - configured local service URL
            payload = response.read().decode("utf-8")
        data = json.loads(payload)
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError):
        return []
    return data if isinstance(data, list) else []


def build_stamp_index_data(document: Document) -> dict[str, str]:
    """Build the index_data payload expected by PDFStampede templates."""
    metadata = metadata_from_json(document.metadata_json)
    data = {str(key): "" if value is None else str(value) for key, value in metadata.items()}
    data.setdefault("document_id", str(document.id))
    data.setdefault("document_short_id", str(document.id).split("-", maxsplit=1)[0])
    data.setdefault("document_name", document.name)
    data.setdefault("document_title", document.title or document.name)
    data.setdefault("document_type", document.document_type.name if document.document_type else "")
    data.setdefault("received_at", document.created_at.strftime("%d.%m.%Y") if document.created_at else "")
    data.setdefault("workflow_status", "Erfasst")
    return data


def stamp_pdf_with_pdf_stampede(
    *,
    settings: Settings,
    source_pdf_path: Path,
    document: Document,
    document_type: DocumentType,
) -> bytes:
    """Render a stamped PDF using the template configured on the document type."""
    if not pdf_stampede_enabled(settings):
        raise PdfStampedeError("PDFStampede ist in config.yaml nicht aktiviert.")

    template_id = (document_type.pdf_stampede_template_id or settings.pdf_stampede.default_template_id or "").strip()
    if not template_id:
        raise PdfStampedeError("Keine PDFStampede-Vorlage am Dokumenttyp oder in der Konfiguration hinterlegt.")

    url = f"{str(settings.pdf_stampede.base_url).rstrip('/')}/pdf-stamps/render"
    boundary = f"----archiva-pdfstampede-{uuid.uuid4().hex}"
    payload = {
        "template_id": template_id,
        "index_data": build_stamp_index_data(document),
    }
    # PDFStampede /render expects placements directly. If only a template id is configured,
    # use the template endpoint first and turn it into the render payload.
    template = _get_pdf_stampede_template(settings, template_id)
    payload = {
        "placements": template.get("items") or [],
        "page_strategy": template.get("page_strategy") or "all",
        "index_data": build_stamp_index_data(document),
    }

    body = _multipart_body(
        boundary=boundary,
        fields={"payload": json.dumps(payload, ensure_ascii=False)},
        files={
            "pdf_file": (
                document.name or source_pdf_path.name,
                source_pdf_path.read_bytes(),
                mimetypes.guess_type(source_pdf_path.name)[0] or "application/pdf",
            )
        },
    )
    request = Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}", "Accept": "application/pdf"},
    )
    try:
        with urlopen(request, timeout=settings.pdf_stampede.timeout_seconds) as response:  # noqa: S310 - configured service URL
            return response.read()
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise PdfStampedeError(f"PDFStampede HTTP {exc.code}: {detail}") from exc
    except (URLError, TimeoutError) as exc:
        raise PdfStampedeError(f"PDFStampede nicht erreichbar: {exc}") from exc


def _get_pdf_stampede_template(settings: Settings, template_id: str) -> dict[str, Any]:
    url = f"{str(settings.pdf_stampede.base_url).rstrip('/')}/pdf-stamp-templates/{template_id}"
    request = Request(url, method="GET", headers={"Accept": "application/json"})
    try:
        with urlopen(request, timeout=settings.pdf_stampede.timeout_seconds) as response:  # noqa: S310
            data = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise PdfStampedeError(f"PDFStampede-Vorlage {template_id} nicht gefunden: HTTP {exc.code} {detail}") from exc
    except (URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise PdfStampedeError(f"PDFStampede-Vorlage {template_id} konnte nicht geladen werden: {exc}") from exc
    if not data.get("items"):
        raise PdfStampedeError(f"PDFStampede-Vorlage {template_id} enthält keine Stempel-Elemente.")
    return data


def _multipart_body(
    *,
    boundary: str,
    fields: dict[str, str],
    files: dict[str, tuple[str, bytes, str]],
) -> bytes:
    chunks: list[bytes] = []
    for name, value in fields.items():
        chunks.extend([
            f"--{boundary}\r\n".encode(),
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
            value.encode("utf-8"),
            b"\r\n",
        ])
    for name, (filename, content, content_type) in files.items():
        chunks.extend([
            f"--{boundary}\r\n".encode(),
            f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'.encode(),
            f"Content-Type: {content_type}\r\n\r\n".encode(),
            content,
            b"\r\n",
        ])
    chunks.append(f"--{boundary}--\r\n".encode())
    return b"".join(chunks)
