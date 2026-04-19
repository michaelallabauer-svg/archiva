"""Index job dispatching for Archiva."""

import json

from sqlalchemy.orm import Session

from archiva.models import Document, IndexJob


def enqueue_document_index(db: Session, *, document: Document, reason: str) -> IndexJob:
    document.index_status = "pending"
    document.index_revision = (document.index_revision or 0) + 1

    job = IndexJob(
        document_id=document.id,
        job_type="index_document",
        status="pending",
        payload_json=json.dumps(
            {
                "reason": reason,
                "index_revision": document.index_revision,
            },
            ensure_ascii=False,
        ),
    )
    db.add(document)
    db.add(job)
    db.commit()
    db.refresh(job)
    return job
