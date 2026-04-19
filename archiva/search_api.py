"""Search API endpoints for Archiva."""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from archiva.database import get_db
from archiva.search.service import SearchService

router = APIRouter(prefix="/api/search", tags=["search"])


@router.get("")
def search_documents(
    q: str = Query("", description="Freitextsuche"),
    document_type_id: str | None = Query(None),
    cabinet_type_id: str | None = Query(None),
    cabinet_id: str | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
) -> dict:
    service = SearchService(db)
    return service.search(
        q=q,
        document_type_id=document_type_id,
        cabinet_type_id=cabinet_type_id,
        cabinet_id=cabinet_id,
        page=page,
        page_size=page_size,
    )
