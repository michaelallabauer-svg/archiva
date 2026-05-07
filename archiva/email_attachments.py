"""Create child documents from .eml attachments."""

from __future__ import annotations

import hashlib
import json
import re
from email import policy
from email.parser import BytesParser
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from archiva.email_utils import parse_eml
from archiva.models import DocType, Document
from archiva.preview_queue import enqueue_preview_job
from archiva.storage import StorageManager

ATTACHMENT_RELATION_TYPE = "email_attachment"


def create_email_attachment_children(
    db: Session,
    *,
    storage: StorageManager,
    parent_document: Document,
    eml_path: Path,
    parent_metadata: dict[str, Any] | None = None,
) -> list[Document]:
    """Persist each .eml attachment as a child Document.

    The parent email metadata is updated in-place with child_document_id values when
    it contains the `_email.attachments` structure from email_utils.email_metadata.
    """
    with eml_path.open("rb") as fh:
        message = BytesParser(policy=policy.default).parse(fh)

    parsed = parse_eml(eml_path)
    email_meta = (parent_metadata or {}).setdefault("_email", {})
    attachments_meta = email_meta.setdefault("attachments", [])
    email_meta.setdefault("attachment_count", len(parsed.attachments))

    children: list[Document] = []
    attachment_index = 0
    for part in message.walk() if message.is_multipart() else [message]:
        filename = part.get_filename()
        disposition = (part.get_content_disposition() or "").lower()
        if disposition != "attachment" and not filename:
            continue

        payload = part.get_payload(decode=True) or b""
        safe_name = _safe_filename(filename, attachment_index)
        content_type = (part.get_content_type() or "application/octet-stream").lower()
        relative_path = storage.generate_path(safe_name)
        full_path = storage.full_path(relative_path)
        full_path.parent.mkdir(parents=True, exist_ok=True)
        full_path.write_bytes(payload)

        child_metadata = {
            "_email_attachment": {
                "parent_document_id": str(parent_document.id),
                "parent_message_id": getattr(parsed, "message_id", ""),
                "index": attachment_index,
                "filename": safe_name,
                "content_type": content_type,
                "content_id": part.get("Content-ID"),
            }
        }
        child = Document(
            name=safe_name,
            title=Path(safe_name).stem,
            doc_type=_guess_attachment_doc_type(content_type, safe_name),
            mime_type=content_type,
            size_bytes=len(payload),
            storage_path=str(relative_path),
            metadata_json=json.dumps(child_metadata, ensure_ascii=False),
            cabinet_id=parent_document.cabinet_id,
            parent_document_id=parent_document.id,
            relation_type=ATTACHMENT_RELATION_TYPE,
            source_attachment_name=safe_name,
            source_attachment_index=attachment_index,
            file_hash=hashlib.md5(payload).hexdigest() if payload else None,
        )
        db.add(child)
        db.flush()
        enqueue_preview_job(db, child)
        children.append(child)

        _update_attachment_metadata(attachments_meta, attachment_index, child, safe_name, content_type, len(payload))
        attachment_index += 1

    if parent_metadata is not None:
        parent_document.metadata_json = json.dumps(parent_metadata, ensure_ascii=False)
    return children


def _update_attachment_metadata(
    attachments_meta: list[Any],
    index: int,
    child: Document,
    filename: str,
    content_type: str,
    size_bytes: int,
) -> None:
    while len(attachments_meta) <= index:
        attachments_meta.append({"index": len(attachments_meta)})
    entry = attachments_meta[index]
    if not isinstance(entry, dict):
        entry = {"index": index}
        attachments_meta[index] = entry
    entry.update(
        {
            "index": index,
            "filename": entry.get("filename") or filename,
            "content_type": entry.get("content_type") or content_type,
            "size_bytes": entry.get("size_bytes") if entry.get("size_bytes") is not None else size_bytes,
            "child_document_id": str(child.id),
            "child_status": "created",
        }
    )


def _safe_filename(filename: str | None, index: int) -> str:
    name = Path(filename or f"attachment-{index + 1}.bin").name.strip() or f"attachment-{index + 1}.bin"
    name = re.sub(r"[\x00-\x1f\x7f]", "", name)
    name = re.sub(r"[/\\\\]+", "-", name)
    return name[:240] or f"attachment-{index + 1}.bin"


def _guess_attachment_doc_type(content_type: str, filename: str) -> DocType:
    lower_name = filename.lower()
    if content_type in {"message/rfc822", "application/eml"} or lower_name.endswith(".eml"):
        return DocType.EMAIL
    if content_type == "application/pdf" or lower_name.endswith(".pdf"):
        return DocType.PDF
    if content_type.startswith("image/"):
        return DocType.IMAGE
    if content_type.startswith("text/") or lower_name.endswith((".txt", ".md", ".csv", ".log", ".json")):
        return DocType.TEXT
    if "word" in content_type or "officedocument" in content_type or lower_name.endswith((".doc", ".docx", ".xls", ".xlsx")):
        return DocType.DOC
    return DocType.OTHER
