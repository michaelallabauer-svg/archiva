"""FastAPI routes for Archiva ECM."""

from datetime import datetime
from pathlib import Path
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from archiva.config import Settings, load_settings
from archiva.database import get_db
from archiva.models import Document, DocumentType, DocumentVersion
from archiva.search import build_search_query, update_document_vector
from archiva.storage import StorageManager

router = APIRouter(prefix="/api/v1", tags=["documents"])

# Global storage manager (initialized on startup)
_storage: Optional[StorageManager] = None


def get_storage() -> StorageManager:
    """Get the storage manager instance."""
    if _storage is None:
        raise HTTPException(status_code=500, detail="Storage not initialized")
    return _storage


def init_router(storage: StorageManager) -> None:
    """Initialize router with storage manager."""
    global _storage
    _storage = storage


# --- Pydantic Schemas ---

class DocumentBase(BaseModel):
    """Base document schema."""

    name: str
    title: Optional[str] = None
    author: Optional[str] = None
    description: Optional[str] = None
    tags: Optional[str] = None


class DocumentCreate(DocumentBase):
    """Schema for creating a document."""

    doc_type: DocumentType = DocumentType.OTHER


class DocumentResponse(DocumentBase):
    """Schema for document response."""

    id: UUID
    doc_type: DocumentType
    mime_type: Optional[str]
    size_bytes: int
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class DocumentListResponse(BaseModel):
    """Schema for paginated document list."""

    items: list[DocumentResponse]
    total: int
    page: int
    page_size: int


class SearchResult(BaseModel):
    """Schema for a single search result."""

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
    """Schema for search response."""

    query: str
    results: list[SearchResult]
    total: int


class MessageResponse(BaseModel):
    """Simple message response."""

    message: str


# --- Routes ---

@router.get("/health")
async def health_check() -> dict:
    """Health check endpoint."""
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
    """
    Upload a new document.

    Stores the file, extracts text for indexing, and creates
    a database record.
    """
    # Determine document type from MIME
    doc_type = _guess_doc_type(file.content_type)

    # Generate storage path
    storage_path = storage.generate_path(file.filename)

    # Save file
    await storage.save(file, storage_path)

    # Extract text content for indexing (if possible)
    content_text = await _extract_text(file, storage_path, doc_type)

    # Create database record
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

    # Index content for full-text search
    if content_text:
        update_document_vector(db, document.id, content_text)

    db.commit()
    db.refresh(document)

    return document


@router.get("/documents", response_model=DocumentListResponse)
async def list_documents(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    doc_type: Optional[DocumentType] = None,
    db: Session = Depends(get_db),
) -> DocumentListResponse:
    """
    List documents with pagination.

    Supports filtering by document type.
    """
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
async def get_document(
    document_id: UUID,
    db: Session = Depends(get_db),
) -> Document:
    """
    Get a single document by ID.
    """
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
    """
    Delete a document and its file.
    """
    document = db.query(Document).where(Document.id == document_id).first()

    if not document:
        raise HTTPException(status_code=404, detail="Document not found")

    # Delete file
    storage.delete(Path(document.storage_path))

    # Delete versions
    db.query(DocumentVersion).where(DocumentVersion.document_id == document_id).delete()

    # Delete document
    db.delete(document)
    db.commit()

    return MessageResponse(message="Document deleted successfully")


@router.get("/search", response_model=SearchResponse)
async def search_documents(
    q: str = Query(..., min_length=1),
    doc_type: Optional[DocumentType] = None,
    limit: int = Query(100, ge=1, le=100),
    db: Session = Depends(get_db),
    settings: Settings = Depends(lambda: load_settings()),
) -> SearchResponse:
    """
    Full-text search across documents.

    Uses PostgreSQL tsvector/tsquery for relevance-ranked results
    with highlighted snippets.
    """
    results = build_search_query(
        db,
        q,
        doc_type=doc_type.value if doc_type else None,
        limit=limit,
        highlight_fragment_size=settings.search.highlight_fragment_size,
    )

    return SearchResponse(
        query=q,
        results=[SearchResult(**r) for r in results],
        total=len(results),
    )


# --- Helpers ---

def _guess_doc_type(mime_type: Optional[str]) -> DocumentType:
    """Guess document type from MIME type."""
    if not mime_type:
        return DocumentType.OTHER

    if mime_type.startswith("text/"):
        return DocumentType.TEXT
    elif mime_type == "application/pdf":
        return DocumentType.PDF
    elif mime_type in ("application/msword", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"):
        return DocumentType.DOC
    elif mime_type.startswith("image/"):
        return DocumentType.IMAGE
    return DocumentType.OTHER


async def _extract_text(file: UploadFile, storage_path: Path, doc_type: DocumentType) -> Optional[str]:
    """
    Extract text content for indexing.

    Currently returns None - actual implementation would use
    pdfminer, python-docx, etc. for text extraction.
    """
    # TODO: Implement text extraction based on doc_type
    # - PDF: pdfminer.six
    # - DOCX: python-docx
    # - Images: OCR (pytesseract) - future
    return None
