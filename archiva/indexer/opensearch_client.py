"""Minimal OpenSearch client abstraction for Archiva."""

from dataclasses import dataclass
import json
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from archiva.config import load_settings


@dataclass
class OpenSearchConfig:
    url: str = "http://localhost:9200"
    index_name: str = "archiva-documents-v1"


class OpenSearchClient:
    def __init__(self, config: OpenSearchConfig | None = None):
        if config is None:
            settings = load_settings()
            config = OpenSearchConfig(
                url=settings.search.opensearch_url,
                index_name=settings.search.index_name,
            )
        self.config = config

    def ensure_index(self) -> dict:
        mapping = {
            "mappings": {
                "properties": {
                    "document_id": {"type": "keyword"},
                    "title": {"type": "text"},
                    "filename": {"type": "text"},
                    "document_type": {"type": "keyword"},
                    "document_type_id": {"type": "keyword"},
                    "cabinet_type": {"type": "keyword"},
                    "cabinet_type_id": {"type": "keyword"},
                    "cabinet": {"type": "keyword"},
                    "cabinet_id": {"type": "keyword"},
                    "register": {"type": "keyword"},
                    "register_id": {"type": "keyword"},
                    "metadata": {"type": "object", "enabled": True},
                    "metadata_labels": {"type": "object", "enabled": True},
                    "fulltext": {"type": "text"},
                    "created_at": {"type": "date"},
                    "updated_at": {"type": "date"},
                }
            }
        }
        try:
            response = _http_json("PUT", f"{self.config.url}/{self.config.index_name}", mapping)
            return {"ok": True, "index_name": self.config.index_name, "created": response.get("acknowledged", True)}
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="ignore")
            if exc.code == 400 and "resource_already_exists_exception" in body:
                return {"ok": True, "index_name": self.config.index_name, "created": False}
            return {"ok": False, "status_code": exc.code, "body": body}
        except URLError as exc:
            return {"ok": False, "status_code": None, "body": str(exc)}

    def index_document(self, search_document: dict) -> dict:
        self.ensure_index()
        document_id = search_document.get("document_id")
        try:
            _http_json("PUT", f"{self.config.url}/{self.config.index_name}/_doc/{document_id}", search_document)
            return {
                "ok": True,
                "index_name": self.config.index_name,
                "document_id": document_id,
            }
        except HTTPError as exc:
            return {
                "ok": False,
                "index_name": self.config.index_name,
                "document_id": document_id,
                "status_code": exc.code,
                "body": exc.read().decode("utf-8", errors="ignore"),
            }
        except URLError as exc:
            return {
                "ok": False,
                "index_name": self.config.index_name,
                "document_id": document_id,
                "status_code": None,
                "body": str(exc),
            }

    def search(self, *, q: str, page: int, page_size: int, filters: dict | None = None) -> dict:
        self.ensure_index()
        filters = filters or {}
        must = []
        if q.strip():
            must.append(
                {
                    "multi_match": {
                        "query": q,
                        "fields": ["title^4", "filename^2", "fulltext", "metadata.*"],
                    }
                }
            )
        else:
            must.append({"match_all": {}})

        filter_clauses = []
        for field_name, value in filters.items():
            if value:
                filter_clauses.append({"term": {field_name: value}})

        query_text = (q or "").strip()
        body = {
            "from": max(0, (page - 1) * page_size),
            "size": page_size,
            "query": {
                "bool": {
                    "must": must,
                    "filter": filter_clauses,
                    "should": [
                        {"match": {"fulltext": {"query": query_text, "operator": "and"}}},
                        {"match_phrase": {"fulltext": {"query": query_text}}},
                        {"match": {"title": {"query": query_text, "operator": "and", "boost": 4}}},
                        {"simple_query_string": {"query": query_text, "fields": ["fulltext^3", "title^5", "filename^2", "metadata.*"], "default_operator": "and"}},
                    ] if query_text else [],
                    "minimum_should_match": 1 if query_text else 0,
                }
            },
            "highlight": {
                "fields": {
                    "fulltext": {},
                    "title": {},
                    "metadata.*": {},
                }
            },
        }
        try:
            response = _http_json("POST", f"{self.config.url}/{self.config.index_name}/_search", body)
            return {"ok": True, "data": response}
        except HTTPError as exc:
            return {"ok": False, "status_code": exc.code, "body": exc.read().decode("utf-8", errors="ignore")}
        except URLError as exc:
            return {"ok": False, "status_code": None, "body": str(exc)}


def _http_json(method: str, url: str, payload: dict) -> dict:
    data = json.dumps(payload).encode("utf-8")
    request = Request(url, data=data, method=method)
    request.add_header("Content-Type", "application/json")
    with urlopen(request, timeout=5) as response:
        raw = response.read().decode("utf-8", errors="ignore")
        return json.loads(raw) if raw else {}
