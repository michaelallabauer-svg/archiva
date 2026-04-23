"""Temporary search query builder.

Startpunkt: aktuell noch Postgres-basierter Fallback.
Später wird diese Schicht auf OpenSearch umgestellt.
"""

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from archiva.metadata_validation import metadata_from_json
from archiva.models import Document


def build_search_response(
    *,
    db: Session,
    q: str,
    document_type_id: str | None,
    cabinet_type_id: str | None,
    cabinet_id: str | None,
    page: int,
    page_size: int,
) -> dict:
    documents = db.query(Document).all()
    normalized_q = (q or "").strip().lower()

    hits: list[dict] = []
    for document in documents:
        if document_type_id and str(document.document_type_id or "") != document_type_id:
            continue
        if cabinet_id and str(document.cabinet_id or "") != cabinet_id:
            continue
        if cabinet_type_id:
            resolved_cabinet = getattr(document, "cabinet", None)
            resolved_cabinet_type_id = str(resolved_cabinet.cabinet_type_id) if resolved_cabinet and resolved_cabinet.cabinet_type_id else ""
            if resolved_cabinet_type_id != cabinet_type_id:
                continue

        metadata = metadata_from_json(document.metadata_json) or {}
        fallback_haystack = " ".join(
            [
                document.title or "",
                document.name or "",
                document.document_type.name if document.document_type else "",
                str(metadata),
            ]
        ).lower()
        if normalized_q:
            content_match = False
            try:
                content_match = bool(
                    db.execute(
                        select(Document.id).where(
                            Document.id == document.id,
                            text("content_vector @@ plainto_tsquery('english', :q)"),
                        ),
                        {"q": q},
                    ).first()
                )
            except Exception:
                content_match = False
            if normalized_q not in fallback_haystack and not content_match:
                continue

        hits.append(
            {
                "document_id": str(document.id),
                "title": document.title or document.name,
                "document_type": document.document_type.name if document.document_type else None,
                "cabinet_id": str(document.cabinet_id) if document.cabinet_id else None,
                "score": 1.0,
                "highlights": {},
            }
        )

    start = (page - 1) * page_size
    end = start + page_size
    return {
        "hits": hits[start:end],
        "facets": {},
        "total": len(hits),
        "page": page,
        "page_size": page_size,
    }
