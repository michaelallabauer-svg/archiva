"""Minimal index worker startpoint for Archiva."""

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from archiva.database import get_session
from archiva.indexer.extractor import extract_text_for_indexing
from archiva.indexer.opensearch_client import OpenSearchClient
from archiva.models import Document, IndexJob
from archiva.search.mapping import build_search_document


class IndexWorker:
    def __init__(self, worker_id: str = "archiva-indexer-1"):
        self.worker_id = worker_id
        self.client = OpenSearchClient()

    def run_once(self) -> dict:
        with get_session() as db:
            job = self._claim_next_job(db)
            if not job:
                return {"ok": True, "status": "idle"}
            return self._process_job(db, job)

    def _claim_next_job(self, db: Session) -> IndexJob | None:
        statement = (
            select(IndexJob)
            .where(IndexJob.status == "pending")
            .order_by(IndexJob.priority.asc(), IndexJob.created_at.asc())
            .with_for_update(skip_locked=True)
        )
        job = db.execute(statement).scalars().first()
        if not job:
            return None
        job.status = "running"
        job.worker_id = self.worker_id
        job.started_at = datetime.utcnow()
        job.attempts += 1
        db.add(job)
        db.flush()
        return job

    def _process_job(self, db: Session, job: IndexJob) -> dict:
        document = db.query(Document).where(Document.id == job.document_id).first()
        if not document:
            job.status = "error"
            job.error_message = "document_not_found"
            job.finished_at = datetime.utcnow()
            db.add(job)
            db.flush()
            return {"ok": False, "error": "document_not_found"}

        extracted_text, used_ocr, engine = extract_text_for_indexing(document.storage_path, document.mime_type)
        search_document = build_search_document(document, fulltext=extracted_text)
        result = self.client.index_document(search_document)
        if not result.get("ok"):
            job.status = "error"
            job.error_message = result.get("body") or "opensearch_index_failed"
            job.finished_at = datetime.utcnow()
            document.index_status = "error"
            document.index_error = job.error_message
            db.add(document)
            db.add(job)
            db.flush()
            return {"ok": False, "error": job.error_message}

        document.index_status = "done"
        document.index_error = None
        document.indexed_at = datetime.utcnow()
        document.index_engine = engine
        document.index_ocr_used = used_ocr
        job.status = "done"
        job.finished_at = datetime.utcnow()
        db.add(document)
        db.add(job)
        db.flush()
        return {"ok": True, "job_id": str(job.id), "document_id": str(document.id)}
