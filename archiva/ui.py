"""Server-rendered UI split into Admin and App surfaces for Archiva."""

from __future__ import annotations

import json
from html import escape
from pathlib import Path
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from urllib.parse import quote_plus
from sqlalchemy import text
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from archiva.database import get_db
from archiva.config import load_settings
from archiva.metadata_validation import metadata_from_json, validate_document_metadata, MetadataValidationError
from archiva.models import AssignmentTarget, Cabinet, CabinetType, DocType, Document, DocumentType, IndexJob, MetadataField, PreviewJob, Register, RegisterType, Role, Team, TeamMembership, User, UserRoleAssignment, WorkflowDefinition, WorkflowStepDefinition, WorkflowTransitionDefinition
from archiva.preview_queue import enqueue_preview_job, get_latest_preview_artifact, get_latest_preview_job
from archiva.indexer.dispatcher import enqueue_document_index
from archiva.indexer.status import indexing_runtime_status
from archiva.search.service import SearchService
from archiva.search_legacy import update_document_vector
from archiva.storage import StorageManager

router = APIRouter(tags=["ui"])


def _admin_identity_redirect(*, identity_tab: str = "users", selected_user_id: str | None = None, selected_role_id: str | None = None, message: str | None = None) -> RedirectResponse:
    parts = [f"identity_tab={quote_plus(identity_tab)}"]
    if selected_user_id:
        parts.append(f"selected_user_id={quote_plus(selected_user_id)}")
    if selected_role_id:
        parts.append(f"selected_role_id={quote_plus(selected_role_id)}")
    if message:
        parts.append(f"message={quote_plus(message)}")
    return RedirectResponse(url=f"/ui/admin/identity?{'&'.join(parts)}#identity-admin", status_code=303)


def _normalized_label(value: str | None) -> str:
    return " ".join((value or "").strip().lower().split())


def _is_bridge_like_cabinet(cab: Cabinet | None) -> bool:
    if not cab or not cab.cabinet_type:
        return False
    name = _normalized_label(cab.name)
    type_name = _normalized_label(cab.cabinet_type.name)
    return bool(name) and name == type_name


def _is_real_target_cabinet(cab: Cabinet) -> bool:
    if not cab.cabinet_type:
        return False
    name = _normalized_label(cab.name)
    if not name:
        return False
    if _is_bridge_like_cabinet(cab):
        return False
    if _normalized_label(cab.cabinet_type.name) == "bestand":
        return False
    return True


def _sorted_real_target_cabinets(items: list[Cabinet]) -> list[Cabinet]:
    unique_by_id: dict[str, Cabinet] = {}
    for cab in items:
        if _is_real_target_cabinet(cab):
            unique_by_id[str(cab.id)] = cab
    return sorted(
        unique_by_id.values(),
        key=lambda item: (
            (item.cabinet_type.name if item.cabinet_type else "").lower(),
            0 if (item.name or "").strip().isdigit() else 1,
            int((item.name or "0").strip()) if (item.name or "").strip().isdigit() else 0,
            item.order,
            (item.name or "").lower(),
        ),
    )


def _legacy_child_type_name(cab: Cabinet | None) -> str | None:
    if not cab:
        return None
    if _normalized_label(getattr(cab, "name", None)) == "bestand":
        return None
    if cab.cabinet_type and _normalized_label(cab.cabinet_type.name) == "bestand":
        return cab.name
    return None


def _build_move_resolution(document: Document, cabinets: list[Cabinet]) -> dict[str, Any]:
    current_cabinet = document.cabinet
    if current_cabinet is None and document.document_type:
        if document.document_type.cabinet:
            current_cabinet = document.document_type.cabinet
        elif document.document_type.register:
            current_cabinet = document.document_type.register.cabinet

    cabinets_by_type_name: dict[str, list[Cabinet]] = {}
    legacy_register_derived_cabinets_by_type_name: dict[str, list[dict[str, Any]]] = {}
    for cab in cabinets:
        if not cab or not cab.cabinet_type:
            continue
        type_key = _normalized_label(cab.cabinet_type.name)
        if type_key:
            cabinets_by_type_name.setdefault(type_key, []).append(cab)
        if type_key == "bestand":
            legacy_type_key = _normalized_label(cab.name)
            if legacy_type_key:
                for register in (cab.registers or []):
                    legacy_register_derived_cabinets_by_type_name.setdefault(legacy_type_key, []).append({
                        "id": register.id,
                        "name": register.name,
                        "order": register.order,
                        "cabinet_type_name": cab.name,
                    })

    def candidate_cabinets_for_type_name(type_name: str | None) -> tuple[list[Cabinet], list[dict[str, Any]], str]:
        normalized_type_name = _normalized_label(type_name)
        if not normalized_type_name:
            return [], [], "none"
        direct_candidates = _sorted_real_target_cabinets(cabinets_by_type_name.get(normalized_type_name, []))
        if direct_candidates:
            return direct_candidates, [], "direct_cabinets"
        legacy_candidates_raw = legacy_register_derived_cabinets_by_type_name.get(normalized_type_name, [])
        unique_by_id: dict[str, dict[str, Any]] = {}
        for item in legacy_candidates_raw:
            item_name = _normalized_label(item.get("name"))
            type_name_inner = _normalized_label(item.get("cabinet_type_name"))
            if not item_name or item_name == type_name_inner:
                continue
            unique_by_id[str(item["id"])] = item
        legacy_candidates = sorted(
            unique_by_id.values(),
            key=lambda item: (
                0 if str(item.get("name") or "").strip().isdigit() else 1,
                int(str(item.get("name") or "0").strip()) if str(item.get("name") or "").strip().isdigit() else 0,
                int(item.get("order") or 0),
                _normalized_label(item.get("name")),
            ),
        )
        return [], legacy_candidates, "legacy_registers"

    def resolve_candidate_type_name() -> str | None:
        if current_cabinet:
            legacy_type_name = _legacy_child_type_name(current_cabinet)
            if legacy_type_name:
                return legacy_type_name
            if _is_bridge_like_cabinet(current_cabinet):
                return current_cabinet.name
            if current_cabinet.cabinet_type:
                return current_cabinet.cabinet_type.name

        if document.document_type:
            register = document.document_type.register
            direct_cabinet = document.document_type.cabinet
            if register and register.cabinet:
                legacy_type_name = _legacy_child_type_name(register.cabinet)
                if legacy_type_name:
                    return legacy_type_name
                if _is_bridge_like_cabinet(register.cabinet):
                    return register.cabinet.name
                if register.cabinet.cabinet_type:
                    return register.cabinet.cabinet_type.name
            if direct_cabinet:
                legacy_type_name = _legacy_child_type_name(direct_cabinet)
                if legacy_type_name:
                    return legacy_type_name
                if _is_bridge_like_cabinet(direct_cabinet):
                    return direct_cabinet.name
                if direct_cabinet.cabinet_type:
                    return direct_cabinet.cabinet_type.name

        if document.document_type:
            matching_doc_type_cabinets = _sorted_real_target_cabinets([
                cab for cab in cabinets
                if any(doc_type.id == document.document_type.id for doc_type in (cab.document_types or []))
            ])
            if matching_doc_type_cabinets and matching_doc_type_cabinets[0].cabinet_type:
                return matching_doc_type_cabinets[0].cabinet_type.name
        return None

    candidate_type_name = resolve_candidate_type_name()
    candidate_cabinets, legacy_candidate_cabinets, resolution_path = candidate_cabinets_for_type_name(candidate_type_name)
    if not candidate_cabinets and not legacy_candidate_cabinets and document.document_type:
        matching_doc_type_cabinets = _sorted_real_target_cabinets([
            cab for cab in cabinets
            if any(doc_type.id == document.document_type.id for doc_type in (cab.document_types or []))
        ])
        if matching_doc_type_cabinets:
            candidate_cabinets = matching_doc_type_cabinets
            resolution_path = "document_type_direct_cabinets"
            if matching_doc_type_cabinets[0].cabinet_type:
                candidate_type_name = matching_doc_type_cabinets[0].cabinet_type.name
        else:
            resolution_path = "document_type_direct_cabinets_empty"

    if not candidate_cabinets and not legacy_candidate_cabinets:
        candidate_cabinets = _sorted_real_target_cabinets(cabinets)
        if candidate_cabinets:
            resolution_path = "all_real_cabinets_fallback"

    return {
        "current_cabinet": current_cabinet,
        "candidate_type_name": candidate_type_name,
        "candidate_cabinets": candidate_cabinets,
        "legacy_candidate_cabinets": legacy_candidate_cabinets,
        "resolution_path": resolution_path,
    }


def _has_column(db: Session, table_name: str, column_name: str) -> bool:
    result = db.execute(
        text(
            """
            SELECT 1
            FROM information_schema.columns
            WHERE table_name = :table_name AND column_name = :column_name
            LIMIT 1
            """
        ),
        {"table_name": table_name, "column_name": column_name},
    ).first()
    return result is not None


def _safe_load_cabinets(db: Session) -> tuple[list[Cabinet], bool]:
    if _has_column(db, "cabinets", "cabinet_type_id"):
        return db.query(Cabinet).order_by(Cabinet.order).all(), True
    legacy_cabinets = db.execute(
        text(
            'SELECT id, name, description, "order", created_at, updated_at FROM cabinets ORDER BY "order"'
        )
    ).mappings().all()
    cabinets: list[Cabinet] = []
    for row in legacy_cabinets:
        cabinet = Cabinet.__new__(Cabinet)
        cabinet.id = row["id"]
        cabinet.cabinet_type_id = None
        cabinet.name = row["name"]
        cabinet.description = row["description"]
        cabinet.order = row["order"]
        cabinet.created_at = row["created_at"]
        cabinet.updated_at = row["updated_at"]
        cabinet.cabinet_type = None
        cabinet.registers = []
        cabinet.document_types = []
        cabinet.metadata_fields = []
        cabinets.append(cabinet)
    return cabinets, False


def _migrate_bestand_structure(db: Session) -> tuple[bool, str]:
    bestand_type = db.query(CabinetType).where(CabinetType.name.ilike("bestand")).first()
    if not bestand_type:
        return False, "Keine Bestand-Bridge-Struktur gefunden"

    legacy_cabinets = db.query(Cabinet).where(Cabinet.cabinet_type_id == bestand_type.id).order_by(Cabinet.order).all()
    if not legacy_cabinets:
        return False, "Keine Cabinets unter Bestand gefunden"

    migrated_count = 0
    remapped_documents = 0
    for legacy_cabinet in legacy_cabinets:
        new_type = db.query(CabinetType).where(CabinetType.name == legacy_cabinet.name).first()
        if not new_type:
            new_type = CabinetType(
                name=legacy_cabinet.name,
                description=legacy_cabinet.description,
                order=legacy_cabinet.order,
            )
            db.add(new_type)
            db.flush()

        migrated_cabinets: list[Cabinet] = []
        register_name_to_new_cabinet: dict[str, Cabinet] = {}
        registers = list(sorted(legacy_cabinet.registers, key=lambda item: item.order))
        for legacy_register in registers:
            new_cabinet = db.query(Cabinet).where(
                Cabinet.cabinet_type_id == new_type.id,
                Cabinet.name == legacy_register.name,
            ).first()
            if not new_cabinet:
                new_cabinet = Cabinet(
                    cabinet_type_id=new_type.id,
                    name=legacy_register.name,
                    description=legacy_register.description,
                    order=legacy_register.order,
                )
                db.add(new_cabinet)
                db.flush()
            migrated_cabinets.append(new_cabinet)
            register_name_to_new_cabinet[_normalized_label(legacy_register.name)] = new_cabinet

            for doc_type in list(sorted(legacy_register.document_types, key=lambda item: item.order)):
                doc_type.cabinet_id = new_cabinet.id
                doc_type.register_id = None
            for field in list(sorted(legacy_register.metadata_fields, key=lambda item: item.order)):
                field.cabinet_id = new_cabinet.id
                field.register_id = None

        fallback_cabinet = next((cab for cab in migrated_cabinets if cab.name == "2026"), None)
        if fallback_cabinet is None and migrated_cabinets:
            fallback_cabinet = migrated_cabinets[0]

        for doc_type in list(sorted(legacy_cabinet.document_types, key=lambda item: item.order)):
            doc_type.cabinet_id = fallback_cabinet.id if fallback_cabinet else None
            doc_type.register_id = None
        for field in list(sorted(legacy_cabinet.metadata_fields, key=lambda item: item.order)):
            field.cabinet_id = fallback_cabinet.id if fallback_cabinet else None
            field.register_id = None

        documents_to_remap = db.query(Document).where(Document.cabinet_id == legacy_cabinet.id).all()
        for document in documents_to_remap:
            target_cabinet = None
            if document.document_type and document.document_type.cabinet_id:
                target_cabinet = db.query(Cabinet).where(Document.cabinet_id != legacy_cabinet.id, Cabinet.id == document.document_type.cabinet_id).first() or db.query(Cabinet).where(Cabinet.id == document.document_type.cabinet_id).first()
            if target_cabinet is None and document.document_type and document.document_type.register:
                target_cabinet = register_name_to_new_cabinet.get(_normalized_label(document.document_type.register.name))
            if target_cabinet is None:
                target_cabinet = fallback_cabinet
            if target_cabinet and document.cabinet_id != target_cabinet.id:
                document.cabinet_id = target_cabinet.id
                remapped_documents += 1

        for legacy_register in registers:
            register_documents = db.query(Document).join(DocumentType, Document.document_type_id == DocumentType.id, isouter=True).where(Document.cabinet_id == legacy_register.id).all()
            for document in register_documents:
                target_cabinet = register_name_to_new_cabinet.get(_normalized_label(legacy_register.name)) or fallback_cabinet
                if target_cabinet and document.cabinet_id != target_cabinet.id:
                    document.cabinet_id = target_cabinet.id
                    remapped_documents += 1

            db.delete(legacy_register)
            migrated_count += 1

        db.delete(legacy_cabinet)

    remaining_bestand_cabinets = db.query(Cabinet).where(Cabinet.cabinet_type_id == bestand_type.id).count()
    if remaining_bestand_cabinets == 0:
        db.delete(bestand_type)

    db.commit()
    return True, f"Bestand-Struktur migriert, {migrated_count} Register zu Cabinets umgewandelt, {remapped_documents} Dokumente umgehängt"


def _backfill_document_cabinet_ids(db: Session) -> tuple[int, int]:
    updated = 0
    unresolved = 0
    documents = db.query(Document).order_by(Document.created_at).all()
    for document in documents:
        resolved_cabinet_id = None
        if document.document_type:
            if document.document_type.cabinet_id:
                resolved_cabinet_id = document.document_type.cabinet_id
            elif document.document_type.register and document.document_type.register.cabinet_id:
                resolved_cabinet_id = document.document_type.register.cabinet_id

        if resolved_cabinet_id:
            if document.cabinet_id != resolved_cabinet_id:
                document.cabinet_id = resolved_cabinet_id
                updated += 1
        else:
            unresolved += 1

    db.commit()
    return updated, unresolved


class PreviewStatusResponse(BaseModel):
    document_id: UUID
    status: str
    artifact_ready: bool
    artifact_url: str | None = None
    job_id: UUID | None = None
    error_message: str | None = None


def _ui_redirect_with_message(url: str) -> RedirectResponse:
    return RedirectResponse(url=url, status_code=303)


def _escape(value: Any) -> str:
    return escape("" if value is None else str(value), quote=True)


