"""Document management routes for Archiva ECM."""

import json
import shutil
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from pydantic import BaseModel
from sqlalchemy.orm import Session

from archiva.database import get_db
from archiva.indexer.dispatcher import enqueue_document_index
from archiva.metadata_validation import (
    metadata_from_json,
    metadata_to_json,
    validate_document_metadata,
)
from archiva.models import DocType, Document, DocumentType, DocumentVersion
from archiva.search_legacy import build_search_query, update_document_vector
from archiva.storage import StorageManager

router = APIRouter(prefix="/api/v1", tags=["documents"])

_storage: Optional[StorageManager] = None


def get_storage() -> StorageManager:
    if _storage is None:
        raise HTTPException(status_code=500, detail="Storage not initialized")
    return _storage


def init_router(storage: StorageManager) -> None:
    global _storage
    _storage = storage


class DocumentBase(BaseModel):
    name: str
    title: Optional[str] = None
    author: Optional[str] = None
    description: Optional[str] = None
    tags: Optional[str] = None


class DocumentTypeSummary(BaseModel):
    id: UUID
    name: str
    icon: Optional[str] = None
    register_id: UUID

    class Config:
        from_attributes = True


class DocumentResponse(DocumentBase):
    id: UUID
    doc_type: str
    mime_type: Optional[str]
    size_bytes: int
    document_type_id: Optional[UUID] = None
    cabinet_id: Optional[UUID] = None
    metadata: Optional[dict[str, Any]] = None
    document_type: Optional[DocumentTypeSummary] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class DocumentListResponse(BaseModel):
    items: list[DocumentResponse]
    total: int
    page: int
    page_size: int


class SearchResult(BaseModel):
    id: str
    name: str
    title: Optional[str]
    doc_type: str
    author: Optional[str]
    description: Optional[str]
    tags: Optional[str]
    created_at: Optional[str]
    rank: float
    snippet: str


class SearchResponse(BaseModel):
    query: str
    results: list[SearchResult]
    total: int


class MessageResponse(BaseModel):
    message: str


@router.get("/health")
async def health_check() -> dict:
    return {"status": "healthy", "service": "archiva"}


@router.post("/documents", response_model=DocumentResponse)
async def upload_document(
    file: UploadFile = File(...),
    title: Optional[str] = Form(default=None),
    author: Optional[str] = Form(default=None),
    description: Optional[str] = Form(default=None),
    tags: Optional[str] = Form(default=None),
    document_type_id: Optional[UUID] = Form(default=None),
    metadata: Optional[str] = Form(default=None),
    db: Session = Depends(get_db),
    storage: StorageManager = Depends(get_storage),
) -> DocumentResponse:
    metadata_payload = _parse_metadata_payload(metadata)
    metadata_json = None

    if metadata_payload and not document_type_id:
        raise HTTPException(
            status_code=422,
            detail={"errors": [{"field": "document_type_id", "message": "document_type_id is required when metadata is provided"}]},
        )

    if document_type_id:
        validation = validate_document_metadata(db, document_type_id, metadata_payload)
        metadata_payload = validation.normalized
        metadata_json = metadata_to_json(metadata_payload)

    doc_type = _guess_doc_type(file.content_type)
    storage_path = storage.generate_path(file.filename)
    await storage.save(file, storage_path)

    content_text = await _extract_text(file, storage_path, doc_type)

    document = Document(
        name=file.filename,
        doc_type=doc_type,
        mime_type=file.content_type,
        size_bytes=file.size or 0,
        storage_path=str(storage_path),
        title=title or file.filename,
        author=author,
        description=description,
        tags=tags,
        document_type_id=document_type_id,
        cabinet_id=_resolve_cabinet_id(db, document_type_id),
        metadata_json=metadata_json,
    )

    db.add(document)
    db.flush()

    if content_text:
        update_document_vector(db, document.id, content_text)

    db.commit()
    db.refresh(document)
    enqueue_document_index(db, document=document, reason="document_uploaded_api")
    return _document_to_response(document)


