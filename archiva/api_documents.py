"""Document management routes for Archiva ECM."""

from datetime import datetime
from pathlib import Path
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from pydantic import BaseModel
from sqlalchemy.orm import Session

from archiva.config import Settings, load_settings
from archiva.database import get_db
from archiva.models import DocType, Document, DocumentVersion
from archiva.search import build_search_query, update_document_vector
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


class DocumentResponse(DocumentBase):
    id: UUID
    doc_type: str
    mime_type: Optional[str]
    size_bytes: int
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
    title: Optional[str] = None,
    author: Optional[str] = None,
    description: Optional[str] = None,
    tags: Optional[str] = None,
    db: Session = Depends(get_db),
    storage: StorageManager = Depends(get_storage),
) -> Document:
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
    )

    db.add(document)
    db.flush()

    if content_text:
        update_document_vector(db, document.id, content_text)

    db.commit()
    db.refresh(document)
    return document


@router.get("/documents", response_model=DocumentListResponse)
async def list_documents(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    doc_type: Optional[str] = None,
    db: Session = Depends(get_db),
) -> DocumentListResponse:
    query = db.query(Document)

    if doc_type:
        query = query.where(Document.doc_type == doc_type)

    total = query.count()
    offset = (page - 1) * page_size

    documents = query.order_by(Document.created_at.desc()).offset(offset).limit(page_size).all()

    return DocumentListResponse(
        items=[DocumentResponse.model_validate(d) for d in documents],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/documents/{document_id}", response_model=DocumentResponse)
async def get_document(document_id: UUID, db: Session = Depends(get_db)) -> Document:
    document = db.query(Document).where(Document.id == document_id).first()
    if not document:
        raise HTTPException(status_code=404, detail="Document not found")
    return document


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
    # TODO: Implement text extraction
    # - PDF: pdfminer.six
    # - DOCX: python-docx
    # - Images: OCR (pytesseract)
    return None
