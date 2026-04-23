"""Standalone index worker for Archiva."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from pathlib import Path

from fastapi import UploadFile

from archiva.api_documents import _extract_text
from archiva.config import load_settings
from archiva.database import create_tables, get_session, init_db
from archiva.indexer.opensearch_client import OpenSearchClient
from archiva.models import DocType, Document, IndexJob
from archiva.search_legacy import update_document_vector
from archiva.storage import StorageManager

logger = logging.getLogger("archiva.index_worker")


def _search_document_payload(document: Document, fulltext: str) -> dict:
    cabinet = document.cabinet
    cabinet_type = cabinet.cabinet_type if cabinet else None
    metadata = {}
    try:
        import json
        metadata = json.loads(document.metadata_json) if document.metadata_json else {}
    except Exception:
        metadata = {}
    return {
        "document_id": str(document.id),
        "title": document.title or document.name,
        "filename": document.name,
        "document_type": document.document_type.name if document.document_type else None,
        "document_type_id": str(document.document_type_id) if document.document_type_id else None,
        "cabinet_type": cabinet_type.name if cabinet_type else None,
        "cabinet_type_id": str(cabinet_type.id) if cabinet_type else None,
        "cabinet": cabinet.name if cabinet else None,
        "cabinet_id": str(cabinet.id) if cabinet else None,
        "register": None,
        "register_id": None,
        "metadata": metadata,
        "metadata_labels": metadata,
        "fulltext": fulltext,
        "created_at": document.created_at.isoformat() if document.created_at else None,
        "updated_at": document.updated_at.isoformat() if document.updated_at else None,
    }


def process_pending_index_jobs(storage: StorageManager, client: OpenSearchClient, worker_id: str = "index-worker") -> int:
    processed = 0
    with get_session() as db:
        jobs = (
            db.query(IndexJob)
            .where(IndexJob.status == "pending")
            .order_by(IndexJob.created_at.asc())
            .limit(10)
            .all()
        )

        for job in jobs:
            document = db.query(Document).where(Document.id == job.document_id).first()
            if not document:
                job.status = "failed"
                job.error_message = "Document not found"
                job.finished_at = datetime.utcnow()
                processed += 1
                continue

            try:
                job.status = "processing"
                job.started_at = datetime.utcnow()
                job.worker_id = worker_id
                job.attempts = int(job.attempts or 0) + 1
                db.add(job)
                db.flush()

                full_path = storage.full_path(Path(document.storage_path))
                if not full_path.exists():
                    raise FileNotFoundError("Stored file not found")

                doc_type = document.doc_type if isinstance(document.doc_type, DocType) else DocType(str(document.doc_type))
                with full_path.open("rb") as source_file:
                    upload = UploadFile(filename=document.name, file=source_file)
                    upload.headers = None
                    fulltext = asyncio.run(_extract_text(upload, Path(document.storage_path), doc_type)) or ""

                if fulltext:
                    update_document_vector(db, document.id, fulltext)
                document.extracted_text_preview = (fulltext or "")[:4000] or None
                document.extracted_text_length = len(fulltext or "")

                result = client.index_document(_search_document_payload(document, fulltext))
                if not result.get("ok"):
                    raise RuntimeError(result.get("body") or "OpenSearch indexing failed")

                document.index_status = "completed"
                document.indexed_at = datetime.utcnow()
                document.index_error = None if fulltext else "Kein extrahierbarer PDF-Text gefunden"
                document.index_engine = "opensearch"
                db.add(document)

                job.status = "completed"
                job.finished_at = datetime.utcnow()
                job.error_message = None
                db.add(job)
                processed += 1
            except Exception as exc:
                document.index_status = "failed"
                document.index_error = str(exc)
                document.extracted_text_preview = None
                document.extracted_text_length = None
                db.add(document)
                job.status = "failed"
                job.error_message = str(exc)
                job.finished_at = datetime.utcnow()
                db.add(job)
                processed += 1
    return processed


def run_worker(poll_interval_seconds: float = 2.0) -> None:
    settings = load_settings()
    init_db(settings)
    create_tables()
    storage = StorageManager(settings.storage.base_path)
    client = OpenSearchClient()

    logger.info("Index worker started, poll_interval_seconds=%s", poll_interval_seconds)
    while True:
        try:
            processed = process_pending_index_jobs(storage, client)
            if processed:
                logger.info("Processed %s index job(s)", processed)
            else:
                time.sleep(poll_interval_seconds)
        except KeyboardInterrupt:
            logger.info("Index worker stopped")
            raise
        except Exception:
            logger.exception("Index worker loop failed")
            time.sleep(max(poll_interval_seconds, 5.0))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    run_worker()
