"""Mapping from Archiva models to search documents."""

from archiva.models import Document
from archiva.ui import metadata_from_json


def build_search_document(document: Document, *, fulltext: str = "") -> dict:
    metadata = metadata_from_json(document.metadata_json) or {}
    metadata_labels = {
        field.name: (field.label or field.name)
        for field in (document.document_type.fields if document.document_type else [])
    }
    cabinet = document.cabinet
    cabinet_type = cabinet.cabinet_type if cabinet and cabinet.cabinet_type else None

    return {
        "document_id": str(document.id),
        "title": document.title or document.name,
        "filename": document.name,
        "document_type": document.document_type.name if document.document_type else None,
        "document_type_id": str(document.document_type_id) if document.document_type_id else None,
        "cabinet_type": cabinet_type.name if cabinet_type else None,
        "cabinet_type_id": str(cabinet_type.id) if cabinet_type else None,
        "cabinet": cabinet.name if cabinet else None,
        "cabinet_id": str(document.cabinet_id) if document.cabinet_id else None,
        "register": document.document_type.register.name if document.document_type and document.document_type.register else None,
        "register_id": str(document.document_type.register_id) if document.document_type and document.document_type.register_id else None,
        "metadata": metadata,
        "metadata_labels": metadata_labels,
        "fulltext": fulltext,
        "created_at": document.created_at.isoformat() if document.created_at else None,
        "updated_at": document.updated_at.isoformat() if document.updated_at else None,
    }
