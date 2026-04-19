"""Validation helpers for document metadata driven by MetadataField definitions."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy.orm import Session

from archiva.models import Document, DocumentType, MetadataField


@dataclass
class MetadataValidationResult:
    normalized: dict[str, Any]
    fields: list[MetadataField]
    document_type: DocumentType


class MetadataValidationError(HTTPException):
    def __init__(self, errors: list[dict[str, str]]) -> None:
        super().__init__(status_code=422, detail={"errors": errors})


class MetadataNotFoundError(HTTPException):
    def __init__(self, document_type_id: UUID) -> None:
        super().__init__(status_code=404, detail=f"Document type {document_type_id} not found")


BOOLEAN_TRUE = {True, 1, "1", "true", "yes", "on"}
BOOLEAN_FALSE = {False, 0, "0", "false", "no", "off"}


def validate_document_metadata(
    db: Session,
    document_type_id: UUID,
    metadata: dict[str, Any] | None,
    *,
    current_document_id: UUID | None = None,
) -> MetadataValidationResult:
    document_type = db.query(DocumentType).where(DocumentType.id == document_type_id).first()
    if not document_type:
        raise MetadataNotFoundError(document_type_id)

    fields = sorted(document_type.fields, key=lambda field: field.order)
    metadata = metadata or {}
    errors: list[dict[str, str]] = []
    normalized: dict[str, Any] = {}

    allowed_field_names = {field.name for field in fields}
    for key in metadata:
        if key not in allowed_field_names:
            errors.append({"field": key, "message": "Unknown metadata field for document type"})

    for field in fields:
        raw_value = metadata.get(field.name)
        if _is_empty(raw_value):
            if field.is_required:
                errors.append({"field": field.name, "message": "Field is required"})
                continue
            normalized[field.name] = None
            continue

        try:
            normalized_value = _normalize_value(field, raw_value)
        except ValueError as exc:
            errors.append({"field": field.name, "message": str(exc)})
            continue

        if field.is_unique and _has_duplicate_value(
            db, field.name, normalized_value, document_type_id, current_document_id
        ):
            errors.append({"field": field.name, "message": "Value must be unique"})
            continue

        normalized[field.name] = normalized_value

    if errors:
        raise MetadataValidationError(errors)

    return MetadataValidationResult(
        normalized=normalized,
        fields=fields,
        document_type=document_type,
    )


def metadata_to_json(metadata: dict[str, Any]) -> str:
    return json.dumps(metadata, ensure_ascii=False)


def metadata_from_json(raw: str | None) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _normalize_value(field: MetadataField, raw_value: Any) -> Any:
    field_type = field.field_type

    if field_type in {"text", "long_text", "url", "email", "phone"}:
        value = str(raw_value).strip()
        _validate_string_constraints(field, value)
        return value

    if field_type in {"number", "currency"}:
        return _normalize_number(field, raw_value)

    if field_type == "boolean":
        return _normalize_boolean(raw_value)

    if field_type == "date":
        return _normalize_date(raw_value)

    if field_type == "datetime":
        return _normalize_datetime(raw_value)

    if field_type == "selection":
        value = str(raw_value).strip()
        _validate_options(field, [value])
        return value

    if field_type == "multi_selection":
        values = _normalize_multi_selection(raw_value)
        _validate_options(field, values)
        return values

    value = str(raw_value).strip()
    _validate_string_constraints(field, value)
    return value


def _normalize_number(field: MetadataField, raw_value: Any) -> float:
    try:
        decimal_value = Decimal(str(raw_value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("Value must be a number") from exc

    value = float(decimal_value)
    if field.min_value is not None and value < field.min_value:
        raise ValueError(f"Value must be >= {field.min_value}")
    if field.max_value is not None and value > field.max_value:
        raise ValueError(f"Value must be <= {field.max_value}")
    return value


def _normalize_boolean(raw_value: Any) -> bool:
    normalized = raw_value
    if isinstance(raw_value, str):
        normalized = raw_value.strip().lower()
    if normalized in BOOLEAN_TRUE:
        return True
    if normalized in BOOLEAN_FALSE:
        return False
    raise ValueError("Value must be a boolean")


def _normalize_date(raw_value: Any) -> str:
    if isinstance(raw_value, date) and not isinstance(raw_value, datetime):
        return raw_value.isoformat()
    try:
        return date.fromisoformat(str(raw_value)).isoformat()
    except ValueError as exc:
        raise ValueError("Value must be an ISO date (YYYY-MM-DD)") from exc


def _normalize_datetime(raw_value: Any) -> str:
    if isinstance(raw_value, datetime):
        return raw_value.isoformat()
    try:
        return datetime.fromisoformat(str(raw_value)).isoformat()
    except ValueError as exc:
        raise ValueError("Value must be an ISO datetime") from exc


def _normalize_multi_selection(raw_value: Any) -> list[str]:
    if isinstance(raw_value, list):
        values = raw_value
    else:
        values = [part.strip() for part in str(raw_value).split(",") if part.strip()]
    if not values:
        raise ValueError("Select at least one option")
    return [str(value).strip() for value in values if str(value).strip()]


def _validate_string_constraints(field: MetadataField, value: str) -> None:
    if field.min_length is not None and len(value) < field.min_length:
        raise ValueError(f"Value must be at least {field.min_length} characters long")
    if field.max_length is not None and len(value) > field.max_length:
        raise ValueError(f"Value must be at most {field.max_length} characters long")
    if field.pattern and not re.fullmatch(field.pattern, value):
        raise ValueError("Value does not match the required pattern")


def _validate_options(field: MetadataField, values: list[str]) -> None:
    options = _parse_options(field)
    if not options:
        return
    invalid = [value for value in values if value not in options]
    if invalid:
        raise ValueError(f"Invalid option(s): {', '.join(invalid)}")


def _parse_options(field: MetadataField) -> list[str]:
    if not field.options:
        return []
    try:
        raw = json.loads(field.options)
    except json.JSONDecodeError:
        return []
    return [str(option) for option in raw]


def _is_empty(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _has_duplicate_value(
    db: Session,
    field_name: str,
    value: Any,
    document_type_id: UUID,
    current_document_id: UUID | None,
) -> bool:
    query = db.query(Document).where(Document.document_type_id == document_type_id)
    if current_document_id is not None:
        query = query.where(Document.id != current_document_id)

    serialized = _canonicalize_unique_value(value)
    for document in query.all():
        metadata = metadata_from_json(document.metadata_json)
        if not metadata:
            continue
        if field_name not in metadata:
            continue
        if _canonicalize_unique_value(metadata[field_name]) == serialized:
            return True
    return False


def _canonicalize_unique_value(value: Any) -> str:
    if isinstance(value, list):
        return json.dumps(sorted(str(item) for item in value), ensure_ascii=False)
    return json.dumps(value, ensure_ascii=False, sort_keys=True)