@router.get("/documents", response_model=DocumentListResponse)
async def list_documents(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    doc_type: Optional[str] = None,
    document_type_id: Optional[UUID] = None,
    db: Session = Depends(get_db),
) -> DocumentListResponse:
    query = db.query(Document)

    if doc_type:
        query = query.where(Document.doc_type == doc_type)
    if document_type_id:
        query = query.where(Document.document_type_id == document_type_id)

    total = query.count()
    offset = (page - 1) * page_size

    documents = query.order_by(Document.created_at.desc()).offset(offset).limit(page_size).all()

    return DocumentListResponse(
        items=[_document_to_response(d) for d in documents],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/documents/{document_id}", response_model=DocumentResponse)
async def get_document(document_id: UUID, db: Session = Depends(get_db)) -> DocumentResponse:
    document = db.query(Document).where(Document.id == document_id).first()
    if not document:
        raise HTTPException(status_code=404, detail="Document not found")
    return _document_to_response(document)


@router.delete("/documents/{document_id}", response_model=MessageResponse)
async def delete_document(
    document_id: UUID,
    db: Session = Depends(get_db),
    storage: StorageManager = Depends(get_storage),
) -> MessageResponse:
    document = db.query(Document).where(Document.id == document_id).first()
    if not document:
        raise HTTPException(status_code=404, detail="Document not found")

    storage.delete(Path(document.storage_path))
    db.query(DocumentVersion).where(DocumentVersion.document_id == document_id).delete()
    db.delete(document)
    db.commit()

    return MessageResponse(message="Document deleted successfully")


@router.get("/document-types/{document_type_id}/capture")
async def get_capture_definition(document_type_id: UUID, db: Session = Depends(get_db)) -> dict[str, Any]:
    document_type = db.query(DocumentType).where(DocumentType.id == document_type_id).first()
    if not document_type:
        raise HTTPException(status_code=404, detail="Document type not found")

    fields = []
    for field in sorted(document_type.fields, key=lambda item: item.order):
        options = None
        if field.options:
            try:
                options = json.loads(field.options)
            except json.JSONDecodeError:
                options = None
        fields.append(
            {
                "id": str(field.id),
                "name": field.name,
                "label": field.label or field.name,
                "field_type": field.field_type,
                "description": field.description,
                "placeholder": field.placeholder,
                "default_value": field.default_value,
                "is_required": field.is_required,
                "is_unique": field.is_unique,
                "order": field.order,
                "width": field.width,
                "options": options,
                "min_value": field.min_value,
                "max_value": field.max_value,
                "min_length": field.min_length,
                "max_length": field.max_length,
                "pattern": field.pattern,
            }
        )

    return {
        "document_type": {
            "id": str(document_type.id),
            "name": document_type.name,
            "description": document_type.description,
            "icon": document_type.icon,
            "register_id": str(document_type.register_id),
        },
        "fields": fields,
    }


@router.get("/search", response_model=SearchResponse)
async def search_documents(
    q: str = Query(..., min_length=1),
    doc_type: Optional[str] = None,
    limit: int = Query(100, ge=1, le=100),
    db: Session = Depends(get_db),
) -> SearchResponse:
    results = build_search_query(
        db, q, doc_type=doc_type, limit=limit, highlight_fragment_size=150
    )
    return SearchResponse(query=q, results=[SearchResult(**r) for r in results], total=len(results))


def _document_to_response(document: Document) -> DocumentResponse:
    document_type = None
    if document.document_type:
        document_type = DocumentTypeSummary.model_validate(document.document_type)

    return DocumentResponse(
        id=document.id,
        name=document.name,
        title=document.title,
        author=document.author,
        description=document.description,
        tags=document.tags,
        doc_type=document.doc_type.value if hasattr(document.doc_type, "value") else str(document.doc_type),
        mime_type=document.mime_type,
        size_bytes=document.size_bytes,
        document_type_id=document.document_type_id,
        cabinet_id=document.cabinet_id,
        metadata=metadata_from_json(document.metadata_json),
        document_type=document_type,
        created_at=document.created_at,
        updated_at=document.updated_at,
    )


def _resolve_cabinet_id(db: Session, document_type_id: Optional[UUID]) -> Optional[UUID]:
    if not document_type_id:
        return None
    document_type = db.query(DocumentType).where(DocumentType.id == document_type_id).first()
    if not document_type:
        return None
    if document_type.cabinet_id:
        return document_type.cabinet_id
    if document_type.register and document_type.register.cabinet_id:
        return document_type.register.cabinet_id
    return None


def _parse_metadata_payload(metadata: Optional[str]) -> dict[str, Any] | None:
    if metadata is None or not metadata.strip():
        return None
    try:
        payload = json.loads(metadata)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=422,
            detail={"errors": [{"field": "metadata", "message": "metadata must be valid JSON"}]},
        ) from exc
    if not isinstance(payload, dict):
        raise HTTPException(
            status_code=422,
            detail={"errors": [{"field": "metadata", "message": "metadata must be a JSON object"}]},
        )
    return payload


def _guess_doc_type(mime_type: Optional[str]) -> DocType:
    if not mime_type:
        return DocType.OTHER
    if mime_type.startswith("text/"):
        return DocType.TEXT
    elif mime_type == "application/pdf":
        return DocType.PDF
    elif mime_type in (
        "application/msword",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ):
        return DocType.DOC
    elif mime_type.startswith("image/"):
        return DocType.IMAGE
    return DocType.OTHER


async def _extract_text(
    file: UploadFile, storage_path: Path, doc_type: DocType
) -> Optional[str]:
    if doc_type == DocType.PDF:
        suffix = storage_path.suffix or ".pdf"
        temp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
                temp_path = Path(tmp.name)
                await file.seek(0)
                shutil.copyfileobj(file.file, tmp)
            await file.seek(0)

            try:
                from pypdf import PdfReader
            except Exception:
                return None

            reader = PdfReader(str(temp_path))
            parts: list[str] = []
            for page in reader.pages:
                try:
                    text = page.extract_text() or ""
                except Exception:
                    text = ""
                if text.strip():
                    parts.append(text.strip())
            extracted = "\n\n".join(parts).strip()
            if extracted:
                return extracted

            try:
                pdftotext = shutil.which("pdftotext")
                if pdftotext:
                    result = subprocess.run(
                        [pdftotext, str(temp_path), "-"],
                        check=False,
                        capture_output=True,
                        text=True,
                    )
                    fallback_text = (result.stdout or "").strip()
                    if fallback_text:
                        return fallback_text
            except Exception:
                pass
            return None
        finally:
            if temp_path and temp_path.exists():
                temp_path.unlink(missing_ok=True)

    return None
