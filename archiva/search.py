"""Full-text search using PostgreSQL tsvector/tsquery."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from archiva.models import Document


def update_document_vector(session: Session, document_id: UUID, content: str) -> None:
    """
    Update the tsvector content for a document.

    Uses PostgreSQL's to_tsvector with english configuration for
    stemming and relevance ranking.
    """
    session.execute(
        update(Document)
        .where(Document.id == document_id)
        .values(
            content_vector=func.to_tsvector("english", content),
            indexed_at=datetime.utcnow(),
        )
    )


def build_search_query(
    session: Session,
    query: str,
    doc_type: str | None = None,
    limit: int = 100,
    highlight_fragment_size: int = 150,
) -> list[dict]:
    """
    Execute a full-text search with relevance ranking and highlighting.

    Returns documents matching the query, ordered by relevance,
    with highlighted snippets.
    """
    # Build the search vector
    search_vector = func.to_tsvector("english", query)

    # Build the query with ranking
    stmt = (
        select(
            Document.id,
            Document.name,
            Document.title,
            Document.doc_type,
            Document.author,
            Document.description,
            Document.tags,
            Document.created_at,
            func.ts_rank(Document.content_vector, search_vector).label("rank"),
            func.ts_headline(
                "english",
                func.coalesce(Document.description, ""),
                search_vector,
                f"MaxWords={highlight_fragment_size}, MinWords=20",
            ).label("snippet"),
        )
        .where(Document.content_vector.op("@@")(func.to_tsquery("english", query)))
        .order_by(func.ts_rank(Document.content_vector, search_vector).desc())
        .limit(limit)
    )

    # Filter by document type if specified
    if doc_type:
        stmt = stmt.where(Document.doc_type == doc_type)

    result = session.execute(stmt)
    rows = result.all()

    return [
        {
            "id": str(row.id),
            "name": row.name,
            "title": row.title,
            "doc_type": row.doc_type,
            "author": row.author,
            "description": row.description,
            "tags": row.tags,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "rank": float(row.rank),
            "snippet": row.snippet,
        }
        for row in rows
    ]


def build_auto_complete_query(session: Session, prefix: str, limit: int = 10) -> list[str]:
    """
    Build prefix-based autocomplete suggestions.

    Uses word_suggestion approach for prefix matching.
    """
    # Get distinct words from indexed documents that start with prefix
    stmt = select(Document.tags).where(Document.tags.ilike(f"%{prefix}%")).limit(limit)

    result = session.execute(stmt)
    tags = [row.tags for row in result.all() if row.tags]

    # Extract individual words and dedupe
    words = set()
    for tag_string in tags:
        for word in tag_string.split(","):
            word = word.strip().lower()
            if word.startswith(prefix.lower()):
                words.add(word)

    return sorted(list(words))[:limit]
