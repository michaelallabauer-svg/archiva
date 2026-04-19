"""High-level search service for Archiva."""

from sqlalchemy.orm import Session

from archiva.indexer.opensearch_client import OpenSearchClient
from archiva.search.query_builder import build_search_response


class SearchService:
    def __init__(self, db: Session):
        self.db = db
        self.client = OpenSearchClient()

    def search(
        self,
        *,
        q: str,
        document_type_id: str | None,
        cabinet_type_id: str | None,
        cabinet_id: str | None,
        page: int,
        page_size: int,
    ) -> dict:
        result = self.client.search(
            q=q,
            page=page,
            page_size=page_size,
            filters={
                "document_type_id": document_type_id,
                "cabinet_type_id": cabinet_type_id,
                "cabinet_id": cabinet_id,
            },
        )
        if result.get("ok"):
            payload = result["data"]
            hits = payload.get("hits", {}).get("hits", [])
            return {
                "hits": [
                    {
                        "document_id": hit.get("_source", {}).get("document_id"),
                        "title": hit.get("_source", {}).get("title"),
                        "document_type": hit.get("_source", {}).get("document_type"),
                        "cabinet_id": hit.get("_source", {}).get("cabinet_id"),
                        "score": hit.get("_score", 0),
                        "highlights": hit.get("highlight", {}),
                    }
                    for hit in hits
                ],
                "facets": {},
                "total": payload.get("hits", {}).get("total", {}).get("value", 0),
                "page": page,
                "page_size": page_size,
            }
        return build_search_response(
            db=self.db,
            q=q,
            document_type_id=document_type_id,
            cabinet_type_id=cabinet_type_id,
            cabinet_id=cabinet_id,
            page=page,
            page_size=page_size,
        )