def _parse_json_dict(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _parse_field_options(field: MetadataField) -> list[str]:
    if not field.options:
        return []
    try:
        parsed_options = json.loads(field.options)
    except json.JSONDecodeError:
        return []
    return [str(option) for option in parsed_options] if isinstance(parsed_options, list) else []


def _selected_document_type(selected_document_type_id: str | None, db: Session) -> DocumentType | None:
    if not selected_document_type_id:
        return None
    try:
        return db.query(DocumentType).where(DocumentType.id == UUID(selected_document_type_id)).first()
    except ValueError:
        return None


def _definition_fields_for_document_type(document_type: DocumentType | None) -> list[MetadataField]:
    if not document_type:
        return []
    fields: list[MetadataField] = []
    if getattr(document_type, "cabinet_type_definition", None):
        fields.extend(document_type.cabinet_type_definition.metadata_fields or [])
    if getattr(document_type, "register_type_definition", None):
        fields.extend(document_type.register_type_definition.metadata_fields or [])
    fields.extend(document_type.fields or [])
    unique_by_name: dict[str, MetadataField] = {}
    for field in sorted(fields, key=lambda item: (item.order, item.label or item.name, str(item.id))):
        unique_by_name[field.name] = field
    return list(unique_by_name.values())


def _resolved_document_cabinet(document: Document) -> Cabinet | None:
    if document.cabinet:
        return document.cabinet
    if document.document_type:
        if document.document_type.cabinet:
            return document.document_type.cabinet
        if document.document_type.register and document.document_type.register.cabinet:
            return document.document_type.register.cabinet
    return None


def _available_document_types_for_node(selected_node: dict[str, Any] | None, cabinets: list[Cabinet]) -> list[DocumentType]:
    if not selected_node:
        return []
    node_kind = selected_node.get("kind")
    node_id = selected_node.get("id")
    available: list[DocumentType] = []
    if node_kind == "document_type":
        for cabinet in cabinets:
            for doc_type in cabinet.document_types:
                if str(doc_type.id) == str(node_id):
                    return [doc_type]
            for register in cabinet.registers:
                for doc_type in register.document_types:
                    if str(doc_type.id) == str(node_id):
                        return [doc_type]
        return []
    if node_kind == "register":
        for cabinet in cabinets:
            for register in cabinet.registers:
                if str(register.id) == str(node_id):
                    if register.register_type:
                        available.extend(register.register_type.document_type_definitions or [])
                    available.extend(register.document_types or [])
                    unique_by_id: dict[str, DocumentType] = {}
                    for doc_type in available:
                        unique_by_id[str(doc_type.id)] = doc_type
                    return list(unique_by_id.values())
        return []
    if node_kind == "cabinet":
        for cabinet in cabinets:
            if str(cabinet.id) != str(node_id):
                continue
            if cabinet.cabinet_type:
                available.extend(cabinet.cabinet_type.document_type_definitions or [])
                for register_type in cabinet.cabinet_type.register_types or []:
                    available.extend(register_type.document_type_definitions or [])
            available.extend(cabinet.document_types or [])
            unique_by_id: dict[str, DocumentType] = {}
            for doc_type in available:
                unique_by_id[str(doc_type.id)] = doc_type
            return list(unique_by_id.values())
    return []


def _selected_document_type_for_node(selected_node: dict[str, Any] | None, document_types: list[DocumentType], cabinets: list[Cabinet]) -> DocumentType | None:
    if not selected_node:
        return None
    node_kind = selected_node.get("kind")
    node_id = selected_node.get("id")
    if node_kind == "document_type":
        return next((doc_type for doc_type in document_types if str(doc_type.id) == str(node_id)), None)
    if node_kind == "register":
        for cabinet in cabinets:
            for register in cabinet.registers:
                if str(register.id) == str(node_id):
                    register_types = sorted(register.document_types, key=lambda item: (item.order, item.name or ""))
                    return register_types[0] if register_types else None
    if node_kind == "cabinet":
        for cabinet in cabinets:
            if str(cabinet.id) != str(node_id):
                continue
            direct_types = sorted(cabinet.document_types, key=lambda item: (item.order, item.name or ""))
            if direct_types:
                return direct_types[0]
            for register in sorted(cabinet.registers, key=lambda item: (item.order, item.name or "")):
                register_types = sorted(register.document_types, key=lambda item: (item.order, item.name or ""))
                if register_types:
                    return register_types[0]
    return None


def _collect_form_metadata(form: Any, document_type: DocumentType) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    for field in _definition_fields_for_document_type(document_type):
        input_name = f"metadata_{field.name}"
        values = form.getlist(input_name)
        cleaned_values = [value for value in values if value not in (None, "")]
        if not cleaned_values:
            continue
        metadata[field.name] = cleaned_values if field.field_type == "multi_selection" else cleaned_values[-1]
    return metadata


def _document_detail_message_url(
    document_id: UUID | str,
    *,
    message: str,
    error_field: str | None = None,
    error_message: str | None = None,
    form_data: dict[str, Any] | None = None,
) -> str:
    parts = [
        f"/ui/app/documents/{document_id}",
        f"message={quote_plus(message)}",
    ]
    if error_field:
        parts.append(f"error_field={quote_plus(str(error_field))}")
    if error_message:
        parts.append(f"error_message={quote_plus(str(error_message))}")
    if form_data:
        parts.append(f"form_data={quote_plus(json.dumps(form_data, ensure_ascii=False))}")
    return "?".join([parts[0], "&".join(parts[1:])]) if len(parts) > 1 else parts[0]


def _app_message_url(
    document_type_id: UUID | str,
    *,
    message: str,
    error_field: str | None = None,
    error_message: str | None = None,
    form_data: dict[str, Any] | None = None,
) -> str:
    parts = [
        f"/ui/app?selected_document_type_id={document_type_id}",
        f"message={quote_plus(message)}",
    ]
    if error_field:
        parts.append(f"error_field={quote_plus(str(error_field))}")
    if error_message:
        parts.append(f"error_message={quote_plus(str(error_message))}")
    if form_data:
        parts.append(f"form_data={quote_plus(json.dumps(form_data, ensure_ascii=False))}")
    return "&".join(parts)


def _option_list(items: list[tuple[str, str]], selected_value: str | None = None, include_blank: str | None = None) -> str:
    options: list[str] = []
    if include_blank is not None:
        options.append(f'<option value="">{_escape(include_blank)}</option>')
    for value, label in items:
        selected_attr = "selected" if selected_value == value else ""
        options.append(f'<option value="{value}" {selected_attr}>{_escape(label)}</option>')
    return "".join(options)


def _invoice_default_fields(document_type_id: UUID) -> list[MetadataField]:
    field_defs = [
        ("invoice_number", "Rechnungsnummer", "text", True, True, "z. B. 2026-000123", "Eindeutige Rechnungsnummer des Lieferanten"),
        ("invoice_date", "Rechnungsdatum", "date", True, False, "", "Ausstellungsdatum der Rechnung"),
        ("supplier_name", "Lieferant", "text", True, False, "z. B. Musterlieferant GmbH", "Rechnungssteller / Lieferant"),
        ("customer_name", "Kunde", "text", False, False, "z. B. Musterfirma GmbH", "Rechnungsempfänger"),
        ("net_amount", "Nettobetrag", "currency", True, False, "", "Betrag ohne Steuer"),
        ("tax_amount", "Steuerbetrag", "currency", False, False, "", "Gesamte ausgewiesene Steuer"),
        ("gross_amount", "Gesamtbetrag", "currency", True, False, "", "Rechnungsbetrag inklusive Steuer"),
        ("currency", "Währung", "selection", True, False, "EUR", "Abrechnungswährung"),
        ("due_date", "Fälligkeitsdatum", "date", False, False, "", "Zahlungsziel / Fälligkeit"),
        ("purchase_order_number", "Bestellnummer", "text", False, False, "", "Interne oder externe Bestellreferenz"),
        ("invoice_status", "Status", "selection", False, False, "offen", "Bearbeitungs- oder Zahlungsstatus"),
    ]

    fields: list[MetadataField] = []
    for order, (name, label, field_type, is_required, is_unique, placeholder, description) in enumerate(field_defs):
        field = MetadataField(
            document_type_id=document_type_id,
            name=name,
            label=label,
            field_type=field_type,
            description=description,
            placeholder=placeholder or None,
            default_value="EUR" if name == "currency" else ("offen" if name == "invoice_status" else None),
            is_required=is_required,
            is_unique=is_unique,
            order=order,
            width="half",
        )
        if name == "currency":
            field.options = json.dumps(["EUR", "USD", "CHF"], ensure_ascii=False)
        if name == "invoice_status":
            field.options = json.dumps(["offen", "in Prüfung", "freigegeben", "bezahlt"], ensure_ascii=False)
        fields.append(field)
    return fields


@router.get("/", response_class=HTMLResponse)
async def ui_root(request: Request) -> RedirectResponse:
    return RedirectResponse(url="/ui/admin", status_code=303)


@router.get("/admin", response_class=HTMLResponse)
async def ui_admin_home(
    request: Request,
    message: str | None = None,
    selected_definition_kind: str | None = None,
    selected_definition_id: str | None = None,
    selected_metadata_field_id: str | None = None,
    identity_tab: str | None = None,
    selected_user_id: str | None = None,
    selected_role_id: str | None = None,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    cabinet_types = db.query(CabinetType).order_by(CabinetType.order, CabinetType.name).all()
    cabinets = db.query(Cabinet).order_by(Cabinet.order).all()
    document_types = db.query(DocumentType).order_by(DocumentType.name).all()
    recent_documents = db.query(Document).order_by(Document.created_at.desc()).limit(10).all()
    preview_jobs = db.query(PreviewJob).order_by(PreviewJob.created_at.desc()).limit(12).all()
    index_jobs = db.query(IndexJob).order_by(IndexJob.created_at.desc()).limit(12).all()
    selected_document_type = document_types[0] if document_types else None
    return HTMLResponse(
        content=_render_admin_page(
            cabinet_types=cabinet_types,
            cabinets=cabinets,
            document_types=document_types,
            recent_documents=recent_documents,
            selected_document_type=selected_document_type,
            preview_jobs=preview_jobs,
            index_jobs=index_jobs,
            selected_definition_kind=selected_definition_kind,
            selected_definition_id=selected_definition_id,
            selected_metadata_field_id=selected_metadata_field_id,
        )
    )


@router.get("/admin/document-types/{document_type_id}", response_class=HTMLResponse)
async def ui_admin_document_type_detail(
    document_type_id: UUID,
    request: Request,
    message: str | None = None,
    selected_definition_kind: str | None = None,
    selected_definition_id: str | None = None,
    selected_metadata_field_id: str | None = None,
    identity_tab: str | None = None,
    selected_user_id: str | None = None,
    selected_role_id: str | None = None,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    cabinet_types = db.query(CabinetType).order_by(CabinetType.order, CabinetType.name).all()
    cabinets = db.query(Cabinet).order_by(Cabinet.order).all()
    document_types = db.query(DocumentType).order_by(DocumentType.name).all()
    recent_documents = db.query(Document).order_by(Document.created_at.desc()).limit(10).all()
    preview_jobs = db.query(PreviewJob).order_by(PreviewJob.created_at.desc()).limit(12).all()
    index_jobs = db.query(IndexJob).order_by(IndexJob.created_at.desc()).limit(12).all()
    selected_document_type = db.query(DocumentType).where(DocumentType.id == document_type_id).first()
    return HTMLResponse(
        content=_render_admin_page(
            cabinet_types=cabinet_types,
            cabinets=cabinets,
            document_types=document_types,
            recent_documents=recent_documents,
            selected_document_type=selected_document_type,
            preview_jobs=preview_jobs,
            index_jobs=index_jobs,
            selected_definition_kind=selected_definition_kind or "document_type",
            selected_definition_id=selected_definition_id or str(document_type_id),
            selected_metadata_field_id=selected_metadata_field_id,
        )
    )


@router.get("/admin/queues", response_class=HTMLResponse)
async def ui_admin_queues(
    request: Request,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    preview_jobs = db.query(PreviewJob).order_by(PreviewJob.created_at.desc()).limit(40).all()
    index_jobs = db.query(IndexJob).order_by(IndexJob.created_at.desc()).limit(40).all()
    return HTMLResponse(content=_render_admin_queues_page(preview_jobs=preview_jobs, index_jobs=index_jobs))


@router.get("/admin/identity", response_class=HTMLResponse)
async def ui_admin_identity(
    request: Request,
    identity_tab: str | None = None,
    selected_user_id: str | None = None,
    selected_role_id: str | None = None,
    selected_team_id: str | None = None,
    message: str | None = None,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    users = db.query(User).order_by(User.display_name, User.email).all()
    roles = db.query(Role).order_by(Role.name).all()
    teams = db.query(Team).order_by(Team.name).all()
    return HTMLResponse(
        content=_render_admin_identity_page(
            users=users,
            roles=roles,
            teams=teams,
            identity_tab=identity_tab or "users",
            selected_user_id=selected_user_id,
            selected_role_id=selected_role_id,
            selected_team_id=selected_team_id,
            message=message,
        )
    )


@router.post("/admin/documents/{document_id}/reindex")
async def ui_admin_reindex_document(
    document_id: UUID,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    document = db.query(Document).where(Document.id == document_id).first()
    if not document:
        return _ui_redirect_with_message("/ui/admin/queues?message=Dokument+nicht+gefunden")
    enqueue_document_index(db, document=document, reason="manual_reindex_ui")
    return _ui_redirect_with_message(f"/ui/admin/queues?message={quote_plus(f'Indexjob für {document.title or document.name} erneut eingereiht')}")


@router.get("/admin/documents/{document_id}/extracted-text", response_class=HTMLResponse)
async def ui_admin_document_extracted_text(
    document_id: UUID,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    document = db.query(Document).where(Document.id == document_id).first()
    if not document:
        return HTMLResponse(content="<h1>Dokument nicht gefunden</h1>", status_code=404)
    preview = _escape(document.extracted_text_preview or "Kein extrahierter Text gespeichert.")
    return HTMLResponse(content=f"""<!doctype html><html lang='de'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width, initial-scale=1'><title>Extrahierter Text</title><style>:root {{ color-scheme: dark; }} body {{ margin:0; font-family: Inter, ui-sans-serif, system-ui, sans-serif; background:#0b1020; color:#eef2ff; }} .page {{ max-width: 1100px; margin:0 auto; padding:16px; }} .panel {{ background:#121933; border:1px solid rgba(77,212,255,0.10); border-radius:18px; padding:16px; }} a {{ color:#4dd4ff; text-decoration:none; }} pre {{ white-space:pre-wrap; word-break:break-word; font: .95rem/1.5 ui-monospace, SFMono-Regular, Menlo, monospace; }}</style></head><body><div class='page'><div class='panel'><p><a href='/ui/admin/queues'>← Zurück zu Queues & Logs</a></p><h1 style='margin-top:0;'>Extrahierter Text, { _escape(document.title or document.name) }</h1><p style='color:#a8b2d1;'>Gespeicherte Vorschau des zuletzt extrahierten Volltexts.</p><pre>{preview}</pre></div></div></body></html>""")


@router.get("/app", response_class=HTMLResponse)
async def ui_app_home(
    request: Request,
    selected_document_type_id: str | None = None,
    node_kind: str | None = None,
    node_id: str | None = None,
    message: str | None = None,
    error_field: str | None = None,
    error_message: str | None = None,
    form_data: str | None = None,
    q: str | None = None,
    filter_kind: str | None = None,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    all_documents = db.query(Document).order_by(Document.created_at.desc()).all()
    search_payload: dict[str, Any] | None = None
    if (q or "").strip():
        search_payload = SearchService(db).search(
            q=q or "",
            document_type_id=selected_document_type_id,
            cabinet_type_id=None,
            cabinet_id=node_id if node_kind == "cabinet" else None,
            page=1,
            page_size=100,
        )
        hit_ids = [str(hit.get("document_id")) for hit in search_payload.get("hits", []) if hit.get("document_id")]
        documents_by_id = {str(document.id): document for document in all_documents}
        all_documents = [documents_by_id[hit_id] for hit_id in hit_ids if hit_id in documents_by_id]
    recent_documents = all_documents[:10]
    cabinets, cabinet_type_model_ready = _safe_load_cabinets(db)
    cabinet_types = db.query(CabinetType).order_by(CabinetType.order, CabinetType.name).all()
    document_types = db.query(DocumentType).order_by(DocumentType.order, DocumentType.name).all()
    selected_document_type = _selected_document_type(selected_document_type_id, db)
    _resolve_archive_node._all_documents = all_documents
    selected_node = _resolve_archive_node(node_kind, node_id, cabinets, document_types)
    if selected_document_type is None:
        selected_document_type = _selected_document_type_for_node(selected_node, document_types, cabinets)
    form_values = _parse_json_dict(form_data)
    return HTMLResponse(
        content=_render_app_page(
            cabinets,
            document_types,
            recent_documents,
            selected_document_type,
            selected_node,
            message if cabinet_type_model_ready else ((message + " · " if message else "") + "Datenbank noch im Altmodell, Cabinettypen noch nicht migriert"),
            error_field,
            error_message,
            form_values,
            all_documents,
            q or "",
            filter_kind or "all",
            cabinet_types,
        )
    )


@router.get("/app/documents/{document_id}", response_class=HTMLResponse)
async def ui_app_document_detail(
    document_id: UUID,
    request: Request,
    message: str | None = None,
    error_field: str | None = None,
    error_message: str | None = None,
    form_data: str | None = None,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    document = db.query(Document).where(Document.id == document_id).first()
    if not document:
        raise HTTPException(status_code=404, detail="Document not found")
    form_values = _parse_json_dict(form_data) if form_data else None
    cabinets = db.query(Cabinet).order_by(Cabinet.order).all()
    return HTMLResponse(content=_render_document_detail_page(document, cabinets=cabinets, message=message, error_field=error_field, error_message=error_message, form_values=form_values))


@router.post("/app/documents/{document_id}/metadata")
async def ui_app_document_update_metadata(
    document_id: UUID,
    request: Request,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    document = db.query(Document).where(Document.id == document_id).first()
    if not document:
        raise HTTPException(status_code=404, detail="Document not found")
    if not document.document_type_id or not document.document_type:
        return _ui_redirect_with_message(_document_detail_message_url(document_id, message="Dokumenttyp fehlt, Metadaten können nicht bearbeitet werden"))

    form = await request.form()
    metadata = _collect_form_metadata(form, document.document_type)

    try:
        validation = validate_document_metadata(
            db,
            document.document_type_id,
            metadata,
            current_document_id=document.id,
        )
        document.metadata_json = json.dumps(validation.normalized, ensure_ascii=False)
        db.add(document)
        db.commit()
    except MetadataValidationError as exc:
        first = exc.detail.get("errors", [{}])[0]
        first_error = first.get("message", "Metadaten ungültig")
        error_field = first.get("field", "")
        return _ui_redirect_with_message(
            _document_detail_message_url(
                document_id,
                message=f"Metadaten ungültig: {first_error}",
                error_field=str(error_field),
                error_message=str(first_error),
                form_data=metadata,
            )
        )

    return _ui_redirect_with_message(
        _document_detail_message_url(document_id, message="Metadaten erfolgreich aktualisiert")
    )


@router.post("/app/documents/{document_id}/cabinet")
async def ui_app_document_update_cabinet(
    document_id: UUID,
    cabinet_id: str = Form(...),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    document = db.query(Document).where(Document.id == document_id).first()
    if not document:
        raise HTTPException(status_code=404, detail="Document not found")

    resolved_cabinet: Cabinet | None = None
    try:
        resolved_uuid = UUID(cabinet_id)
        resolved_cabinet = db.query(Cabinet).where(Cabinet.id == resolved_uuid).first()
    except ValueError:
        resolved_uuid = None

    if not resolved_cabinet and resolved_uuid:
        cabinets = db.query(Cabinet).order_by(Cabinet.order).all()
        move_resolution = _build_move_resolution(document, cabinets)
        legacy_match = next(
            (item for item in move_resolution["legacy_candidate_cabinets"] if item.get("id") == resolved_uuid),
            None,
        )
        candidate_type_name = move_resolution.get("candidate_type_name")
        if legacy_match and candidate_type_name:
            resolved_cabinet = db.query(Cabinet).join(CabinetType).where(
                Cabinet.name == legacy_match.get("name"),
                CabinetType.name == candidate_type_name,
            ).first()

    if not resolved_cabinet:
        return _ui_redirect_with_message(_document_detail_message_url(document_id, message="Ziel-Cabinet nicht gefunden"))

    document.cabinet_id = resolved_cabinet.id
    db.add(document)
    db.commit()
    return _ui_redirect_with_message(
        _document_detail_message_url(document_id, message=f"Dokument erfolgreich nach {resolved_cabinet.name} verschoben")
    )


@router.get("/app/documents/{document_id}/download")
async def ui_app_document_download(
    document_id: UUID,
    db: Session = Depends(get_db),
) -> FileResponse:
    document = db.query(Document).where(Document.id == document_id).first()
    if not document:
        raise HTTPException(status_code=404, detail="Document not found")

    settings = load_settings("config.yaml")
    storage = StorageManager(settings.storage.base_path)
    full_path = storage.full_path(Path(document.storage_path))
    if not full_path.exists():
        raise HTTPException(status_code=404, detail="Stored file not found")

    return FileResponse(path=full_path, filename=document.name, media_type=document.mime_type or "application/octet-stream")


@router.get("/preview/documents/{document_id}/status", response_model=PreviewStatusResponse)
async def ui_preview_document_status(
    document_id: UUID,
    db: Session = Depends(get_db),
) -> PreviewStatusResponse:
    document = db.query(Document).where(Document.id == document_id).first()
    if not document:
        raise HTTPException(status_code=404, detail="Document not found")

    artifact = get_latest_preview_artifact(db, document.id)
    job = get_latest_preview_job(db, document.id)
    if artifact and artifact.storage_path:
        return PreviewStatusResponse(
            document_id=document.id,
            status="ready",
            artifact_ready=True,
            artifact_url=f"/ui/preview/documents/{document.id}",
            job_id=job.id if job else None,
            error_message=None,
        )

    if job is None:
        job = enqueue_preview_job(db, document)

    return PreviewStatusResponse(
        document_id=document.id,
        status=job.status.value if hasattr(job.status, "value") else str(job.status),
        artifact_ready=False,
        artifact_url=None,
        job_id=job.id,
        error_message=job.error_message,
    )


@router.get("/preview/documents/{document_id}")
async def ui_preview_document(
    document_id: UUID,
    db: Session = Depends(get_db),
) -> Response:
    document = db.query(Document).where(Document.id == document_id).first()
    if not document:
        raise HTTPException(status_code=404, detail="Document not found")

    settings = load_settings("config.yaml")
    storage = StorageManager(settings.storage.base_path)

    artifact = get_latest_preview_artifact(db, document.id)
    if artifact and artifact.storage_path:
        preview_path = storage.full_path(Path(artifact.storage_path))
        if preview_path.exists():
            return Response(content=preview_path.read_bytes(), media_type=artifact.mime_type)

    job = get_latest_preview_job(db, document.id)
    if job is None:
        job = enqueue_preview_job(db, document)

    waiting_html = _render_preview_waiting_state(document.id, document.name, job.status.value if hasattr(job.status, 'value') else str(job.status), job.error_message)
    return Response(content=waiting_html, media_type="text/html; charset=utf-8")


@router.get("/api/v1/duplicate-check")
async def api_duplicate_check(
    hash: str,
    document_type_id: UUID | None = None,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Check if a document with the given MD5 hash already exists."""
    settings = load_settings("config.yaml")
    if not settings.app.md5_duplicate_check:
        return {"enabled": False, "duplicate": False}

    if document_type_id:
        doc_type = db.query(DocumentType).where(DocumentType.id == document_type_id).first()
        if doc_type and not doc_type.md5_duplicate_check:
            return {"enabled": False, "duplicate": False}

    existing = db.query(Document).where(Document.file_hash == hash).first()
    if existing:
        return {
            "enabled": True,
            "duplicate": True,
            "existing_document": {
                "id": str(existing.id),
                "name": existing.name,
                "title": existing.title,
                "created_at": existing.created_at.isoformat() if existing.created_at else None,
            },
        }
    return {"enabled": True, "duplicate": False}


@router.post("/app/intake")
async def ui_app_intake(
    request: Request,
    file: UploadFile = File(...),
    document_type_id: UUID = Form(...),
    hash: str = Form(""),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    document_type = db.query(DocumentType).where(DocumentType.id == document_type_id).first()
    if not document_type:
        return _ui_redirect_with_message("/ui/app?message=Dokumenttyp+nicht+gefunden")

    # --- MD5 duplicate check ---
    settings = load_settings("config.yaml")
    global_check = settings.app.md5_duplicate_check
    doc_type_check = document_type.md5_duplicate_check if document_type else True

    if global_check and doc_type_check and hash:
        existing = db.query(Document).where(Document.file_hash == hash).first()
        if existing:
            return _ui_redirect_with_message(
                _app_message_url(
                    document_type_id,
                    message=f"Achtung: Ein Dokument mit identischem Inhalt existiert bereits: {existing.name} (hochgeladen am {existing.created_at.strftime('%d.%m.%Y') if existing.created_at else 'unbekannt'}). Duplikat wurde nicht erneut gespeichert.",
                    error_field="file",
                    error_message="Duplikat erkannt",
                )
            )

    form = await request.form()
    metadata = _collect_form_metadata(form, document_type)

    try:
        validation = validate_document_metadata(db, document_type.id, metadata)
        metadata = validation.normalized
    except MetadataValidationError as exc:
        first = exc.detail.get("errors", [{}])[0]
        first_error = first.get("message", "Metadaten ungültig")
        error_field = first.get("field", "")
        return _ui_redirect_with_message(
            _app_message_url(
                document_type_id,
                message=f"Metadaten ungültig: {first_error}",
                error_field=str(error_field),
                error_message=str(first_error),
                form_data=metadata,
            )
        )

    settings = load_settings("config.yaml")
    storage = StorageManager(settings.storage.base_path)
    original_filename = file.filename or "upload.bin"
    relative_path = storage.generate_path(original_filename)
    saved_path = await storage.save(file, relative_path)
    file_size = saved_path.stat().st_size if saved_path.exists() else 0

    detected_doc_type = DocType.OTHER
    content_type = (file.content_type or "").lower()
    if content_type.startswith("image/"):
        detected_doc_type = DocType.IMAGE
    elif "pdf" in content_type:
        detected_doc_type = DocType.PDF
    elif content_type.startswith("text/"):
        detected_doc_type = DocType.TEXT
    elif "word" in content_type or "officedocument" in content_type:
        detected_doc_type = DocType.DOC

    cabinet_id_raw = str(form.get("cabinet_id") or "").strip()
    register_id_raw = str(form.get("register_id") or "").strip()

    resolved_cabinet = None
    if cabinet_id_raw:
        try:
            resolved_cabinet = db.query(Cabinet).where(Cabinet.id == UUID(cabinet_id_raw)).first()
        except ValueError:
            resolved_cabinet = None
    if not resolved_cabinet and register_id_raw:
        try:
            resolved_register = db.query(Register).where(Register.id == UUID(register_id_raw)).first()
            if resolved_register and resolved_register.cabinet:
                resolved_cabinet = resolved_register.cabinet
        except ValueError:
            resolved_register = None
    if not resolved_cabinet and document_type.cabinet:
        resolved_cabinet = document_type.cabinet
    elif not resolved_cabinet and document_type.register and document_type.register.cabinet:
        resolved_cabinet = document_type.register.cabinet

    document = Document(
        name=original_filename,
        title=Path(original_filename).stem,
        doc_type=detected_doc_type,
        document_type_id=document_type.id,
        cabinet_id=resolved_cabinet.id if resolved_cabinet else None,
        mime_type=file.content_type,
        size_bytes=int(file_size),
        storage_path=str(relative_path),
        metadata_json=json.dumps(metadata, ensure_ascii=False),
        file_hash=hash or None,
    )
    db.add(document)
    db.flush()

    extracted_text = None

    enqueue_preview_job(db, document)
    db.commit()
    db.refresh(document)
    enqueue_document_index(db, document=document, reason="document_uploaded_ui")

    success_message = "Dokument erfolgreich gespeichert, Preview-Rendering und Volltextindexierung eingereiht"
    if not extracted_text:
        success_message = "Dokument gespeichert, Preview-Rendering eingereiht. Kein extrahierbarer Text für Volltext gefunden"

    return _ui_redirect_with_message(
        _app_message_url(document_type_id, message=success_message)
    )


@router.get("/workflows", response_class=HTMLResponse)
async def ui_workflows_home(request: Request) -> HTMLResponse:
    return RedirectResponse(url="/ui/workflow-designer", status_code=303)


@router.get("/workflow-designer", response_class=HTMLResponse)
async def ui_workflow_designer_home(
    request: Request,
    selected_workflow_id: str | None = None,
    selected_step_id: str | None = None,
    message: str | None = None,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    _sync_assignment_targets(db)
    workflows = db.query(WorkflowDefinition).order_by(WorkflowDefinition.name).all()
    assignment_targets = db.query(AssignmentTarget).order_by(AssignmentTarget.target_type, AssignmentTarget.label).all()
    selected_workflow = None
    selected_step = None
    if selected_workflow_id:
        selected_workflow = db.query(WorkflowDefinition).where(WorkflowDefinition.id == UUID(selected_workflow_id)).first()
    elif workflows:
        selected_workflow = workflows[0]
    if selected_step_id:
        selected_step = db.query(WorkflowStepDefinition).where(WorkflowStepDefinition.id == UUID(selected_step_id)).first()
    elif selected_workflow and selected_workflow.steps:
        selected_step = sorted(selected_workflow.steps, key=lambda item: (item.order, item.name.lower()))[0]
    return HTMLResponse(content=_render_workflow_designer_page(workflows=workflows, assignment_targets=assignment_targets, selected_workflow=selected_workflow, selected_step=selected_step, message=message))


@router.post("/workflow-designer/workflows")
async def ui_workflow_designer_create_workflow(
    name: str = Form(...),
    description: str = Form(""),
    is_active: str | None = Form(None),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    workflow = WorkflowDefinition(
        name=name.strip(),
        description=description.strip() or None,
        is_active=bool(is_active),
    )
    db.add(workflow)
    db.commit()
    db.refresh(workflow)
    return RedirectResponse(url=f"/ui/workflow-designer?selected_workflow_id={workflow.id}&message={quote_plus(f'Workflow {workflow.name} angelegt')}#designer", status_code=303)


@router.post("/workflow-designer/steps")
async def ui_workflow_designer_create_step(
    workflow_definition_id: UUID = Form(...),
    name: str = Form(...),
    step_key: str = Form(...),
    description: str = Form(""),
    order: int = Form(0),
    assignment_target_id: str = Form(""),
    due_in_days: str = Form(""),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    _sync_assignment_targets(db)
    workflow = db.query(WorkflowDefinition).where(WorkflowDefinition.id == workflow_definition_id).first()
    if not workflow:
        return RedirectResponse(url="/ui/workflow-designer?message=Workflow+nicht+gefunden#designer", status_code=303)
    resolved_assignment_target_id = UUID(assignment_target_id) if assignment_target_id else None
    resolved_due_in_days = int(due_in_days) if str(due_in_days).strip() else None
    step = WorkflowStepDefinition(
        workflow_definition_id=workflow_definition_id,
        name=name.strip(),
        step_key=step_key.strip(),
        description=description.strip() or None,
        order=order,
        assignment_target_id=resolved_assignment_target_id,
        due_in_days=resolved_due_in_days,
    )
    db.add(step)
    db.commit()
    db.refresh(step)
    return RedirectResponse(url=f"/ui/workflow-designer?selected_workflow_id={workflow.id}&selected_step_id={step.id}&message={quote_plus(f'Schritt {step.name} angelegt')}#designer", status_code=303)


@router.post("/workflow-designer/steps/{step_id}")
async def ui_workflow_designer_update_step(
    step_id: UUID,
    name: str = Form(...),
    step_key: str = Form(...),
    description: str = Form(""),
    order: int = Form(0),
    assignment_target_id: str = Form(""),
    due_in_days: str = Form(""),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    _sync_assignment_targets(db)
    step = db.query(WorkflowStepDefinition).where(WorkflowStepDefinition.id == step_id).first()
    if not step:
        return RedirectResponse(url="/ui/workflow-designer?message=Schritt+nicht+gefunden#designer", status_code=303)
    step.name = name.strip()
    step.step_key = step_key.strip()
    step.description = description.strip() or None
    step.order = order
    step.assignment_target_id = UUID(assignment_target_id) if assignment_target_id else None
    step.due_in_days = int(due_in_days) if str(due_in_days).strip() else None
    db.add(step)
    db.commit()
    return RedirectResponse(url=f"/ui/workflow-designer?selected_workflow_id={step.workflow_definition_id}&selected_step_id={step.id}&message={quote_plus(f'Schritt {step.name} aktualisiert')}#designer", status_code=303)


@router.post("/workflow-designer/steps/{step_id}/move")
async def ui_workflow_designer_move_step(
    step_id: UUID,
    direction: str = Form(...),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    step = db.query(WorkflowStepDefinition).where(WorkflowStepDefinition.id == step_id).first()
    if not step:
        return RedirectResponse(url="/ui/workflow-designer?message=Schritt+nicht+gefunden#designer", status_code=303)
    sibling_steps = sorted(
        db.query(WorkflowStepDefinition).where(WorkflowStepDefinition.workflow_definition_id == step.workflow_definition_id).all(),
        key=lambda item: (item.order, item.name.lower()),
    )
    current_index = next((index for index, item in enumerate(sibling_steps) if item.id == step.id), None)
    if current_index is None:
        return RedirectResponse(url=f"/ui/workflow-designer?selected_workflow_id={step.workflow_definition_id}&message=Schritt+nicht+einsortierbar#designer", status_code=303)
    target_index = current_index - 1 if direction == "up" else current_index + 1
    if target_index < 0 or target_index >= len(sibling_steps):
        return RedirectResponse(url=f"/ui/workflow-designer?selected_workflow_id={step.workflow_definition_id}&selected_step_id={step.id}&message=Schritt+kann+nicht+weiter+verschoben+werden#designer", status_code=303)
    sibling_steps[current_index], sibling_steps[target_index] = sibling_steps[target_index], sibling_steps[current_index]
    for index, sibling in enumerate(sibling_steps, start=1):
        sibling.order = index * 10
        db.add(sibling)
    db.commit()
    return RedirectResponse(url=f"/ui/workflow-designer?selected_workflow_id={step.workflow_definition_id}&selected_step_id={step.id}&message={quote_plus('Reihenfolge aktualisiert')}#designer", status_code=303)


@router.post("/workflow-designer/steps/{step_id}/delete")
async def ui_workflow_designer_delete_step(
    step_id: UUID,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    step = db.query(WorkflowStepDefinition).where(WorkflowStepDefinition.id == step_id).first()
    if not step:
        return _workflow_designer_redirect(message="Schritt nicht gefunden")

    workflow_id = step.workflow_definition_id
    step_name = step.name
    incoming_count = db.query(WorkflowTransitionDefinition).where(WorkflowTransitionDefinition.to_step_id == step.id).count()
    outgoing_count = db.query(WorkflowTransitionDefinition).where(WorkflowTransitionDefinition.from_step_id == step.id).count()
    if incoming_count or outgoing_count:
        parts: list[str] = []
        if incoming_count:
            parts.append(f"{incoming_count} eingehende")
        if outgoing_count:
            parts.append(f"{outgoing_count} ausgehende")
        return _workflow_designer_redirect(
            workflow_id=workflow_id,
            step_id=step.id,
            message=f"Schritt {step_name} kann nicht gelöscht werden, solange {', '.join(parts)} Transitionen existieren",
        )

    remaining_steps = sorted(
        db.query(WorkflowStepDefinition).where(
            WorkflowStepDefinition.workflow_definition_id == workflow_id,
            WorkflowStepDefinition.id != step.id,
        ).all(),
        key=lambda item: (item.order, item.name.lower()),
    )
    next_selected_step_id = str(remaining_steps[0].id) if remaining_steps else None

    db.delete(step)
    db.commit()

    for index, sibling in enumerate(remaining_steps, start=1):
        sibling.order = index * 10
        db.add(sibling)
    db.commit()

    return _workflow_designer_redirect(
        workflow_id=workflow_id,
        step_id=next_selected_step_id,
        message=f"Schritt {step_name} gelöscht",
    )


@router.post("/workflow-designer/workflows/{workflow_id}/reorder")
async def ui_workflow_designer_reorder_steps(
    workflow_id: UUID,
    step_ids: str = Form(...),
    selected_step_id: str = Form(""),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    workflow = db.query(WorkflowDefinition).where(WorkflowDefinition.id == workflow_id).first()
    if not workflow:
        return RedirectResponse(url="/ui/workflow-designer?message=Workflow+nicht+gefunden#designer", status_code=303)

    ordered_ids: list[UUID] = []
    for raw_id in [item.strip() for item in step_ids.split(",") if item.strip()]:
        try:
            ordered_ids.append(UUID(raw_id))
        except ValueError:
            continue

    steps = db.query(WorkflowStepDefinition).where(WorkflowStepDefinition.workflow_definition_id == workflow_id).all()
    steps_by_id = {step.id: step for step in steps}
    final_steps = [steps_by_id[step_id] for step_id in ordered_ids if step_id in steps_by_id]
    missing_steps = [step for step in steps if step.id not in {item.id for item in final_steps}]
    final_steps.extend(sorted(missing_steps, key=lambda item: (item.order, item.name.lower())))

    for index, step in enumerate(final_steps, start=1):
        step.order = index * 10
        db.add(step)
    db.commit()

    selected_step_query = f"&selected_step_id={selected_step_id}" if selected_step_id else ""
    return RedirectResponse(url=f"/ui/workflow-designer?selected_workflow_id={workflow_id}{selected_step_query}&message={quote_plus('Grafische Reihenfolge aktualisiert')}#designer", status_code=303)


def _workflow_designer_redirect(*, workflow_id: UUID | str | None = None, step_id: UUID | str | None = None, message: str | None = None) -> RedirectResponse:
    params: list[str] = []
    if workflow_id:
        params.append(f"selected_workflow_id={quote_plus(str(workflow_id))}")
    if step_id:
        params.append(f"selected_step_id={quote_plus(str(step_id))}")
    if message:
        params.append(f"message={quote_plus(message)}")
    query = f"?{'&'.join(params)}" if params else ""
    return RedirectResponse(url=f"/ui/workflow-designer{query}#designer", status_code=303)


def _validate_workflow_transition_payload(
    *,
    db: Session,
    workflow_definition_id: UUID,
    from_step_id: UUID,
    to_step_id: UUID,
    label: str,
    is_default: bool,
    transition_id: UUID | None = None,
) -> tuple[WorkflowDefinition | None, WorkflowStepDefinition | None, str | None]:
    workflow = db.query(WorkflowDefinition).where(WorkflowDefinition.id == workflow_definition_id).first()
    if not workflow:
        return None, None, "Workflow nicht gefunden"

    from_step = db.query(WorkflowStepDefinition).where(WorkflowStepDefinition.id == from_step_id).first()
    if not from_step or from_step.workflow_definition_id != workflow_definition_id:
        return workflow, None, "Ausgangsschritt nicht gefunden"

    to_step = db.query(WorkflowStepDefinition).where(WorkflowStepDefinition.id == to_step_id).first()
    if not to_step or to_step.workflow_definition_id != workflow_definition_id:
        return workflow, from_step, "Zielschritt nicht gefunden oder gehört zu einem anderen Workflow"

    normalized_label = label.strip()
    if not normalized_label:
        return workflow, from_step, "Transitions-Label fehlt"

    if from_step_id == to_step_id:
        return workflow, from_step, "Transition auf denselben Schritt ist nicht erlaubt"

    existing_query = db.query(WorkflowTransitionDefinition).where(
        WorkflowTransitionDefinition.workflow_definition_id == workflow_definition_id,
        WorkflowTransitionDefinition.from_step_id == from_step_id,
        WorkflowTransitionDefinition.to_step_id == to_step_id,
        WorkflowTransitionDefinition.label.ilike(normalized_label),
    )
    if transition_id:
        existing_query = existing_query.where(WorkflowTransitionDefinition.id != transition_id)
    if existing_query.first():
        return workflow, from_step, "Diese Transition existiert bereits"

    if is_default:
        default_query = db.query(WorkflowTransitionDefinition).where(
            WorkflowTransitionDefinition.workflow_definition_id == workflow_definition_id,
            WorkflowTransitionDefinition.from_step_id == from_step_id,
            WorkflowTransitionDefinition.is_default.is_(True),
        )
        if transition_id:
            default_query = default_query.where(WorkflowTransitionDefinition.id != transition_id)
        if default_query.first():
            return workflow, from_step, "Es ist nur ein Standardübergang pro Schritt erlaubt"

    return workflow, from_step, None


@router.post("/workflow-designer/transitions")
async def ui_workflow_designer_create_transition(
    workflow_definition_id: UUID = Form(...),
    from_step_id: UUID = Form(...),
    to_step_id: UUID = Form(...),
    label: str = Form("Weiter"),
    is_default: str | None = Form(None),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    workflow, from_step, error_message = _validate_workflow_transition_payload(
        db=db,
        workflow_definition_id=workflow_definition_id,
        from_step_id=from_step_id,
        to_step_id=to_step_id,
        label=label,
        is_default=bool(is_default),
    )
    if error_message:
        return _workflow_designer_redirect(
            workflow_id=workflow_definition_id if workflow else None,
            step_id=from_step_id if from_step else None,
            message=error_message,
        )

    transition = WorkflowTransitionDefinition(
        workflow_definition_id=workflow_definition_id,
        from_step_id=from_step_id,
        to_step_id=to_step_id,
        label=label.strip(),
        is_default=bool(is_default),
    )
    db.add(transition)
    db.commit()
    return _workflow_designer_redirect(
        workflow_id=workflow_definition_id,
        step_id=from_step_id,
        message="Transition angelegt",
    )


@router.post("/workflow-designer/transitions/{transition_id}")
async def ui_workflow_designer_update_transition(
    transition_id: UUID,
    to_step_id: UUID = Form(...),
    label: str = Form("Weiter"),
    is_default: str | None = Form(None),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    transition = db.query(WorkflowTransitionDefinition).where(WorkflowTransitionDefinition.id == transition_id).first()
    if not transition:
        return _workflow_designer_redirect(message="Transition nicht gefunden")

    workflow, from_step, error_message = _validate_workflow_transition_payload(
        db=db,
        workflow_definition_id=transition.workflow_definition_id,
        from_step_id=transition.from_step_id,
        to_step_id=to_step_id,
        label=label,
        is_default=bool(is_default),
        transition_id=transition.id,
    )
    if error_message:
        return _workflow_designer_redirect(
            workflow_id=transition.workflow_definition_id if workflow else None,
            step_id=transition.from_step_id if from_step else None,
            message=error_message,
        )

    transition.to_step_id = to_step_id
    transition.label = label.strip()
    transition.is_default = bool(is_default)
    db.add(transition)
    db.commit()
    return _workflow_designer_redirect(
        workflow_id=transition.workflow_definition_id,
        step_id=transition.from_step_id,
        message="Transition aktualisiert",
    )


@router.post("/workflow-designer/transitions/{transition_id}/delete")
async def ui_workflow_designer_delete_transition(
    transition_id: UUID,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    transition = db.query(WorkflowTransitionDefinition).where(WorkflowTransitionDefinition.id == transition_id).first()
    if not transition:
        return _workflow_designer_redirect(message="Transition nicht gefunden")

    workflow_id = transition.workflow_definition_id
    from_step_id = transition.from_step_id
    label = transition.label
    db.delete(transition)
    db.commit()
    return _workflow_designer_redirect(
        workflow_id=workflow_id,
        step_id=from_step_id,
        message=f"Transition {label} gelöscht",
    )


@router.post("/admin/cabinet-types")
async def ui_create_cabinet_type(
    name: str = Form(...),
    description: str = Form(""),
    order: int = Form(0),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    cabinet_type = CabinetType(name=name.strip(), description=description.strip() or None, order=order)
    db.add(cabinet_type)
    db.commit()
    return RedirectResponse(url="/ui/admin", status_code=303)


@router.post("/admin/migrate-bestand")
async def ui_migrate_bestand_structure(
    return_to: str = Form("app"),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    changed, message = _migrate_bestand_structure(db)
    target = "/ui/app" if return_to == "app" else "/ui/admin"
    prefix = "Migration abgeschlossen: " if changed else "Migration nicht ausgeführt: "
    return RedirectResponse(url=f"{target}?message={quote_plus(prefix + message)}", status_code=303)


@router.post("/admin/backfill-document-cabinets")
async def ui_backfill_document_cabinet_ids(
    return_to: str = Form("app"),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    updated, unresolved = _backfill_document_cabinet_ids(db)
    target = "/ui/app" if return_to == "app" else "/ui/admin"
    message = f"Document-Cabinet-Backfill abgeschlossen: {updated} gesetzt, {unresolved} ungelöst"
    return RedirectResponse(url=f"{target}?message={quote_plus(message)}", status_code=303)


@router.post("/admin/repair-bestand")
async def ui_repair_bestand(
    return_to: str = Form("app"),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    changed, migrate_message = _migrate_bestand_structure(db)
    updated, unresolved = _backfill_document_cabinet_ids(db)
    target = "/ui/app" if return_to == "app" else "/ui/admin"
    status_prefix = "Repair abgeschlossen" if changed else "Repair geprüft"
    message = f"{status_prefix}: {migrate_message}. Backfill: {updated} gesetzt, {unresolved} ungelöst"
    return RedirectResponse(url=f"{target}?message={quote_plus(message)}", status_code=303)


@router.post("/admin/cabinets")
async def ui_create_cabinet(
    cabinet_type_id: UUID = Form(...),
    name: str = Form(...),
    description: str = Form(""),
    order: int = Form(0),
    return_to: str = Form("admin"),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    cabinet_type = db.query(CabinetType).where(CabinetType.id == cabinet_type_id).first()
    if not cabinet_type:
        return RedirectResponse(url="/ui/admin?message=Cabinettyp+nicht+gefunden", status_code=303)
    cabinet = Cabinet(cabinet_type_id=cabinet_type_id, name=name.strip(), description=description.strip() or None, order=order)
    db.add(cabinet)
    db.commit()
    db.refresh(cabinet)
    if return_to == "app":
        return RedirectResponse(
            url=f"/ui/app?node_kind=cabinet&node_id={cabinet.id}&message={quote_plus(f'Cabinet {cabinet.name} erfolgreich angelegt')}",
            status_code=303,
        )
    return RedirectResponse(url="/ui/admin", status_code=303)


@router.post("/admin/registers")
async def ui_create_register(
    cabinet_id: UUID = Form(...),
    register_type_id: str = Form(""),
    name: str = Form(...),
    description: str = Form(""),
    order: int = Form(0),
    return_to: str = Form("admin"),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    resolved_register_type_id = UUID(register_type_id) if register_type_id else None
    register = Register(
        cabinet_id=cabinet_id,
        register_type_id=resolved_register_type_id,
        name=name.strip(),
        description=description.strip() or None,
        order=order,
    )
    db.add(register)
    db.commit()
    db.refresh(register)
    if return_to == "app":
        return RedirectResponse(
            url=f"/ui/app?node_kind=register&node_id={register.id}&message={quote_plus(f'Register {register.name} erfolgreich angelegt')}",
            status_code=303,
        )
    return RedirectResponse(url="/ui/admin", status_code=303)


@router.post("/admin/roles")
async def ui_create_role(
    name: str = Form(...),
    description: str = Form(""),
    permissions_json: str = Form("[]"),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    normalized_name = name.strip()
    if not normalized_name:
        return _admin_identity_redirect(identity_tab="roles", message="Rollenname fehlt")
    existing_role = db.query(Role).where(Role.name.ilike(normalized_name)).first()
    if existing_role:
        return _admin_identity_redirect(identity_tab="roles", selected_role_id=str(existing_role.id), message=f"Rolle {existing_role.name} existiert bereits")
    role = Role(
        name=normalized_name,
        description=description.strip() or None,
        permissions_json=permissions_json.strip() or "[]",
        is_system=False,
    )
    try:
        db.add(role)
        db.commit()
        db.refresh(role)
    except IntegrityError:
        db.rollback()
        return _admin_identity_redirect(identity_tab="roles", message="Rolle konnte nicht gespeichert werden, wahrscheinlich wegen doppeltem Namen")
    return _admin_identity_redirect(identity_tab="roles", selected_role_id=str(role.id), message=f"Rolle {role.name} gespeichert")


@router.post("/admin/users")
async def ui_create_user(
    email: str = Form(...),
    display_name: str = Form(...),
    status: str = Form("active"),
    role_ids: list[str] | None = Form(None),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    normalized_email = email.strip().lower()
    if not normalized_email or not display_name.strip():
        return _admin_identity_redirect(identity_tab="users", message="Anzeigename und E-Mail sind erforderlich")
    existing_user = db.query(User).where(User.email == normalized_email).first()
    if existing_user:
        return _admin_identity_redirect(identity_tab="users", selected_user_id=str(existing_user.id), message=f"Benutzer mit {existing_user.email} existiert bereits")
    user = User(
        email=normalized_email,
        display_name=display_name.strip(),
        status=status.strip() or "active",
        auth_source="local",
    )
    try:
        db.add(user)
        db.flush()

        selected_role_ids = role_ids or []
        for role_id in selected_role_ids:
            try:
                role_uuid = UUID(role_id)
            except ValueError:
                continue
            role = db.query(Role).where(Role.id == role_uuid).first()
            if role:
                db.add(UserRoleAssignment(user_id=user.id, role_id=role.id))

        db.commit()
        db.refresh(user)
    except IntegrityError:
        db.rollback()
        return _admin_identity_redirect(identity_tab="users", message="Benutzer konnte nicht gespeichert werden, wahrscheinlich wegen doppelter E-Mail")
    return _admin_identity_redirect(identity_tab="users", selected_user_id=str(user.id), message=f"Benutzer {user.display_name} gespeichert")


@router.post("/admin/users/{user_id}")
async def ui_update_user(
    user_id: UUID,
    email: str = Form(...),
    display_name: str = Form(...),
    status: str = Form("active"),
    role_ids: list[str] | None = Form(None),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    user = db.query(User).where(User.id == user_id).first()
    if not user:
        return _admin_identity_redirect(identity_tab="users", message="Benutzer nicht gefunden")

    normalized_email = email.strip().lower()
    if not normalized_email or not display_name.strip():
        return _admin_identity_redirect(identity_tab="users", selected_user_id=str(user.id), message="Anzeigename und E-Mail sind erforderlich")

    duplicate_user = db.query(User).where(User.email == normalized_email, User.id != user.id).first()
    if duplicate_user:
        return _admin_identity_redirect(identity_tab="users", selected_user_id=str(user.id), message=f"Die E-Mail {normalized_email} ist bereits vergeben")

    user.email = normalized_email
    user.display_name = display_name.strip()
    user.status = status.strip() or "active"
    try:
        db.add(user)

        db.query(UserRoleAssignment).where(UserRoleAssignment.user_id == user.id).delete()
        selected_role_ids = role_ids or []
        for role_id in selected_role_ids:
            try:
                role_uuid = UUID(role_id)
            except ValueError:
                continue
            role = db.query(Role).where(Role.id == role_uuid).first()
            if role:
                db.add(UserRoleAssignment(user_id=user.id, role_id=role.id))

        db.commit()
    except IntegrityError:
        db.rollback()
        return _admin_identity_redirect(identity_tab="users", selected_user_id=str(user.id), message="Benutzer konnte nicht aktualisiert werden")
    return _admin_identity_redirect(identity_tab="users", selected_user_id=str(user.id), message=f"Benutzer {user.display_name} aktualisiert")


@router.post("/admin/users/{user_id}/toggle-status")
async def ui_toggle_user_status(
    user_id: UUID,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    user = db.query(User).where(User.id == user_id).first()
    if not user:
        return _admin_identity_redirect(identity_tab="users", message="Benutzer nicht gefunden")
    user.status = "disabled" if user.status == "active" else "active"
    db.add(user)
    db.commit()
    return _admin_identity_redirect(identity_tab="users", selected_user_id=str(user.id), message=f"Status von {user.display_name} ist jetzt {user.status}")


@router.post("/admin/roles/{role_id}")
async def ui_update_role(
    role_id: UUID,
    name: str = Form(...),
    description: str = Form(""),
    permissions_json: str = Form("[]"),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    role = db.query(Role).where(Role.id == role_id).first()
    if not role:
        return _admin_identity_redirect(identity_tab="roles", message="Rolle nicht gefunden")

    normalized_name = name.strip()
    if not normalized_name:
        return _admin_identity_redirect(identity_tab="roles", selected_role_id=str(role.id), message="Rollenname fehlt")

    duplicate_role = db.query(Role).where(Role.name.ilike(normalized_name), Role.id != role.id).first()
    if duplicate_role:
        return _admin_identity_redirect(identity_tab="roles", selected_role_id=str(role.id), message=f"Die Rolle {normalized_name} existiert bereits")

    role.name = normalized_name
    role.description = description.strip() or None
    role.permissions_json = permissions_json.strip() or "[]"
    try:
        db.add(role)
        db.commit()
    except IntegrityError:
        db.rollback()
        return _admin_identity_redirect(identity_tab="roles", selected_role_id=str(role.id), message="Rolle konnte nicht aktualisiert werden")
    return _admin_identity_redirect(identity_tab="roles", selected_role_id=str(role.id), message=f"Rolle {role.name} aktualisiert")


@router.post("/admin/teams")
async def ui_create_team(
    name: str = Form(...),
    description: str = Form(""),
    member_user_ids: list[str] | None = Form(None),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    normalized_name = name.strip()
    if not normalized_name:
        return _admin_identity_redirect(identity_tab="teams", message="Teamname fehlt")
    existing_team = db.query(Team).where(Team.name.ilike(normalized_name)).first()
    if existing_team:
        return _admin_identity_redirect(identity_tab="teams", selected_role_id=str(existing_team.id), message=f"Team {existing_team.name} existiert bereits")

    team = Team(name=normalized_name, description=description.strip() or None)
    try:
        db.add(team)
        db.flush()
        for user_id in member_user_ids or []:
            try:
                user_uuid = UUID(user_id)
            except ValueError:
                continue
            user = db.query(User).where(User.id == user_uuid).first()
            if user:
                db.add(TeamMembership(team_id=team.id, user_id=user.id))
        db.commit()
        db.refresh(team)
    except IntegrityError:
        db.rollback()
        return _admin_identity_redirect(identity_tab="teams", message="Team konnte nicht gespeichert werden")
    return RedirectResponse(url=f"/ui/admin/identity?identity_tab=teams&selected_team_id={team.id}#identity-admin", status_code=303)


@router.post("/admin/teams/{team_id}")
async def ui_update_team(
    team_id: UUID,
    name: str = Form(...),
    description: str = Form(""),
    member_user_ids: list[str] | None = Form(None),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    team = db.query(Team).where(Team.id == team_id).first()
    if not team:
        return RedirectResponse(url="/ui/admin/identity?identity_tab=teams#identity-admin", status_code=303)

    normalized_name = name.strip()
    if not normalized_name:
        return RedirectResponse(url=f"/ui/admin/identity?identity_tab=teams&selected_team_id={team.id}&message={quote_plus('Teamname fehlt')}#identity-admin", status_code=303)
    duplicate_team = db.query(Team).where(Team.name.ilike(normalized_name), Team.id != team.id).first()
    if duplicate_team:
        return RedirectResponse(url=f"/ui/admin/identity?identity_tab=teams&selected_team_id={team.id}&message={quote_plus(f'Team {normalized_name} existiert bereits')}#identity-admin", status_code=303)

    team.name = normalized_name
    team.description = description.strip() or None
    try:
        db.add(team)
        db.query(TeamMembership).where(TeamMembership.team_id == team.id).delete()
        for user_id in member_user_ids or []:
            try:
                user_uuid = UUID(user_id)
            except ValueError:
                continue
            user = db.query(User).where(User.id == user_uuid).first()
            if user:
                db.add(TeamMembership(team_id=team.id, user_id=user.id))
        db.commit()
    except IntegrityError:
        db.rollback()
        return RedirectResponse(url=f"/ui/admin/identity?identity_tab=teams&selected_team_id={team.id}&message={quote_plus('Team konnte nicht aktualisiert werden')}#identity-admin", status_code=303)
    return RedirectResponse(url=f"/ui/admin/identity?identity_tab=teams&selected_team_id={team.id}&message={quote_plus(f'Team {team.name} aktualisiert')}#identity-admin", status_code=303)


@router.post("/admin/register-types")
async def ui_create_register_type(
    cabinet_type_id: UUID = Form(...),
    name: str = Form(...),
    description: str = Form(""),
    order: int = Form(0),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    cabinet_type = db.query(CabinetType).where(CabinetType.id == cabinet_type_id).first()
    if not cabinet_type:
        return _ui_redirect_with_message("/ui/admin?message=Cabinettyp+nicht+gefunden")
    register_type = RegisterType(
        cabinet_type_id=cabinet_type_id,
        name=name.strip(),
        description=description.strip() or None,
        order=order,
    )
    db.add(register_type)
    db.commit()
    return RedirectResponse(url="/ui/admin", status_code=303)


@router.post("/admin/document-types")
async def ui_create_document_type(
    target_kind: str = Form(...),
    register_id: str = Form(""),
    cabinet_id: str = Form(""),
    register_type_id: str = Form(""),
    cabinet_type_id: str = Form(""),
    name: str = Form(...),
    description: str = Form(""),
    icon: str = Form(""),
    order: int = Form(0),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    resolved_register_id = UUID(register_id) if register_id else None
    resolved_cabinet_id = UUID(cabinet_id) if cabinet_id else None
    resolved_register_type_id = UUID(register_type_id) if register_type_id else None
    resolved_cabinet_type_id = UUID(cabinet_type_id) if cabinet_type_id else None
    resolved_register_id = None
    resolved_cabinet_id = None

    if target_kind == "cabinet_type":
        if resolved_cabinet_type_id is None:
            return _ui_redirect_with_message("/ui/admin?message=Bitte+einen+gültigen+Cabinettyp+wählen")
        cabinet_type = db.query(CabinetType).where(CabinetType.id == resolved_cabinet_type_id).first()
        if not cabinet_type:
            return _ui_redirect_with_message("/ui/admin?message=Gewählter+Cabinettyp+nicht+gefunden")
        resolved_register_type_id = None
    elif target_kind == "register_type":
        if resolved_register_type_id is None:
            return _ui_redirect_with_message("/ui/admin?message=Bitte+einen+gültigen+Registertyp+wählen")
        register_type = db.query(RegisterType).where(RegisterType.id == resolved_register_type_id).first()
        if not register_type:
            return _ui_redirect_with_message("/ui/admin?message=Gewählter+Registertyp+nicht+gefunden")
        resolved_cabinet_type_id = None
    else:
        return _ui_redirect_with_message("/ui/admin?message=Ungültiger+Zieltyp+für+Dokumenttyp")

    document_type = DocumentType(
        register_id=resolved_register_id,
        cabinet_id=resolved_cabinet_id,
        register_type_id=resolved_register_type_id,
        cabinet_type_id=resolved_cabinet_type_id,
        name=name.strip(),
        description=description.strip() or None,
        icon=icon.strip() or None,
        order=order,
    )
    db.add(document_type)
    db.commit()
    db.refresh(document_type)
    return RedirectResponse(url=f"/ui/admin/document-types/{document_type.id}", status_code=303)


@router.post("/admin/metadata-fields")
async def ui_create_metadata_field(
    target_kind: str = Form(...),
    document_type_id: str = Form(""),
    cabinet_id: str = Form(""),
    register_id: str = Form(""),
    cabinet_type_id: str = Form(""),
    register_type_id: str = Form(""),
    name: str = Form(...),
    label: str = Form(""),
    field_type: str = Form(...),
    description: str = Form(""),
    placeholder: str = Form(""),
    default_value: str = Form(""),
    width: str = Form("half"),
    is_required: str | None = Form(None),
    is_unique: str | None = Form(None),
    order: int = Form(0),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    resolved_document_type_id = UUID(document_type_id) if document_type_id else None
    resolved_cabinet_id = UUID(cabinet_id) if cabinet_id else None
    resolved_register_id = UUID(register_id) if register_id else None
    resolved_cabinet_type_id = UUID(cabinet_type_id) if cabinet_type_id else None
    resolved_register_type_id = UUID(register_type_id) if register_type_id else None

    if target_kind == "cabinet":
        resolved_document_type_id = None
        resolved_register_id = None
        resolved_cabinet_type_id = None
        resolved_register_type_id = None
    elif target_kind == "register":
        resolved_document_type_id = None
        resolved_cabinet_id = None
        resolved_cabinet_type_id = None
        resolved_register_type_id = None
    elif target_kind == "cabinet_type":
        resolved_document_type_id = None
        resolved_cabinet_id = None
        resolved_register_id = None
        resolved_register_type_id = None
    elif target_kind == "register_type":
        resolved_document_type_id = None
        resolved_cabinet_id = None
        resolved_register_id = None
        resolved_cabinet_type_id = None
    else:
        resolved_cabinet_id = None
        resolved_register_id = None
        resolved_cabinet_type_id = None
        resolved_register_type_id = None

    field = MetadataField(
        document_type_id=resolved_document_type_id,
        cabinet_id=resolved_cabinet_id,
        register_id=resolved_register_id,
        cabinet_type_id=resolved_cabinet_type_id,
        register_type_id=resolved_register_type_id,
        name=name.strip(),
        label=label.strip() or name.strip(),
        field_type=field_type,
        description=description.strip() or None,
        placeholder=placeholder.strip() or None,
        default_value=default_value.strip() or None,
        width=width,
        is_required=bool(is_required),
        is_unique=bool(is_unique),
        order=order,
    )
    db.add(field)
    db.commit()
    redirect_target = "/ui/admin"
    return RedirectResponse(url=redirect_target, status_code=303)


@router.post("/admin/document-types/{document_type_id}/seed-invoice")
async def ui_seed_invoice_fields(
    document_type_id: UUID,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    document_type = db.query(DocumentType).where(DocumentType.id == document_type_id).first()
    if not document_type:
        return _ui_redirect_with_message("/ui/admin?message=Dokumenttyp+nicht+gefunden")

    existing_names = {field.name for field in document_type.fields}
    for field in _invoice_default_fields(document_type.id):
        if field.name in existing_names:
            continue
        db.add(field)

    db.commit()
    return _ui_redirect_with_message(f"/ui/admin/document-types/{document_type.id}")


@router.post("/admin/cabinet-types/{cabinet_type_id}/delete")
async def ui_delete_cabinet_type(
    cabinet_type_id: UUID,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    cabinet_type = db.query(CabinetType).where(CabinetType.id == cabinet_type_id).first()
    if not cabinet_type:
        return _ui_redirect_with_message("/ui/admin?message=Cabinettyp+nicht+gefunden")
    name = cabinet_type.name
    db.delete(cabinet_type)
    db.commit()
    return _ui_redirect_with_message(f"/ui/admin?message={quote_plus(f'Cabinettyp {name} gelöscht')}")


@router.post("/admin/register-types/{register_type_id}/delete")
async def ui_delete_register_type(
    register_type_id: UUID,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    register_type = db.query(RegisterType).where(RegisterType.id == register_type_id).first()
    if not register_type:
        return _ui_redirect_with_message("/ui/admin?message=Registertyp+nicht+gefunden")
    name = register_type.name
    db.delete(register_type)
    db.commit()
    return _ui_redirect_with_message(f"/ui/admin?message={quote_plus(f'Registertyp {name} gelöscht')}")


@router.post("/admin/document-types/{document_type_id}/delete")
async def ui_delete_document_type(
    document_type_id: UUID,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    document_type = db.query(DocumentType).where(DocumentType.id == document_type_id).first()
    if not document_type:
        return _ui_redirect_with_message("/ui/admin?message=Dokumenttyp+nicht+gefunden")
    name = document_type.name
    db.delete(document_type)
    db.commit()
    return _ui_redirect_with_message(f"/ui/admin?message={quote_plus(f'Dokumenttyp {name} gelöscht')}")


@router.post("/admin/metadata-fields/{metadata_field_id}/delete")
async def ui_delete_metadata_field(
    metadata_field_id: UUID,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    field = db.query(MetadataField).where(MetadataField.id == metadata_field_id).first()
    if not field:
        return _ui_redirect_with_message("/ui/admin?message=Metadatenfeld+nicht+gefunden")
    name = field.name
    doc_type_id = field.document_type_id
    db.delete(field)
    db.commit()
    if doc_type_id:
        return _ui_redirect_with_message(f"/ui/admin/document-types/{doc_type_id}?message={quote_plus(f'Metadatenfeld {name} gelöscht')}")
    return _ui_redirect_with_message(f"/ui/admin?message={quote_plus(f'Metadatenfeld {name} gelöscht')}")


@router.post("/admin/metadata-fields/{metadata_field_id}")
async def ui_update_metadata_field(
    metadata_field_id: UUID,
    name: str = Form(...),
    label: str = Form(""),
    field_type: str = Form(...),
    description: str = Form(""),
    placeholder: str = Form(""),
    default_value: str = Form(""),
    width: str = Form("half"),
    is_required: str | None = Form(None),
    is_unique: str | None = Form(None),
    order: int = Form(0),
    selected_definition_kind: str = Form(""),
    selected_definition_id: str = Form(""),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    field = db.query(MetadataField).where(MetadataField.id == metadata_field_id).first()
    if not field:
        return _ui_redirect_with_message("/ui/admin?message=Metadatenfeld+nicht+gefunden")

    field.name = name.strip()
    field.label = label.strip() or name.strip()
    field.field_type = field_type
    field.description = description.strip() or None
    field.placeholder = placeholder.strip() or None
    field.default_value = default_value.strip() or None
    field.width = width
    field.is_required = bool(is_required)
    field.is_unique = bool(is_unique)
    field.order = order
    db.add(field)
    db.commit()
    redirect_url = "/ui/admin"
    if selected_definition_kind and selected_definition_id:
        redirect_url += (
            f"?selected_definition_kind={quote_plus(selected_definition_kind)}"
            f"&selected_definition_id={quote_plus(selected_definition_id)}"
            f"&selected_metadata_field_id={metadata_field_id}"
            f"&message={quote_plus('Metadatenfeld aktualisiert. Bestehende JSON-Werte bleiben unverändert erhalten.')}"
        )
    else:
        redirect_url += f"?selected_metadata_field_id={metadata_field_id}&message={quote_plus('Metadatenfeld aktualisiert. Bestehende JSON-Werte bleiben unverändert erhalten.')}"
    return _ui_redirect_with_message(redirect_url)


def _render_queue_panel(title: str, subtitle: str, jobs: list[Any], kind: str) -> str:
    if not jobs:
        return f"<div class='panel'><h2>{_escape(title)}</h2><p class='muted'>{_escape(subtitle)}</p><div class='def-empty'>Keine Einträge vorhanden.</div></div>"

    rows: list[str] = []
    for job in jobs:
        document = getattr(job, 'document', None)
        document_name = getattr(document, 'title', None) or getattr(document, 'name', None) or 'Unbekanntes Dokument'
        status = str(getattr(job, 'status', 'unbekannt'))
        error_message = getattr(job, 'error_message', None)
        meta_parts = [
            f"Status: {_escape(status)}",
            f"Erstellt: {_escape(str(getattr(job, 'created_at', '')))}",
        ]
        started_at = getattr(job, 'started_at', None)
        finished_at = getattr(job, 'finished_at', None)
        if started_at:
            meta_parts.append(f"Start: {_escape(str(started_at))}")
        if finished_at:
            meta_parts.append(f"Ende: {_escape(str(finished_at))}")
        diagnostics_html = ""
        if kind == 'index':
            attempts = getattr(job, 'attempts', None)
            if attempts is not None:
                meta_parts.append(f"Versuche: {_escape(str(attempts))}")
            if document:
                diag_parts = [
                    f"Indexstatus: {_escape(str(getattr(document, 'index_status', '')))}",
                    f"Engine: {_escape(str(getattr(document, 'index_engine', '') or 'unbekannt'))}",
                    f"Text: {_escape(str(getattr(document, 'extracted_text_length', 0) or 0))} Zeichen",
                    f"Indexed at: {_escape(str(getattr(document, 'indexed_at', '') or ''))}",
                ]
                diagnostics_html = f"<div class='queue-diagnostics'>{' · '.join(part for part in diag_parts if part and not part.endswith(': '))}</div>"
                if getattr(document, 'index_error', None):
                    diagnostics_html += f"<div class='queue-error'>{_escape(str(document.index_error))}</div>"
        rows.append(
            "<div class='queue-item'>"
            f"<div class='queue-head'><div><strong>{_escape(document_name)}</strong><div class='queue-meta'>{' · '.join(meta_parts)}</div></div><span class='queue-status queue-status-{_escape(status.lower())}'>{_escape(status)}</span></div>"
            + diagnostics_html
            + (f"<div class='queue-error'>{_escape(error_message)}</div>" if error_message else "")
            + (f"<div class='queue-actions'><a class='pill' href='/ui/app/documents/{document.id}'>Dokument öffnen</a><a class='pill' href='/ui/admin/documents/{document.id}/extracted-text' target='_blank' rel='noopener noreferrer'>Extrahierten Text anzeigen</a><form method='post' action='/ui/admin/documents/{document.id}/reindex' style='display:inline;'><button class='pill' type='submit'>Indexjob wiederholen</button></form></div>" if document and kind == 'index' else (f"<div class='queue-actions'><a class='pill' href='/ui/app/documents/{document.id}'>Dokument öffnen</a></div>" if document else ""))
            + "</div>"
        )

    return f"<div class='panel'><h2>{_escape(title)}</h2><p class='muted'>{_escape(subtitle)}</p><div class='queue-list'>{''.join(rows)}</div></div>"



def _render_admin_queues_page(*, preview_jobs: list[PreviewJob], index_jobs: list[IndexJob]) -> str:
    preview_queue_html = _render_queue_panel('Preview Queue', 'Livebild der letzten Preview-Renderjobs inklusive Fehlern.', preview_jobs, 'preview')
    index_queue_html = _render_queue_panel('Index Queue', 'Livebild der letzten Indexjobs inklusive Fehlern und Wiederholungen.', index_jobs, 'index')
    return f"""
<!doctype html>
<html lang="de">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Archiva Admin Queues</title>
  <link rel="icon" type="image/svg+xml" href="/assets/archiva-favicon.svg">
  <style>
    :root {{ color-scheme: dark; --bg: #0b1020; --panel: #121933; --panel-deep:#0f1630; --text: #eef2ff; --muted: #a8b2d1; --accent:#4f8cff; --accent-2:#4dd4ff; --shadow:0 18px 48px rgba(0,0,0,0.28); }}
    * {{ box-sizing:border-box; }}
    body {{ margin:0; font-family: Inter, ui-sans-serif, system-ui, sans-serif; background: radial-gradient(circle at top left, rgba(77,212,255,0.08), transparent 30%), var(--bg); color:var(--text); }}
    a {{ color:var(--accent-2); text-decoration:none; }}
    .page {{ padding:14px 16px; max-width:1400px; margin:0 auto; }}
    .panel {{ background: linear-gradient(180deg, rgba(18,25,51,0.96), rgba(15,22,48,0.96)); border:1px solid rgba(77,212,255,0.10); border-radius:18px; padding:16px; box-shadow:var(--shadow); }}
    .hero {{ margin-bottom:16px; }}
    .eyebrow {{ letter-spacing:.12em; text-transform:uppercase; font-size:.78rem; color: var(--accent-2); font-weight:700; }}
    .hero h1 {{ margin:4px 0 6px; font-size:1.7rem; }}
    .hero p {{ margin:0; color:var(--muted); max-width:64ch; line-height:1.45; }}
    .pillbar {{ display:flex; gap:8px; flex-wrap:wrap; margin-top:12px; }}
    .pill {{ background: rgba(255,255,255,0.04); border: 1px solid rgba(77,212,255,0.12); border-radius: 999px; padding: 7px 11px; color: var(--text); font-size:.92rem; }}
    .stack {{ display:grid; gap:16px; }}
    .muted {{ color:var(--muted); }}
    .def-empty {{ color:var(--muted); font-style:italic; font-size:.9rem; padding:10px 0; }}
    .queue-list {{ display:grid; gap:10px; }}
    .queue-item {{ padding:14px; border-radius:16px; border:1px solid rgba(77,212,255,0.10); background:linear-gradient(180deg, rgba(255,255,255,0.035), rgba(255,255,255,0.02)); box-shadow: inset 0 1px 0 rgba(255,255,255,0.03); }}
    .queue-head {{ display:flex; justify-content:space-between; gap:12px; align-items:flex-start; }}
    .queue-head strong {{ font-size:1rem; line-height:1.35; }}
    .queue-meta {{ margin-top:8px; color:var(--muted); font-size:.85rem; line-height:1.5; }}
    .queue-status {{ display:inline-flex; align-items:center; justify-content:center; border-radius:999px; padding:5px 10px; font-size:.78rem; border:1px solid rgba(77,212,255,0.10); background:rgba(255,255,255,0.04); text-transform:uppercase; letter-spacing:.08em; }}
    .queue-status-pending {{ color:#fcd34d; border-color:rgba(252,211,77,0.24); background:rgba(252,211,77,0.10); }}
    .queue-status-processing {{ color:#7dd3fc; border-color:rgba(125,211,252,0.24); background:rgba(125,211,252,0.10); }}
    .queue-status-completed {{ color:#86efac; border-color:rgba(134,239,172,0.24); background:rgba(134,239,172,0.10); }}
    .queue-status-failed {{ color:#fca5a5; border-color:rgba(252,165,165,0.24); background:rgba(252,165,165,0.10); }}
    .queue-error {{ margin-top:12px; padding:10px 12px; border-radius:12px; background:rgba(255,123,123,0.10); border:1px solid rgba(255,123,123,0.24); color:#ffd0d0; font-size:.88rem; }}
    .queue-diagnostics {{ margin-top:12px; color:var(--muted); font-size:.84rem; line-height:1.5; padding-top:10px; border-top:1px solid rgba(255,255,255,0.06); }}
    .queue-actions {{ margin-top:12px; display:flex; gap:8px; flex-wrap:wrap; }}
  </style>
</head>
<body>
  <div class="page">
    <section class="panel hero">
      <div class="eyebrow">Admin Monitoring</div>
      <h1>Queues & Logs</h1>
      <p>Die letzten Preview- und Indexjobs, kompakt und ruhig dargestellt, mit Reindex und Textprüfung direkt am jeweiligen Eintrag.</p>
      <div class="pillbar">
        <a class="pill" href="/ui/admin">Zurück zum Admin</a>
        <a class="pill" href="/ui/app">Zur ECM-App</a>
      </div>
    </section>
    <section class="stack">
      {preview_queue_html}
      {index_queue_html}
    </section>
  </div>
</body>
</html>
"""


def _render_admin_identity_page(
    *,
    users: list[User],
    roles: list[Role],
    teams: list[Team],
    identity_tab: str,
    selected_user_id: str | None,
    selected_role_id: str | None,
    selected_team_id: str | None,
    message: str | None,
) -> str:
    identity_html = _render_identity_panel(
        users,
        roles,
        teams,
        identity_tab=identity_tab,
        selected_user_id=selected_user_id,
        selected_role_id=selected_role_id,
        selected_team_id=selected_team_id,
        message=message,
    )
    return f"""
<!doctype html>
<html lang="de">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Archiva Admin Identity</title>
  <link rel="icon" type="image/svg+xml" href="/assets/archiva-favicon.svg">
  <style>
    :root {{ color-scheme: dark; --bg: #0b1020; --panel: #121933; --panel-deep:#0f1630; --text: #eef2ff; --muted: #a8b2d1; --accent:#4f8cff; --accent-2:#4dd4ff; --shadow:0 18px 48px rgba(0,0,0,0.28); }}
    * {{ box-sizing:border-box; }}
    body {{ margin:0; font-family: Inter, ui-sans-serif, system-ui, sans-serif; background: radial-gradient(circle at top left, rgba(77,212,255,0.08), transparent 30%), var(--bg); color:var(--text); }}
    a {{ color:var(--accent-2); text-decoration:none; }}
    .page {{ padding:14px 16px; max-width:1400px; margin:0 auto; }}
    .panel {{ background: linear-gradient(180deg, rgba(18,25,51,0.96), rgba(15,22,48,0.96)); border:1px solid rgba(77,212,255,0.10); border-radius:18px; padding:16px; box-shadow:var(--shadow); }}
    .hero {{ margin-bottom:16px; }}
    .eyebrow {{ letter-spacing:.12em; text-transform:uppercase; font-size:.78rem; color: var(--accent-2); font-weight:700; }}
    .hero h1 {{ margin:4px 0 6px; font-size:1.7rem; }}
    .hero p {{ margin:0; color:var(--muted); max-width:64ch; line-height:1.45; }}
    .pillbar {{ display:flex; gap:8px; flex-wrap:wrap; margin-top:12px; }}
    .pill {{ background: rgba(255,255,255,0.04); border: 1px solid rgba(77,212,255,0.12); border-radius: 999px; padding: 7px 11px; color: var(--text); font-size:.92rem; }}
    .stack {{ display:grid; gap:16px; }}
    .field-grid {{ display:grid; grid-template-columns: repeat(2, minmax(0,1fr)); gap:12px; }}
    .field {{ display:grid; gap:6px; }}
    .field.full {{ grid-column: 1 / -1; }}
    .checkbox-group {{ display:grid; gap:10px; padding:14px; border:1px solid rgba(77,212,255,0.08); border-radius:16px; background:var(--panel-deep); }}
    .checkbox-item {{ display:flex; gap:10px; align-items:center; font-weight:400; padding:8px 10px; border-radius:12px; background:rgba(255,255,255,0.02); }}
    label {{ font-weight:600; font-size:0.95rem; }}
    input, textarea, select {{ width:100%; border-radius:14px; border:1px solid rgba(77,212,255,0.10); background:var(--panel-deep); color:var(--text); padding:12px; font:inherit; }}
    textarea {{ min-height:92px; resize:vertical; }}
    .actions {{ display:flex; gap:10px; flex-wrap:wrap; margin-top:16px; align-items:flex-start; }}
    button {{ border:none; border-radius:999px; padding:10px 14px; font:inherit; cursor:pointer; max-width:100%; }}
    .primary {{ background: linear-gradient(135deg, var(--accent), var(--accent-2)); color: white; box-shadow: 0 8px 24px rgba(77,212,255,0.22); }}
    .badge {{ display:inline-block; margin-right:8px; margin-bottom:6px; padding:4px 8px; border-radius:999px; background:rgba(77,212,255,0.16); color:var(--accent-2); font-size:0.85rem; }}
    .def-detail-card {{ background:linear-gradient(135deg, rgba(176,126,255,0.07), rgba(77,212,255,0.04)); border:1px solid rgba(176,126,255,0.20); border-radius:16px; padding:14px; }}
    .def-detail-card h3 {{ margin:0 0 10px; font-size:1rem; color:#b07ae6; }}
    .def-detail-row {{ display:grid; grid-template-columns:140px 1fr; gap:10px; padding:7px 0; border-bottom:1px solid rgba(255,255,255,.06); font-size:.92rem; }}
    .def-detail-row:last-child {{ border-bottom:none; }}
    .def-detail-key {{ color:var(--muted); font-weight:600; }}
    .def-empty {{ color:var(--muted); font-style:italic; font-size:.9rem; padding:10px 0; }}
    .muted {{ color:var(--muted); }}
  </style>
</head>
<body>
  <div class="page">
    <section class="panel hero">
      <div class="eyebrow">Admin Identity</div>
      <h1>Identity & Rollen</h1>
      <p>Native Benutzer- und Rollenverwaltung für Archiva, bewusst getrennt vom Haupt-Admin und vorbereitet für spätere Entra-ID-Anbindung.</p>
      <div class="pillbar">
        <a class="pill" href="/ui/admin">Zurück zum Admin</a>
        <a class="pill" href="/ui/admin/queues">Queues & Logs</a>
        <a class="pill" href="/ui/app">Zur ECM-App</a>
      </div>
    </section>
    <section class="stack" id="identity-admin">
      {identity_html}
    </section>
  </div>
</body>
</html>
"""



def _render_admin_page(
    *,
    cabinet_types: list[CabinetType],
    cabinets: list[Cabinet],
    document_types: list[DocumentType],
    recent_documents: list[Document],
    selected_document_type: DocumentType | None,
    preview_jobs: list[PreviewJob],
    index_jobs: list[IndexJob],
    selected_definition_kind: str | None = None,
    selected_definition_id: str | None = None,
    selected_metadata_field_id: str | None = None,
) -> str:
    structure_html = _render_structure(cabinets)
    definition_structure_html = _render_definition_structure(
        cabinet_types,
        selected_definition_kind=selected_definition_kind,
        selected_definition_id=selected_definition_id,
    )
    definition_detail_html = _render_definition_detail(
        cabinet_types=cabinet_types,
        cabinets=cabinets,
        selected_definition_kind=selected_definition_kind,
        selected_definition_id=selected_definition_id,
    )
    cabinet_type_list_html = "".join(
        f'<li><strong>{_escape(cabinet_type.name)}</strong><div class="small">{_escape(cabinet_type.description or "Ohne Beschreibung")}</div></li>'
        for cabinet_type in cabinet_types
    ) or "<li>Keine Cabinettypen vorhanden.</li>"
    type_list_html = "".join(
        f'<li><a href="/ui/admin/document-types/{doc_type.id}">{doc_type.name}</a></li>'
        for doc_type in document_types
    ) or "<li>Keine Dokumenttypen vorhanden.</li>"
    recent_documents_html = _render_recent_documents(recent_documents)
    admin_summary_html = _render_admin_summary(selected_document_type)
    admin_create_html = _render_admin_create_panel(
        cabinet_types,
        cabinets,
        selected_document_type,
        selected_definition_kind=selected_definition_kind,
        selected_definition_id=selected_definition_id,
        selected_metadata_field_id=selected_metadata_field_id,
    )
    indexing_status = indexing_runtime_status()
    indexing_status_html = f"<div class='status-chip'><span>OCR / Index</span><span class='service-badge'><span class='status-dot'></span> {'bereit' if any(item.get('available') for item in indexing_status.get('ocr', {}).values() if isinstance(item, dict)) else 'Basisbetrieb'}</span></div>"
    tooltip_hint = '<span class="tooltip" tabindex="0">?<span class="tooltip-bubble">Mehr Kontext bei Hover oder Fokus.</span></span>'

    return f"""
<!doctype html>
<html lang="de">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Archiva Admin</title>
  <link rel="icon" type="image/svg+xml" href="/assets/archiva-favicon.svg">
  <style>
    :root {{
      color-scheme: dark;
      --bg: #0b1020;
      --panel: #121933;
      --panel-soft: #1a2345;
      --panel-deep:#0f1630;
      --text: #eef2ff;
      --muted: #a8b2d1;
      --accent: #4f8cff;
      --accent-2:#4dd4ff;
      --border: #2d3b69;
      --glow: rgba(77,212,255,0.18);
      --shadow: 0 18px 48px rgba(0,0,0,0.28);
    }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; font-family: Inter, ui-sans-serif, system-ui, sans-serif; background: radial-gradient(circle at top left, rgba(77,212,255,0.08), transparent 30%), var(--bg); color: var(--text); }}
    a {{ color: var(--accent-2); text-decoration: none; }}
    a:hover {{ text-decoration: none; }}
    .page {{ padding: 14px 16px; max-width: 1760px; margin: 0 auto; }}
    .hero {{ display: grid; grid-template-columns: minmax(0, 1.15fr) minmax(280px, 0.85fr); gap: 16px; align-items: stretch; margin-bottom: 16px; }}
    .hero-main, .hero-side {{ position:relative; overflow:hidden; }}
    .hero-main::before, .hero-side::before {{ content:""; position:absolute; inset:0; background: linear-gradient(135deg, rgba(79,140,255,0.14), rgba(77,212,255,0.06) 45%, transparent 80%); pointer-events:none; }}
    .hero-brand {{ display:flex; gap:14px; align-items:center; position:relative; z-index:1; }}
    .brand-mark {{ width:54px; height:54px; border-radius:18px; display:grid; place-items:center; background: linear-gradient(135deg, rgba(79,140,255,0.28), rgba(77,212,255,0.18)); border:1px solid rgba(77,212,255,0.25); }}
    .brand-mark svg {{ width:34px; height:34px; }}
    .eyebrow {{ letter-spacing:.12em; text-transform:uppercase; font-size:.78rem; color: var(--accent-2); font-weight:700; }}
    .hero h1 {{ margin: 4px 0 6px; font-size: 1.7rem; letter-spacing: -0.02em; }}
    .hero p {{ margin: 0; color: var(--muted); max-width: 62ch; line-height:1.45; position:relative; z-index:1; font-size:.95rem; }}
    .pillbar {{ display: flex; gap: 8px; flex-wrap: wrap; margin-top: 12px; position:relative; z-index:1; }}
    .pill {{ background: rgba(255,255,255,0.04); border: 1px solid rgba(77,212,255,0.12); border-radius: 999px; padding: 7px 11px; color: var(--text); font-size:.92rem; }}
    .grid {{ display: grid; grid-template-columns: 320px minmax(0, 1fr) 360px; gap: 16px; align-items:start; }}
    .panel {{ background: linear-gradient(180deg, rgba(18,25,51,0.96), rgba(15,22,48,0.96)); border: 1px solid rgba(77,212,255,0.10); border-radius: 18px; padding: 16px; box-shadow: var(--shadow); }}
    .panel h2, .panel h3 {{ margin-top: 0; }}
    .stack {{ display: grid; gap: 20px; }}
    .cols {{ display: grid; grid-template-columns: 1.1fr 0.9fr; gap: 20px; }}
    .tree ul {{ list-style: none; padding-left: 18px; margin: 10px 0; }}
    .tree li {{ margin: 6px 0; color: var(--muted); }}
    .def-tree {{ display: grid; gap: 6px; }}
    .def-node {{ display:flex; align-items:flex-start; justify-content:space-between; gap:8px; padding:9px 12px; border-radius:14px; border:1px solid rgba(77,212,255,0.08); background:rgba(255,255,255,0.02); position:relative; transition: border-color .15s, background .15s; cursor:pointer; user-select:none; }}
    .def-node:hover {{ border-color:rgba(77,212,255,.32); background:rgba(77,212,255,0.05); }}
    .def-node.active {{ border-color:rgba(176,126,255,.52); box-shadow:0 0 0 4px var(--glow-purple); background:rgba(176,126,255,0.10); }}
    .def-node.depth-1 {{ margin-left:16px; }}
    .def-node.depth-2 {{ margin-left:32px; }}
    .def-node.depth-3 {{ margin-left:48px; }}
    .def-node-label {{ flex:1; min-width:0; word-break:break-word; }}
    .def-node-label a {{ color:var(--text); }}
    .def-node-label a:hover {{ color:var(--accent-2); }}
    .def-node-meta {{ font-size:.82rem; color:var(--muted); }}
    .def-node-actions {{ display:flex; gap:6px; align-items:center; flex-shrink:0; }}
    .def-action-btn {{ border:none; border-radius:10px; padding:5px 8px; background:rgba(255,255,255,0.03); color:var(--muted); cursor:pointer; font-size:.82rem; transition: background .12s; }}
    .def-action-btn:hover {{ background:rgba(77,212,255,0.14); color:var(--accent-2); }}
    .def-action-btn.danger:hover {{ background:rgba(255,123,123,0.14); color:#ff7b7b; }}
    .def-context-menu {{ position:absolute; z-index:200; min-width:200px; padding:6px; border-radius:14px; border:1px solid rgba(77,212,255,0.30); background:#111a36; box-shadow:0 18px 48px rgba(0,0,0,.45); display:none; }}
    .def-context-menu.open {{ display:block; }}
    .def-context-menu button, .def-context-menu a {{ width:100%; display:flex; align-items:center; gap:8px; text-align:left; background:rgba(255,255,255,0.02); color:var(--text); border:none; border-radius:10px; padding:9px 12px; cursor:pointer; font:inherit; font-size:.9rem; }}
    .def-context-menu button:hover, .def-context-menu a:hover {{ background:rgba(77,212,255,0.10); text-decoration:none; }}
    .def-context-menu button.danger-item:hover {{ background:rgba(255,123,123,0.12); color:#ff7b7b; }}
    .def-detail {{ display:grid; gap:14px; }}
    .def-detail-card {{ background:linear-gradient(135deg, rgba(176,126,255,0.07), rgba(77,212,255,0.04)); border:1px solid rgba(176,126,255,0.20); border-radius:16px; padding:14px; }}
    .def-detail-card h3 {{ margin:0 0 10px; font-size:1rem; color:var(--accent-3, #b07ae6); }}
    .def-detail-row {{ display:grid; grid-template-columns:140px 1fr; gap:10px; padding:7px 0; border-bottom:1px solid rgba(255,255,255,.06); font-size:.92rem; }}
    .def-detail-row:last-child {{ border-bottom:none; }}
    .def-detail-key {{ color:var(--muted); font-weight:600; }}
    .def-field-item {{ display:flex; justify-content:space-between; align-items:center; gap:10px; padding:8px 10px; border:1px solid rgba(77,212,255,0.08); border-radius:12px; background:rgba(255,255,255,0.02); margin-bottom:6px; font-size:.9rem; }}
    .def-field-item:last-child {{ margin-bottom:0; }}
    .def-field-name {{ font-weight:600; }}
    .def-field-type {{ color:var(--muted); font-size:.82rem; }}
    .def-empty {{ color:var(--muted); font-style:italic; font-size:.9rem; padding:10px 0; }}
    .def-actions {{ display:flex; flex-wrap:wrap; gap:8px; margin-top:10px; }}
    .muted {{ color: var(--muted); }}
    .badge {{ display: inline-block; margin-right: 8px; margin-bottom: 6px; padding: 4px 8px; border-radius: 999px; background: rgba(77,212,255,0.16); color: var(--accent-2); font-size: 0.85rem; }}
    .field-grid {{ display: grid; grid-template-columns: repeat(2, minmax(0,1fr)); gap: 12px; }}
    .field {{ display: grid; gap: 6px; }}
    .field.full {{ grid-column: 1 / -1; }}
    label {{ font-weight: 600; font-size: 0.95rem; }}
    input, textarea, select {{ width: 100%; border-radius: 14px; border: 1px solid rgba(77,212,255,0.10); background: var(--panel-deep); color: var(--text); padding: 12px; font: inherit; }}
    textarea {{ min-height: 92px; resize: vertical; }}
    .actions {{ display: flex; gap: 10px; flex-wrap: wrap; margin-top: 16px; align-items:flex-start; }}
    button {{ border: none; border-radius: 999px; padding: 10px 14px; font: inherit; cursor: pointer; max-width:100%; }}
    .primary {{ background: linear-gradient(135deg, var(--accent), var(--accent-2)); color: white; box-shadow: 0 8px 24px rgba(77,212,255,0.22); }}
    .admin-create-toggle {{ flex:0 1 auto; white-space:normal; line-height:1.2; }}
    .admin-create-toggle.is-active {{ box-shadow:0 0 0 4px rgba(77,212,255,0.16); }}
    .small {{ font-size: 0.88rem; color: var(--muted); }}
    .tooltip {{ position:relative; display:inline-flex; align-items:center; justify-content:center; width:18px; height:18px; margin-left:8px; border-radius:999px; border:1px solid rgba(77,212,255,0.24); color:var(--accent-2); font-size:.78rem; font-weight:700; cursor:help; vertical-align:middle; }}
    .tooltip-bubble {{ position:absolute; left:50%; bottom:calc(100% + 10px); transform:translateX(-50%); min-width:220px; max-width:320px; padding:10px 12px; border-radius:12px; background:#0f1630; border:1px solid rgba(77,212,255,0.24); box-shadow:0 18px 48px rgba(0,0,0,0.35); color:var(--text); font-size:.84rem; line-height:1.45; opacity:0; pointer-events:none; transition:opacity .14s ease, transform .14s ease; z-index:30; }}
    .tooltip:hover .tooltip-bubble, .tooltip:focus .tooltip-bubble, .tooltip:focus-within .tooltip-bubble {{ opacity:1; transform:translateX(-50%) translateY(-2px); }}
    .status-stack {{ display:grid; gap:8px; position:relative; z-index:1; }}
    .status-chip {{ display:flex; align-items:center; justify-content:space-between; gap:8px; padding:8px 10px; border-radius:12px; background: rgba(255,255,255,0.03); border:1px solid rgba(77,212,255,0.10); font-size:.9rem; }}
    .status-dot {{ width:10px; height:10px; border-radius:999px; background: #6ee7b7; box-shadow: 0 0 12px rgba(110,231,183,0.5); display:inline-block; }}
    .queue-list {{ display:grid; gap:10px; }}
    .queue-item {{ padding:12px; border-radius:14px; border:1px solid rgba(77,212,255,0.10); background:rgba(255,255,255,0.03); }}
    .queue-head {{ display:flex; justify-content:space-between; gap:10px; align-items:flex-start; }}
    .queue-meta {{ margin-top:6px; color:var(--muted); font-size:.85rem; line-height:1.4; }}
    .queue-status {{ display:inline-flex; align-items:center; justify-content:center; border-radius:999px; padding:5px 10px; font-size:.8rem; border:1px solid rgba(77,212,255,0.10); background:rgba(255,255,255,0.04); text-transform:uppercase; }}
    .queue-status-pending {{ color:#fcd34d; border-color:rgba(252,211,77,0.24); background:rgba(252,211,77,0.10); }}
    .queue-status-processing {{ color:#7dd3fc; border-color:rgba(125,211,252,0.24); background:rgba(125,211,252,0.10); }}
    .queue-status-completed {{ color:#86efac; border-color:rgba(134,239,172,0.24); background:rgba(134,239,172,0.10); }}
    .queue-status-failed {{ color:#fca5a5; border-color:rgba(252,165,165,0.24); background:rgba(252,165,165,0.10); }}
    .queue-error {{ margin-top:10px; padding:10px 12px; border-radius:12px; background:rgba(255,123,123,0.10); border:1px solid rgba(255,123,123,0.24); color:#ffd0d0; font-size:.88rem; }}
    .queue-actions {{ margin-top:10px; display:flex; gap:8px; flex-wrap:wrap; }}
    @media (max-width: 1400px) {{ .grid {{ grid-template-columns: 280px minmax(0, 1fr); }} .admin-detail-column {{ grid-column: 1 / -1; }} }}
    @media (max-width: 1100px) {{ .grid, .hero, .cols {{ grid-template-columns: 1fr; }} }}
  </style>
</head>
<body>
  <div class="page">
    <section class="hero">
      <div class="panel hero-main">
        <div class="hero-brand">
          <div class="brand-mark">
            <img src="/assets/archiva-logo-flow.svg" alt="Archiva Logo" style="width:34px;height:34px;display:block;">
          </div>
          <div>
            <div class="eyebrow">Structure, Schema, Control</div>
            <h1>Archiva Admin</h1>
          </div>
        </div>
        <p>Definitionen und konkrete Archivstruktur.{tooltip_hint.replace('Mehr Kontext bei Hover oder Fokus.', 'Im Admin definierst du Cabinettypen, Registertypen und Dokumenttypen. Konkrete Cabinets, Register und Dokumente leben davon getrennt in der App.')}</p>
        <div class="pillbar">
          <span class="pill">Admin-Struktur</span>
          <span class="pill">Cabinettypen / Registertypen / Dokumenttypen</span>
          <span class="pill">Metadatenmodell</span>
          <a class="pill" href="/ui/app">Zur ECM-App</a>
          <a class="pill" href="/ui/admin/queues">Queues & Logs</a>
          <a class="pill" href="/ui/admin/identity">Identity & Rollen</a>
          <a class="pill" href="/ui/workflow-designer">Workflow Designer</a>
        </div>
      </div>
      <div class="panel hero-side">
        <h3>Systemstatus{tooltip_hint.replace('Mehr Kontext bei Hover oder Fokus.', 'Hier siehst du die technischen Lauflichter für Preview, Suche und OCR/Index direkt im Admin.')}</h3>
        <div class="status-stack" style="margin-top:12px;">
          <div class="status-chip"><span>Preview</span><span class="service-badge"><span class="status-dot"></span> async</span></div>
          <div class="status-chip"><span>Suche</span><span class="service-badge"><span class="status-dot"></span> bereit</span></div>
          {indexing_status_html}
        </div>
      </div>
    </section>
    <section class="grid">
      <aside class="stack">
        <div class="panel tree"><h2>Definitionsmodell</h2>{definition_structure_html}</div>
        <div class="panel tree"><h2>Instanzstruktur</h2>{structure_html}</div>
        <div class="panel"><h2>Cabinettypen</h2><ul>{cabinet_type_list_html}</ul></div>
        <div class="panel"><h2>Dokumenttypen</h2><ul>{type_list_html}</ul></div>
      </aside>
      <main class="stack">
        <div class="panel"><h2>Objekte anlegen</h2>{admin_create_html}</div>
      </main>
      <aside class="stack admin-detail-column">
        <div class="panel"><h2>Details</h2>{definition_detail_html}</div>
      </aside>
    </section>
  </div>
  <div id="def-context-menu" class="def-context-menu" aria-hidden="true"></div>
  {_render_admin_script()}
</body>
</html>
"""


def _render_admin_script() -> str:
    return """
  <script>
    const adminCreateButtons = document.querySelectorAll('.admin-create-toggle');
    const adminCreateSections = document.querySelectorAll('.admin-create-section');

    const openAdminCreateSection = (targetId) => {
      adminCreateSections.forEach((section) => {
        section.style.display = section.id === targetId ? 'block' : 'none';
      });
      adminCreateButtons.forEach((button) => {
        button.classList.toggle('is-active', button.dataset.target === targetId);
      });
      const target = document.getElementById(targetId);
      const firstInput = target ? target.querySelector('input, select, textarea') : null;
      if (firstInput) firstInput.focus();
    };

    adminCreateButtons.forEach((button) => {
      button.addEventListener('click', () => openAdminCreateSection(button.dataset.target));
    });

    const selectedDefinitionKind = document.getElementById('admin-create-selected-definition-kind')?.value || '';
    const selectedDefinitionId = document.getElementById('admin-create-selected-definition-id')?.value || '';
    const selectedMetadataFieldId = document.getElementById('admin-create-selected-metadata-field-id')?.value || '';
    const defaultTarget = document.getElementById('admin-create-default-target')?.value || '';

    const prefillAdminCreateForms = () => {
      if (selectedMetadataFieldId) return;
      if (!selectedDefinitionKind || !selectedDefinitionId) return;
      if (selectedDefinitionKind === 'cabinet_type') {
        const registerTypeSelect = document.querySelector('#admin-form-register-type select[name="cabinet_type_id"]');
        if (registerTypeSelect) registerTypeSelect.value = selectedDefinitionId;
        const documentTypeKind = document.querySelector('#admin-form-document-type select[name="target_kind"]');
        const documentTypeCabinetType = document.querySelector('#admin-form-document-type select[name="cabinet_type_id"]');
        const metadataKind = document.querySelector('#admin-form-metadata-field select[name="target_kind"]');
        const metadataCabinetType = document.querySelector('#admin-form-metadata-field select[name="cabinet_type_id"]');
        if (documentTypeKind) documentTypeKind.value = 'cabinet_type';
        if (documentTypeCabinetType) documentTypeCabinetType.value = selectedDefinitionId;
        if (metadataKind) metadataKind.value = 'cabinet_type';
        if (metadataCabinetType) metadataCabinetType.value = selectedDefinitionId;
      } else if (selectedDefinitionKind === 'register_type') {
        const documentTypeKind = document.querySelector('#admin-form-document-type select[name="target_kind"]');
        const documentTypeRegisterType = document.querySelector('#admin-form-document-type select[name="register_type_id"]');
        const metadataKind = document.querySelector('#admin-form-metadata-field select[name="target_kind"]');
        const metadataRegisterType = document.querySelector('#admin-form-metadata-field select[name="register_type_id"]');
        if (documentTypeKind) documentTypeKind.value = 'register_type';
        if (documentTypeRegisterType) documentTypeRegisterType.value = selectedDefinitionId;
        if (metadataKind) metadataKind.value = 'register_type';
        if (metadataRegisterType) metadataRegisterType.value = selectedDefinitionId;
      } else if (selectedDefinitionKind === 'document_type') {
        const metadataKind = document.querySelector('#admin-form-metadata-field select[name="target_kind"]');
        const metadataDocumentType = document.querySelector('#admin-form-metadata-field select[name="document_type_id"]');
        if (metadataKind) metadataKind.value = 'document_type';
        if (metadataDocumentType) metadataDocumentType.value = selectedDefinitionId;
      }
    };

    prefillAdminCreateForms();
    if (defaultTarget) openAdminCreateSection(defaultTarget);

    document.querySelectorAll('.admin-create-toggle-inline').forEach((button) => {
      button.addEventListener('click', () => {
        const target = button.dataset.target || '';
        const selectName = button.dataset.selectName || '';
        const selectValue = button.dataset.selectValue || '';
        const targetKind = button.dataset.targetKind || '';
        if (target) openAdminCreateSection(target);
        if (selectName && selectValue) {
          const select = document.querySelector(`#${target} select[name="${selectName}"]`);
          if (select) select.value = selectValue;
        }
        if (targetKind) {
          const targetKindSelect = document.querySelector(`#${target} select[name="target_kind"]`);
          if (targetKindSelect) targetKindSelect.value = targetKind;
        }
      });
    });

    // Definition tree: clickable nodes + context menus
    const defContextMenu = document.getElementById('def-context-menu');
    let activeDefNode = null;

    document.querySelectorAll('.def-node[data-kind]').forEach((node) => {
      node.addEventListener('click', (e) => {
        // Ignore if clicking on a button or link inside the node
        if (e.target.closest('button') || e.target.closest('a')) return;

        const kind = node.dataset.kind;
        const id = node.dataset.id;
        if (!kind || !id) return;

        // Update active state
        document.querySelectorAll('.def-node.active').forEach((n) => n.classList.remove('active'));
        node.classList.add('active');

        // Navigate to selection
        const url = new URL(window.location.href);
        url.searchParams.set('selected_definition_kind', kind);
        url.searchParams.set('selected_definition_id', id);
        window.location.href = url.toString();
      });
    });

    document.querySelectorAll('.def-node .def-menu-btn').forEach((btn) => {
      btn.addEventListener('click', (e) => {
        e.stopPropagation();
        const node = btn.closest('.def-node');
        if (!node || !defContextMenu) return;

        // Remove any existing active states
        document.querySelectorAll('.def-node.active').forEach((n) => n.classList.remove('active'));
        node.classList.add('active');

        const kind = node.dataset.kind || '';
        const id = node.dataset.id || '';
        const label = node.dataset.label || '';
        const actions = JSON.parse(node.dataset.actions || '[]');

        if (!actions.length) {
          defContextMenu.innerHTML = '<div style="padding:10px 12px;color:var(--muted);font-size:.88rem;">Keine Aktionen verfügbar</div>';
        } else {
          defContextMenu.innerHTML = actions.map((action) => {
            const dangerClass = action.danger ? ' danger-item' : '';
            if (action.href) {
              return `<a href="${action.href}" class="${dangerClass}">${action.title}</a>`;
            }
            return `<button type="button" class="${dangerClass}" data-action="${action.action}" data-kind="${kind}" data-id="${id}" data-target="${action.target || ''}" data-select-name="${action.select_name || ''}" data-select-value="${action.select_value || ''}" data-target-kind="${action.target_kind || ''}">${action.title}</button>`;
          }).join('');
        }

        // Position menu next to the button
        const rect = btn.getBoundingClientRect();
        defContextMenu.style.left = `${rect.left}px`;
        defContextMenu.style.top = `${rect.bottom + 4}px`;
        defContextMenu.classList.add('open');
        activeDefNode = node;
      });
    });

    document.addEventListener('click', (e) => {
      if (defContextMenu && !defContextMenu.contains(e.target) && !e.target.closest('.def-menu-btn')) {
        defContextMenu.classList.remove('open');
        document.querySelectorAll('.def-node.active').forEach((n) => n.classList.remove('active'));
      }
    });

    if (defContextMenu) {
      defContextMenu.addEventListener('click', (e) => {
        const button = e.target.closest('button[data-action]');
        if (!button) return;
        const action = button.dataset.action;
        const kind = button.dataset.kind || '';
        const id = button.dataset.id || '';
        const target = button.dataset.target || '';
        const selectName = button.dataset.selectName || '';
        const selectValue = button.dataset.selectValue || '';
        const targetKind = button.dataset.targetKind || '';

        if (action === 'open-form') {
          if (target) openAdminCreateSection(target);
          if (selectName && selectValue) {
            const select = document.querySelector(`#${target} select[name="${selectName}"]`);
            if (select) select.value = selectValue;
          }
          if (targetKind) {
            const targetKindSelect = document.querySelector(`#${target} select[name="target_kind"]`);
            if (targetKindSelect) targetKindSelect.value = targetKind;
          }
        }

        if (action === 'delete-def') {
          if (!confirm('Soll dieses Element wirklich gelöscht werden?')) return;
          const form = document.createElement('form');
          form.method = 'post';
          const deletePath = kind === 'cabinet_type'
            ? `/ui/admin/cabinet-types/${id}/delete`
            : kind === 'register_type'
              ? `/ui/admin/register-types/${id}/delete`
              : `/ui/admin/document-types/${id}/delete`;
          form.action = deletePath;
          document.body.appendChild(form);
          form.submit();
        }

        defContextMenu.classList.remove('open');
      });
    }
  </script>
"""


def _render_search_results(documents: list[Document], search_query: str) -> tuple[str, str]:
    if not documents:
        return (
            "<div class='panel'><p class='muted'>Keine Objekte für diese Suche oder Filter gefunden.</p></div>",
            f"<div class=\"panel\" style=\"margin-bottom:16px;\"><h2 style=\"margin-top:0;\">Suchtreffer</h2><p class=\"muted\">Volltextsuche nach: {_escape(search_query)}</p></div>",
        )
    results = ''.join(
        f"<a class='object-card' href='/ui/app?node_kind=document&node_id={document.id}'><strong>📄 {_escape(document.title or document.name)}</strong><div class='muted'>{_escape(document.document_type.name if document.document_type else 'Ohne Dokumenttyp')} · {_escape(document.name)}</div></a>"
        for document in documents
    )
    header = f"<div class=\"panel\" style=\"margin-bottom:16px;\"><h2 style=\"margin-top:0;\">Suchtreffer</h2><p class=\"muted\">Volltextsuche nach: {_escape(search_query)} · {len(documents)} Treffer</p></div>"
    return f"<div class='object-list'>{results}</div>", header



def _render_app_page(
    cabinets: list[Cabinet],
    document_types: list[DocumentType],
    recent_documents: list[Document],
    selected_document_type: DocumentType | None,
    selected_node: dict[str, Any] | None,
    message: str | None,
    error_field: str | None,
    error_message: str | None,
    form_values: dict[str, Any],
    all_documents: list[Document],
    search_query: str,
    filter_kind: str,
    cabinet_types: list[CabinetType] | None = None,
) -> str:
    recent_documents_html = _render_recent_documents(recent_documents)
    indexing_status = indexing_runtime_status()
    indexing_tools = indexing_status.get("ocr", {})
    indexing_status_html = " ".join(
        f"<div class='status-chip'><span>{name}</span><span class='service-badge'>{'bereit' if info.get('available') else 'fehlt'}</span></div>"
        for name, info in indexing_tools.items()
    )
    archive_tree_html = _render_archive_tree(cabinets, selected_node, search_query, document_types, cabinet_types or [])
    tooltip_hint = '<span class="tooltip" tabindex="0">?<span class="tooltip-bubble">Mehr Kontext bei Hover oder Fokus.</span></span>'
    object_overview_html, object_summary_html, recent_favorites_html = _render_object_overview(
        all_documents,
        search_query=search_query,
        filter_kind=filter_kind,
    )
    if search_query.strip():
        node_results_html, node_header_html = _render_search_results(all_documents, search_query)
    else:
        node_results_html, node_header_html = _render_node_results(cabinets, all_documents, selected_node, search_query)
    context_panel_html = _render_context_panel(selected_node, cabinets, cabinet_types)
    selected_document = None
    if selected_node and selected_node.get("kind") == "document":
        selected_document = next((document for document in all_documents if str(document.id) == str(selected_node.get("id"))), None)
    selected_cabinet = None
    selected_register = None
    selected_capture_document_type = selected_document_type or _selected_document_type_for_node(selected_node, document_types, cabinets)
    show_intake_panel = bool(selected_capture_document_type)
    if selected_node:
        selected_kind = selected_node.get("kind")
        selected_id = str(selected_node.get("id") or "")
        if selected_kind == "cabinet":
            selected_cabinet = next((cabinet for cabinet in cabinets if str(cabinet.id) == selected_id), None)
        elif selected_kind == "document":
            selected_document = next((document for document in all_documents if str(document.id) == selected_id), None)
            if selected_document:
                resolved_document_cabinet = _resolved_document_cabinet(selected_document)
                if resolved_document_cabinet:
                    selected_cabinet = resolved_document_cabinet
        elif selected_kind == "register":
            for cabinet in cabinets:
                register_match = next((register for register in cabinet.registers if str(register.id) == selected_id), None)
                if register_match:
                    selected_register = register_match
                    selected_cabinet = cabinet
                    break
    if selected_capture_document_type:
        selected_register = selected_register or selected_capture_document_type.register
        selected_cabinet = selected_cabinet or selected_capture_document_type.cabinet or (selected_register.cabinet if selected_register else None)

    cabinet_options = _option_list(
        [
            (str(cabinet.id), f"{cabinet.cabinet_type.name if cabinet.cabinet_type else 'Ohne Typ'} → {cabinet.name}")
            for cabinet in cabinets
        ],
        selected_value=str(selected_cabinet.id) if selected_cabinet else None,
        include_blank="Bitte wählen",
    )
    register_candidates = selected_cabinet.registers if selected_cabinet else []
    register_options = _option_list(
        [(str(register.id), register.name) for register in register_candidates],
        selected_value=str(selected_register.id) if selected_register else None,
        include_blank="Kein Register",
    )
    available_document_types = _available_document_types_for_node(selected_node, cabinets)
    effective_document_type_choices = available_document_types or document_types
    document_type_options = _option_list(
        [(str(doc_type.id), doc_type.name) for doc_type in effective_document_type_choices],
        selected_value=str(selected_capture_document_type.id) if selected_capture_document_type else None,
    )
    capture_fields = []
    capture_field_inputs = []
    selected_document_metadata_html = "<div class='muted'>Kein Objekt ausgewählt.</div>"
    selected_document_preview_html = "<div class='muted'>Keine Vorschau verfügbar.</div>"
    if selected_node and selected_node.get("kind") == "cabinet":
        selected_cabinet_documents = [document for document in all_documents if _resolved_document_cabinet(document) and selected_cabinet and str(_resolved_document_cabinet(document).id) == str(selected_cabinet.id)]
        selected_document_metadata_html = (
            f"<div class='meta-display-row'><div class='meta-display-label'>Cabinet</div><div class='meta-display-value'>{_escape(selected_cabinet.name if selected_cabinet else '')}</div></div>"
            f"<div class='meta-display-row'><div class='meta-display-label'>Dokumente</div><div class='meta-display-value'>{len(selected_cabinet_documents)}</div></div>"
            f"<div class='meta-display-row'><div class='meta-display-label'>Register</div><div class='meta-display-value'>{len(selected_cabinet.registers) if selected_cabinet else 0}</div></div>"
        )
    elif selected_node and selected_node.get("kind") == "register":
        selected_register_documents = [document for document in all_documents if document.document_type and document.document_type.register_id and selected_register and str(document.document_type.register_id) == str(selected_register.id)]
        selected_document_metadata_html = (
            f"<div class='meta-display-row'><div class='meta-display-label'>Register</div><div class='meta-display-value'>{_escape(selected_register.name if selected_register else '')}</div></div>"
            f"<div class='meta-display-row'><div class='meta-display-label'>Dokumente</div><div class='meta-display-value'>{len(selected_register_documents)}</div></div>"
            f"<div class='meta-display-row'><div class='meta-display-label'>Dokumenttypen</div><div class='meta-display-value'>{len(selected_register.document_types) if selected_register else 0}</div></div>"
        )
    elif selected_document:
        selected_document_metadata = metadata_from_json(selected_document.metadata_json) or {}
        selected_document_field_map = {
            field.name: (field.label or field.name)
            for field in (selected_document.document_type.fields if selected_document.document_type else [])
        }
        metadata_rows = "".join(
            f"<div class='meta-display-row'><div class='meta-display-label'>{_escape(str(selected_document_field_map.get(key, key)))}</div><div class='meta-display-value'>{_escape(', '.join(value) if isinstance(value, list) else str(value))}</div></div>"
            for key, value in selected_document_metadata.items()
        ) or "<div class='muted'>Dieses Dokument hat noch keine Metadaten.</div>"
        indexing_rows = (
            f"<div class='meta-display-row'><div class='meta-display-label'>Indexstatus</div><div class='meta-display-value'>{_escape(selected_document.index_status or 'unbekannt')}</div></div>"
            f"<div class='meta-display-row'><div class='meta-display-label'>Index-Engine</div><div class='meta-display-value'>{_escape(selected_document.index_engine or 'nicht protokolliert')}</div></div>"
            f"<div class='meta-display-row'><div class='meta-display-label'>OCR verwendet</div><div class='meta-display-value'>{'Ja' if getattr(selected_document, 'index_ocr_used', False) else 'Nein'}</div></div>"
        )
        selected_document_metadata_html = metadata_rows + indexing_rows
        selected_document_preview_html = _render_document_preview(selected_document, f"/ui/app/documents/{selected_document.id}/download")
    if selected_capture_document_type:
        for field in sorted(selected_capture_document_type.fields or [], key=lambda item: (item.order, (item.name or '').lower())):
            capture_fields.append(f'{field.label or field.name}: {field.field_type}')
            input_name = f"metadata_{field.name}"
            label = field.label or field.name
            placeholder = field.placeholder or ""
            required = "required" if field.is_required else ""
            value = form_values.get(field.name, field.default_value or "")
            safe_label = _escape(label)
            safe_placeholder = _escape(placeholder)
            required_badge = ' <span class="required-badge">Pflicht</span>' if field.is_required else ''
            field_error_class = " error" if error_field == field.name else ""
            options = _parse_field_options(field)
            if field.field_type == "long_text":
                control = f'<textarea name="{input_name}" placeholder="{safe_placeholder}" {required}>{_escape(value)}</textarea>'
            elif field.field_type == "boolean":
                checked = "checked" if str(value).lower() == "true" else ""
                control = (
                    f'<label class="toggle">'
                    f'<input type="hidden" name="{input_name}" value="false">'
                    f'<input type="checkbox" name="{input_name}" value="true" {checked}>'
                    f'<span class="toggle-slider"></span>'
                    f'<span class="toggle-label">Ja / Nein</span>'
                    f'</label>'
                )
            elif field.field_type == "selection" and options:
                option_html = "".join(
                    f'<option value="{_escape(option)}" {"selected" if str(value) == option else ""}>{_escape(option)}</option>'
                    for option in options
                )
                control = f'<select name="{input_name}" {required}><option value="">Bitte wählen</option>{option_html}</select>'
            elif field.field_type == "multi_selection" and options:
                selected_values = value if isinstance(value, list) else [part.strip() for part in str(value).split(",") if part.strip()]
                option_html = "".join(
                    f'<option value="{option}" {"selected" if option in selected_values else ""}>{option}</option>'
                    for option in options
                )
                control = f'<div class="checkbox-group">' + ''.join(
                    f'<label class="checkbox-item"><input type="checkbox" name="{input_name}" value="{_escape(option)}" {"checked" if option in selected_values else ""}> {_escape(option)}</label>'
                    for option in options
                ) + '</div>'
            elif field.field_type == "date":
                control = f'<input type="date" name="{input_name}" value="{_escape(value)}" placeholder="{safe_placeholder}" {required}>'
            elif field.field_type == "datetime":
                control = f'<input type="datetime-local" name="{input_name}" value="{_escape(value)}" placeholder="{safe_placeholder}" {required}>'
            elif field.field_type in ("number", "currency"):
                control = f'<input type="number" step="any" name="{input_name}" value="{_escape(value)}" placeholder="{safe_placeholder}" {required}>'
            elif field.field_type == "email":
                control = f'<input type="email" name="{input_name}" value="{_escape(value)}" placeholder="{safe_placeholder}" {required}>'
            elif field.field_type == "url":
                control = f'<input type="url" name="{input_name}" value="{_escape(value)}" placeholder="{safe_placeholder}" {required}>'
            elif field.field_type == "phone":
                control = f'<input type="tel" name="{input_name}" value="{_escape(value)}" placeholder="{safe_placeholder}" {required}>'
            else:
                control = f'<input type="text" name="{input_name}" value="{_escape(value)}" placeholder="{safe_placeholder}" {required}>'
            description_html = f'<div class="muted field-help">{_escape(field.description)}</div>' if field.description else ""
            field_error_html = f'<div class="field-error">{_escape(error_message)}</div>' if error_field == field.name and error_message else ""
            capture_field_inputs.append(f'<div class="field full{field_error_class}"><label>{safe_label}{required_badge}</label>{control}{description_html}{field_error_html}</div>')
    capture_preview = "".join(f'<div class="preview-item">{_escape(item)}</div>' for item in capture_fields) if capture_fields else '<div class="muted">Noch keine dynamischen Felder für diesen Typ definiert.</div>'
    capture_fields_html = "".join(capture_field_inputs)
    banner_class = "banner error-banner" if error_message else "banner success-banner"
    success_actions = ""
    if message and not error_message and selected_node:
        selected_kind_label = selected_node.get("kind", "Objekt")
        selected_label = selected_node.get("label", "aktuelles Element")
        success_actions = (
            "<div class='success-actions'>"
            f"<a class='chip' href='#quick-create'>Weiter Struktur anlegen</a>"
            f"<a class='chip' href='#intake-form'>Dokument in {_escape(selected_label)} erfassen</a>"
            f"<a class='chip' href='/ui/admin'>Admin öffnen</a>"
            "</div>"
        )
    message_html = f'<div class="{banner_class}"><strong>{_escape(message)}</strong>{success_actions}</div>' if message else ""
    clear_filter_link = f"/ui/app?selected_document_type_id={selected_capture_document_type.id}" if selected_capture_document_type else "/ui/app"
    all_filter_link = f"/ui/app?filter_kind=all&q={quote_plus(search_query or '')}{f'&selected_document_type_id={selected_capture_document_type.id}' if selected_capture_document_type else ''}"
    typed_filter_link = f"/ui/app?filter_kind=typed&q={quote_plus(search_query or '')}{f'&selected_document_type_id={selected_capture_document_type.id}' if selected_capture_document_type else ''}"
    untyped_filter_link = f"/ui/app?filter_kind=untyped&q={quote_plus(search_query or '')}{f'&selected_document_type_id={selected_capture_document_type.id}' if selected_capture_document_type else ''}"
    recent_filter_link = f"/ui/app?filter_kind=recent&q={quote_plus(search_query or '')}{f'&selected_document_type_id={selected_capture_document_type.id}' if selected_capture_document_type else ''}"
    capture_empty_hint_html = f'<div class="muted" style="margin-top:10px;">{capture_preview}</div>' if not capture_field_inputs else ""
    intake_panel_html = ""
    if show_intake_panel:
        intake_panel_html = (
            f'<div id="intake-form" class="panel">'
            f'<div class="section-head"><div><h2 style="margin:0;">Dokument erfassen</h2><div class="muted" style="margin-top:6px;">Datei per Drag and Drop ablegen und Indexdaten direkt im aktuellen Kontext erfassen.</div></div></div>'
            f'<form method="post" action="/ui/app/intake" enctype="multipart/form-data" id="intake-form-element">'
            f'<input type="hidden" name="node_kind" value="{_escape(selected_node.get("kind", "") if selected_node else "")}">'
            f'<input type="hidden" name="node_id" value="{_escape(selected_node.get("id", "") if selected_node else "")}">'
            f'<input type="hidden" name="selected_document_type_id" value="{_escape(str(selected_capture_document_type.id)) if selected_capture_document_type else ""}">'
            f'<input type="hidden" name="hash" id="file-hash-input" value="">'
            f'<div id="file-dropzone" class="dropzone" style="margin-top:8px;"><strong>Datei hier ablegen</strong><div class="muted" id="dropzone-hint" style="margin-top:8px;">oder klicken, um eine Datei auszuwählen</div><input id="file-input" type="file" name="file" required style="display:none"></div>'
            f'<div class="field-grid" style="margin-top:16px;"><div class="field"><label>Cabinet</label><select name="cabinet_id" required>{cabinet_options}</select></div><div class="field"><label>Register</label><select name="register_id">{register_options}</select></div><div class="field full"><label>Dokumenttyp</label><select id="document-type-select" name="document_type_id" required>{document_type_options}</select></div></div>'
            f'<div class="panel" style="margin:16px 0 0 0; padding:16px;"><h3 style="margin:0 0 12px 0;">Indexdaten</h3><div class="field-grid">{capture_fields_html}</div>{capture_empty_hint_html}</div>'
            f'<div id="duplicate-warning" class="panel" style="display:none; margin-top:12px; padding:12px 16px; background:rgba(255,123,123,0.10); border:1px solid rgba(255,123,123,0.30); border-radius:12px;"><strong style="color:#ff7b7b;">⚠ Duplikat erkannt!</strong><div id="duplicate-info" style="margin-top:6px; color:var(--muted); font-size:.9rem;"></div></div>'
            f'<div class="actions"><button class="primary" type="submit" id="intake-submit-btn">Dokument speichern</button></div></form></div>'
        )

    return f"""
<!doctype html>
<html lang="de">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Archiva App</title>
  <link rel="icon" type="image/svg+xml" href="/assets/archiva-favicon.svg">
  <style>
    :root {{ color-scheme: dark; --bg: #0b1020; --panel: #121933; --panel-soft: #1a2345; --panel-deep: #0f1630; --text: #eef2ff; --muted: #a8b2d1; --accent: #4f8cff; --accent-2: #4dd4ff; --success: #6ee7b7; --border: #2d3b69; --glow: rgba(77, 212, 255, 0.18); --shadow: 0 18px 48px rgba(0,0,0,0.28); }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; font-family: Inter, ui-sans-serif, system-ui, sans-serif; background: radial-gradient(circle at top left, rgba(77,212,255,0.08), transparent 30%), radial-gradient(circle at top right, rgba(79,140,255,0.08), transparent 28%), var(--bg); color: var(--text); }}
    a {{ color: var(--accent-2); text-decoration: none; }} a:hover {{ text-decoration: none; }}
    .page {{ max-width: 1760px; margin: 0 auto; padding: 14px 16px; }}
    .hero {{ display:grid; grid-template-columns: minmax(0, 1.2fr) minmax(220px, 0.55fr) 320px; gap:12px; align-items:stretch; margin-bottom:12px; }}
    .hero-card, .hero-status {{ position:relative; overflow:hidden; }}
    .hero-card::before, .hero-status::before {{ content:""; position:absolute; inset:0; background: linear-gradient(135deg, rgba(79,140,255,0.14), rgba(77,212,255,0.06) 45%, transparent 75%); pointer-events:none; }}
    .hero-brand {{ display:flex; gap:12px; align-items:center; margin-bottom:10px; position:relative; z-index:1; }}
    .brand-mark {{ width:48px; height:48px; border-radius:16px; display:grid; place-items:center; background: linear-gradient(135deg, rgba(79,140,255,0.28), rgba(77,212,255,0.18)); border:1px solid rgba(77,212,255,0.28); box-shadow: 0 0 0 1px rgba(255,255,255,0.02) inset; }}
    .brand-mark svg {{ width:30px; height:30px; }}
    .eyebrow {{ letter-spacing:.12em; text-transform:uppercase; font-size:.74rem; color: var(--accent-2); font-weight:700; }}
    .hero-title {{ margin: 0; font-size: 2rem; line-height: 1.02; letter-spacing: -0.03em; }}
    .hero-subtitle {{ margin: 8px 0 0; color: var(--muted); max-width: 62ch; line-height: 1.45; font-size: .95rem; }}
    .flow-lanes {{ display:grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap:10px; margin-top:12px; position:relative; z-index:1; }}
    .flow-lane {{ padding:10px 12px; border-radius:14px; background: rgba(255,255,255,0.03); border:1px solid rgba(77,212,255,0.12); box-shadow: 0 8px 24px rgba(0,0,0,0.10); }}
    .flow-lane strong {{ display:block; margin-bottom:0; }}
    .pillbar {{ display:flex; gap:8px; flex-wrap:wrap; margin-top:12px; position:relative; z-index:1; }}
    .pill {{ background: rgba(255,255,255,0.04); border:1px solid rgba(77,212,255,0.12); border-radius:999px; padding:7px 11px; color:var(--text); transition: all .18s ease; font-size:.92rem; }}
    .pill:hover {{ border-color: rgba(77,212,255,0.4); box-shadow: 0 0 0 4px var(--glow); }}
    .panel {{ background: linear-gradient(180deg, rgba(18,25,51,0.96), rgba(15,22,48,0.96)); border: 1px solid rgba(77,212,255,0.10); border-radius: 18px; padding: 16px; margin-bottom: 16px; box-shadow: var(--shadow); backdrop-filter: blur(10px); }}
    .hero-status h3, .panel h2 {{ margin-top:0; }}
    .hero-status {{ padding:12px 14px; }}
    .hero-status h3 {{ margin-bottom:8px; font-size:1rem; }}
    .status-stack {{ display:grid; gap:8px; position:relative; z-index:1; }}
    .status-chip {{ display:flex; align-items:center; justify-content:space-between; gap:8px; padding:8px 10px; border-radius:12px; background: rgba(255,255,255,0.03); border:1px solid rgba(77,212,255,0.10); font-size:.9rem; }}
    .status-dot {{ width:10px; height:10px; border-radius:999px; background: var(--success); box-shadow: 0 0 12px rgba(110,231,183,0.5); }}
    .banner {{ border-radius:18px; padding:14px 16px; margin-bottom:20px; border:1px solid rgba(77,212,255,0.16); }} .success-banner {{ background:rgba(110,231,183,.10); border-color:rgba(110,231,183,.26); color:#d9ffec; box-shadow:0 0 0 4px rgba(110,231,183,0.10); }} .error-banner {{ background:rgba(255,120,120,.12); border-color:rgba(255,120,120,.28); color:#ffd0d0; }}
    .success-actions {{ display:flex; flex-wrap:wrap; gap:10px; margin-top:12px; }}
    .main-grid {{ display:grid; grid-template-columns: 1.15fr 0.85fr; gap:16px; align-items:start; }}
    .workspace-grid {{ display:grid; grid-template-columns: 260px minmax(0, 1fr) 420px; gap:12px; align-items:start; }}
    .field-grid {{ display:grid; grid-template-columns: repeat(2, minmax(0,1fr)); gap:12px; }}
    .compact-indexdata .field-grid {{ grid-template-columns: 1fr; gap:10px; }}
    .compact-indexdata .panel {{ margin:0; padding:0; border:none; box-shadow:none; background:transparent; }}
    .compact-indexdata .field {{ align-content:start; }}
    .compact-indexdata input,
    .compact-indexdata textarea,
    .compact-indexdata select {{ min-height:48px; }}
    .field {{ display:grid; gap:6px; }} .field.full {{ grid-column: 1 / -1; }} .field.error label {{ color:#ff8f8f; }} .field.error input, .field.error textarea, .field.error select, .field.error .checkbox-group {{ border-color:#ff8f8f; box-shadow:0 0 0 1px rgba(255,143,143,.25) inset; }} .checkbox-group {{ display:grid; gap:10px; padding:14px; border:1px solid rgba(77,212,255,0.08); border-radius:16px; background:var(--panel-deep); }} .checkbox-item {{ display:flex; gap:10px; align-items:center; font-weight:400; padding:8px 10px; border-radius:12px; background:rgba(255,255,255,0.02); }} .checkbox-item input {{ width:auto; }} .toggle {{ display:inline-flex; align-items:center; gap:12px; cursor:pointer; }} .toggle input[type="hidden"] {{ display:none; }} .toggle input[type="checkbox"] {{ display:none; }} .toggle-slider {{ position:relative; width:52px; height:30px; background:#33406b; border-radius:999px; transition:background .2s ease; }} .toggle-slider::after {{ content:""; position:absolute; top:3px; left:3px; width:24px; height:24px; background:white; border-radius:50%; transition:transform .2s ease; }} .toggle input[type="checkbox"]:checked + .toggle-slider {{ background:var(--accent); }} .toggle input[type="checkbox"]:checked + .toggle-slider::after {{ transform:translateX(22px); }} .toggle-label {{ color:var(--muted); font-weight:500; }} .field-help {{ font-size:.9rem; }} .field-error {{ color:#ff8f8f; font-size:.92rem; font-weight:600; }} .preview-item {{ padding:10px 12px; border:1px solid rgba(77,212,255,0.08); border-radius:12px; background:rgba(255,255,255,0.03); margin-bottom:8px; }} .required-badge {{ display:inline-block; margin-left:8px; padding:2px 8px; border-radius:999px; font-size:.78rem; background:rgba(77,212,255,.16); color:var(--accent-2); vertical-align:middle; }}
    label {{ font-weight:600; font-size:0.95rem; }}
    input, textarea, select {{ width:100%; border-radius:14px; border:1px solid rgba(77,212,255,0.10); background:var(--panel-deep); color:var(--text); padding:12px; font:inherit; }}
    input:focus, textarea:focus, select:focus {{ outline:none; border-color: rgba(77,212,255,0.46); box-shadow: 0 0 0 4px var(--glow); }}
    textarea {{ min-height: 110px; resize: vertical; }}
    .dropzone {{ border:2px dashed rgba(77,212,255,0.16); border-radius:20px; padding:28px; text-align:center; background:linear-gradient(180deg, rgba(255,255,255,0.03), rgba(255,255,255,0.02)); }}
    .duplicate-warning {{ margin-top:12px; padding:12px 16px; background:rgba(255,123,123,0.10); border:1px solid rgba(255,123,123,0.30); border-radius:12px; display:none; }}
    .duplicate-warning.visible {{ display:block; }}
    .actions {{ display:flex; gap:12px; flex-wrap:wrap; margin-top:16px; }}
    button, .chip {{ border:none; border-radius:999px; padding:10px 14px; font:inherit; cursor:pointer; transition: all .18s ease; }}
    .primary {{ background: linear-gradient(135deg, var(--accent), var(--accent-2)); color:white; box-shadow: 0 8px 24px rgba(77,212,255,0.22); }}
    .primary:hover, .chip:hover {{ transform: translateY(-1px); }}
    .muted {{ color: var(--muted); }}
    .overview-toolbar {{ display:grid; gap:14px; margin-bottom:16px; }}
    .search-row {{ display:grid; grid-template-columns: 1fr auto; gap:12px; }}
    .chip-row {{ display:flex; flex-wrap:wrap; gap:10px; }}
    .chip {{ display:inline-flex; align-items:center; background:rgba(255,255,255,0.03); border:1px solid rgba(77,212,255,0.10); color:var(--text); }}
    .chip.active {{ background:rgba(77,212,255,.12); color:var(--text); border-color:rgba(77,212,255,.40); box-shadow: 0 0 0 4px var(--glow); }}
    .stats-grid {{ display:grid; grid-template-columns: repeat(3, minmax(0,1fr)); gap:12px; margin-bottom:16px; }}
    .stat-card {{ padding:16px; border:1px solid rgba(77,212,255,0.10); border-radius:18px; background:linear-gradient(180deg, rgba(255,255,255,0.04), rgba(255,255,255,0.02)); }}
    .stat-card strong {{ display:block; font-size:1.55rem; margin-top:6px; }}
    .object-list {{ display:grid; gap:14px; }}
    .object-card {{ display:block; padding:18px; border:1px solid rgba(77,212,255,0.10); border-radius:18px; background:linear-gradient(180deg, rgba(255,255,255,0.04), rgba(255,255,255,0.02)); color:var(--text); position:relative; overflow:hidden; }}
    .object-card::after {{ content:""; position:absolute; inset:auto -40px -40px auto; width:140px; height:140px; background: radial-gradient(circle, rgba(77,212,255,0.14), transparent 65%); }}
    .object-card:hover {{ border-color:rgba(77,212,255,.38); box-shadow: 0 0 0 4px var(--glow); text-decoration:none; transform: translateY(-1px); }}
    .object-top {{ display:flex; justify-content:space-between; gap:12px; align-items:flex-start; margin-bottom:8px; position:relative; z-index:1; }}
    .meta-row {{ display:flex; gap:8px; flex-wrap:wrap; margin:8px 0; position:relative; z-index:1; }}
    .meta-pill {{ padding:4px 10px; border-radius:999px; background:rgba(255,255,255,0.04); border:1px solid rgba(77,212,255,0.10); color:var(--muted); font-size:.85rem; }}
    .metadata-preview {{ margin-top:10px; color:var(--muted); font-size:.92rem; position:relative; z-index:1; }}
    .section-head {{ display:flex; justify-content:space-between; gap:16px; align-items:center; margin-bottom:12px; }}
    .recent-grid {{ display:grid; gap:10px; }}
    .recent-link {{ display:block; padding:14px; border:1px solid rgba(77,212,255,0.10); border-radius:16px; background:rgba(255,255,255,.03); color:var(--text); }}
    .recent-link:hover {{ text-decoration:none; border-color:rgba(110,231,183,.45); box-shadow:0 0 0 4px rgba(110,231,183,0.12); }}
    .service-card {{ position:relative; overflow:hidden; }}
    .mini-cta-row {{ display:flex; flex-wrap:wrap; gap:10px; margin-top:14px; }}
    .mini-cta {{ display:inline-flex; align-items:center; gap:8px; padding:8px 12px; border-radius:999px; background:rgba(77,212,255,0.10); border:1px solid rgba(77,212,255,0.18); color:var(--text); font-size:.92rem; }}
    .mini-cta:hover {{ text-decoration:none; border-color:rgba(77,212,255,.42); box-shadow:0 0 0 4px var(--glow); }}
    .context-note {{ margin-top:10px; padding:10px 12px; border-radius:14px; background:rgba(255,255,255,0.03); border:1px solid rgba(77,212,255,0.10); color:var(--muted); font-size:.92rem; }}
    .service-card::before {{ content:""; position:absolute; inset:0; background: linear-gradient(135deg, rgba(79,140,255,0.10), rgba(77,212,255,0.03) 55%, transparent 80%); pointer-events:none; }}
    .service-header {{ display:flex; justify-content:space-between; gap:12px; align-items:flex-start; position:relative; z-index:1; }}
    .service-badge {{ display:inline-flex; align-items:center; gap:8px; padding:6px 10px; border-radius:999px; background:rgba(110,231,183,0.10); border:1px solid rgba(110,231,183,0.18); color:#d6fff0; font-size:.85rem; }}
    .tooltip {{ position:relative; display:inline-flex; align-items:center; justify-content:center; width:18px; height:18px; margin-left:8px; border-radius:999px; border:1px solid rgba(77,212,255,0.24); color:var(--accent-2); font-size:.78rem; font-weight:700; cursor:help; vertical-align:middle; }}
    .tooltip-bubble {{ position:absolute; left:50%; bottom:calc(100% + 10px); transform:translateX(-50%); min-width:220px; max-width:320px; padding:10px 12px; border-radius:12px; background:#0f1630; border:1px solid rgba(77,212,255,0.24); box-shadow:0 18px 48px rgba(0,0,0,0.35); color:var(--text); font-size:.84rem; line-height:1.45; opacity:0; pointer-events:none; transition:opacity .14s ease, transform .14s ease; z-index:30; }}
    .tooltip:hover .tooltip-bubble, .tooltip:focus .tooltip-bubble, .tooltip:focus-within .tooltip-bubble {{ opacity:1; transform:translateX(-50%) translateY(-2px); }}
    .archive-tree {{ display:grid; gap:8px; position:relative; }}
    .tree-node {{ display:flex; align-items:center; justify-content:space-between; gap:10px; padding:10px 12px; border-radius:14px; border:1px solid rgba(77,212,255,0.08); background:rgba(255,255,255,0.02); position:relative; }}
    .tree-node[data-menu]:hover {{ border-color:rgba(77,212,255,.32); }}
    .tree-actions {{ display:flex; align-items:center; gap:8px; }}
    .tree-menu-button {{ border:none; border-radius:10px; padding:6px 10px; background:rgba(255,255,255,0.03); color:var(--accent-2); cursor:pointer; }}
    .tree-menu-button:hover {{ background:rgba(77,212,255,0.10); }}
    .context-menu {{ position:absolute; z-index:1000; min-width:220px; padding:8px; border-radius:16px; border:1px solid rgba(77,212,255,0.32); background:#111a36; box-shadow:0 18px 48px rgba(0,0,0,0.45); display:none; pointer-events:auto; }}
    .context-menu.open {{ display:block; }}
    .context-menu button, .context-menu a {{ width:100%; display:flex; align-items:center; justify-content:flex-start; text-align:left; background:rgba(255,255,255,0.02); color:var(--text); border:none; border-radius:12px; padding:10px 12px; cursor:pointer; font:inherit; }}
    .context-menu button:hover, .context-menu a:hover {{ background:rgba(77,212,255,0.10); text-decoration:none; }}
    .tree-node.active {{ border-color:rgba(77,212,255,0.42); box-shadow:0 0 0 4px var(--glow); background:rgba(77,212,255,0.08); }}
    .tree-node.just-created {{ border-color:rgba(110,231,183,.45); box-shadow:0 0 0 4px rgba(110,231,183,0.14); background:rgba(110,231,183,0.10); }}
    .tree-node.depth-1 {{ margin-left:16px; }}
    .tree-node.depth-2 {{ margin-left:32px; }}
    .tree-node.depth-3 {{ margin-left:48px; }}
    .tree-link {{ color:var(--text); flex:1; }}
    .tree-tab-link {{ color:var(--accent-2); opacity:.8; }}
    .meta-display-row {{ display:grid; grid-template-columns: 110px 1fr; gap:10px; align-items:start; padding:8px 0; border-bottom:1px solid rgba(255,255,255,.06); }}
    .meta-display-row:last-child {{ border-bottom:none; }}
    .meta-display-label {{ display:block; color:#7dd3fc; font-weight:700; letter-spacing:.02em; font-size:.82rem; padding-top:4px; }}
    .meta-display-value {{ display:block; color:var(--text); background:rgba(255,255,255,0.04); border:1px solid rgba(77,212,255,0.12); border-radius:10px; padding:8px 10px; line-height:1.35; word-break:break-word; box-shadow:none; font-size:.92rem; }}
    @media (max-width: 1100px) {{ .main-grid, .hero, .workspace-grid {{ grid-template-columns: 1fr; }} .flow-lanes, .stats-grid, .hero-cta-strip {{ grid-template-columns: 1fr; }} .search-row {{ grid-template-columns: 1fr; }} }}
  </style>
</head>
<body>
  <div class="page">
    <section class="hero">
      <div class="panel hero-card">
        <div class="hero-brand">
          <div class="brand-mark" aria-hidden="true">
            <img src="/assets/archiva-logo-flow.svg" alt="" style="width:34px;height:34px;display:block;">
          </div>
          <div>
            <div class="eyebrow">Content, Structure, Flow</div>
            <h1 class="hero-title">Archiva</h1>
          </div>
        </div>
        <p class="hero-subtitle">ECM-Arbeitsraum für Aufnahme, Klassifikation und Wiederfinden.{tooltip_hint.replace('Mehr Kontext bei Hover oder Fokus.', 'Archiva verbindet Struktur mit Bewegung, nicht nur Ablage mit Formularen.')}</p>
        <form method="get" action="/ui/app" class="overview-toolbar" id="search-form" style="margin-top:18px; position:relative; z-index:1;">
          {f'<input type="hidden" name="node_kind" value="{_escape(selected_node["kind"])}">' if selected_node else ''}
          {f'<input type="hidden" name="node_id" value="{_escape(selected_node["id"])}">' if selected_node else ''}
          {f'<input type="hidden" name="selected_document_type_id" value="{_escape(str(selected_capture_document_type.id))}">' if selected_capture_document_type else ''}
          <div class="search-row">
            <input type="search" name="q" value="{_escape(search_query)}" placeholder="Volltextsuche in Archiva, z. B. Titel, Metadaten, Tags, Typen">
            <button class="primary" type="submit">Suchen</button>
          </div>
        </form>
      </div>
      <div style="height:100%;">
        {context_panel_html}
      </div>
    </section>
    {message_html}
    <section class="panel">
      <div class="section-head">
        <div>
          <h2 style="margin:0;">Objekte{tooltip_hint.replace('Mehr Kontext bei Hover oder Fokus.', 'Sofort sichtbare Übersicht, Suche ohne Extraklicks, leichte Filter und zuletzt genutzte Einträge.')}</h2>
        </div>
        <div class="actions" style="margin-top:0;">
          <a class="chip" href="#recent-documents">Zuletzt erfasste</a>
          <a class="chip" href="/ui/admin">Zur Admin-Oberfläche</a>
        </div>
      </div>
      <div class="workspace-grid">
        <div class="panel" style="margin-bottom:0;">
          <h2 style="margin-top:0;">Archivbaum{tooltip_hint.replace('Mehr Kontext bei Hover oder Fokus.', 'Strukturansicht von Cabinettypen, Cabinets, Registern und Dokumenttypen. Über das Kontextmenü kannst du direkt neue Elemente anlegen.')}</h2>
          {archive_tree_html}
          <div id="tree-context-menu" class="context-menu" aria-hidden="true"></div>
        </div>
        <div style="display:block;">
          {node_header_html}
          {intake_panel_html}
          {node_results_html}
          <div style="margin-top:20px;">{object_summary_html}</div>
          {object_overview_html}
        </div>
        <div class="admin-detail-column" style="display:block;">
          <div class="panel compact-indexdata" style="margin-bottom:12px;">
            <h2 style="margin-top:0;">Inhaltsvorschau</h2>
            {selected_document_preview_html if selected_document else selected_document_metadata_html}
            {f'<div class="actions"><a class="chip" href="/ui/app/documents/{selected_document.id}">Dokument öffnen</a></div>' if selected_document else ''}
          </div>
          <div class="panel compact-indexdata" style="margin-bottom:0;">
            <h2 style="margin-top:0;">Metadaten</h2>
            {selected_document_metadata_html}
          </div>
        </div>
      </div>
    </section>
  </div>
  <script>
    const documentTypeSelect = document.getElementById('document-type-select');
    const fileInput = document.getElementById('file-input');
    const fileDropzone = document.getElementById('file-dropzone');
    const dropzoneHint = document.getElementById('dropzone-hint');
    const treeContextMenu = document.getElementById('tree-context-menu');

    const openQuickCreate = (mode, nodeKind = '', nodeId = '', nodeLabel = '') => {{
      const details = document.querySelector('#quick-create details');
      if (details) details.open = true;
      const modeInput = document.getElementById('quick-create-mode');
      const nodeKindInput = document.getElementById('quick-create-node-kind');
      const nodeIdInput = document.getElementById('quick-create-node-id');
      const hint = document.getElementById('quick-create-context-hint');
      const cabinetTypeSelect = document.querySelector('#quick-create select[name="cabinet_type_id"]');
      const cabinetSelect = document.querySelector('#quick-create select[name="cabinet_id"]');
      const cabinetForm = document.getElementById('quick-create-cabinet-form');
      const registerForm = document.getElementById('quick-create-register-form');
      const cabinetNameInput = cabinetForm ? cabinetForm.querySelector('input[name="name"]') : null;
      if (modeInput) modeInput.value = mode;
      if (nodeKindInput) nodeKindInput.value = nodeKind;
      if (nodeIdInput) nodeIdInput.value = nodeId;
      if (hint) hint.textContent = nodeLabel ? `Kontext: ${{nodeLabel}}` : 'Kein Kontext gewählt';

      if (cabinetForm) {{
        cabinetForm.style.display = 'block';
        cabinetForm.classList.toggle('is-muted', mode === 'register');
      }}
      if (registerForm) {{
        registerForm.style.display = 'block';
        registerForm.classList.toggle('is-muted', mode === 'cabinet');
      }}

      if (mode === 'cabinet' && cabinetTypeSelect) {{
        if (nodeKind === 'cabinet_type') cabinetTypeSelect.value = nodeId;
        if (cabinetNameInput && !cabinetNameInput.value.trim()) {{
          cabinetNameInput.value = String(new Date().getFullYear());
        }}
      }}
      if (mode === 'register' && cabinetSelect) {{
        if (nodeKind === 'cabinet') cabinetSelect.value = nodeId;
      }}

      if (mode === 'cabinet' && cabinetForm) {{
        const firstInput = cabinetForm.querySelector('input[name="name"]');
        if (firstInput) firstInput.focus();
      }}
      if (mode === 'register' && registerForm) {{
        const firstInput = registerForm.querySelector('input[name="name"]');
        if (firstInput) firstInput.focus();
      }}

      window.location.hash = 'quick-create';
    }};

    document.querySelectorAll('[data-menu]').forEach((node) => {{
      const trigger = node.querySelector('.tree-menu-button');
      if (!trigger || !treeContextMenu) return;
      trigger.addEventListener('click', (event) => {{
        event.preventDefault();
        event.stopPropagation();
        node.appendChild(treeContextMenu);
        const actions = JSON.parse(node.dataset.menu || '[]');
        if (!actions.length) {{
          treeContextMenu.innerHTML = `<div class="context-menu-empty">Keine passenden Objekte definiert</div>`;
        }} else {{
          const groups = [];
          const grouped = new Map();
          actions.forEach((action) => {{
            const group = action.group || 'Aktionen';
            if (!grouped.has(group)) {{
              grouped.set(group, []);
              groups.push(group);
            }}
            grouped.get(group).push(action);
          }});
          treeContextMenu.innerHTML = groups.map((group) => `<div class="context-menu-group"><div class="context-menu-group-label">${{group}}</div>${{grouped.get(group).map((action) => `<button type="button" data-action="${{action.action}}" data-kind="${{action.kind || ''}}" data-id="${{action.id || ''}}" data-label="${{action.label || ''}}" data-document-type-id="${{action.document_type_id || ''}}" data-document-type-name="${{action.document_type_name || ''}}">${{action.title}}</button>`).join('')}}</div>`).join('');
        }}
        treeContextMenu.style.left = 'auto';
        treeContextMenu.style.right = '0';
        treeContextMenu.style.top = 'calc(100% + 6px)';
        treeContextMenu.classList.add('open');
        treeContextMenu.setAttribute('aria-hidden', 'false');
      }});
    }});

    document.addEventListener('click', (event) => {{
      if (!treeContextMenu) return;
      if (!treeContextMenu.contains(event.target)) {{
        treeContextMenu.classList.remove('open');
        treeContextMenu.setAttribute('aria-hidden', 'true');
      }}
    }});

    if (treeContextMenu) {{
      treeContextMenu.addEventListener('click', (event) => {{
        const button = event.target.closest('button[data-action]');
        if (!button) return;
        const action = button.dataset.action;
        const kind = button.dataset.kind || '';
        const id = button.dataset.id || '';
        const label = button.dataset.label || '';
        const documentTypeId = button.dataset.documentTypeId || '';
        if (action === 'new-cabinet') openQuickCreate('cabinet', kind, id, label);
        if (action === 'new-register') openQuickCreate('register', kind, id, label);
        if (action === 'edit-metadata') {{
          const url = new URL('/ui/admin', window.location.origin);
          window.location.href = url.toString();
        }}
        if (action === 'delete-node') {{
          alert('Löschen im App-Kontext ist noch nicht verdrahtet. Bitte vorerst im Admin löschen.');
        }}
        if (action === 'new-document') {{
          const url = new URL(window.location.href);
          if (documentTypeId) url.searchParams.set('selected_document_type_id', documentTypeId);
          if (kind) url.searchParams.set('node_kind', kind);
          if (id) url.searchParams.set('node_id', id);
          url.hash = 'intake-form';
          window.location.href = url.toString();
        }}
        if (action === 'new-cabinet-type') window.location.href = '/ui/admin';
        treeContextMenu.classList.remove('open');
        treeContextMenu.setAttribute('aria-hidden', 'true');
      }});
    }}

    if (documentTypeSelect) {{
      documentTypeSelect.addEventListener('change', (event) => {{
        const value = event.target.value;
        const url = new URL(window.location.href);
        url.searchParams.set('selected_document_type_id', value);
        window.location.href = url.toString();
      }});
    }}

    const quickCreateDocumentForm = document.getElementById('quick-create-document-form');
    if (quickCreateDocumentForm) {{
      quickCreateDocumentForm.addEventListener('submit', () => {{
        window.location.hash = 'intake-form';
      }});
    }}

    if (fileDropzone && fileInput) {{
      const setFile = (fileList, skipHashCheck) => {{
        if (!fileList || !fileList.length) return;
        const file = fileList[0];
        fileInput.files = fileList;
        if (dropzoneHint) {{
          dropzoneHint.textContent = `Ausgewählt: ${{file.name}}`;
        }}
        if (skipHashCheck) return;
        const reader = new FileReader();
        reader.onload = async (e) => {{
          const buffer = e.target?.result;
          if (!buffer) return;
          let hashHex = '';
          try {{
            const shaBuffer = await crypto.subtle.digest('SHA-256', buffer);
            const shaArray = new Uint8Array(shaBuffer);
            hashHex = Array.from(shaArray).map(b => b.toString(16).padStart(2, '0')).join('').substring(0, 32);
          }} catch (err) {{
            console.error('Hash computation failed:', err);
            return;
          }}
          const hashInput = document.getElementById('file-hash-input');
          if (hashInput) hashInput.value = hashHex;
          const docTypeSelect = document.getElementById('document-type-select');
          const docTypeId = docTypeSelect?.value;
          try {{
            const resp = await fetch(`/api/v1/duplicate-check?hash=${{encodeURIComponent(hashHex)}}${{docTypeId ? '&document_type_id=' + docTypeId : ''}}`);
            const data = await resp.json();
            const warning = document.getElementById('duplicate-warning');
            const info = document.getElementById('duplicate-info');
            const submitBtn = document.getElementById('intake-submit-btn');
            if (data.duplicate && data.existing_document) {{
              if (warning) {{ warning.style.display = 'block'; warning.style.background = 'rgba(255,123,123,0.10)'; warning.style.borderColor = 'rgba(255,123,123,0.30)'; }}
              if (info) {{ const date = data.existing_document.created_at ? new Date(data.existing_document.created_at).toLocaleDateString('de-DE') : 'unbekannt'; info.textContent = `Existiert bereits: "${{data.existing_document.name}}" (hochgeladen am ${{date}}) - Dokument wird nicht erneut gespeichert.`; }}
              if (submitBtn) submitBtn.disabled = true;
            }} else {{
              if (warning) warning.style.display = 'none';
              if (submitBtn) submitBtn.disabled = false;
            }}
          }} catch (err) {{ console.error('Duplicate check failed:', err); }}
        }};
        reader.readAsArrayBuffer(file);
      }};

      fileDropzone.addEventListener('click', () => fileInput.click());
      fileInput.addEventListener('change', () => setFile(fileInput.files));

      ['dragenter', 'dragover'].forEach((eventName) => {{
        fileDropzone.addEventListener(eventName, (event) => {{
          event.preventDefault();
          fileDropzone.style.borderColor = '#6ea8fe';
        }});
      }});

      ['dragleave', 'drop'].forEach((eventName) => {{
        fileDropzone.addEventListener(eventName, (event) => {{
          event.preventDefault();
          fileDropzone.style.borderColor = '';
        }});
      }});

      fileDropzone.addEventListener('drop', (event) => {{
        const files = event.dataTransfer?.files;
        if (!files || !files.length) return;
        const dataTransfer = new DataTransfer();
        dataTransfer.items.add(files[0]);
        setFile(dataTransfer.files);
      }});
    }}
  </script>
</body>
</html>
"""


def _render_document_detail_page(
    document: Document,
    cabinets: list[Cabinet],
    *,
    message: str | None = None,
    error_field: str | None = None,
    error_message: str | None = None,
    form_values: dict[str, Any] | None = None,
) -> str:
    metadata = metadata_from_json(document.metadata_json) or {}
    effective_form_values = form_values or metadata
    metadata_rows = "".join(
        f'<div class="detail-row"><div class="detail-key">{_escape(key)}</div><div class="detail-value">{_escape(", ".join(value) if isinstance(value, list) else value)}</div></div>'
        for key, value in metadata.items()
    ) or "<p class='muted'>Keine Metadaten vorhanden.</p>"
    document_type_label = document.document_type.name if document.document_type else "Ohne Dokumenttyp"
    move_resolution = _build_move_resolution(document, cabinets)
    current_cabinet = move_resolution["current_cabinet"]
    candidate_type_name = move_resolution["candidate_type_name"]
    candidate_cabinets = move_resolution["candidate_cabinets"]
    legacy_candidate_cabinets = move_resolution["legacy_candidate_cabinets"]
    resolution_path = move_resolution["resolution_path"]

    move_cabinet_options = "".join(
        f'<option value="{cab.id}" {"selected" if document.cabinet_id == cab.id else ""}>{_escape((cab.cabinet_type.name + " → ") if not candidate_type_name and cab.cabinet_type else "")}{_escape((cab.name or "").strip())}</option>'
        for cab in candidate_cabinets
    )
    if not move_cabinet_options and legacy_candidate_cabinets:
        move_cabinet_options = "".join(
            f'<option value="{str(item["id"])}">{_escape(str(item.get("name") or "").strip())}</option>'
            for item in legacy_candidate_cabinets
        )
    if not move_cabinet_options:
        move_cabinet_options = '<option value="">Keine konkreten Cabinets gefunden</option>'
    resolved_document_cabinet = _resolved_document_cabinet(document)
    current_cabinet_label = resolved_document_cabinet.name if resolved_document_cabinet else "Nicht zugeordnet"
    current_cabinet_type_label = resolved_document_cabinet.cabinet_type.name if resolved_document_cabinet and resolved_document_cabinet.cabinet_type else "Unbekannt"
    status_label = "Klassifiziert" if document.document_type else "Offen"
    download_link = f"/ui/app/documents/{document.id}/download"
    banner_class = "panel" + (" error-banner" if error_message else " success-banner") if message else ""
    message_html = f'<div class="{banner_class}"><strong>{_escape(message)}</strong></div>' if message else ""
    preview_html = _render_document_preview(document, download_link)
    detail_logo = """
    <svg viewBox='0 0 64 64' fill='none' xmlns='http://www.w3.org/2000/svg'>
      <path d='M14 44L26 18H34L22 44H14Z' fill='#4F8CFF'/>
      <path d='M26 44L36 24H44L34 44H26Z' fill='#4DD4FF'/>
      <path d='M36 44L44 30H52L44 44H36Z' fill='#6EE7B7'/>
    </svg>
    """

    edit_fields_html = "<p class='muted'>Keine bearbeitbaren Metadaten vorhanden.</p>"
    effective_document_fields = _definition_fields_for_document_type(document.document_type)
    if document.document_type:
        field_html_parts: list[str] = []
        for field in effective_document_fields:
            input_name = f"metadata_{field.name}"
            label = field.label or field.name
            placeholder = field.placeholder or ""
            required = "required" if field.is_required else ""
            value = effective_form_values.get(field.name, field.default_value or "")
            safe_label = _escape(label)
            safe_placeholder = _escape(placeholder)
            required_badge = ' <span class="required-badge">Pflicht</span>' if field.is_required else ''
            field_error_class = " error" if error_field == field.name else ""
            options = _parse_field_options(field)
            if field.field_type == "long_text":
                control = f'<textarea name="{input_name}" placeholder="{safe_placeholder}" {required}>{_escape(value)}</textarea>'
            elif field.field_type == "boolean":
                checked = "checked" if str(value).lower() == "true" else ""
                control = (
                    f'<label class="toggle">'
                    f'<input type="hidden" name="{input_name}" value="false">'
                    f'<input type="checkbox" name="{input_name}" value="true" {checked}>'
                    f'<span class="toggle-slider"></span>'
                    f'<span class="toggle-label">Ja / Nein</span>'
                    f'</label>'
                )
            elif field.field_type == "selection" and options:
                option_html = "".join(
                    f'<option value="{_escape(option)}" {"selected" if str(value) == option else ""}>{_escape(option)}</option>'
                    for option in options
                )
                control = f'<select name="{input_name}" {required}><option value="">Bitte wählen</option>{option_html}</select>'
            elif field.field_type == "multi_selection" and options:
                selected_values = value if isinstance(value, list) else [part.strip() for part in str(value).split(",") if part.strip()]
                control = f'<div class="checkbox-group">' + ''.join(
                    f'<label class="checkbox-item"><input type="checkbox" name="{input_name}" value="{_escape(option)}" {"checked" if option in selected_values else ""}> {_escape(option)}</label>'
                    for option in options
                ) + '</div>'
            elif field.field_type == "date":
                control = f'<input type="date" name="{input_name}" value="{_escape(value)}" placeholder="{safe_placeholder}" {required}>'
            elif field.field_type == "datetime":
                control = f'<input type="datetime-local" name="{input_name}" value="{_escape(value)}" placeholder="{safe_placeholder}" {required}>'
            elif field.field_type in ("number", "currency"):
                control = f'<input type="number" step="any" name="{input_name}" value="{_escape(value)}" placeholder="{safe_placeholder}" {required}>'
            elif field.field_type == "email":
                control = f'<input type="email" name="{input_name}" value="{_escape(value)}" placeholder="{safe_placeholder}" {required}>'
            elif field.field_type == "url":
                control = f'<input type="url" name="{input_name}" value="{_escape(value)}" placeholder="{safe_placeholder}" {required}>'
            elif field.field_type == "phone":
                control = f'<input type="tel" name="{input_name}" value="{_escape(value)}" placeholder="{safe_placeholder}" {required}>'
            else:
                control = f'<input type="text" name="{input_name}" value="{_escape(value)}" placeholder="{safe_placeholder}" {required}>'
            description_html = f'<div class="muted field-help">{_escape(field.description)}</div>' if field.description else ""
            field_error_html = f'<div class="field-error">{_escape(error_message)}</div>' if error_field == field.name and error_message else ""
            field_html_parts.append(f'<div class="field full{field_error_class}"><label>{safe_label}{required_badge}</label>{control}{description_html}{field_error_html}</div>')
        edit_fields_html = ''.join(field_html_parts)

    return f"""
<!doctype html>
<html lang="de">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Archiva Dokument</title>
  <link rel="icon" type="image/svg+xml" href="/assets/archiva-favicon.svg">
  <style>
    :root {{ color-scheme: dark; --bg: #0b1020; --panel: #121933; --panel-soft: #1a2345; --panel-deep:#0f1630; --text: #eef2ff; --muted: #a8b2d1; --accent: #4f8cff; --accent-2:#4dd4ff; --border: #2d3b69; --success:#6ee7b7; --glow: rgba(77,212,255,0.18); --shadow:0 18px 48px rgba(0,0,0,0.28); }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; font-family: Inter, ui-sans-serif, system-ui, sans-serif; background: radial-gradient(circle at top left, rgba(77,212,255,0.08), transparent 30%), var(--bg); color: var(--text); }}
    a {{ color: var(--accent-2); text-decoration: none; }} a:hover {{ text-decoration: none; }}
    .page {{ max-width: 1440px; margin: 0 auto; padding: 16px; }}
    .panel {{ background: linear-gradient(180deg, rgba(18,25,51,0.96), rgba(15,22,48,0.96)); border:1px solid rgba(77,212,255,0.10); border-radius:18px; padding:14px; margin-bottom:14px; box-shadow:var(--shadow); }}
    .success-banner {{ background:rgba(110,231,183,.10); border-color:rgba(110,231,183,.26); color:#d9ffec; }}
    .error-banner {{ background:rgba(255,120,120,.12); border-color:rgba(255,120,120,.28); color:#ffd0d0; }}
    .hero {{ display:grid; grid-template-columns: minmax(0, 1.25fr) minmax(240px, 0.75fr); gap:12px; align-items:stretch; margin-bottom:12px; }}
    .hero-main {{ position:relative; overflow:hidden; }}
    .hero-main::before {{ content:""; position:absolute; inset:0; background: linear-gradient(135deg, rgba(79,140,255,0.12), rgba(77,212,255,0.05) 45%, transparent 80%); pointer-events:none; }}
    .hero-brand {{ display:flex; gap:10px; align-items:center; position:relative; z-index:1; }}
    .brand-mark {{ width:42px; height:42px; border-radius:14px; display:grid; place-items:center; background: linear-gradient(135deg, rgba(79,140,255,0.28), rgba(77,212,255,0.18)); border:1px solid rgba(77,212,255,0.25); }}
    .brand-mark svg {{ width:26px; height:26px; }}
    .eyebrow {{ letter-spacing:.12em; text-transform:uppercase; font-size:.72rem; color:var(--accent-2); font-weight:700; }}
    .pillbar {{ display:flex; flex-wrap:wrap; gap:8px; margin-top:8px; position:relative; z-index:1; }}
    .pill {{ background: rgba(255,255,255,0.04); border:1px solid rgba(77,212,255,0.10); border-radius:999px; padding:6px 10px; color:var(--text); font-size:.9rem; }}
    .actions {{ display:flex; gap:10px; flex-wrap:wrap; margin-top:10px; position:relative; z-index:1; }}
    .button {{ display:inline-flex; align-items:center; border-radius:999px; padding:9px 13px; border:1px solid rgba(77,212,255,0.10); background:rgba(255,255,255,0.03); color:var(--text); }}
    .button.primary {{ background: linear-gradient(135deg, var(--accent), var(--accent-2)); border:none; color:white; box-shadow: 0 8px 24px rgba(77,212,255,0.22); }}
    .detail-grid {{ display:grid; grid-template-columns: 1.18fr 0.82fr; gap:14px; }}
    .detail-row {{ display:block; padding:10px 0; border-bottom:1px solid rgba(255,255,255,.06); }}
    .panel.compact-indexdata .detail-row {{ padding:12px 0; }}
    .detail-key {{ display:block; color:var(--accent-2); font-weight:700; text-transform:uppercase; letter-spacing:.08em; font-size:.74rem; margin-bottom:6px; opacity:.95; }}
    .panel.compact-indexdata .detail-key {{ color:#7dd3fc; font-size:.72rem; letter-spacing:.12em; margin-bottom:8px; }}
    .detail-value {{ display:block; color:var(--text); word-break:break-word; font-size:1.02rem; line-height:1.5; background:rgba(255,255,255,0.03); border:1px solid rgba(77,212,255,0.10); border-radius:12px; padding:10px 12px; }}
    .panel.compact-indexdata .detail-value {{ background:rgba(255,255,255,0.06); border:1px solid rgba(77,212,255,0.16); border-radius:14px; padding:12px 14px; box-shadow:inset 0 1px 0 rgba(255,255,255,0.04); }}
    .field-grid {{ display:grid; grid-template-columns: repeat(2, minmax(0,1fr)); gap:10px; }}
    .field {{ display:grid; gap:6px; }}
    .field.full {{ grid-column: 1 / -1; }}
    .field.error label {{ color:#ff8f8f; }}
    .field.error input, .field.error textarea, .field.error select, .field.error .checkbox-group {{ border-color:#ff8f8f; box-shadow:0 0 0 1px rgba(255,143,143,.25) inset; }}
    label {{ font-weight:600; font-size:0.95rem; }}
    input, textarea, select {{ width:100%; border-radius:12px; border:1px solid rgba(77,212,255,0.10); background:var(--panel-deep); color:var(--text); padding:10px; font:inherit; }}
    textarea {{ min-height:84px; resize:vertical; }}
    .checkbox-group {{ display:grid; gap:10px; padding:14px; border:1px solid rgba(77,212,255,0.08); border-radius:16px; background:var(--panel-deep); }}
    .checkbox-item {{ display:flex; gap:10px; align-items:center; font-weight:400; padding:8px 10px; border-radius:12px; background:rgba(255,255,255,0.02); }}
    .checkbox-item input {{ width:auto; }}
    .toggle {{ display:inline-flex; align-items:center; gap:12px; cursor:pointer; }}
    .toggle input[type="hidden"] {{ display:none; }}
    .toggle input[type="checkbox"] {{ display:none; }}
    .toggle-slider {{ position:relative; width:52px; height:30px; background:#33406b; border-radius:999px; transition:background .2s ease; }}
    .toggle-slider::after {{ content:""; position:absolute; top:3px; left:3px; width:24px; height:24px; background:white; border-radius:50%; transition:transform .2s ease; }}
    .toggle input[type="checkbox"]:checked + .toggle-slider {{ background:var(--accent); }}
    .toggle input[type="checkbox"]:checked + .toggle-slider::after {{ transform:translateX(22px); }}
    .toggle-label {{ color:var(--muted); font-weight:500; }}
    .field-help {{ font-size:.9rem; color:var(--muted); }}
    .field-error {{ color:#ff8f8f; font-size:.92rem; font-weight:600; }}
    .required-badge {{ display:inline-block; margin-left:8px; padding:2px 8px; border-radius:999px; font-size:.78rem; background:rgba(77,212,255,.16); color:var(--accent-2); vertical-align:middle; }}
    .muted {{ color: var(--muted); }}
    .preview-shell {{ border:1px solid rgba(77,212,255,.10); border-radius:18px; overflow:hidden; background:#09101f; width:100%; height:calc(100vh - 220px); min-height:900px; }}
    .preview-frame {{ width:100%; height:100%; min-height:900px; border:0; background:#0a1224; display:block; }}
    .preview-image-wrap {{ display:flex; justify-content:center; align-items:flex-start; padding:0; background:#09101f; min-height:760px; width:100%; }}
    .preview-image {{ width:100%; height:auto; max-height:none; object-fit:contain; border-radius:0; box-shadow:none; display:block; }}
    .preview-placeholder {{ padding:20px; border:1px dashed rgba(77,212,255,.18); border-radius:14px; background:rgba(255,255,255,.03); }}
    details.compact-panel {{ border:1px solid rgba(77,212,255,0.08); border-radius:14px; background:rgba(255,255,255,0.02); padding:0; }}
    details.compact-panel summary {{ list-style:none; cursor:pointer; padding:12px 14px; font-weight:600; display:flex; align-items:center; justify-content:space-between; gap:12px; }}
    details.compact-panel summary::-webkit-details-marker {{ display:none; }}
    details.compact-panel .compact-body {{ padding:0 14px 14px; }}
    details.compact-panel summary .summary-meta {{ color:var(--muted); font-size:.88rem; font-weight:500; }}
    .service-badge {{ display:inline-flex; align-items:center; gap:8px; padding:6px 10px; border-radius:999px; background:rgba(110,231,183,0.10); border:1px solid rgba(110,231,183,0.18); color:#d6fff0; font-size:.85rem; }}
    .status-dot {{ width:10px; height:10px; border-radius:999px; background: var(--success); box-shadow: 0 0 12px rgba(110,231,183,0.5); }}
    pre {{ margin:0; white-space:pre-wrap; word-break:break-word; font:inherit; color:var(--text); }}
    @media (max-width: 900px) {{ .hero, .detail-grid, .detail-row {{ display:block; }} .detail-row {{ padding:14px 0; }} .detail-key {{ margin-bottom:6px; }} .field-grid {{ grid-template-columns:1fr; }} .preview-frame {{ min-height:480px; }} }}
  </style>
</head>
<body>
  <div class="page">
    {message_html}
    <div class="hero">
      <div class="panel hero-main">
        <a href="/ui/app">← Zur Übersicht</a>
        <div class="hero-brand" style="margin-top:12px;">
          <div class="brand-mark"><img src="/assets/archiva-logo-flow.svg" alt="Archiva Logo" style="width:32px;height:32px;display:block;"></div>
          <div>
            <div class="eyebrow">Document Flow</div>
            <h1 style="margin:4px 0 5px; font-size:1.4rem; line-height:1.1;">{_escape(document.title or document.name)}</h1>
            <div class="muted" style="font-size:.92rem;">Dateiname: {_escape(document.name)}</div>
          </div>
        </div>
        <div class="pillbar">
          <span class="pill">{_escape(document_type_label)}</span>
          <span class="pill">Status: {_escape(status_label)}</span>
          <span class="pill">Typ: {_escape(str(document.doc_type))}</span>
          <span class="pill">Größe: {_escape(str(document.size_bytes))} Bytes</span>
        </div>
        <div class="actions">
          <a class="button primary" href="{download_link}">Download</a>
          <a class="button" href="#metadata-edit">Metadaten editieren</a>
          <a class="button" href="/ui/app">Zurück zur Übersicht</a>
        </div>
        <div class="panel compact-indexdata" style="margin-top:12px; margin-bottom:0; position:relative; z-index:1;">
          <h2 style="margin-top:0;">Metadaten</h2>
          {metadata_rows}
        </div>
      </div>
      <div class="panel">
        <h2 style="margin-top:0;">Vorschau</h2>
        {preview_html}
      </div>
    </div>

    <div class="detail-grid">
      <div>
        <div class="panel" id="metadata-edit">
          <details class="compact-panel" open>
            <summary>
              <span>Metadaten bearbeiten</span>
              <span class="summary-meta">öffnen</span>
            </summary>
            <div class="compact-body">
              <form method="post" action="/ui/app/documents/{document.id}/metadata">
                <div class="field-grid">
                  {edit_fields_html}
                </div>
                <div class="actions">
                  <button class="button primary" type="submit">Metadaten speichern</button>
                </div>
              </form>
            </div>
          </details>
        </div>
      </div>
      <div>
        <div class="panel">
          <div style="display:flex; justify-content:space-between; gap:10px; align-items:flex-start;">
            <div>
              <h3 style="margin-top:0; margin-bottom:6px;">Dokumentinfo</h3>
            </div>
            <span class="service-badge"><span class="status-dot"></span> aktiv</span>
          </div>
          <div class="detail-row"><div class="detail-key">Erstellt</div><div class="detail-value">{_escape(str(document.created_at))}</div></div>
          <div class="detail-row"><div class="detail-key">Aktualisiert</div><div class="detail-value">{_escape(str(document.updated_at))}</div></div>
          <div class="detail-row"><div class="detail-key">MIME-Type</div><div class="detail-value">{_escape(document.mime_type or 'unbekannt')}</div></div>
          <div class="detail-row"><div class="detail-key">Storage-Pfad</div><div class="detail-value"><pre>{_escape(document.storage_path)}</pre></div></div>
          <div class="detail-row"><div class="detail-key">Cabinettyp</div><div class="detail-value">{_escape(current_cabinet_type_label)}</div></div>
          <div class="detail-row"><div class="detail-key">Cabinet</div><div class="detail-value">{_escape(current_cabinet_label)}</div></div>
        </div>
        <div class="panel">
          <h2 style="margin-top:0;">Einordnung</h2>
          <div class="detail-row"><div class="detail-key">Dokumenttyp</div><div class="detail-value">{_escape(document_type_label)}</div></div>
          <div class="detail-row"><div class="detail-key">Cabinet</div><div class="detail-value">{_escape(current_cabinet_label)}</div></div>
          <div class="detail-row"><div class="detail-key">Interner Dateityp</div><div class="detail-value">{_escape(str(document.doc_type))}</div></div>
          <div class="detail-row"><div class="detail-key">Dokument-ID</div><div class="detail-value"><pre>{_escape(str(document.id))}</pre></div></div>
        </div>
        <div class="panel">
          <h2 style="margin-top:0;">Verschieben</h2>
          <p class="muted">Dokument in ein anderes Cabinet desselben Cabinettyps verschieben.</p>
          {f'<form method="post" action="/ui/app/documents/{document.id}/cabinet"><div class="field-grid"><div class="field full"><label>Ziel-Cabinet</label><select name="cabinet_id" required>{move_cabinet_options}</select></div></div><div class="actions"><button class="button primary" type="submit">Cabinet ändern</button></div></form>' if move_cabinet_options else '<p class="muted">Kein Ziel-Cabinet verfügbar. Erst Cabinet-Zuordnung oder Cabinettyp prüfen.</p>'}
        </div>
      </div>
    </div>
  </div>
</body>
</html>
"""


def _render_document_preview(document: Document, download_link: str) -> str:
    preview_link = f"/ui/preview/documents/{document.id}"
    return (
        '<div class="preview-shell">'
        f'<iframe class="preview-frame" src="{preview_link}" title="Dokumentvorschau"></iframe>'
        '</div>'
        f'<div class="actions"><a class="button" href="{download_link}" target="_blank" rel="noopener noreferrer">Original herunterladen</a></div>'
    )


def _render_preview_waiting_state(document_id: UUID, document_name: str, status: str, error_message: str | None = None) -> bytes:
    error_html = f'<p><strong>Fehler:</strong> {_escape(error_message)}</p>' if error_message else ''
    html = f"""
<!doctype html>
<html lang=\"de\">
<head>
  <meta charset=\"utf-8\">
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">
  <title>Preview wird erstellt</title>
  <style>
    body {{ margin:0; font-family: Inter, ui-sans-serif, system-ui, sans-serif; background:#0b1020; color:#eef2ff; display:flex; align-items:center; justify-content:center; min-height:100vh; }}
    .card {{ max-width:640px; margin:24px; padding:24px; border-radius:18px; background:#121933; border:1px solid #2d3b69; }}
    p {{ color:#a8b2d1; line-height:1.5; }}
  </style>
</head>
<body>
  <div class=\"card\">
    <h1>Vorschau wird vorbereitet</h1>
    <p>Für <strong>{_escape(document_name)}</strong> wurde ein Rendering-Auftrag in die Queue gestellt.</p>
    <p>Aktueller Status: <strong id=\"preview-status\">{_escape(status)}</strong></p>
    {error_html}
    <p>Die Ansicht aktualisiert sich automatisch, sobald das Preview-Artefakt fertig ist.</p>
  </div>
  <script>
    const statusUrl = '/ui/preview/documents/{document_id}/status';
    const poll = async () => {{
      try {{
        const res = await fetch(statusUrl, {{ cache: 'no-store' }});
        if (!res.ok) return;
        const data = await res.json();
        const el = document.getElementById('preview-status');
        if (el) el.textContent = data.status;
        if (data.artifact_ready && data.artifact_url) {{
          window.location.replace(data.artifact_url);
          return;
        }}
      }} catch (err) {{
        console.warn('preview status polling failed', err);
      }}
      window.setTimeout(poll, 2000);
    }};
    window.setTimeout(poll, 1200);
  </script>
</body>
</html>
"""
    return html.encode("utf-8")


def _render_workflow_designer_page(*, workflows: list[WorkflowDefinition], assignment_targets: list[AssignmentTarget], selected_workflow: WorkflowDefinition | None, selected_step: WorkflowStepDefinition | None, message: str | None = None) -> str:
    workflow_cards: list[str] = []
    for workflow in workflows:
        active_badge = "aktiv" if workflow.is_active else "inaktiv"
        workflow_cards.append(
            f"<a class='workflow-card' href='/ui/workflow-designer?selected_workflow_id={workflow.id}#designer'><strong>{_escape(workflow.name)}</strong><div class='muted'>{_escape(workflow.description or 'Keine Beschreibung')}</div><div class='pillbar' style='margin-top:10px;'><span class='pill'>{active_badge}</span><span class='pill'>v{workflow.version}</span><span class='pill'>{len(workflow.steps)} Schritte</span></div></a>"
        )
    workflows_html = "".join(workflow_cards) or "<div class='muted'>Noch keine Workflows definiert.</div>"

    def assignment_label_for_target(target: AssignmentTarget | None) -> str:
        if not target:
            return "Nicht zugewiesen"
        return target.label or (
            target.user.display_name if target.user else (
                target.role.name if target.role else (
                    target.team.name if target.team else "Unbenannt"
                )
            )
        )

    assignment_options_create_rows = ['<option value="">Nicht zuweisen</option>']
    assignment_options_edit_rows = ['<option value="">Nicht zuweisen</option>']
    for target in assignment_targets:
        target_label = assignment_label_for_target(target)
        option_label = f"{target.target_type} · {target_label}"
        assignment_options_create_rows.append(
            f'<option value="{target.id}">{_escape(option_label)}</option>'
        )
        selected_attr = " selected" if selected_step and selected_step.assignment_target_id == target.id else ""
        assignment_options_edit_rows.append(
            f'<option value="{target.id}"{selected_attr}>{_escape(option_label)}</option>'
        )
    assignment_options_create_html = "".join(assignment_options_create_rows)
    assignment_options_edit_html = "".join(assignment_options_edit_rows)

    sorted_steps: list[WorkflowStepDefinition] = []
    step_cards: list[str] = []
    workflow_graph_nodes: list[str] = []
    if selected_workflow:
        sorted_steps = sorted(selected_workflow.steps, key=lambda item: (item.order, item.name.lower()))
        for index, step in enumerate(sorted_steps, start=1):
            assignment_label = assignment_label_for_target(step.assignment_target)
            selected_class = " is-selected" if selected_step and selected_step.id == step.id else ""
            workflow_graph_nodes.append(
                f"<div class='workflow-graph-node{selected_class}' draggable='true' data-step-id='{step.id}'><div class='workflow-graph-order'>{step.order}</div><strong><a href='/ui/workflow-designer?selected_workflow_id={selected_workflow.id}&selected_step_id={step.id}#designer'>{_escape(step.name)}</a></strong><div class='muted' style='font-size:.92rem;'>{_escape(assignment_label)}</div><div class='workflow-graph-key'>{_escape(step.step_key)}</div></div>"
            )
            transition_count = len(step.outgoing_transitions)
            default_transition_count = sum(1 for transition in step.outgoing_transitions if transition.is_default)
            transition_summary = f"{transition_count} Transitionen"
            if default_transition_count:
                transition_summary += f" · {default_transition_count} Standard"
            delete_hint = 'Schritt erst löschen, wenn alle eingehenden und ausgehenden Transitionen entfernt sind.' if transition_count else 'Schritt kann gelöscht werden.'
            step_cards.append(
                f"<div class='panel' style='margin-bottom:0;'><div class='eyebrow'>Schritt {index}</div><h3 style='margin:6px 0 6px;'><a href='/ui/workflow-designer?selected_workflow_id={selected_workflow.id}&selected_step_id={step.id}#designer'>{_escape(step.name)}</a></h3><p class='muted' style='margin:0 0 12px 0;'>{_escape(step.description or 'Keine Beschreibung')}</p><div class='pillbar'><span class='pill'>Key: {_escape(step.step_key)}</span><span class='pill'>Reihenfolge: {step.order}</span><span class='pill'>Zuweisung: {_escape(assignment_label)}</span><span class='pill'>Frist: {step.due_in_days if step.due_in_days is not None else '—'} Tage</span></div><div class='workflow-step-summary'><span class='pill'>{_escape(transition_summary)}</span></div><div class='workflow-transition-hint'>{'Mehrere Ausgänge vorhanden.' if transition_count > 1 else ('Ein Standardpfad ist gesetzt.' if default_transition_count else 'Noch kein Standardpfad definiert.')} { _escape(delete_hint) }</div><div class='actions'><form method='post' action='/ui/workflow-designer/steps/{step.id}/move'><input type='hidden' name='direction' value='up'><button type='submit'>↑ Hoch</button></form><form method='post' action='/ui/workflow-designer/steps/{step.id}/move'><input type='hidden' name='direction' value='down'><button type='submit'>↓ Runter</button></form><form method='post' action='/ui/workflow-designer/steps/{step.id}/delete' onsubmit=\"return confirm('Schritt wirklich löschen?');\"><button type='submit'>Schritt löschen</button></form></div></div>"
            )

    workflow_graph_html = "<div class='workflow-graph-lane'><div class='workflow-graph-empty muted'>Noch keine Schritte für die grafische Ansicht vorhanden.</div></div>"
    if workflow_graph_nodes and selected_workflow:
        ordered_ids = ",".join(str(step.id) for step in sorted_steps)
        selected_step_id_value = str(selected_step.id) if selected_step else ""
        workflow_graph_html = (
            f"<form method='post' action='/ui/workflow-designer/workflows/{selected_workflow.id}/reorder' class='workflow-graph-form' id='workflow-graph-form'>"
            f"<input type='hidden' name='step_ids' id='workflow-graph-step-ids' value='{ordered_ids}'>"
            f"<input type='hidden' name='selected_step_id' id='workflow-graph-selected-step-id' value='{selected_step_id_value}'>"
            "<div class='workflow-graph-lane'><div class='workflow-graph-start'>Start</div><div class='workflow-graph-arrow'>→</div><div class='workflow-graph-track'><div class='workflow-graph-sequence' id='workflow-graph-sequence'>"
            + "<div class='workflow-graph-connector'></div>".join(workflow_graph_nodes)
            + "</div></div><div class='workflow-graph-arrow'>→</div><div class='workflow-graph-end'>Ende</div></div><div class='actions' style='margin-top:12px;'><button type='submit'>Grafische Reihenfolge speichern</button></div></form>"
        )

    transition_target_options_rows: list[str] = []
    transition_target_options_by_selected_step_id: dict[str, str] = {}
    if selected_step:
        for candidate_step in sorted_steps:
            if candidate_step.id == selected_step.id:
                continue
            transition_target_options_rows.append(
                f'<option value="{candidate_step.id}">{_escape(candidate_step.name)}</option>'
            )
        for transition_step in sorted_steps:
            options_rows_for_step: list[str] = []
            for candidate_step in sorted_steps:
                if candidate_step.id == transition_step.id:
                    continue
                options_rows_for_step.append(
                    f'<option value="{candidate_step.id}">{_escape(candidate_step.name)}</option>'
                )
            transition_target_options_by_selected_step_id[str(transition_step.id)] = "".join(options_rows_for_step) or '<option value="">Kein anderer Schritt verfügbar</option>'
    transition_target_options_html = "".join(transition_target_options_rows) or '<option value="">Kein anderer Schritt verfügbar</option>'

    step_transition_rows: list[str] = []
    if selected_step:
        sorted_transitions = sorted(selected_step.outgoing_transitions, key=lambda item: (0 if item.is_default else 1, item.label.lower(), str(item.id)))
        if sorted_transitions:
            edit_transition_cards: list[str] = []
            for transition in sorted_transitions:
                target_label = transition.to_step.name if transition.to_step else "Unbekannt"
                default_badge = " <span class='pill'>Standard</span>" if transition.is_default else ""
                selected_target_options_rows: list[str] = []
                for candidate_step in sorted_steps:
                    if candidate_step.id == selected_step.id:
                        continue
                    selected_attr = " selected" if transition.to_step_id == candidate_step.id else ""
                    selected_target_options_rows.append(
                        f'<option value="{candidate_step.id}"{selected_attr}>{_escape(candidate_step.name)}</option>'
                    )
                transition_target_select_html = "".join(selected_target_options_rows) or '<option value="">Kein anderer Schritt verfügbar</option>'
                edit_transition_cards.append(
                    f"<div class='panel' style='margin-bottom:0;'>"
                    f"<div style='display:flex;justify-content:space-between;gap:12px;align-items:flex-start;flex-wrap:wrap;'>"
                    f"<div><strong>{_escape(transition.label)}</strong>{default_badge}<div class='muted'>→ {_escape(target_label)}</div></div>"
                    f"<form method='post' action='/ui/workflow-designer/transitions/{transition.id}/delete' style='margin:0;'>"
                    f"<button type='submit' onclick=\"return confirm('Transition wirklich löschen?');\">Löschen</button>"
                    f"</form>"
                    f"</div>"
                    f"<form method='post' action='/ui/workflow-designer/transitions/{transition.id}' style='margin-top:14px;'>"
                    f"<div class='field-grid'>"
                    f"<div class='field'><label>Label</label><input type='text' name='label' value='{_escape(transition.label)}' required></div>"
                    f"<div class='field'><label>Zielschritt</label><select name='to_step_id' required>{transition_target_select_html}</select></div>"
                    f"<div class='field full'><label><input type='checkbox' name='is_default' {'checked' if transition.is_default else ''}> Standardübergang</label></div>"
                    f"</div>"
                    f"<div class='actions'><button type='submit'>Transition aktualisieren</button></div>"
                    f"</form>"
                    f"</div>"
                )
            step_transition_rows = edit_transition_cards
        else:
            outgoing_count = len(selected_step.outgoing_transitions)
            step_transition_rows.append(
                f"<div class='panel' style='margin-bottom:0;'><strong>Noch keine Transitionen</strong><div class='muted'>Dieser Schritt hat aktuell {outgoing_count} ausgehende Übergänge.</div></div>"
            )
    step_transitions_html = "".join(step_transition_rows) or "<div class='muted'>Noch keine Transitionen für diesen Schritt.</div>"
    step_cards_html = "".join(step_cards) or "<div class='muted'>Noch keine Schritte angelegt.</div>"
    message_html = f"<div class='panel' style='padding:12px 16px;'><strong>{_escape(message)}</strong></div>" if message else ""

    return f"""
<!doctype html>
<html lang=\"de\">
<head>
  <meta charset=\"utf-8\">
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">
  <title>Archiva Workflow Designer</title>
  <link rel="icon" type="image/svg+xml" href="/assets/archiva-favicon.svg">
  <style>
    :root {{ color-scheme: dark; }}
    body {{ font-family: Inter, ui-sans-serif, system-ui, sans-serif; background: radial-gradient(circle at top left, rgba(77,212,255,0.08), transparent 30%), #0b1020; color: #eef2ff; margin: 0; }}
    .page {{ max-width: 1280px; margin: 0 auto; padding: 28px; }}
    .panel {{ background: linear-gradient(180deg, rgba(18,25,51,0.96), rgba(15,22,48,0.96)); border: 1px solid rgba(77,212,255,0.10); border-radius: 20px; padding: 22px; margin-bottom: 20px; box-shadow: 0 18px 48px rgba(0,0,0,0.28); }}
    a {{ color: #4dd4ff; text-decoration: none; }}
    .muted {{ color: #a8b2d1; line-height: 1.6; }}
    .eyebrow {{ letter-spacing:.12em; text-transform:uppercase; font-size:.78rem; color:#4dd4ff; font-weight:700; }}
    .pillbar {{ display:flex; gap:12px; flex-wrap:wrap; margin-top:16px; }}
    .pill {{ background: rgba(255,255,255,0.04); border:1px solid rgba(77,212,255,0.12); border-radius:999px; padding:9px 14px; color:#eef2ff; }}
    .grid {{ display:grid; grid-template-columns: 340px minmax(0, 1fr); gap:20px; align-items:start; }}
    .stack {{ display:grid; gap:16px; }}
    .workflow-card {{ display:block; padding:16px; border-radius:18px; border:1px solid rgba(77,212,255,0.10); background:rgba(255,255,255,0.03); color:#eef2ff; }}
    .field-grid {{ display:grid; grid-template-columns: repeat(2, minmax(0,1fr)); gap:12px; }}
    .field {{ display:grid; gap:6px; }}
    .field.full {{ grid-column:1 / -1; }}
    label {{ font-weight:600; font-size:.95rem; }}
    input, textarea, select {{ width:100%; border-radius:14px; border:1px solid rgba(77,212,255,0.10); background:#0f1630; color:#eef2ff; padding:12px; font:inherit; }}
    textarea {{ min-height:92px; resize:vertical; }}
    .actions {{ display:flex; gap:10px; flex-wrap:wrap; margin-top:16px; }}
    button {{ border:none; border-radius:999px; padding:10px 14px; font:inherit; cursor:pointer; }}
    .primary {{ background: linear-gradient(135deg, #4f8cff, #4dd4ff); color: white; box-shadow: 0 8px 24px rgba(77,212,255,0.22); }}
    .workflow-graph-lane {{ display:flex; align-items:center; gap:14px; overflow:auto; padding-bottom:8px; }}
    .workflow-graph-track {{ overflow:auto; flex:1; }}
    .workflow-graph-sequence {{ display:flex; align-items:center; gap:0; min-width:max-content; padding:6px 0; }}
    .workflow-graph-form {{ display:block; }}
    .workflow-graph-node {{ min-width:220px; max-width:220px; padding:16px; border-radius:18px; border:1px solid rgba(77,212,255,0.16); background:rgba(255,255,255,0.04); color:#eef2ff; position:relative; box-shadow: 0 12px 32px rgba(0,0,0,0.18); cursor:grab; }}
    .workflow-graph-node.is-selected {{ border-color:#4dd4ff; box-shadow: 0 0 0 1px rgba(77,212,255,0.55), 0 16px 36px rgba(77,212,255,0.16); }}
    .workflow-graph-order {{ display:inline-flex; padding:4px 10px; border-radius:999px; background:rgba(77,212,255,0.12); color:#4dd4ff; font-size:.84rem; margin-bottom:10px; }}
    .workflow-graph-key {{ margin-top:12px; color:#a8b2d1; font-size:.84rem; }}
    .workflow-graph-connector {{ width:54px; height:2px; background:linear-gradient(90deg, rgba(77,212,255,0.30), rgba(77,212,255,0.85)); margin:0 10px; border-radius:999px; }}
    .workflow-graph-start, .workflow-graph-end {{ padding:10px 14px; border-radius:999px; border:1px solid rgba(77,212,255,0.18); background:rgba(255,255,255,0.03); color:#eef2ff; white-space:nowrap; }}
    .workflow-graph-arrow {{ color:#4dd4ff; font-size:1.2rem; }}
    .workflow-graph-empty {{ padding:8px 0; }}
    .workflow-step-summary {{ display:flex; gap:8px; flex-wrap:wrap; margin-top:12px; }}
    .workflow-transition-hint {{ margin-top:10px; font-size:.88rem; color:#a8b2d1; }}
  </style>
</head>
<body>
  <div class=\"page\">
    <div class=\"panel\">
      <div class=\"eyebrow\">Workflow Designer</div>
      <h1>Archiva Workflow Designer</h1>
      <p class=\"muted\">Diese eigene App ist für die Definition von Workflows vorgesehen, getrennt von Admin, Identity und Monitoring. Hier entstehen später Workflow-Definitionen, Schritte, Zuständigkeiten, Regeln, Fristen und Eskalationen.</p>
      <div class=\"pillbar\">
        <span class=\"pill\">Definitionsebene</span>
        <span class=\"pill\">Schritte & Zuständigkeiten</span>
        <span class=\"pill\">Assignment Targets</span>
      </div>
      <div class=\"pillbar\">
        <a class=\"pill\" href=\"/ui/app\">Zur ECM-App</a>
        <a class=\"pill\" href=\"/ui/admin\">Zur Admin-Oberfläche</a>
        <a class=\"pill\" href=\"/ui/admin/identity\">Zur Identity</a>
      </div>
    </div>
    {message_html}
    <div class=\"grid\" id=\"designer\">
      <div class=\"stack\">
        <div class=\"panel\">
          <h2 style=\"margin-top:0;\">Workflows</h2>
          <p class=\"muted\">Ein Workflow ist die Vorlage. Schritte werden darunter in Reihenfolge definiert.</p>
          <div class=\"stack\">{workflows_html}</div>
        </div>
        <form method=\"post\" action=\"/ui/workflow-designer/workflows\" class=\"panel\">
          <h2 style=\"margin-top:0;\">Workflow anlegen</h2>
          <div class=\"field-grid\">
            <div class=\"field\"><label>Name</label><input type=\"text\" name=\"name\" required></div>
            <div class=\"field\"><label><input type=\"checkbox\" name=\"is_active\" checked> Aktiv</label></div>
            <div class=\"field full\"><label>Beschreibung</label><textarea name=\"description\"></textarea></div>
          </div>
          <div class=\"actions\"><button class=\"primary\" type=\"submit\">Workflow speichern</button></div>
        </form>
      </div>
      <div class=\"stack\">
        <div class=\"panel\">
          <h2 style=\"margin-top:0;\">Designer</h2>
          <p class=\"muted\">Schritte werden aktuell über ihre Reihenfolge verknüpft. Die visuelle Kette ist also bewusst einfach: Schritt 10, 20, 30. Zuständigkeiten laufen bereits über Assignment Targets. Diese grafische Ansicht sitzt absichtlich direkt auf dem bestehenden Modell und ist der erste Schritt in Richtung visuellem Workflow-Editor.</p>
          <div class=\"pillbar\">
            <span class=\"pill\">Verknüpfung: Reihenfolge</span>
            <span class=\"pill\">Identität: Assignment Target</span>
            <span class=\"pill\">Grafische Ansicht v1</span>
          </div>
          <div style=\"margin-top:18px;\">{workflow_graph_html}</div>
        </div>
        <div class=\"panel\">
          <h2 style=\"margin-top:0;\">{_escape(selected_workflow.name) if selected_workflow else 'Noch kein Workflow ausgewählt'}</h2>
          <p class=\"muted\">{_escape(selected_workflow.description or 'Wähle links einen Workflow oder lege einen neuen an.') if selected_workflow else 'Ein Workflow sammelt definierte Schritte mit Reihenfolge, Zuständigkeit und Frist.'}</p>
          <div class=\"stack\">{step_cards_html}</div>
        </div>
        <form method=\"post\" action=\"/ui/workflow-designer/steps\" class=\"panel\" style=\"{'display:block;' if selected_workflow else 'display:none;'}\">
          <h2 style=\"margin-top:0;\">Schritt anlegen</h2>
          <input type=\"hidden\" name=\"workflow_definition_id\" value=\"{selected_workflow.id if selected_workflow else ''}\">
          <div class=\"field-grid\">
            <div class=\"field\"><label>Name</label><input type=\"text\" name=\"name\" required></div>
            <div class=\"field\"><label>Step Key</label><input type=\"text\" name=\"step_key\" placeholder=\"z. B. freigabe\" required></div>
            <div class=\"field\"><label>Reihenfolge</label><input type=\"number\" name=\"order\" value=\"{(len(selected_workflow.steps) + 1) * 10 if selected_workflow else 10}\"></div>
            <div class=\"field\"><label>Frist in Tagen</label><input type=\"number\" name=\"due_in_days\" placeholder=\"optional\"></div>
            <div class=\"field full\"><label>Zuweisung</label><select name=\"assignment_target_id\">{assignment_options_create_html}</select></div>
            <div class=\"field full\"><label>Beschreibung</label><textarea name=\"description\"></textarea></div>
          </div>
          <div class=\"actions\"><button class=\"primary\" type=\"submit\">Schritt speichern</button></div>
        </form>
        <form method=\"post\" action=\"/ui/workflow-designer/steps/{selected_step.id if selected_step else ''}\" class=\"panel\" style=\"{'display:block;' if selected_step else 'display:none;'}\">
          <h2 style=\"margin-top:0;\">Schritt bearbeiten</h2>
          <div class=\"field-grid\">
            <div class=\"field\"><label>Name</label><input type=\"text\" name=\"name\" value=\"{_escape(selected_step.name) if selected_step else ''}\" required></div>
            <div class=\"field\"><label>Step Key</label><input type=\"text\" name=\"step_key\" value=\"{_escape(selected_step.step_key) if selected_step else ''}\" required></div>
            <div class=\"field\"><label>Reihenfolge</label><input type=\"number\" name=\"order\" value=\"{selected_step.order if selected_step else 10}\"></div>
            <div class=\"field\"><label>Frist in Tagen</label><input type=\"number\" name=\"due_in_days\" value=\"{selected_step.due_in_days if selected_step and selected_step.due_in_days is not None else ''}\" placeholder=\"optional\"></div>
            <div class=\"field full\"><label>Zuweisung</label><select name=\"assignment_target_id\">{assignment_options_edit_html}</select></div>
            <div class=\"field full\"><label>Beschreibung</label><textarea name=\"description\">{_escape(selected_step.description or '') if selected_step else ''}</textarea></div>
          </div>
          <div class=\"actions\"><button class=\"primary\" type=\"submit\">Schritt aktualisieren</button></div>
        </form>
        <div class=\"panel\" style=\"{'display:block;' if selected_step else 'display:none;'}\">
          <h2 style=\"margin-top:0;\">Transitionen</h2>
          <p class=\"muted\">Hier werden echte Übergänge definiert, also z. B. Genehmigt, Abgelehnt oder Rückfrage.</p>
          <div class=\"stack\">{step_transitions_html}</div>
        </div>
        <form method=\"post\" action=\"/ui/workflow-designer/transitions\" class=\"panel\" style=\"{'display:block;' if selected_step else 'display:none;'}\">
          <h2 style=\"margin-top:0;\">Transition anlegen</h2>
          <input type=\"hidden\" name=\"workflow_definition_id\" value=\"{selected_workflow.id if selected_workflow else ''}\">
          <input type=\"hidden\" name=\"from_step_id\" value=\"{selected_step.id if selected_step else ''}\">
          <div class=\"field-grid\">
            <div class=\"field\"><label>Label</label><input type=\"text\" name=\"label\" placeholder=\"z. B. genehmigt\" required></div>
            <div class=\"field\"><label>Zielschritt</label><select name=\"to_step_id\" required>{transition_target_options_html}</select></div>
            <div class=\"field full\"><label><input type=\"checkbox\" name=\"is_default\"> Standardübergang</label></div>
          </div>
          <div class=\"actions\"><button class=\"primary\" type=\"submit\">Transition speichern</button></div>
        </form>
      </div>
    </div>
  </div>
  <script>
    (() => {{
      const sequence = document.getElementById('workflow-graph-sequence');
      const stepIdsInput = document.getElementById('workflow-graph-step-ids');
      if (!sequence || !stepIdsInput) return;

      let dragged = null;
      const syncOrder = () => {{
        const ids = Array.from(sequence.querySelectorAll('.workflow-graph-node')).map((node) => node.dataset.stepId).filter(Boolean);
        stepIdsInput.value = ids.join(',');
      }};

      sequence.querySelectorAll('.workflow-graph-node').forEach((node) => {{
        node.addEventListener('dragstart', () => {{
          dragged = node;
          node.style.opacity = '0.45';
        }});
        node.addEventListener('dragend', () => {{
          node.style.opacity = '1';
          dragged = null;
          syncOrder();
        }});
        node.addEventListener('dragover', (event) => {{
          event.preventDefault();
        }});
        node.addEventListener('drop', (event) => {{
          event.preventDefault();
          if (!dragged || dragged === node) return;
          const nodes = Array.from(sequence.querySelectorAll('.workflow-graph-node'));
          const draggedIndex = nodes.indexOf(dragged);
          const targetIndex = nodes.indexOf(node);
          if (draggedIndex < targetIndex) {{
            sequence.insertBefore(dragged, node.nextSibling);
          }} else {{
            sequence.insertBefore(dragged, node);
          }}
          syncOrder();
        }});
      }});
      syncOrder();
    }})();
  </script>
</body>
</html>
"""


def _sync_assignment_targets(db: Session) -> None:
    users = db.query(User).order_by(User.display_name).all()
    roles = db.query(Role).order_by(Role.name).all()
    teams = db.query(Team).order_by(Team.name).all()

    existing_user_ids = {target.user_id for target in db.query(AssignmentTarget).where(AssignmentTarget.user_id.isnot(None)).all()}
    existing_role_ids = {target.role_id for target in db.query(AssignmentTarget).where(AssignmentTarget.role_id.isnot(None)).all()}
    existing_team_ids = {target.team_id for target in db.query(AssignmentTarget).where(AssignmentTarget.team_id.isnot(None)).all()}

    changed = False

    for user in users:
        if user.id not in existing_user_ids:
            db.add(AssignmentTarget(target_type="user", user_id=user.id, label=user.display_name, description=user.email))
            changed = True
    for role in roles:
        if role.id not in existing_role_ids:
            db.add(AssignmentTarget(target_type="role", role_id=role.id, label=role.name, description=role.description))
            changed = True
    for team in teams:
        if team.id not in existing_team_ids:
            db.add(AssignmentTarget(target_type="team", team_id=team.id, label=team.name, description=team.description))
            changed = True

    if changed:
        db.commit()


def _render_structure(cabinets: list[Cabinet]) -> str:
    if not cabinets:
        return "<p class='muted'>Noch keine Cabinets angelegt.</p>"

    def render_field_badges(fields: list[MetadataField]) -> str:
        if not fields:
            return ""
        return "<div class='small' style='margin:6px 0 0 0;'>Metadaten: " + ", ".join(
            f"<span class='badge'>{field.label or field.name}</span>" for field in sorted(fields, key=lambda item: item.order)
        ) + "</div>"

    def render_doc_type_list(document_types: list[DocumentType]) -> str:
        if not document_types:
            return ""
        parts = ["<ul>"]
        for doc_type in sorted(document_types, key=lambda item: item.order):
            icon = doc_type.icon or "📄"
            parts.append(
                f'<li>{icon} <a href="/ui/admin/document-types/{doc_type.id}">{doc_type.name}</a> '
                f'<span class="small">({len(doc_type.fields)} Dokumentfelder)</span></li>'
            )
        parts.append("</ul>")
        return "".join(parts)

    grouped: dict[str, list[Cabinet]] = {}
    for cabinet in cabinets:
        type_name = cabinet.cabinet_type.name if cabinet.cabinet_type else "Ohne Cabinettyp"
        grouped.setdefault(type_name, []).append(cabinet)

    chunks: list[str] = ["<ul>"]
    for type_name, typed_cabinets in grouped.items():
        chunks.append(f"<li>🧩 <strong>{_escape(type_name)}</strong><ul>")
        for cabinet in typed_cabinets:
            cabinet_meta_count = len(cabinet.metadata_fields)
            cabinet_doc_types = sorted(cabinet.document_types, key=lambda item: item.order)
            chunks.append(
                f"<li>🗂️ <strong>{cabinet.name}</strong> <span class='small'>({cabinet_meta_count} Cabinet-Felder, {len(cabinet_doc_types)} direkte Typen)</span>{render_field_badges(cabinet.metadata_fields)}"
            )
            if cabinet_doc_types:
                chunks.append("<div class='small' style='margin-top:6px;'>Direkte Dokumenttypen</div>")
                chunks.append(render_doc_type_list(cabinet_doc_types))
            if cabinet.registers:
                chunks.append("<ul>")
                for register in sorted(cabinet.registers, key=lambda item: item.order):
                    register_meta_count = len(register.metadata_fields)
                    chunks.append(
                        f"<li>📑 {register.name} <span class='small'>({register_meta_count} Register-Felder)</span>{render_field_badges(register.metadata_fields)}"
                    )
                    if register.document_types:
                        chunks.append(render_doc_type_list(register.document_types))
                    chunks.append("</li>")
                chunks.append("</ul>")
            chunks.append("</li>")
        chunks.append("</ul></li>")
    chunks.append("</ul>")
    return "".join(chunks)


def _render_admin_summary(selected_document_type: DocumentType | None) -> str:
    if not selected_document_type:
        return "<p class='muted'>Lege zuerst einen Dokumenttyp an, dann erscheint hier die Modellzusammenfassung.</p>"

    def render_field_list(fields: list[Any]) -> str:
        items = []
        for field in fields:
            flags = []
            if field.is_required:
                flags.append("Pflicht")
            if field.is_unique:
                flags.append("Unique")
            flags_label = f" — {', '.join(flags)}" if flags else ""
            items.append(f"<li><strong>{field.label or field.name}</strong> <span class='small'>[{field.field_type}/{field.width}]</span>{flags_label}</li>")
        return "".join(items) or "<li>Keine Felder definiert.</li>"

    register = selected_document_type.register
    cabinet = selected_document_type.cabinet or (register.cabinet if register else None)
    cabinet_fields_html = render_field_list(sorted(cabinet.metadata_fields, key=lambda item: item.order)) if cabinet else "<li>Kein Cabinet zugeordnet.</li>"
    register_fields_html = render_field_list(sorted(register.metadata_fields, key=lambda item: item.order)) if register else "<li>Kein Register zugeordnet.</li>"
    document_fields_html = render_field_list(sorted(selected_document_type.fields, key=lambda item: item.order))

    return f"""
      <p><strong>{selected_document_type.name}</strong></p>
      <p class="muted">{selected_document_type.description or 'Keine Beschreibung hinterlegt.'}</p>
      <div class="pillbar">
        <span class="pill">Dokumentfelder: {len(selected_document_type.fields)}</span>
        <span class="pill">Registerfelder: {len(register.metadata_fields) if register else 0}</span>
        <span class="pill">Cabinetfelder: {len(cabinet.metadata_fields) if cabinet else 0}</span>
        <span class="pill">Cabinet: {cabinet.name if cabinet else '—'}</span>
        <span class="pill">Register: {register.name if register else 'direkt am Cabinet'}</span>
        <span class="pill">Icon: {selected_document_type.icon or '📄'}</span>
      </div>
      <form method="post" action="/ui/admin/document-types/{selected_document_type.id}/seed-invoice" style="margin:16px 0;">
        <button class="primary" type="submit">Standardfelder für Rechnung anlegen</button>
      </form>
      <h3>Cabinet-Metadaten</h3><ul>{cabinet_fields_html}</ul>
      <h3>Register-Metadaten</h3><ul>{register_fields_html}</ul>
      <h3>Dokumenttyp-Metadaten</h3><ul>{document_fields_html}</ul>
    """


def _render_recent_documents(recent_documents: list[Document]) -> str:
    if not recent_documents:
        return "<p class='muted'>Noch keine Dokumente gespeichert.</p>"
    cards = []
    for document in recent_documents:
        metadata = metadata_from_json(document.metadata_json)
        metadata_html = f"<pre>{json.dumps(metadata, ensure_ascii=False, indent=2)}</pre>" if metadata else ""
        doc_type_label = document.document_type.name if document.document_type else "ohne Typ"
        cards.append(f"<div class='panel'><div><strong>{document.title or document.name}</strong></div><div class='muted'>{doc_type_label} · {document.created_at}</div>{metadata_html}</div>")
    return "".join(cards)


def _render_definition_structure(
    cabinet_types: list[CabinetType],
    *,
    selected_definition_kind: str | None = None,
    selected_definition_id: str | None = None,
) -> str:
    if not cabinet_types:
        return "<p class='muted'>Noch keine Definitionen vorhanden.</p>"

    def def_node(
        kind: str,
        node_id: str,
        label: str,
        icon: str,
        depth: int,
        meta: str = "",
        actions: list[dict[str, Any]] | None = None,
    ) -> str:
        is_active = selected_definition_kind == kind and selected_definition_id == node_id
        active_class = " active" if is_active else ""
        actions_json = json.dumps(actions or [], ensure_ascii=False)
        node_id_attr = _escape(node_id)
        kind_attr = _escape(kind)
        label_attr = _escape(label)
        meta_html = f'<div class="def-node-meta">{meta}</div>' if meta else ""
        return (
            f'<div class="def-node depth-{depth}{active_class}" '
            f'data-kind="{kind_attr}" data-id="{node_id_attr}" '
            f'data-label="{label_attr}" data-actions="{_escape(actions_json)}">'
            f'<div class="def-node-label">{icon} {_escape(label)}</div>'
            f'{meta_html}'
            f'<div class="def-node-actions">'
            f'<button type="button" class="def-menu-btn" title="Aktionen">⋯</button>'
            f'</div>'
            f'</div>'
        )

    chunks: list[str] = ['<div class="def-tree">']
    for cabinet_type in sorted(cabinet_types, key=lambda item: item.order):
        ct_id = str(cabinet_type.id)
        ct_actions: list[dict[str, Any]] = [
            {"title": "📑 Registertyp anlegen", "action": "open-form", "target": "admin-form-register-type", "select_name": "cabinet_type_id", "select_value": ct_id},
            {"title": "📄 Dokumenttyp anlegen", "action": "open-form", "target": "admin-form-document-type", "select_name": "cabinet_type_id", "select_value": ct_id, "target_kind": "cabinet_type"},
            {"title": "🔢 Metadatenfeld anlegen", "action": "open-form", "target": "admin-form-metadata-field", "select_name": "cabinet_type_id", "select_value": ct_id, "target_kind": "cabinet_type"},
            {"title": "🗑️ Löschen", "action": "delete-def", "danger": True},
        ]
        chunks.append(def_node(
            kind="cabinet_type", node_id=ct_id, label=cabinet_type.name,
            icon="🗄️", depth=1,
            actions=ct_actions,
        ))
        # Direkte Dokumenttypen am Cabinettyp
        for doc_type in sorted(cabinet_type.document_type_definitions, key=lambda item: item.order):
            dt_id = str(doc_type.id)
            dt_actions: list[dict[str, Any]] = [
                {"title": "➕ Metadatenfeld anlegen", "action": "open-form", "target": "admin-form-metadata-field", "select_name": "document_type_id", "select_value": dt_id, "target_kind": "document_type"},
                {"title": "🗑️ Löschen", "action": "delete-def", "danger": True},
            ]
            chunks.append(def_node(
                kind="document_type", node_id=dt_id, label=doc_type.name,
                icon="📄", depth=2,
                actions=dt_actions,
            ))
        # Registertypen
        for register_type in sorted(cabinet_type.register_types, key=lambda item: item.order):
            rt_id = str(register_type.id)
            rt_actions: list[dict[str, Any]] = [
                {"title": "📄 Dokumenttyp anlegen", "action": "open-form", "target": "admin-form-document-type", "select_name": "register_type_id", "select_value": rt_id, "target_kind": "register_type"},
                {"title": "🔢 Metadatenfeld anlegen", "action": "open-form", "target": "admin-form-metadata-field", "select_name": "register_type_id", "select_value": rt_id, "target_kind": "register_type"},
                {"title": "🗑️ Löschen", "action": "delete-def", "danger": True},
            ]
            chunks.append(def_node(
                kind="register_type", node_id=rt_id, label=register_type.name,
                icon="📑", depth=2,
                actions=rt_actions,
            ))
            # Dokumenttypen unter Registertyp
            for doc_type in sorted(register_type.document_type_definitions, key=lambda item: item.order):
                dt_id = str(doc_type.id)
                dt_actions: list[dict[str, Any]] = [
                    {"title": "➕ Metadatenfeld anlegen", "action": "open-form", "target": "admin-form-metadata-field", "select_name": "document_type_id", "select_value": dt_id, "target_kind": "document_type"},
                    {"title": "🗑️ Löschen", "action": "delete-def", "danger": True},
                ]
                chunks.append(def_node(
                    kind="document_type", node_id=dt_id, label=doc_type.name,
                    icon="📄", depth=3,
                    actions=dt_actions,
                ))
    chunks.append('</div>')
    return "".join(chunks)


def _render_definition_detail(
    cabinet_types: list[CabinetType],
    cabinets: list[Cabinet],
    *,
    selected_definition_kind: str | None = None,
    selected_definition_id: str | None = None,
) -> str:
    if not selected_definition_kind or not selected_definition_id:
        return "<p class='def-empty'>Wähle einen Knoten im Definitionsmodell, um Details zu sehen.</p>"

    def field_list(fields: list[MetadataField]) -> str:
        if not fields:
            return "<p class='def-empty'>Keine Felder definiert.</p>"
        rows = []
        for field in sorted(fields, key=lambda f: f.order):
            flags = []
            if field.is_required:
                flags.append("Pflicht")
            if field.is_unique:
                flags.append("Unique")
            flags_label = " · ".join(flags)
            rows.append(
                f'<div class="def-field-item" data-field-id="{field.id}">'
                f'<div>'
                f'<div class="def-field-name"><a href="/ui/admin?selected_definition_kind={_escape(kind)}&selected_definition_id={_escape(node_id)}&selected_metadata_field_id={field.id}">{_escape(field.label or field.name)}</a></div>'
                f'<div class="def-field-type">{_escape(field.field_type)} · {flags_label}</div>'
                f'</div>'
                f'<form method="post" action="/ui/admin/metadata-fields/{field.id}/delete" style="display:inline;">'
                f'<button type="submit" class="def-action-btn danger" title="Löschen" onclick="return confirm(\'Feld wirklich löschen?\');">×</button>'
                f'</form>'
                f'</div>'
            )
        return "".join(rows)

    kind = selected_definition_kind
    node_id = selected_definition_id

    if kind == "cabinet_type":
        ct = next((t for t in cabinet_types if str(t.id) == node_id), None)
        if not ct:
            return "<p class='def-empty'>Cabinettyp nicht gefunden.</p>"
        return f"""
        <div class="def-detail-card">
          <h3>🗄️ {_escape(ct.name)}</h3>
          <div class="def-detail-row"><div class="def-detail-key">Name</div><div class="def-detail-value">{_escape(ct.name)}</div></div>
          <div class="def-detail-row"><div class="def-detail-key">Beschreibung</div><div class="def-detail-value">{_escape(ct.description or '—')}</div></div>
          <div class="def-detail-row"><div class="def-detail-key">Reihenfolge</div><div class="def-detail-value">{ct.order}</div></div>
        </div>
        <div class="def-detail-card">
          <h3>🔢 Metadatenfelder</h3>
          {field_list(ct.metadata_fields)}
        </div>
        <div class="def-detail-card">
          <h3>📑 Registertypen</h3>
          {f"<p class='def-empty'>{len(ct.register_types)} Registertypen vorhanden.</p>" if ct.register_types else "<p class='def-empty'>Keine Registertypen angelegt.</p>"}
        </div>
        <div class="def-detail-card">
          <h3>📄 Direkte Dokumenttypen</h3>
          {f"<p class='def-empty'>{len(ct.document_type_definitions)} Dokumenttypen am Cabinettyp.</p>" if ct.document_type_definitions else "<p class='def-empty'>Keine direkt am Cabinettyp.</p>"}
        </div>
        <div class="def-actions">
          <button type="button" class="pill admin-create-toggle-inline" data-target="admin-form-metadata-field" data-select-name="cabinet_type_id" data-select-value="{str(ct.id)}" data-target-kind="cabinet_type">Metadatenfeld anlegen</button>
        </div>
        """

    if kind == "register_type":
        rt = next((rt for ct in cabinet_types for rt in ct.register_types if str(rt.id) == node_id), None)
        if not rt:
            return "<p class='def-empty'>Registertyp nicht gefunden.</p>"
        return f"""
        <div class="def-detail-card">
          <h3>📑 {_escape(rt.name)}</h3>
          <div class="def-detail-row"><div class="def-detail-key">Name</div><div class="def-detail-value">{_escape(rt.name)}</div></div>
          <div class="def-detail-row"><div class="def-detail-key">Beschreibung</div><div class="def-detail-value">{_escape(rt.description or '—')}</div></div>
          <div class="def-detail-row"><div class="def-detail-key">Cabinettyp</div><div class="def-detail-value">{_escape(rt.cabinet_type.name if rt.cabinet_type else '—')}</div></div>
          <div class="def-detail-row"><div class="def-detail-key">Dokumenttypen</div><div class="def-detail-value">{len(rt.document_type_definitions)}</div></div>
        </div>
        <div class="def-detail-card">
          <h3>🔢 Metadatenfelder</h3>
          {field_list(rt.metadata_fields)}
        </div>
        <div class="def-detail-card">
          <h3>📄 Dokumenttypen</h3>
          {f"<p class='def-empty'>{len(rt.document_type_definitions)} Dokumenttypen vorhanden.</p>" if rt.document_type_definitions else "<p class='def-empty'>Keine Dokumenttypen angelegt.</p>"}
        </div>
        <div class="def-actions">
          <button type="button" class="pill admin-create-toggle-inline" data-target="admin-form-metadata-field" data-select-name="register_type_id" data-select-value="{str(rt.id)}" data-target-kind="register_type">Metadatenfeld anlegen</button>
        </div>
        """

    if kind == "document_type":
        dt = next((dt for ct in cabinet_types for rt in ct.register_types for dt in rt.document_type_definitions if str(dt.id) == node_id), None)
        if not dt:
            dt = next((dt for ct in cabinet_types for dt in ct.document_type_definitions if str(dt.id) == node_id), None)
        if not dt:
            return "<p class='def-empty'>Dokumenttyp nicht gefunden.</p>"
        cabinet_path = ""
        if dt.cabinet:
            cabinet_path = f"{dt.cabinet.cabinet_type.name if dt.cabinet.cabinet_type else ''} → {dt.cabinet.name}"
        elif dt.register:
            cabinet_path = f"{dt.register.cabinet.cabinet_type.name if dt.register.cabinet and dt.register.cabinet.cabinet_type else ''} → {dt.register.cabinet.name if dt.register.cabinet else ''} → {dt.register.name}"
        elif dt.cabinet_type_definition:
            cabinet_path = f"{dt.cabinet_type_definition.name} (direkt)"
        elif dt.register_type_definition:
            cabinet_path = f"{dt.register_type_definition.cabinet_type.name if dt.register_type_definition.cabinet_type else ''} → {dt.register_type_definition.name}"
        return f"""
        <div class="def-detail-card">
          <h3>📄 {_escape(dt.name)}</h3>
          <div class="def-detail-row"><div class="def-detail-key">Name</div><div class="def-detail-value">{_escape(dt.name)}</div></div>
          <div class="def-detail-row"><div class="def-detail-key">Beschreibung</div><div class="def-detail-value">{_escape(dt.description or '—')}</div></div>
          <div class="def-detail-row"><div class="def-detail-key">Icon</div><div class="def-detail-value">{dt.icon or '📄'}</div></div>
          <div class="def-detail-row"><div class="def-detail-key">Pfad</div><div class="def-detail-value">{_escape(cabinet_path) if cabinet_path else '—'}</div></div>
          <div class="def-detail-row"><div class="def-detail-key">Felder</div><div class="def-detail-value">{len(dt.fields)} definiert</div></div>
        </div>
        <div class="def-detail-card">
          <h3>🔢 Metadatenfelder</h3>
          {field_list(dt.fields)}
        </div>
        <div class="def-actions">
          <button type="button" class="pill admin-create-toggle-inline" data-target="admin-form-metadata-field" data-select-name="document_type_id" data-select-value="{str(dt.id)}" data-target-kind="document_type">Metadatenfeld anlegen</button>
        </div>
        """

    return "<p class='def-empty'>Unbekannter Knotentyp.</p>"


def _resolve_archive_node(
    node_kind: str | None,
    node_id: str | None,
    cabinets: list[Cabinet],
    document_types: list[DocumentType],
) -> dict[str, Any] | None:
    if not node_kind or not node_id:
        return None
    if node_kind == "cabinet":
        for cabinet in cabinets:
            if str(cabinet.id) == node_id:
                return {"kind": "cabinet", "id": str(cabinet.id), "label": cabinet.name}
    elif node_kind == "register":
        for cabinet in cabinets:
            for register in cabinet.registers:
                if str(register.id) == node_id:
                    return {"kind": "register", "id": str(register.id), "label": register.name}
    elif node_kind == "cabinet_type":
        seen: set[str] = set()
        for cabinet in cabinets:
            cabinet_type = getattr(cabinet, "cabinet_type", None)
            if not cabinet_type:
                continue
            cabinet_type_id = str(cabinet_type.id)
            if cabinet_type_id in seen:
                continue
            seen.add(cabinet_type_id)
            if cabinet_type_id == node_id:
                return {"kind": "cabinet_type", "id": cabinet_type_id, "label": cabinet_type.name}
        cabinet_type_names = {
            getattr(cabinet.cabinet_type, "name", "").strip().lower()
            for cabinet in cabinets
            if getattr(cabinet, "cabinet_type", None)
        }
        if cabinet_type_names == {"bestand"}:
            for cabinet in cabinets:
                if str(cabinet.id) == node_id:
                    return {"kind": "cabinet_type", "id": str(cabinet.id), "label": cabinet.name}
    elif node_kind == "document_type":
        for doc_type in document_types:
            if str(doc_type.id) == node_id:
                return {"kind": "document_type", "id": str(doc_type.id), "label": doc_type.name}
    elif node_kind == "document":
        for document in getattr(_resolve_archive_node, "_all_documents", []):
            if str(document.id) == node_id:
                return {"kind": "document", "id": str(document.id), "label": document.title or document.name}

    if not cabinets and node_kind == "document_type":
        for doc_type in document_types:
            if str(doc_type.id) == node_id:
                return {"kind": "document_type", "id": str(doc_type.id), "label": doc_type.name}
    return None


def _creation_actions_for_node(
    *,
    node_kind: str,
    node_id: str,
    node_label: str,
    cabinet: Cabinet | None = None,
    register: Register | None = None,
    document_type: DocumentType | None = None,
) -> list[dict[str, str]]:
    actions: list[dict[str, str]] = []
    if node_kind == "root":
        return [{"title": "Cabinettyp im Admin anlegen", "action": "new-cabinet-type", "group": "Struktur"}]
    if node_kind == "cabinet_type":
        return [{"title": "Cabinet anlegen", "action": "new-cabinet", "kind": node_kind, "id": node_id, "label": node_label, "group": "Struktur"}]
    if node_kind == "cabinet" and cabinet is not None:
        if cabinet.cabinet_type and cabinet.cabinet_type.register_types:
            actions.append({"title": "Register anlegen", "action": "new-register", "kind": node_kind, "id": node_id, "label": node_label, "group": "Struktur"})
        if cabinet.cabinet_type:
            definition_doc_types = sorted(cabinet.cabinet_type.document_type_definitions, key=lambda item: (item.order, (item.name or "").lower()))
            for register_type in sorted(cabinet.cabinet_type.register_types, key=lambda item: (item.order, (item.name or "").lower())):
                definition_doc_types.extend(sorted(register_type.document_type_definitions, key=lambda item: (item.order, (item.name or "").lower())))
            unique_doc_types: dict[str, DocumentType] = {}
            for doc_type in definition_doc_types:
                unique_doc_types[str(doc_type.id)] = doc_type
            for doc_type in unique_doc_types.values():
                actions.append({
                    "title": f"Dokument erfassen: {doc_type.name}",
                    "action": "new-document",
                    "kind": node_kind,
                    "id": node_id,
                    "label": node_label,
                    "group": "Dokumente",
                    "document_type_id": str(doc_type.id),
                    "document_type_name": doc_type.name,
                })
        for doc_type in sorted(cabinet.document_types, key=lambda item: (item.order, (item.name or "").lower())):
            actions.append({
                "title": f"Dokument erfassen: {doc_type.name}",
                "action": "new-document",
                "kind": node_kind,
                "id": node_id,
                "label": node_label,
                "group": "Dokumente",
                "document_type_id": str(doc_type.id),
                "document_type_name": doc_type.name,
            })
        actions.append({"title": "Metadaten bearbeiten", "action": "edit-metadata", "kind": node_kind, "id": node_id, "label": node_label, "group": "Bearbeiten"})
        actions.append({"title": "Löschen", "action": "delete-node", "kind": node_kind, "id": node_id, "label": node_label, "group": "Gefahr"})
        return actions
    if node_kind == "register" and register is not None:
        available_doc_types: list[DocumentType] = []
        if register.register_type:
            available_doc_types.extend(sorted(register.register_type.document_type_definitions, key=lambda item: (item.order, (item.name or "").lower())))
        available_doc_types.extend(sorted(register.document_types, key=lambda item: (item.order, (item.name or "").lower())))
        unique_doc_types: dict[str, DocumentType] = {}
        for doc_type in available_doc_types:
            unique_doc_types[str(doc_type.id)] = doc_type
        for doc_type in unique_doc_types.values():
            actions.append({
                "title": f"Dokument erfassen: {doc_type.name}",
                "action": "new-document",
                "kind": node_kind,
                "id": node_id,
                "label": node_label,
                "group": "Dokumente",
                "document_type_id": str(doc_type.id),
                "document_type_name": doc_type.name,
            })
        actions.append({"title": "Metadaten bearbeiten", "action": "edit-metadata", "kind": node_kind, "id": node_id, "label": node_label, "group": "Bearbeiten"})
        actions.append({"title": "Löschen", "action": "delete-node", "kind": node_kind, "id": node_id, "label": node_label, "group": "Gefahr"})
        return actions
    if node_kind == "document_type" and document_type is not None:
        actions.append({
            "title": f"Dokument anlegen: {document_type.name}",
            "action": "new-document",
            "kind": node_kind,
            "id": node_id,
            "label": node_label,
            "group": "Dokumente",
            "document_type_id": str(document_type.id),
            "document_type_name": document_type.name,
        })
        return actions
    return actions


def _render_archive_tree(
    cabinets: list[Cabinet],
    selected_node: dict[str, Any] | None,
    search_query: str,
    document_types: list[DocumentType] | None = None,
    cabinet_types: list[CabinetType] | None = None,
) -> str:
    root_menu = _creation_actions_for_node(node_kind="root", node_id="", node_label="Wurzel")
    document_types = document_types or []
    cabinet_types = cabinet_types or []
    selected_kind = selected_node.get("kind") if selected_node else None
    selected_id = selected_node.get("id") if selected_node else None

    def node_link(kind: str, node_id: str, label: str, depth: int = 0, menu: list[dict[str, str]] | None = None) -> str:
        active = " active" if selected_kind == kind and selected_id == node_id else ""
        just_created = " just-created" if selected_kind == kind and selected_id == node_id else ""
        href = f"/ui/app?node_kind={kind}&node_id={node_id}"
        menu_attr = f" data-menu='{_escape(json.dumps(menu or [], ensure_ascii=False))}'" if menu is not None else ""
        menu_button = "<div class='tree-actions'><button type='button' class='tree-menu-button'>⋯</button><a class='tree-tab-link' href='{}' target='_blank' rel='noopener noreferrer'>↗</a></div>".format(href) if menu is not None else f'<a class="tree-tab-link" href="{href}" target="_blank" rel="noopener noreferrer">↗</a>'
        return (
            f'<div class="tree-node depth-{depth}{active}{just_created}"{menu_attr}>'
            f'<a class="tree-link" href="{href}">{_escape(label)}</a>'
            f'{menu_button}'
            f'</div>'
        )

    if not cabinets:
        if not document_types and not cabinet_types:
            return f"<div class='archive-tree'><div class='tree-node depth-0' data-menu='{_escape(json.dumps(root_menu, ensure_ascii=False))}'><div class='tree-link'><strong>Leere Struktur</strong></div><div class='tree-actions'><button type='button' class='tree-menu-button'>⋯</button></div></div></div>"
        chunks = ['<div class="archive-tree">']
        chunks.append(f"<div class='tree-node depth-0' data-menu='{_escape(json.dumps(root_menu, ensure_ascii=False))}'><div class='tree-link'><strong>Wurzel</strong></div><div class='tree-actions'><button type='button' class='tree-menu-button'>⋯</button></div></div>")
        if cabinet_types:
            for cabinet_type in sorted(cabinet_types, key=lambda item: (item.order, (item.name or '').lower())):
                type_menu = _creation_actions_for_node(node_kind="cabinet_type", node_id=str(cabinet_type.id), node_label=cabinet_type.name)
                chunks.append(node_link("cabinet_type", str(cabinet_type.id), f"🧩 {cabinet_type.name}", 1, type_menu))
                for doc_type in sorted(cabinet_type.document_type_definitions, key=lambda item: (item.order, (item.name or '').lower())):
                    dt_menu = _creation_actions_for_node(node_kind="document_type", node_id=str(doc_type.id), node_label=doc_type.name, document_type=doc_type)
                    chunks.append(node_link("document_type", str(doc_type.id), f"📄 {doc_type.name}", 2, dt_menu))
                for register_type in sorted(cabinet_type.register_types, key=lambda item: (item.order, (item.name or '').lower())):
                    chunks.append(node_link("register_type", str(register_type.id), f"📑 {register_type.name}", 2))
                    for doc_type in sorted(register_type.document_type_definitions, key=lambda item: (item.order, (item.name or '').lower())):
                        dt_menu = _creation_actions_for_node(node_kind="document_type", node_id=str(doc_type.id), node_label=doc_type.name, document_type=doc_type)
                        chunks.append(node_link("document_type", str(doc_type.id), f"📄 {doc_type.name}", 3, dt_menu))
        else:
            grouped_document_types: dict[str, list[DocumentType]] = {}
            for doc_type in document_types:
                if getattr(doc_type, 'cabinet_type_definition', None):
                    group_label = doc_type.cabinet_type_definition.name
                elif getattr(doc_type, 'register_type_definition', None) and getattr(doc_type.register_type_definition, 'cabinet_type', None):
                    group_label = doc_type.register_type_definition.cabinet_type.name
                else:
                    group_label = 'Ohne Cabinettyp'
                grouped_document_types.setdefault(group_label, []).append(doc_type)
            for group_label, grouped_items in sorted(grouped_document_types.items(), key=lambda item: item[0].lower()):
                chunks.append(node_link("cabinet_type", quote_plus(group_label), f"🧩 {group_label}", 1, []))
                for doc_type in sorted(grouped_items, key=lambda item: (item.order, (item.name or '').lower())):
                    dt_menu = _creation_actions_for_node(node_kind="document_type", node_id=str(doc_type.id), node_label=doc_type.name, document_type=doc_type)
                    chunks.append(node_link("document_type", str(doc_type.id), f"📄 {doc_type.name}", 2, dt_menu))
        chunks.append("</div>")
        return "".join(chunks)
        active = " active" if selected_kind == kind and selected_id == node_id else ""
        just_created = " just-created" if selected_kind == kind and selected_id == node_id else ""
        href = f"/ui/app?node_kind={kind}&node_id={node_id}&q={quote_plus(search_query or '')}"
        menu_attr = f" data-menu='{_escape(json.dumps(menu or [], ensure_ascii=False))}'" if menu is not None else ""
        menu_button = "<div class='tree-actions'><button type='button' class='tree-menu-button'>⋯</button><a class='tree-tab-link' href='{}' target='_blank' rel='noopener noreferrer'>↗</a></div>".format(href) if menu is not None else f'<a class="tree-tab-link" href="{href}" target="_blank" rel="noopener noreferrer">↗</a>'
        return (
            f'<div class="tree-node depth-{depth}{active}{just_created}"{menu_attr}>'
            f'<a class="tree-link" href="{href}">{_escape(label)}</a>'
            f'{menu_button}'
            f'</div>'
        )

    grouped: dict[str, list[Cabinet]] = {}
    for cabinet in cabinets:
        cabinet_type = getattr(cabinet, 'cabinet_type', None)
        type_key = str(cabinet_type.id) if cabinet_type else ""
        grouped.setdefault(type_key, []).append(cabinet)

    chunks: list[str] = ['<div class="archive-tree">']
    chunks.append(f"<div class='tree-node depth-0' data-menu='{_escape(json.dumps(root_menu, ensure_ascii=False))}'><div class='tree-link'><strong>Wurzel</strong></div><div class='tree-actions'><button type='button' class='tree-menu-button'>⋯</button></div></div>")

    for cabinet_type in sorted(cabinet_types or [], key=lambda item: (item.order, (item.name or '').lower())):
        type_id = str(cabinet_type.id)
        type_name = cabinet_type.name
        typed_cabinets = sorted(grouped.get(type_id, []), key=lambda item: item.order)
        type_menu = _creation_actions_for_node(node_kind="cabinet_type", node_id=type_id, node_label=type_name)
        chunks.append(node_link("cabinet_type", type_id, f"🗄️ {type_name}", 0, type_menu))

        for cabinet in typed_cabinets:
            cabinet_menu = _creation_actions_for_node(node_kind="cabinet", node_id=str(cabinet.id), node_label=cabinet.name, cabinet=cabinet)
            chunks.append(node_link("cabinet", str(cabinet.id), f"🗂️ {cabinet.name}", 1, cabinet_menu))
            for register in sorted(cabinet.registers, key=lambda item: item.order):
                child_menu = _creation_actions_for_node(node_kind="register", node_id=str(register.id), node_label=register.name, register=register)
                chunks.append(node_link("register", str(register.id), f"📑 {register.name}", 2, child_menu))
                for doc_type in sorted(register.document_types, key=lambda item: item.order):
                    dt_menu = _creation_actions_for_node(node_kind="document_type", node_id=str(doc_type.id), node_label=doc_type.name, document_type=doc_type)
                    chunks.append(node_link("document_type", str(doc_type.id), f"📄 {doc_type.name}", 3, dt_menu))
            for doc_type in sorted(cabinet.document_types, key=lambda item: item.order):
                dt_menu = _creation_actions_for_node(node_kind="document_type", node_id=str(doc_type.id), node_label=doc_type.name, document_type=doc_type)
                chunks.append(node_link("document_type", str(doc_type.id), f"📄 {doc_type.name}", 2, dt_menu))

    remaining_type_ids = set(grouped.keys()) - {str(item.id) for item in (cabinet_types or [])}
    for type_id in sorted(remaining_type_ids):
        typed_cabinets = sorted(grouped.get(type_id, []), key=lambda item: item.order)
        type_name = typed_cabinets[0].cabinet_type.name if typed_cabinets and typed_cabinets[0].cabinet_type else 'Ohne Cabinettyp'
        type_menu = _creation_actions_for_node(node_kind="cabinet_type", node_id=type_id, node_label=type_name)
        chunks.append(node_link("cabinet_type", type_id, f"🗄️ {type_name}", 0, type_menu))
        for cabinet in typed_cabinets:
            cabinet_menu = _creation_actions_for_node(node_kind="cabinet", node_id=str(cabinet.id), node_label=cabinet.name, cabinet=cabinet)
            chunks.append(node_link("cabinet", str(cabinet.id), f"🗂️ {cabinet.name}", 1, cabinet_menu))

    chunks.append("</div>")
    return "".join(chunks)


def _render_node_results(
    cabinets: list[Cabinet],
    all_documents: list[Document],
    selected_node: dict[str, Any] | None,
    search_query: str,
) -> tuple[str, str]:
    normalized_query = (search_query or "").strip().lower()
    if normalized_query:
        matching_documents = []
        for document in all_documents:
            metadata = metadata_from_json(document.metadata_json)
            haystack_parts = [
                document.title or "",
                document.name or "",
                document.document_type.name if document.document_type else "",
                document.description or "",
                document.author or "",
                document.tags or "",
                json.dumps(metadata, ensure_ascii=False) if metadata else "",
            ]
            haystack = " ".join(haystack_parts).lower()
            if normalized_query in haystack:
                matching_documents.append(document)

        result_cards = []
        for document in matching_documents[:30]:
            result_cards.append(
                f"<a class='object-card' href='/ui/app?node_kind=document&node_id={document.id}'><strong>🔎 {_escape(document.title or document.name)}</strong><div class='muted'>{_escape(document.document_type.name if document.document_type else 'Ohne Dokumenttyp')}</div></a>"
            )
        header = f"""
        <div class=\"panel\" style=\"margin-bottom:16px;\">
          <h2 style=\"margin-top:0;\">Suchtreffer</h2>
          <p class=\"muted\">Volltextsuche nach: {_escape(search_query)}</p>
        </div>
        """
        if not result_cards:
            return "<div class='panel'><p class='muted'>Keine Treffer gefunden.</p></div>", header
        return '<div class="object-list">' + ''.join(result_cards) + '</div>', header

    if not selected_node:
        header = """
        <div class=\"panel\" style=\"margin-bottom:16px;\">
          <h2 style=\"margin-top:0;\">Archivinhalt</h2>
          <p class=\"muted\">Wähle links einen Knoten im Archivbaum, um seine Unterelemente und zugehörigen Inhalte zu sehen.</p>
        </div>
        """
        return "<div class='panel'><p class='muted'>Noch kein Archivknoten ausgewählt.</p></div>", header

    selected_kind = selected_node["kind"]
    selected_id = selected_node["id"]
    selected_label = selected_node["label"]

    results: list[str] = []
    subtitle = ""

    if selected_kind == "cabinet_type":
        typed_cabinets = [cab for cab in cabinets if cab.cabinet_type and str(cab.cabinet_type.id) == selected_id]
        typed_documents = [doc for doc in all_documents if _resolved_document_cabinet(doc) and _resolved_document_cabinet(doc).cabinet_type and str(_resolved_document_cabinet(doc).cabinet_type.id) == selected_id]
        subtitle = f"{len(typed_cabinets)} Cabinets · {len(typed_documents)} Dokumente in diesem Cabinettyp"
        for cabinet in sorted(typed_cabinets, key=lambda item: item.order):
            register_count = len(cabinet.registers or [])
            doc_type_count = len(cabinet.document_types or [])
            cabinet_documents = [doc for doc in all_documents if _resolved_document_cabinet(doc) and str(_resolved_document_cabinet(doc).id) == str(cabinet.id)]
            results.append(
                f"<a class='object-card' href='/ui/app?node_kind=cabinet&node_id={cabinet.id}'><strong>🗂️ {_escape(cabinet.name)}</strong><div class='muted'>{register_count} Register · {doc_type_count} direkte Dokumenttypen · {len(cabinet_documents)} Dokumente</div></a>"
            )
    elif selected_kind == "cabinet":
        cabinet = next((cab for cab in cabinets if str(cab.id) == selected_id), None)
        if cabinet:
            subtitle = "Inhalt dieses Cabinets"
            structure_results: list[str] = []
            document_results: list[str] = []
            for register in sorted(cabinet.registers, key=lambda item: item.order):
                register_documents = [doc for doc in all_documents if _resolved_document_cabinet(doc) and str(_resolved_document_cabinet(doc).id) == str(cabinet.id) and doc.document_type and doc.document_type.register_id and str(doc.document_type.register_id) == str(register.id)]
                structure_results.append(f"<a class='object-card' href='/ui/app?node_kind=register&node_id={register.id}'><strong>📑 {_escape(register.name)}</strong><div class='muted'>{len(register.document_types)} Dokumenttypen · {len(register_documents)} Dokumente</div></a>")
            for doc_type in sorted(cabinet.document_types, key=lambda item: item.order):
                matching_documents = [doc for doc in all_documents if doc.document_type_id and str(doc.document_type_id) == str(doc_type.id)]
                structure_results.append(f"<a class='object-card' href='/ui/app?node_kind=document_type&node_id={doc_type.id}'><strong>📄 {_escape(doc_type.name)}</strong><div class='muted'>Direkter Dokumenttyp · {len(matching_documents)} Dokumente</div></a>")
            cabinet_documents = [doc for doc in all_documents if _resolved_document_cabinet(doc) and str(_resolved_document_cabinet(doc).id) == str(cabinet.id)]
            if structure_results:
                results.append("<div class='panel' style='margin-bottom:12px;'><h3 style='margin:0;'>Struktur</h3></div>")
                results.extend(structure_results)
            for document in cabinet_documents[:20]:
                document_results.append(f"<a class='object-card' href='/ui/app?node_kind=document&node_id={document.id}'><strong>📄 {_escape(document.title or document.name)}</strong><div class='muted'>{_escape(document.document_type.name if document.document_type else 'Ohne Dokumenttyp')} · {_escape(str(document.created_at))}</div></a>")
            results.extend(document_results)
    elif selected_kind == "register":
        register = None
        parent_cabinet = None
        for cabinet in cabinets:
            for candidate in cabinet.registers:
                if str(candidate.id) == selected_id:
                    register = candidate
                    parent_cabinet = cabinet
                    break
            if register:
                break
        if register:
            matching_documents = [doc for doc in all_documents if doc.document_type and doc.document_type.register_id and str(doc.document_type.register_id) == selected_id]
            subtitle = f"{len(register.document_types)} Dokumenttypen · {len(matching_documents)} Dokumente dieses Registers"
            for doc_type in sorted(register.document_types, key=lambda item: item.order):
                doc_type_documents = [doc for doc in matching_documents if doc.document_type_id and str(doc.document_type_id) == str(doc_type.id)]
                results.append(f"<a class='object-card' href='/ui/app?node_kind=document_type&node_id={doc_type.id}'><strong>📄 {_escape(doc_type.name)}</strong><div class='muted'>Dokumenttyp in {_escape(register.name)} · {len(doc_type_documents)} Dokumente</div></a>")
            for document in matching_documents[:20]:
                results.append(f"<a class='object-card' href='/ui/app?node_kind=document&node_id={document.id}'><strong>📄 {_escape(document.title or document.name)}</strong><div class='muted'>{_escape(parent_cabinet.name if parent_cabinet else '')}</div></a>")
    elif selected_kind == "document_type":
        matching_documents = [doc for doc in all_documents if doc.document_type_id and str(doc.document_type_id) == selected_id]
        subtitle = f"{len(matching_documents)} Dokumente dieses Dokumenttyps"
        for document in matching_documents[:30]:
            results.append(f"<a class='object-card' href='/ui/app?node_kind=document&node_id={document.id}'><strong>📄 {_escape(document.title or document.name)}</strong><div class='muted'>{_escape(document.name)}</div></a>")
    elif selected_kind == "document":
        document = next((doc for doc in all_documents if str(doc.id) == str(selected_id)), None)
        if document:
            parent_document_type_id = str(document.document_type_id) if document.document_type_id else ""
            matching_documents = [doc for doc in all_documents if parent_document_type_id and doc.document_type_id and str(doc.document_type_id) == parent_document_type_id]
            subtitle = f"{len(matching_documents)} Dokumente dieses Dokumenttyps"
            for candidate in matching_documents[:30]:
                active_class = " active" if str(candidate.id) == str(document.id) else ""
                results.append(f"<a class='object-card{active_class}' href='/ui/app?node_kind=document&node_id={candidate.id}'><strong>📄 {_escape(candidate.title or candidate.name)}</strong><div class='muted'>{_escape(candidate.name)}</div></a>")

    if search_query.strip():
        subtitle = (subtitle + " · " if subtitle else "") + f"Suche aktiv: {_escape(search_query)}"

    if not results and search_query.strip():
        search_documents = [
            doc for doc in all_documents
            if search_query.strip().lower() in ((doc.title or '') + ' ' + (doc.name or '')).lower()
            or search_query.strip().lower() in str(metadata_from_json(doc.metadata_json) or {}).lower()
        ]
        if not search_documents:
            search_documents = all_documents[:30]
        if search_documents:
            subtitle = f"{len(search_documents)} Suchtreffer"
            results = [
                f"<a class='object-card' href='/ui/app?node_kind=document&node_id={document.id}'><strong>📄 {_escape(document.title or document.name)}</strong><div class='muted'>{_escape(document.document_type.name if document.document_type else 'Ohne Dokumenttyp')} · {_escape(document.name)}</div></a>"
                for document in search_documents[:30]
            ]

    if not results:
        results_html = "<div class='panel'><p class='muted'>Für diesen Knoten wurden noch keine Unterelemente gefunden.</p></div>"
    else:
        results_html = '<div class="object-list">' + ''.join(results) + '</div>'

    header_html = f"""
    <div class=\"panel\" style=\"margin-bottom:16px;\">
      <h2 style=\"margin-top:0;\">{_escape(selected_label)}</h2>
      <p class=\"muted\">{subtitle or 'Unterelemente und Inhalte des gewählten Archivknotens'}</p>
    </div>
    """
    return results_html, header_html


def _render_context_panel(selected_node: dict[str, Any] | None, cabinets: list[Cabinet], cabinet_types: list[CabinetType] | None = None) -> str:
    selected_kind = selected_node.get("kind") if selected_node else None
    selected_id = selected_node.get("id") if selected_node else None

    selected_cabinet_id = ""
    selected_register_id = ""
    selected_cabinet_label = ""
    selected_register_label = ""
    selected_cabinet_type_id = ""
    selected_cabinet_type_label = ""

    cabinet_type_map: dict[str, str] = {}
    for cabinet_type in (cabinet_types or []):
        cabinet_type_map[str(cabinet_type.id)] = cabinet_type.name
    for cabinet in cabinets:
        if cabinet.cabinet_type:
            cabinet_type_map[str(cabinet.cabinet_type.id)] = cabinet.cabinet_type.name

    if selected_kind == "cabinet_type" and selected_id:
        selected_cabinet_type_id = selected_id
        if selected_node:
            selected_cabinet_type_label = selected_node.get("label", "")
    elif selected_kind == "cabinet" and selected_id:
        selected_cabinet_id = selected_id
        for cabinet in cabinets:
            if str(cabinet.id) == selected_id:
                selected_cabinet_label = cabinet.name
                if cabinet.cabinet_type:
                    selected_cabinet_type_id = str(cabinet.cabinet_type.id)
                    selected_cabinet_type_label = cabinet.cabinet_type.name
                break
    elif selected_kind == "register" and selected_id:
        selected_register_id = selected_id
        for cabinet in cabinets:
            for register in sorted(cabinet.registers, key=lambda item: item.order):
                if str(register.id) == selected_id:
                    selected_cabinet_id = str(cabinet.id)
                    selected_cabinet_label = cabinet.name
                    selected_register_label = register.name
                    if cabinet.cabinet_type:
                        selected_cabinet_type_id = str(cabinet.cabinet_type.id)
                        selected_cabinet_type_label = cabinet.cabinet_type.name
                    break
            if selected_cabinet_id:
                break
    elif selected_kind == "document_type" and selected_id:
        for cabinet in cabinets:
            for register in sorted(cabinet.registers, key=lambda item: item.order):
                for doc_type in sorted(register.document_types, key=lambda item: item.order):
                    if str(doc_type.id) == selected_id:
                        selected_cabinet_id = str(cabinet.id)
                        selected_register_id = str(register.id)
                        selected_cabinet_label = cabinet.name
                        selected_register_label = register.name
                        if cabinet.cabinet_type:
                            selected_cabinet_type_id = str(cabinet.cabinet_type.id)
                            selected_cabinet_type_label = cabinet.cabinet_type.name
                        break
                if selected_register_id:
                    break
            if selected_register_id:
                break
            for doc_type in sorted(cabinet.document_types, key=lambda item: item.order):
                if str(doc_type.id) == selected_id:
                    selected_cabinet_id = str(cabinet.id)
                    selected_cabinet_label = cabinet.name
                    if cabinet.cabinet_type:
                        selected_cabinet_type_id = str(cabinet.cabinet_type.id)
                        selected_cabinet_type_label = cabinet.cabinet_type.name
                    break
            if selected_cabinet_id:
                break

    if selected_kind == "register" and selected_node and not selected_register_label:
        selected_register_label = selected_node.get("label", "")

    cabinet_options = _option_list(
        [
            (str(cabinet.id), f"{cabinet.cabinet_type.name if cabinet.cabinet_type else 'Ohne Typ'} → {cabinet.name}")
            for cabinet in cabinets
        ],
        selected_value=selected_cabinet_id or None,
        include_blank="Bitte wählen",
    )
    available_document_types = _available_document_types_for_node(selected_node, cabinets)
    cabinet_type_options = _option_list(
        [(type_id, label) for type_id, label in cabinet_type_map.items()],
        selected_value=selected_cabinet_type_id or None,
        include_blank="Bitte wählen",
    ) or '<option value="">Bitte erst Cabinettyp im Admin anlegen</option>'

    accordion_open = " open" if selected_kind in {"cabinet", "register", "document_type"} else ""
    register_cta_label = (
        f"Register in {selected_cabinet_label} anlegen" if selected_kind == "cabinet" and selected_cabinet_label else "Register anlegen"
    )
    cabinet_cta_label = (
        f"Cabinet im Typ {selected_cabinet_type_label} anlegen" if selected_cabinet_type_label else "Cabinet anlegen"
    )
    cabinet_hint = (
        f"Neues Register wird direkt im Cabinet {selected_cabinet_label} vorbereitet."
        if selected_kind == "cabinet" and selected_cabinet_label else
        "Register lassen sich erst innerhalb eines konkreten Cabinets anlegen. Wähle dafür links ein Cabinet wie z. B. 2026."
    )
    cabinet_type_hint = (
        f"Neues Cabinet wird im Cabinettyp {selected_cabinet_type_label} vorbereitet."
        if selected_cabinet_type_label else
        "Wähle einen Cabinettyp, um ein konkretes Cabinet darunter anzulegen."
    )
    contextual_ctas = ["<a class='mini-cta' href='#intake-form'>+ Neues Dokument</a>", "<a class='mini-cta' href='/ui/admin'>Admin öffnen</a>"]
    if selected_cabinet_type_label:
        contextual_ctas.insert(0, f"<span class='mini-cta'>{_escape(selected_cabinet_type_label)} aktiv</span>")
    if selected_kind == "cabinet" and selected_cabinet_label:
        contextual_ctas.insert(1, f"<span class='mini-cta'>{_escape(selected_cabinet_label)} aktiv</span>")
    if selected_register_label:
        contextual_ctas.insert(2, f"<span class='mini-cta'>{_escape(selected_register_label)} im Fokus</span>")
    contextual_cta_html = "".join(contextual_ctas)
    show_cabinet_form = selected_kind in {None, "cabinet_type", "document_type", "register"}
    show_register_form = selected_kind == "cabinet" and any(str(cab.id) == selected_cabinet_id and cab.cabinet_type and cab.cabinet_type.register_types for cab in cabinets)
    cabinet_form_style = "margin-bottom:0; padding:16px;" if show_cabinet_form else "display:none; margin-bottom:0; padding:16px;"
    register_form_style = "margin-bottom:0; padding:16px;" if show_register_form else "display:none; margin-bottom:0; padding:16px;"
    summary_parts: list[str] = []
    if show_cabinet_form:
        summary_parts.append(cabinet_cta_label)
    if show_register_form:
        summary_parts.append(register_cta_label)
    if available_document_types:
        summary_parts.append("Dokument erfassen")
    if not selected_node:
        return f"""
        <div class='panel'>
          <p class='muted'>Wähle links einen Knoten, um hier Kurzinfos, Status und nächste Aktionen zu sehen.</p>
        </div>
        """

    kind_labels = {
        "cabinet_type": "Cabinettyp",
        "cabinet": "Cabinet",
        "register_type": "Registertyp",
        "register": "Register",
        "document_type": "Dokumenttyp",
    }
    label = selected_node.get("label", "Auswahl")
    kind = kind_labels.get(selected_node.get("kind"), selected_node.get("kind", "Objekt"))
    node_id = selected_node.get("id", "")
    create_hint = (
        "Dieser Cabinettyp ist ausgewählt, du kannst jetzt konkrete Cabinets darunter anlegen."
        if selected_node.get("kind") == "cabinet_type" else (
            "Dieses Cabinet ist ausgewählt, du kannst direkt darunter weitere Register ergänzen."
            if selected_node.get("kind") == "cabinet" else (
                "Dieser Registertyp ist ausgewählt, konkrete Register entstehen später innerhalb eines Cabinets."
                if selected_node.get("kind") == "register_type" else (
                    "Dieses Register ist ausgewählt, die Struktur bleibt hier im Blick und neue Register/Cabinets sind direkt anlegbar."
                    if selected_node.get("kind") == "register" else "Dokumenttyp-Kontext aktiv, Strukturaktionen bleiben trotzdem direkt verfügbar."
                )
            )
        )
    )
    return f"""
    <div class='panel'>
      <div class='detail-row'><div class='detail-key'>Auswahl</div><div class='detail-value'>{_escape(label)}</div></div>
      <div class='detail-row'><div class='detail-key'>Art</div><div class='detail-value'>{_escape(kind)}</div></div>
      <div class='detail-row'><div class='detail-key'>ID</div><div class='detail-value'><pre>{_escape(node_id)}</pre></div></div>
      <div class='actions'>
        <a class='chip' href='/ui/app?node_kind={_escape(selected_node.get("kind", ""))}&node_id={_escape(node_id)}' target='_blank' rel='noopener noreferrer'>In neuem Tab ↗</a>
        <a class='chip' href='/ui/admin'>Admin</a>
      </div>
    </div>
    """


def _render_object_overview(
    all_documents: list[Document],
    *,
    search_query: str,
    filter_kind: str,
) -> tuple[str, str, str]:
    normalized_query = (search_query or "").strip().lower()
    active_filter = filter_kind if filter_kind in {"all", "typed", "untyped", "recent"} else "all"

    def matches_query(document: Document) -> bool:
        if not normalized_query:
            return True
        metadata = metadata_from_json(document.metadata_json)
        haystack_parts = [
            document.title or "",
            document.name or "",
            document.document_type.name if document.document_type else "",
            document.description or "",
            document.author or "",
            document.tags or "",
            json.dumps(metadata, ensure_ascii=False) if metadata else "",
        ]
        haystack = " ".join(haystack_parts).lower()
        return normalized_query in haystack

    def matches_filter(document: Document) -> bool:
        if active_filter == "typed":
            return document.document_type is not None
        if active_filter == "untyped":
            return document.document_type is None
        if active_filter == "recent":
            return True
        return True

    filtered_documents = [document for document in all_documents if matches_filter(document) and matches_query(document)]
    if active_filter == "recent":
        filtered_documents = filtered_documents[:8]

    total_count = len(all_documents)
    typed_count = sum(1 for document in all_documents if document.document_type is not None)
    untyped_count = total_count - typed_count

    def chip(label: str, value: str, count: int | None = None) -> str:
        active = " active" if active_filter == value else ""
        suffix = f" ({count})" if count is not None else ""
        return f'<a class="chip{active}" href="/ui/app?filter_kind={value}&q={quote_plus(search_query or "")}">{_escape(label + suffix)}</a>'

    stats_html = f"""
      <div class="stats-grid">
        <div class="stat-card"><div class="muted">Gesamt</div><strong>{total_count}</strong></div>
        <div class="stat-card"><div class="muted">Mit Typ</div><strong>{typed_count}</strong></div>
        <div class="stat-card"><div class="muted">Ohne Typ</div><strong>{untyped_count}</strong></div>
      </div>
    """

    toolbar_html = f"""
      <div class="overview-toolbar">
        <div class="chip-row">
          {chip('Alle', 'all', total_count)}
          {chip('Mit Typ', 'typed', typed_count)}
          {chip('Ohne Typ', 'untyped', untyped_count)}
          {chip('Zuletzt verwendet', 'recent', min(total_count, 8))}
          <a class="chip" href="/ui/app">Zurücksetzen</a>
        </div>
      </div>
    """

    if not filtered_documents:
        list_html = "<div class='muted'>Keine Objekte für diese Suche oder Filter gefunden.</div>"
    else:
        cards: list[str] = []
        for document in filtered_documents:
            metadata = metadata_from_json(document.metadata_json)
            metadata_items = []
            for key, value in list(metadata.items())[:4]:
                pretty_value = ", ".join(value) if isinstance(value, list) else value
                metadata_items.append(f"{_escape(key)}: {_escape(pretty_value)}")
            metadata_preview = " · ".join(metadata_items)
            metadata_preview_html = f'<div class="metadata-preview">{metadata_preview}</div>' if metadata_preview else ""
            type_label = document.document_type.name if document.document_type else "Ohne Dokumenttyp"
            status_label = "Klassifiziert" if document.document_type else "Offen"
            status_icon = "●" if document.document_type else "○"
            doc_type_value = str(getattr(document.doc_type, "value", document.doc_type)).lower()
            type_icon = "🧾" if doc_type_value == "pdf" else ("🖼️" if doc_type_value == "image" else ("📝" if doc_type_value == "text" else "📄"))
            cards.append(
                f'<a class="object-card" href="/ui/app/documents/{document.id}">'
                f'<div class="object-top"><div><strong>{type_icon} {_escape(document.title or document.name)}</strong><div class="muted">{_escape(document.name)}</div></div>'
                f'<div class="meta-pill">{_escape(status_icon)} {_escape(status_label)}</div></div>'
                f'<div class="meta-row">'
                f'<span class="meta-pill">{_escape(type_label)}</span>'
                f'<span class="meta-pill">{_escape(str(document.created_at))}</span>'
                f'<span class="meta-pill">{_escape(type_icon)} {_escape(doc_type_value.upper())}</span>'
                f'</div>'
                f'{metadata_preview_html}'
                f'</a>'
            )
        list_html = '<div class="object-list">' + ''.join(cards) + '</div>'

    recent_links = []
    for index, document in enumerate(all_documents[:6]):
        marker = "★" if index < 2 else "🕘"
        recent_links.append(
            f'<a class="recent-link" href="/ui/app/documents/{document.id}"><strong>{marker} {_escape(document.title or document.name)}</strong>'
            f'<div class="muted">{_escape(document.document_type.name if document.document_type else "Ohne Dokumenttyp")}</div></a>'
        )
    recent_html = ''.join(recent_links) or "<p class='muted'>Noch keine zuletzt genutzten Objekte.</p>"

    summary_html = stats_html + toolbar_html
    overview_html = list_html
    return overview_html, summary_html, f'<div class="recent-grid">{recent_html}</div>'


def _admin_document_type_options(cabinets: list[Cabinet], selected_document_type: DocumentType | None) -> str:
    items: list[tuple[str, str]] = []
    selected_value = str(selected_document_type.id) if selected_document_type else None
    for cabinet in cabinets:
        for register in sorted(cabinet.registers, key=lambda item: item.order):
            for doc_type in sorted(register.document_types, key=lambda item: item.order):
                items.append((str(doc_type.id), f"{cabinet.name} → {register.name} → {doc_type.name}"))
        for doc_type in sorted(cabinet.document_types, key=lambda item: item.order):
            items.append((str(doc_type.id), f"{cabinet.name} → {doc_type.name}"))
    return _option_list(items, selected_value=selected_value, include_blank="Bitte wählen")


def _render_admin_create_panel(
    cabinet_types: list[CabinetType],
    cabinets: list[Cabinet],
    selected_document_type: DocumentType | None,
    *,
    selected_definition_kind: str | None = None,
    selected_definition_id: str | None = None,
    selected_metadata_field_id: str | None = None,
) -> str:
    cabinet_options = _option_list([(str(cabinet.id), f"{cabinet.cabinet_type.name if cabinet.cabinet_type else 'Ohne Typ'} → {cabinet.name}") for cabinet in cabinets], include_blank="Bitte wählen")
    register_type_options = _option_list(
        [
            (str(register_type.id), f"{register_type.cabinet_type.name if register_type.cabinet_type else 'Ohne Cabinettyp'} → {register_type.name}")
            for cabinet_type in cabinet_types
            for register_type in sorted(cabinet_type.register_types, key=lambda item: item.order)
        ],
        include_blank="Bitte wählen",
    )
    register_options = _option_list(
        [
            (str(register.id), f"{cabinet.name} → {register.name}")
            for cabinet in cabinets
            for register in sorted(cabinet.registers, key=lambda item: item.order)
        ],
        include_blank="Bitte wählen",
    )
    cabinet_type_options = _option_list([(str(cabinet_type.id), cabinet_type.name) for cabinet_type in cabinet_types], include_blank="Bitte wählen")
    document_type_field_options = _admin_document_type_options(cabinets, selected_document_type)
    field_type_options = "".join(f'<option value="{value}">{value}</option>' for value in ["text", "number", "currency", "date", "datetime", "selection", "multi_selection", "boolean", "long_text", "url", "email", "phone"])
    width_options = "".join(f'<option value="{value}">{value}</option>' for value in ["full", "half", "third", "quarter"])

    selected_cabinet_type = next((ct for ct in cabinet_types if str(ct.id) == selected_definition_id), None) if selected_definition_kind == "cabinet_type" else None
    selected_register_type = next((rt for ct in cabinet_types for rt in ct.register_types if str(rt.id) == selected_definition_id), None) if selected_definition_kind == "register_type" else None
    selected_document_type_definition = None
    if selected_definition_kind == "document_type":
        selected_document_type_definition = next((dt for ct in cabinet_types for dt in ct.document_type_definitions if str(dt.id) == selected_definition_id), None)
        if not selected_document_type_definition:
            selected_document_type_definition = next((dt for ct in cabinet_types for rt in ct.register_types for dt in rt.document_type_definitions if str(dt.id) == selected_definition_id), None)
    selected_metadata_field = None
    if selected_metadata_field_id:
        selected_metadata_field = next((field for ct in cabinet_types for field in ct.metadata_fields if str(field.id) == selected_metadata_field_id), None)
        if not selected_metadata_field:
            selected_metadata_field = next((field for ct in cabinet_types for rt in ct.register_types for field in rt.metadata_fields if str(field.id) == selected_metadata_field_id), None)
        if not selected_metadata_field:
            selected_metadata_field = next((field for ct in cabinet_types for dt in ct.document_type_definitions for field in dt.fields if str(field.id) == selected_metadata_field_id), None)
        if not selected_metadata_field:
            selected_metadata_field = next((field for ct in cabinet_types for rt in ct.register_types for dt in rt.document_type_definitions for field in dt.fields if str(field.id) == selected_metadata_field_id), None)

    context_hint = "Öffne genau die Maske, die du gerade brauchst."
    default_target = ""
    if selected_cabinet_type:
        context_hint = f"{selected_cabinet_type.name} ist ausgewählt. Sinnvoll sind jetzt Registertyp, Dokumenttyp oder Metadatenfeld."
        default_target = "admin-form-register-type"
    elif selected_register_type:
        context_hint = f"{selected_register_type.name} ist ausgewählt. Sinnvoll sind jetzt Dokumenttyp oder Metadatenfeld."
        default_target = "admin-form-document-type"
    elif selected_document_type_definition:
        context_hint = f"{selected_document_type_definition.name} ist ausgewählt. Sinnvoll sind jetzt Metadatenfelder für diesen Dokumenttyp."
        default_target = "admin-form-metadata-field"
    if selected_metadata_field:
        context_hint = f"Metadatenfeld {selected_metadata_field.label or selected_metadata_field.name} wird bearbeitet. Bestehende JSON-Werte bleiben bei Typ- oder Längenänderungen erhalten."
        default_target = "admin-form-metadata-field-edit"
    edit_field_type_options = "".join(
        f'<option value="{value}" {"selected" if selected_metadata_field and selected_metadata_field.field_type == value else ""}>{value}</option>'
        for value in ["text", "number", "currency", "date", "datetime", "selection", "multi_selection", "boolean", "long_text", "url", "email", "phone"]
    )
    edit_width_options = "".join(
        f'<option value="{value}" {"selected" if selected_metadata_field and selected_metadata_field.width == value else ""}>{value}</option>'
        for value in ["full", "half", "third", "quarter"]
    )

    return f"""
      <div class="stack">
        <div class="panel" style="margin-bottom:0;">
          <h3>Neu anlegen</h3>
          <p class="muted">{_escape(context_hint)}</p>
          <input type="hidden" id="admin-create-default-target" value="{_escape(default_target)}">
          <input type="hidden" id="admin-create-selected-definition-kind" value="{_escape(selected_definition_kind or '')}">
          <input type="hidden" id="admin-create-selected-definition-id" value="{_escape(selected_definition_id or '')}">
          <input type="hidden" id="admin-create-selected-metadata-field-id" value="{_escape(selected_metadata_field_id or '')}">
          <div class="actions">
            <button type="button" class="primary admin-create-toggle" data-target="admin-form-cabinet-type">Cabinettyp</button>
            <button type="button" class="primary admin-create-toggle" data-target="admin-form-register-type">Registertyp</button>
            <button type="button" class="primary admin-create-toggle" data-target="admin-form-document-type">Dokumenttyp</button>
            <button type="button" class="primary admin-create-toggle" data-target="admin-form-cabinet">Cabinet</button>
            <button type="button" class="primary admin-create-toggle" data-target="admin-form-register">Register</button>
            <button type="button" class="primary admin-create-toggle" data-target="admin-form-metadata-field">Metadatenfeld</button>
          </div>
        </div>

        <form method="post" action="/ui/admin/cabinet-types" class="panel admin-create-section" id="admin-form-cabinet-type" style="display:none; margin-bottom:0;"><h3>Cabinettyp anlegen</h3><p class="muted">Definiere die fachliche Klasse von Cabinets, z. B. ERB, Personal oder Verträge.</p><div class="field-grid"><div class="field"><label>Name</label><input type="text" name="name" required></div><div class="field"><label>Reihenfolge</label><input type="number" name="order" value="0"></div><div class="field full"><label>Beschreibung</label><textarea name="description"></textarea></div></div><div class="actions"><button class="primary" type="submit">Cabinettyp speichern</button></div></form>

        <form method="post" action="/ui/admin/register-types" class="panel admin-create-section" id="admin-form-register-type" style="display:none; margin-bottom:0;"><h3>Registertyp anlegen</h3><p class="muted">Definiere erlaubte Registertypen unter einem Cabinettyp.</p><div class="field-grid"><div class="field"><label>Cabinettyp</label><select name="cabinet_type_id" required>{cabinet_type_options}</select></div><div class="field"><label>Name</label><input type="text" name="name" required></div><div class="field"><label>Reihenfolge</label><input type="number" name="order" value="0"></div><div class="field full"><label>Beschreibung</label><textarea name="description"></textarea></div></div><div class="actions"><button class="primary" type="submit">Registertyp speichern</button></div></form>

        <form method="post" action="/ui/admin/document-types" class="panel admin-create-section" id="admin-form-document-type" style="display:none; margin-bottom:0;"><h3>Dokumenttyp anlegen</h3><p class="muted">Definiere Objekttypen auf der Definitionsebene, entweder direkt unter einem Cabinettyp oder unter einem Registertyp.</p><div class="field-grid"><div class="field"><label>Zieltyp</label><select name="target_kind"><option value="cabinet_type">Cabinettyp</option><option value="register_type">Registertyp</option></select></div><div class="field"><label>Cabinettyp</label><select name="cabinet_type_id"><option value="">Bitte wählen</option>{cabinet_type_options}</select></div><div class="field"><label>Registertyp</label><select name="register_type_id"><option value="">Bitte wählen</option>{register_type_options}</select></div><div class="field"><label>Name</label><input type="text" name="name" required></div><div class="field"><label>Icon</label><input type="text" name="icon" placeholder="optional"></div><div class="field"><label>Reihenfolge</label><input type="number" name="order" value="0"></div><div class="field full"><label>Beschreibung</label><textarea name="description"></textarea></div></div><div class="actions"><button class="primary" type="submit">Dokumenttyp speichern</button></div></form>

        <form method="post" action="/ui/admin/cabinets" class="panel admin-create-section" id="admin-form-cabinet" style="display:none; margin-bottom:0;"><h3>Cabinet anlegen</h3><p class="muted">Lege ein konkretes Cabinet innerhalb eines Cabinettyps an, z. B. 2025 oder 2026 unter ERB.</p><div class="field-grid"><div class="field"><label>Cabinettyp</label><select name="cabinet_type_id" required>{cabinet_type_options}</select></div><div class="field"><label>Name</label><input type="text" name="name" required></div><div class="field"><label>Reihenfolge</label><input type="number" name="order" value="0"></div><div class="field full"><label>Beschreibung</label><textarea name="description"></textarea></div></div><div class="actions"><button class="primary" type="submit">Cabinet speichern</button></div></form>

        <form method="post" action="/ui/admin/registers" class="panel admin-create-section" id="admin-form-register" style="display:none; margin-bottom:0;"><h3>Register anlegen</h3><p class="muted">Lege konkrete Register in einem Cabinet an und ordne optional einen Registertyp zu.</p><div class="field-grid"><div class="field"><label>Cabinet</label><select name="cabinet_id" required>{cabinet_options}</select></div><div class="field"><label>Registertyp</label><select name="register_type_id"><option value="">Bitte wählen</option>{register_type_options}</select></div><div class="field"><label>Name</label><input type="text" name="name" required></div><div class="field"><label>Reihenfolge</label><input type="number" name="order" value="0"></div><div class="field full"><label>Beschreibung</label><textarea name="description"></textarea></div></div><div class="actions"><button class="primary" type="submit">Register speichern</button></div></form>

        <form method="post" action="/ui/admin/metadata-fields" class="panel admin-create-section" id="admin-form-metadata-field" style="display:none; margin-bottom:0;"><h3>Metadatenfeld anlegen</h3><p class="muted">Lege strukturierte Felder für Cabinettyp, Registertyp, Cabinet, Register oder Dokumenttyp fest.</p><div class="field-grid"><div class="field"><label>Zieltyp</label><select name="target_kind"><option value="cabinet_type">Cabinettyp</option><option value="register_type">Registertyp</option><option value="document_type">Dokumenttyp</option><option value="cabinet">Cabinet</option><option value="register">Register</option></select></div><div class="field"><label>Cabinettyp</label><select name="cabinet_type_id"><option value="">Bitte wählen</option>{cabinet_type_options}</select></div><div class="field"><label>Registertyp</label><select name="register_type_id"><option value="">Bitte wählen</option>{register_type_options}</select></div><div class="field"><label>Cabinet</label><select name="cabinet_id"><option value="">Bitte wählen</option>{cabinet_options}</select></div><div class="field"><label>Register</label><select name="register_id"><option value="">Bitte wählen</option>{register_options}</select></div><div class="field"><label>Dokumenttyp</label><select name="document_type_id"><option value="">Bitte wählen</option>{document_type_field_options}</select></div><div class="field"><label>Name</label><input type="text" name="name" required></div><div class="field"><label>Label</label><input type="text" name="label"></div><div class="field"><label>Feldtyp</label><select name="field_type">{field_type_options}</select></div><div class="field"><label>Breite</label><select name="width">{width_options}</select></div><div class="field"><label>Reihenfolge</label><input type="number" name="order" value="0"></div><div class="field"><label>Placeholder</label><input type="text" name="placeholder"></div><div class="field"><label>Default</label><input type="text" name="default_value"></div><div class="field"><label><input type="checkbox" name="is_required"> Pflichtfeld</label></div><div class="field"><label><input type="checkbox" name="is_unique"> Eindeutig</label></div><div class="field full"><label>Beschreibung</label><textarea name="description"></textarea></div></div><div class="actions"><button class="primary" type="submit">Feld speichern</button></div></form>
        <form method="post" action="/ui/admin/metadata-fields/{selected_metadata_field.id if selected_metadata_field else ''}" class="panel admin-create-section" id="admin-form-metadata-field-edit" style="display:none; margin-bottom:0;"><h3>Metadatenfeld bearbeiten</h3><p class="muted">Änderungen wirken auf Darstellung und Validierung. Bestehende JSON-Werte werden nicht umgeschrieben oder gelöscht.</p><input type="hidden" name="selected_definition_kind" value="{_escape(selected_definition_kind or '')}"><input type="hidden" name="selected_definition_id" value="{_escape(selected_definition_id or '')}"><div class="field-grid"><div class="field"><label>Name</label><input type="text" name="name" value="{_escape(selected_metadata_field.name) if selected_metadata_field else ''}" required></div><div class="field"><label>Label</label><input type="text" name="label" value="{_escape(selected_metadata_field.label or '') if selected_metadata_field else ''}"></div><div class="field"><label>Feldtyp</label><select name="field_type">{edit_field_type_options}</select></div><div class="field"><label>Breite</label><select name="width">{edit_width_options}</select></div><div class="field"><label>Reihenfolge</label><input type="number" name="order" value="{selected_metadata_field.order if selected_metadata_field else 0}"></div><div class="field"><label>Placeholder</label><input type="text" name="placeholder" value="{_escape(selected_metadata_field.placeholder or '') if selected_metadata_field else ''}"></div><div class="field"><label>Default</label><input type="text" name="default_value" value="{_escape(selected_metadata_field.default_value or '') if selected_metadata_field else ''}"></div><div class="field"><label><input type="checkbox" name="is_required" {'checked' if selected_metadata_field and selected_metadata_field.is_required else ''}> Pflichtfeld</label></div><div class="field"><label><input type="checkbox" name="is_unique" {'checked' if selected_metadata_field and selected_metadata_field.is_unique else ''}> Eindeutig</label></div><div class="field full"><label>Beschreibung</label><textarea name="description">{_escape(selected_metadata_field.description or '') if selected_metadata_field else ''}</textarea></div></div><div class="actions"><button class="primary" type="submit">Metadatenfeld speichern</button></div></form>
      </div>
    """


def _render_identity_panel(
    users: list[User],
    roles: list[Role],
    teams: list[Team],
    *,
    identity_tab: str = "users",
    selected_user_id: str | None = None,
    selected_role_id: str | None = None,
    selected_team_id: str | None = None,
    message: str | None = None,
) -> str:
    active_tab = identity_tab if identity_tab in {"users", "roles", "teams"} else "users"
    selected_user = next((user for user in users if str(user.id) == str(selected_user_id or "")), None)
    selected_role = next((role for role in roles if str(role.id) == str(selected_role_id or "")), None)
    selected_team = next((team for team in teams if str(team.id) == str(selected_team_id or "")), None)
    role_options = "".join(
        f'<label class="checkbox-item"><input type="checkbox" name="role_ids" value="{role.id}"> {_escape(role.name)}</label>'
        for role in roles
    ) or '<div class="muted">Noch keine Rollen vorhanden.</div>'

    edit_role_options = "".join(
        f'<label class="checkbox-item"><input type="checkbox" name="role_ids" value="{role.id}" {"checked" if selected_user and any(assignment.role_id == role.id for assignment in selected_user.role_assignments) else ""}> {_escape(role.name)}</label>'
        for role in roles
    ) or '<div class="muted">Noch keine Rollen vorhanden.</div>'
    team_member_options = "".join(
        f'<label class="checkbox-item"><input type="checkbox" name="member_user_ids" value="{user.id}"> {_escape(user.display_name)} <span class="muted">({_escape(user.email)})</span></label>'
        for user in users
    ) or '<div class="muted">Noch keine Benutzer vorhanden.</div>'
    edit_team_member_options = "".join(
        f'<label class="checkbox-item"><input type="checkbox" name="member_user_ids" value="{user.id}" {"checked" if selected_team and any(membership.user_id == user.id for membership in selected_team.memberships) else ""}> {_escape(user.display_name)} <span class="muted">({_escape(user.email)})</span></label>'
        for user in users
    ) or '<div class="muted">Noch keine Benutzer vorhanden.</div>'
    message_html = f'<div class="badge" style="display:inline-block;">{_escape(message)}</div>' if message else ""

    user_rows = []
    for user in users:
        assigned_roles = sorted({assignment.role.name for assignment in user.role_assignments if assignment.role})
        role_badges = " ".join(f'<span class="badge">{_escape(role_name)}</span>' for role_name in assigned_roles) or '<span class="muted">Keine Rollen</span>'
        source_badge = "Lokal" if user.auth_source == "local" else user.auth_source
        user_rows.append(
            f"<div class='def-detail-card'>"
            f"<h3>👤 {_escape(user.display_name)}</h3>"
            f"<div class='def-detail-row'><div class='def-detail-key'>E-Mail</div><div class='def-detail-value'>{_escape(user.email)}</div></div>"
            f"<div class='def-detail-row'><div class='def-detail-key'>Quelle</div><div class='def-detail-value'>{_escape(source_badge)}</div></div>"
            f"<div class='def-detail-row'><div class='def-detail-key'>Status</div><div class='def-detail-value'>{_escape(user.status)}</div></div>"
            f"<div class='def-detail-row'><div class='def-detail-key'>Rollen</div><div class='def-detail-value'>{role_badges}</div></div>"
            f"<div class='actions'><a class='chip' href='/ui/admin/identity?identity_tab=users&selected_user_id={user.id}#identity-admin'>Bearbeiten</a>"
            f"<form method='post' action='/ui/admin/users/{user.id}/toggle-status' style='display:inline;'><button class='chip' type='submit'>{'Deaktivieren' if user.status == 'active' else 'Aktivieren'}</button></form></div>"
            f"</div>"
        )
    users_html = "".join(user_rows) or "<p class='def-empty'>Noch keine Benutzer angelegt.</p>"

    role_rows = []
    for role in roles:
        assignment_count = sum(1 for assignment in role.assignments if assignment.user)
        role_rows.append(
            f"<div class='def-detail-card'>"
            f"<h3>🛡️ {_escape(role.name)}</h3>"
            f"<div class='def-detail-row'><div class='def-detail-key'>Beschreibung</div><div class='def-detail-value'>{_escape(role.description or '—')}</div></div>"
            f"<div class='def-detail-row'><div class='def-detail-key'>Systemrolle</div><div class='def-detail-value'>{'Ja' if role.is_system else 'Nein'}</div></div>"
            f"<div class='def-detail-row'><div class='def-detail-key'>Zuweisungen</div><div class='def-detail-value'>{assignment_count}</div></div>"
            f"<div class='def-detail-row'><div class='def-detail-key'>Rechte</div><div class='def-detail-value'><pre style='margin:0;white-space:pre-wrap;word-break:break-word;'>{_escape(role.permissions_json or '[]')}</pre></div></div>"
            f"<div class='actions'><a class='chip' href='/ui/admin/identity?identity_tab=roles&selected_role_id={role.id}#identity-admin'>Bearbeiten</a></div>"
            f"</div>"
        )
    roles_html = "".join(role_rows) or "<p class='def-empty'>Noch keine Rollen angelegt.</p>"

    team_rows = []
    for team in teams:
        member_names = sorted({membership.user.display_name for membership in team.memberships if membership.user})
        member_badges = " ".join(f'<span class="badge">{_escape(member_name)}</span>' for member_name in member_names) or '<span class="muted">Keine Mitglieder</span>'
        team_rows.append(
            f"<div class='def-detail-card'>"
            f"<h3>👥 {_escape(team.name)}</h3>"
            f"<div class='def-detail-row'><div class='def-detail-key'>Beschreibung</div><div class='def-detail-value'>{_escape(team.description or '—')}</div></div>"
            f"<div class='def-detail-row'><div class='def-detail-key'>Mitglieder</div><div class='def-detail-value'>{member_badges}</div></div>"
            f"<div class='actions'><a class='chip' href='/ui/admin/identity?identity_tab=teams&selected_team_id={team.id}#identity-admin'>Bearbeiten</a></div>"
            f"</div>"
        )
    teams_html = "".join(team_rows) or "<p class='def-empty'>Noch keine Teams angelegt.</p>"

    users_section_style = "display:block;" if active_tab == "users" else "display:none;"
    roles_section_style = "display:block;" if active_tab == "roles" else "display:none;"
    teams_section_style = "display:block;" if active_tab == "teams" else "display:none;"

    return f"""
      <div class="stack">
        <div class="panel" style="margin-bottom:0;">
          <h3>User & Rollen</h3>
          <p class="muted">Natives Identitätsmodell für Archiva. Später kann Entra ID über dieselben Benutzerkonten und Rollen andocken.</p>
          {message_html}
          <div class="actions">
            <a class="pill{' is-active' if active_tab == 'users' else ''}" href="/ui/admin/identity?identity_tab=users#identity-admin">Benutzer</a>
            <a class="pill{' is-active' if active_tab == 'roles' else ''}" href="/ui/admin/identity?identity_tab=roles#identity-admin">Rollen</a>
            <a class="pill{' is-active' if active_tab == 'teams' else ''}" href="/ui/admin/identity?identity_tab=teams#identity-admin">Teams</a>
          </div>
        </div>

        <div id="identity-admin" style="{users_section_style}">
          <form method="post" action="/ui/admin/users" class="panel" style="margin-bottom:0;">
            <h3>Benutzer anlegen</h3>
            <p class="muted">Für v1 reicht ein lokaler Benutzerstamm. Externe Anmeldung kann später über `external_subject` und `auth_source` ergänzt werden.</p>
            <div class="field-grid">
              <div class="field"><label>Anzeigename</label><input type="text" name="display_name" required></div>
              <div class="field"><label>E-Mail</label><input type="email" name="email" required></div>
              <div class="field"><label>Status</label><select name="status"><option value="active">active</option><option value="invited">invited</option><option value="disabled">disabled</option></select></div>
              <div class="field full"><label>Rollen</label><div class="checkbox-group">{role_options}</div></div>
            </div>
            <div class="actions"><button class="primary" type="submit">Benutzer speichern</button></div>
          </form>
          <form method="post" action="/ui/admin/users/{selected_user.id if selected_user else ''}" class="panel" style="margin:16px 0 0 0; {'display:block;' if selected_user else 'display:none;'}">
            <h3>Benutzer bearbeiten</h3>
            <p class="muted">Bestehende Benutzer können hier umbenannt, neu gerollt oder deaktiviert werden.</p>
            <div class="field-grid">
              <div class="field"><label>Anzeigename</label><input type="text" name="display_name" value="{_escape(selected_user.display_name) if selected_user else ''}" required></div>
              <div class="field"><label>E-Mail</label><input type="email" name="email" value="{_escape(selected_user.email) if selected_user else ''}" required></div>
              <div class="field"><label>Status</label><select name="status"><option value="active" {'selected' if selected_user and selected_user.status == 'active' else ''}>active</option><option value="invited" {'selected' if selected_user and selected_user.status == 'invited' else ''}>invited</option><option value="disabled" {'selected' if selected_user and selected_user.status == 'disabled' else ''}>disabled</option></select></div>
              <div class="field full"><label>Rollen</label><div class="checkbox-group">{edit_role_options}</div></div>
            </div>
            <div class="actions"><button class="primary" type="submit">Benutzer aktualisieren</button></div>
          </form>
          <div class="stack" style="margin-top:16px;">{users_html}</div>
        </div>

        <div style="{roles_section_style}">
          <form method="post" action="/ui/admin/roles" class="panel" style="margin-bottom:0;">
            <h3>Rolle anlegen</h3>
            <p class="muted">Rollen bleiben bewusst grob und lesbar. Feingranulare Rechte können wir später verfeinern.</p>
            <div class="field-grid">
              <div class="field"><label>Name</label><input type="text" name="name" required></div>
              <div class="field full"><label>Beschreibung</label><textarea name="description"></textarea></div>
              <div class="field full"><label>Rechte als JSON</label><textarea name="permissions_json">[]</textarea></div>
            </div>
            <div class="actions"><button class="primary" type="submit">Rolle speichern</button></div>
          </form>
          <form method="post" action="/ui/admin/roles/{selected_role.id if selected_role else ''}" class="panel" style="margin:16px 0 0 0; {'display:block;' if selected_role else 'display:none;'}">
            <h3>Rolle bearbeiten</h3>
            <p class="muted">Name, Beschreibung und Rechte-JSON lassen sich hier direkt anpassen.</p>
            <div class="field-grid">
              <div class="field"><label>Name</label><input type="text" name="name" value="{_escape(selected_role.name) if selected_role else ''}" required></div>
              <div class="field full"><label>Beschreibung</label><textarea name="description">{_escape(selected_role.description or '') if selected_role else ''}</textarea></div>
              <div class="field full"><label>Rechte als JSON</label><textarea name="permissions_json">{_escape(selected_role.permissions_json or '[]') if selected_role else '[]'}</textarea></div>
            </div>
            <div class="actions"><button class="primary" type="submit">Rolle aktualisieren</button></div>
          </form>
          <div class="stack" style="margin-top:16px;">{roles_html}</div>
        </div>

        <div style="{teams_section_style}">
          <form method="post" action="/ui/admin/teams" class="panel" style="margin-bottom:0;">
            <h3>Team anlegen</h3>
            <p class="muted">Teams und Gruppen sind die nächste fachliche Schicht für Workflow-Zuständigkeiten.</p>
            <div class="field-grid">
              <div class="field"><label>Name</label><input type="text" name="name" required></div>
              <div class="field full"><label>Beschreibung</label><textarea name="description"></textarea></div>
              <div class="field full"><label>Mitglieder</label><div class="checkbox-group">{team_member_options}</div></div>
            </div>
            <div class="actions"><button class="primary" type="submit">Team speichern</button></div>
          </form>
          <form method="post" action="/ui/admin/teams/{selected_team.id if selected_team else ''}" class="panel" style="margin:16px 0 0 0; {'display:block;' if selected_team else 'display:none;'}">
            <h3>Team bearbeiten</h3>
            <p class="muted">Mitglieder und Beschreibung lassen sich hier direkt pflegen.</p>
            <div class="field-grid">
              <div class="field"><label>Name</label><input type="text" name="name" value="{_escape(selected_team.name) if selected_team else ''}" required></div>
              <div class="field full"><label>Beschreibung</label><textarea name="description">{_escape(selected_team.description or '') if selected_team else ''}</textarea></div>
              <div class="field full"><label>Mitglieder</label><div class="checkbox-group">{edit_team_member_options}</div></div>
            </div>
            <div class="actions"><button class="primary" type="submit">Team aktualisieren</button></div>
          </form>
          <div class="stack" style="margin-top:16px;">{teams_html}</div>
        </div>
      </div>
    """
