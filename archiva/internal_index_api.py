"""Internal indexing endpoints for Archiva."""

from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from archiva.database import get_db
from archiva.indexer.dispatcher import enqueue_document_index
from archiva.indexer.status import indexing_runtime_status
from archiva.models import Document

router = APIRouter(prefix="/api/internal/index", tags=["index-internal"])


@router.get("/status")
def index_runtime_status() -> dict:
    return indexing_runtime_status()


@router.post("/documents/{document_id}/reindex")
def reindex_document(document_id: UUID, db: Session = Depends(get_db)) -> dict:
    document = db.query(Document).where(Document.id == document_id).first()
    if not document:
        return {"ok": False, "error": "document_not_found"}
    job = enqueue_document_index(db, document=document, reason="manual_reindex")
    return {"ok": True, "job_id": str(job.id), "document_id": str(document.id)}
