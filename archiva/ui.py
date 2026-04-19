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

from archiva.database import get_db
from archiva.config import load_settings
from archiva.metadata_validation import metadata_from_json, validate_document_metadata, MetadataValidationError
from archiva.models import Cabinet, CabinetType, DocType, Document, DocumentType, MetadataField, Register
from archiva.preview_queue import enqueue_preview_job, get_latest_preview_artifact, get_latest_preview_job
from archiva.storage import StorageManager

router = APIRouter(tags=["ui"])


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
    cabinet_types = db.query(CabinetType).order_by(CabinetType.order, CabinetType.name).all()
    if len(cabinet_types) != 1 or cabinet_types[0].name.strip().lower() != "bestand":
        return False, "Keine Bestand-Bridge-Struktur gefunden"

    bestand_type = cabinet_types[0]
    legacy_cabinets = db.query(Cabinet).where(Cabinet.cabinet_type_id == bestand_type.id).order_by(Cabinet.order).all()
    if not legacy_cabinets:
        return False, "Keine Cabinets unter Bestand gefunden"

    migrated_count = 0
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

            for doc_type in list(sorted(legacy_register.document_types, key=lambda item: item.order)):
                doc_type.cabinet_id = new_cabinet.id
                doc_type.register_id = None
            for field in list(sorted(legacy_register.metadata_fields, key=lambda item: item.order)):
                field.cabinet_id = new_cabinet.id
                field.register_id = None

            db.delete(legacy_register)
            migrated_count += 1

        fallback_cabinet = next((cab for cab in migrated_cabinets if cab.name == "2026"), None)
        if fallback_cabinet is None and migrated_cabinets:
            fallback_cabinet = migrated_cabinets[0]

        for doc_type in list(sorted(legacy_cabinet.document_types, key=lambda item: item.order)):
            doc_type.cabinet_id = fallback_cabinet.id if fallback_cabinet else None
        for field in list(sorted(legacy_cabinet.metadata_fields, key=lambda item: item.order)):
            field.cabinet_id = fallback_cabinet.id if fallback_cabinet else None

        db.delete(legacy_cabinet)

    db.delete(bestand_type)
    db.commit()
    return True, f"Bestand-Struktur migriert, {migrated_count} Register zu Cabinets umgewandelt"


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


def _collect_form_metadata(form: Any, document_type: DocumentType) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    for field in sorted(document_type.fields, key=lambda item: item.order):
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
async def ui_admin_home(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    cabinet_types = db.query(CabinetType).order_by(CabinetType.order, CabinetType.name).all()
    cabinets = db.query(Cabinet).order_by(Cabinet.order).all()
    document_types = db.query(DocumentType).order_by(DocumentType.name).all()
    recent_documents = db.query(Document).order_by(Document.created_at.desc()).limit(10).all()
    selected_document_type = document_types[0] if document_types else None
    return HTMLResponse(
        content=_render_admin_page(
            cabinet_types=cabinet_types,
            cabinets=cabinets,
            document_types=document_types,
            recent_documents=recent_documents,
            selected_document_type=selected_document_type,
        )
    )


@router.get("/admin/document-types/{document_type_id}", response_class=HTMLResponse)
async def ui_admin_document_type_detail(
    document_type_id: UUID,
    request: Request,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    cabinet_types = db.query(CabinetType).order_by(CabinetType.order, CabinetType.name).all()
    cabinets = db.query(Cabinet).order_by(Cabinet.order).all()
    document_types = db.query(DocumentType).order_by(DocumentType.name).all()
    recent_documents = db.query(Document).order_by(Document.created_at.desc()).limit(10).all()
    selected_document_type = db.query(DocumentType).where(DocumentType.id == document_type_id).first()
    return HTMLResponse(
        content=_render_admin_page(
            cabinet_types=cabinet_types,
            cabinets=cabinets,
            document_types=document_types,
            recent_documents=recent_documents,
            selected_document_type=selected_document_type,
        )
    )


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
    recent_documents = all_documents[:10]
    cabinets, cabinet_type_model_ready = _safe_load_cabinets(db)
    document_types = db.query(DocumentType).order_by(DocumentType.name).all()
    selected_document_type = _selected_document_type(selected_document_type_id, db)
    selected_node = _resolve_archive_node(node_kind, node_id, cabinets, document_types)
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
    return HTMLResponse(content=_render_document_detail_page(document, message=message, error_field=error_field, error_message=error_message, form_values=form_values))


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


@router.post("/app/intake")
async def ui_app_intake(
    request: Request,
    file: UploadFile = File(...),
    document_type_id: UUID = Form(...),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    document_type = db.query(DocumentType).where(DocumentType.id == document_type_id).first()
    if not document_type:
        return _ui_redirect_with_message("/ui/app?message=Dokumenttyp+nicht+gefunden")

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

    document = Document(
        name=original_filename,
        title=Path(original_filename).stem,
        doc_type=detected_doc_type,
        document_type_id=document_type.id,
        mime_type=file.content_type,
        size_bytes=int(file_size),
        storage_path=str(relative_path),
        metadata_json=json.dumps(metadata, ensure_ascii=False),
    )
    db.add(document)
    db.flush()
    enqueue_preview_job(db, document)
    db.commit()

    return _ui_redirect_with_message(
        _app_message_url(document_type_id, message="Dokument erfolgreich gespeichert, Preview-Rendering eingereiht")
    )


@router.get("/workflows", response_class=HTMLResponse)
async def ui_workflows_home(request: Request) -> HTMLResponse:
    return HTMLResponse(content=_render_workflows_page())


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
async def ui_backfill_document_cabinets(
    return_to: str = Form("app"),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    updated, unresolved = _backfill_document_cabinet_ids(db)
    target = "/ui/app" if return_to == "app" else "/ui/admin"
    message = f"Document-Cabinet-Backfill abgeschlossen: {updated} gesetzt, {unresolved} ungelöst"
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
    name: str = Form(...),
    description: str = Form(""),
    order: int = Form(0),
    return_to: str = Form("admin"),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    register = Register(
        cabinet_id=cabinet_id,
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


@router.post("/admin/document-types")
async def ui_create_document_type(
    target_kind: str = Form(...),
    register_id: str = Form(""),
    cabinet_id: str = Form(""),
    name: str = Form(...),
    description: str = Form(""),
    icon: str = Form(""),
    order: int = Form(0),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    resolved_register_id = UUID(register_id) if register_id else None
    resolved_cabinet_id = UUID(cabinet_id) if cabinet_id else None
    if target_kind == "cabinet":
        resolved_register_id = None
    else:
        resolved_cabinet_id = None

    document_type = DocumentType(
        register_id=resolved_register_id,
        cabinet_id=resolved_cabinet_id,
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

    if target_kind == "cabinet":
        resolved_document_type_id = None
        resolved_register_id = None
    elif target_kind == "register":
        resolved_document_type_id = None
        resolved_cabinet_id = None
    else:
        resolved_cabinet_id = None
        resolved_register_id = None

    field = MetadataField(
        document_type_id=resolved_document_type_id,
        cabinet_id=resolved_cabinet_id,
        register_id=resolved_register_id,
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
    redirect_target = f"/ui/admin/document-types/{resolved_document_type_id}" if resolved_document_type_id else "/ui/admin"
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


def _render_admin_page(
    *,
    cabinet_types: list[CabinetType],
    cabinets: list[Cabinet],
    document_types: list[DocumentType],
    recent_documents: list[Document],
    selected_document_type: DocumentType | None,
) -> str:
    structure_html = _render_structure(cabinets)
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
    admin_create_html = _render_admin_create_panel(cabinet_types, cabinets, selected_document_type)

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
    .page {{ padding: 16px 18px; max-width: 1760px; margin: 0 auto; }}
    .hero {{ display: grid; grid-template-columns: minmax(0, 1.15fr) minmax(320px, 0.85fr); gap: 24px; align-items: stretch; margin-bottom: 24px; }}
    .hero-main, .hero-side {{ position:relative; overflow:hidden; }}
    .hero-main::before, .hero-side::before {{ content:""; position:absolute; inset:0; background: linear-gradient(135deg, rgba(79,140,255,0.14), rgba(77,212,255,0.06) 45%, transparent 80%); pointer-events:none; }}
    .hero-brand {{ display:flex; gap:14px; align-items:center; position:relative; z-index:1; }}
    .brand-mark {{ width:54px; height:54px; border-radius:18px; display:grid; place-items:center; background: linear-gradient(135deg, rgba(79,140,255,0.28), rgba(77,212,255,0.18)); border:1px solid rgba(77,212,255,0.25); }}
    .brand-mark svg {{ width:34px; height:34px; }}
    .eyebrow {{ letter-spacing:.12em; text-transform:uppercase; font-size:.78rem; color: var(--accent-2); font-weight:700; }}
    .hero h1 {{ margin: 6px 0 8px; font-size: 2.08rem; letter-spacing: -0.02em; }}
    .hero p {{ margin: 0; color: var(--muted); max-width: 70ch; line-height:1.6; position:relative; z-index:1; }}
    .pillbar {{ display: flex; gap: 12px; flex-wrap: wrap; margin-top: 16px; position:relative; z-index:1; }}
    .pill {{ background: rgba(255,255,255,0.04); border: 1px solid rgba(77,212,255,0.12); border-radius: 999px; padding: 9px 14px; color: var(--text); }}
    .grid {{ display: grid; grid-template-columns: 320px 1fr; gap: 20px; }}
    .panel {{ background: linear-gradient(180deg, rgba(18,25,51,0.96), rgba(15,22,48,0.96)); border: 1px solid rgba(77,212,255,0.10); border-radius: 20px; padding: 20px; box-shadow: var(--shadow); }}
    .panel h2, .panel h3 {{ margin-top: 0; }}
    .stack {{ display: grid; gap: 20px; }}
    .cols {{ display: grid; grid-template-columns: 1.1fr 0.9fr; gap: 20px; }}
    .tree ul {{ list-style: none; padding-left: 18px; margin: 10px 0; }}
    .tree li {{ margin: 6px 0; color: var(--muted); }}
    .muted {{ color: var(--muted); }}
    .badge {{ display: inline-block; margin-right: 8px; margin-bottom: 6px; padding: 4px 8px; border-radius: 999px; background: rgba(77,212,255,0.16); color: var(--accent-2); font-size: 0.85rem; }}
    .field-grid {{ display: grid; grid-template-columns: repeat(2, minmax(0,1fr)); gap: 12px; }}
    .field {{ display: grid; gap: 6px; }}
    .field.full {{ grid-column: 1 / -1; }}
    label {{ font-weight: 600; font-size: 0.95rem; }}
    input, textarea, select {{ width: 100%; border-radius: 14px; border: 1px solid rgba(77,212,255,0.10); background: var(--panel-deep); color: var(--text); padding: 12px; font: inherit; }}
    textarea {{ min-height: 92px; resize: vertical; }}
    .actions {{ display: flex; gap: 12px; flex-wrap: wrap; margin-top: 16px; }}
    button {{ border: none; border-radius: 999px; padding: 12px 16px; font: inherit; cursor: pointer; }}
    .primary {{ background: linear-gradient(135deg, var(--accent), var(--accent-2)); color: white; box-shadow: 0 8px 24px rgba(77,212,255,0.22); }}
    .small {{ font-size: 0.88rem; color: var(--muted); }}
    .status-chip {{ display:inline-flex; align-items:center; gap:8px; padding:6px 10px; border-radius:999px; background:rgba(110,231,183,0.10); border:1px solid rgba(110,231,183,0.18); color:#d6fff0; }}
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
        <p>Die Admin-Anwendung modelliert die Struktur deines Archivs. Cabinets, Registers, Dokumenttypen und Metadatenfelder bilden hier die ordnende Ebene hinter dem Content Flow.</p>
        <div class="pillbar">
          <span class="pill">Admin-Struktur</span>
          <span class="pill">Cabinets / Registers / Typen</span>
          <span class="pill">Metadatenmodell</span>
          <a class="pill" href="/ui/app">Zur ECM-App</a>
          <a class="pill" href="/ui/workflows">Zu Workflows</a>
        </div>
      </div>
      <div class="panel hero-side">
        <h3>Admin-Kontext</h3>
        <p class="muted">Hier definierst du das Systemgerüst. Die Erfassung und tägliche Nutzung laufen in der App, Workflows bleiben als eigene Ebene getrennt.</p>
        <div class="status-chip">Flow-Archiv Modellierung</div>
      </div>
    </section>
    <section class="grid">
      <aside class="stack">
        <div class="panel tree"><h2>Struktur</h2>{structure_html}</div>
        <div class="panel"><h2>Cabinettypen</h2><ul>{cabinet_type_list_html}</ul></div>
        <div class="panel"><h2>Dokumenttypen</h2><ul>{type_list_html}</ul></div>
      </aside>
      <main class="stack">
        <div class="cols">
          <div class="panel"><h2>Admin-Zusammenfassung</h2>{admin_summary_html}</div>
          <div class="panel"><h2>Zuletzt erfasste Dokumente</h2>{recent_documents_html}</div>
        </div>
        <div class="panel"><h2>Objekte anlegen</h2>{admin_create_html}</div>
        <div class="panel"><h2>Nächster Schritt</h2><p class="muted">Die eigentliche Objekterfassung, Dokumentaufnahme, Suche und nutzerfreundliche Arbeitsoberfläche liegen getrennt unter <a href="/ui/app">/ui/app</a>.</p></div>
      </main>
    </section>
  </div>
</body>
</html>
"""


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
) -> str:
    recent_documents_html = _render_recent_documents(recent_documents)
    archive_tree_html = _render_archive_tree(cabinets, selected_node, search_query)
    object_overview_html, object_summary_html, recent_favorites_html = _render_object_overview(
        all_documents,
        search_query=search_query,
        filter_kind=filter_kind,
    )
    node_results_html, node_header_html = _render_node_results(cabinets, all_documents, selected_node, search_query)
    selected_cabinet_name = ""
    selected_register_name = ""
    if selected_document_type:
        selected_register = selected_document_type.register
        selected_cabinet = selected_document_type.cabinet or (selected_register.cabinet if selected_register else None)
        selected_cabinet_name = selected_cabinet.name if selected_cabinet else ""
        selected_register_name = selected_register.name if selected_register else ""

    cabinet_options = _option_list(
        [("selected", selected_cabinet_name or "Kein Cabinet zugeordnet")],
        selected_value="selected",
    )
    register_options = _option_list(
        [("selected", selected_register_name or "Kein Register zugeordnet")],
        selected_value="selected",
    )
    document_type_options = _option_list(
        [(str(doc_type.id), doc_type.name) for doc_type in document_types],
        selected_value=str(selected_document_type.id) if selected_document_type else None,
    )
    capture_fields = []
    capture_field_inputs = []
    if selected_document_type:
        for field in sorted(selected_document_type.fields, key=lambda item: item.order):
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
    clear_filter_link = f"/ui/app?selected_document_type_id={selected_document_type.id}" if selected_document_type else "/ui/app"
    all_filter_link = f"/ui/app?filter_kind=all&q={quote_plus(search_query or '')}{f'&selected_document_type_id={selected_document_type.id}' if selected_document_type else ''}"
    typed_filter_link = f"/ui/app?filter_kind=typed&q={quote_plus(search_query or '')}{f'&selected_document_type_id={selected_document_type.id}' if selected_document_type else ''}"
    untyped_filter_link = f"/ui/app?filter_kind=untyped&q={quote_plus(search_query or '')}{f'&selected_document_type_id={selected_document_type.id}' if selected_document_type else ''}"
    recent_filter_link = f"/ui/app?filter_kind=recent&q={quote_plus(search_query or '')}{f'&selected_document_type_id={selected_document_type.id}' if selected_document_type else ''}"

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
    .page {{ max-width: 1760px; margin: 0 auto; padding: 16px 18px; }}
    .hero {{ display:grid; grid-template-columns: minmax(0, 1.2fr) minmax(320px, 0.8fr); gap:24px; align-items:stretch; margin-bottom:24px; }}
    .hero-card, .hero-status {{ position:relative; overflow:hidden; }}
    .hero-card::before, .hero-status::before {{ content:""; position:absolute; inset:0; background: linear-gradient(135deg, rgba(79,140,255,0.14), rgba(77,212,255,0.06) 45%, transparent 75%); pointer-events:none; }}
    .hero-brand {{ display:flex; gap:16px; align-items:center; margin-bottom:16px; position:relative; z-index:1; }}
    .brand-mark {{ width:56px; height:56px; border-radius:18px; display:grid; place-items:center; background: linear-gradient(135deg, rgba(79,140,255,0.28), rgba(77,212,255,0.18)); border:1px solid rgba(77,212,255,0.28); box-shadow: 0 0 0 1px rgba(255,255,255,0.02) inset; }}
    .brand-mark svg {{ width:34px; height:34px; }}
    .eyebrow {{ letter-spacing:.12em; text-transform:uppercase; font-size:.78rem; color: var(--accent-2); font-weight:700; }}
    .hero-title {{ margin: 0; font-size: 2.55rem; line-height: 1.01; letter-spacing: -0.03em; }}
    .hero-subtitle {{ margin: 12px 0 0; color: var(--muted); max-width: 70ch; line-height: 1.68; font-size: 1.02rem; }}
    .flow-lanes {{ display:grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap:12px; margin-top:20px; position:relative; z-index:1; }}
    .flow-lane {{ padding:14px 16px; border-radius:18px; background: rgba(255,255,255,0.03); border:1px solid rgba(77,212,255,0.12); box-shadow: 0 10px 30px rgba(0,0,0,0.12); }}
    .flow-lane strong {{ display:block; margin-bottom:4px; }}
    .pillbar {{ display:flex; gap:12px; flex-wrap:wrap; margin-top:18px; position:relative; z-index:1; }}
    .pill {{ background: rgba(255,255,255,0.04); border:1px solid rgba(77,212,255,0.12); border-radius:999px; padding:9px 14px; color:var(--text); transition: all .18s ease; }}
    .pill:hover {{ border-color: rgba(77,212,255,0.4); box-shadow: 0 0 0 4px var(--glow); }}
    .hero-cta-strip {{ display:grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap:12px; margin-top:18px; position:relative; z-index:1; }}
    .cta-card {{ display:flex; align-items:center; justify-content:space-between; gap:12px; padding:14px 16px; border-radius:18px; border:1px solid rgba(77,212,255,0.14); background:linear-gradient(180deg, rgba(255,255,255,0.05), rgba(255,255,255,0.02)); color:var(--text); }}
    .cta-card:hover {{ text-decoration:none; border-color:rgba(77,212,255,.40); box-shadow:0 0 0 4px var(--glow); transform:translateY(-1px); }}
    .cta-card strong {{ display:block; margin-bottom:4px; }}
    .cta-card span {{ color:var(--muted); font-size:.92rem; line-height:1.35; }}
    .cta-icon {{ flex:0 0 auto; width:34px; height:34px; border-radius:12px; display:grid; place-items:center; background:rgba(77,212,255,0.10); color:var(--accent-2); font-size:1rem; font-weight:700; }}
    .panel {{ background: linear-gradient(180deg, rgba(18,25,51,0.96), rgba(15,22,48,0.96)); border: 1px solid rgba(77,212,255,0.10); border-radius: 20px; padding: 22px; margin-bottom: 20px; box-shadow: var(--shadow); backdrop-filter: blur(10px); }}
    .hero-status h3, .panel h2 {{ margin-top:0; }}
    .status-stack {{ display:grid; gap:12px; position:relative; z-index:1; }}
    .status-chip {{ display:flex; align-items:center; justify-content:space-between; gap:12px; padding:12px 14px; border-radius:16px; background: rgba(255,255,255,0.03); border:1px solid rgba(77,212,255,0.10); }}
    .status-dot {{ width:10px; height:10px; border-radius:999px; background: var(--success); box-shadow: 0 0 12px rgba(110,231,183,0.5); }}
    .banner {{ border-radius:18px; padding:14px 16px; margin-bottom:20px; border:1px solid rgba(77,212,255,0.16); }} .success-banner {{ background:rgba(110,231,183,.10); border-color:rgba(110,231,183,.26); color:#d9ffec; box-shadow:0 0 0 4px rgba(110,231,183,0.10); }} .error-banner {{ background:rgba(255,120,120,.12); border-color:rgba(255,120,120,.28); color:#ffd0d0; }}
    .success-actions {{ display:flex; flex-wrap:wrap; gap:10px; margin-top:12px; }}
    .main-grid {{ display:grid; grid-template-columns: 1.15fr 0.85fr; gap:20px; align-items:start; }}
    .workspace-grid {{ display:grid; grid-template-columns: 280px minmax(0, 1fr) 300px; gap:16px; align-items:start; }}
    .field-grid {{ display:grid; grid-template-columns: repeat(2, minmax(0,1fr)); gap:12px; }}
    .field {{ display:grid; gap:6px; }} .field.full {{ grid-column: 1 / -1; }} .field.error label {{ color:#ff8f8f; }} .field.error input, .field.error textarea, .field.error select, .field.error .checkbox-group {{ border-color:#ff8f8f; box-shadow:0 0 0 1px rgba(255,143,143,.25) inset; }} .checkbox-group {{ display:grid; gap:10px; padding:14px; border:1px solid rgba(77,212,255,0.08); border-radius:16px; background:var(--panel-deep); }} .checkbox-item {{ display:flex; gap:10px; align-items:center; font-weight:400; padding:8px 10px; border-radius:12px; background:rgba(255,255,255,0.02); }} .checkbox-item input {{ width:auto; }} .toggle {{ display:inline-flex; align-items:center; gap:12px; cursor:pointer; }} .toggle input[type="hidden"] {{ display:none; }} .toggle input[type="checkbox"] {{ display:none; }} .toggle-slider {{ position:relative; width:52px; height:30px; background:#33406b; border-radius:999px; transition:background .2s ease; }} .toggle-slider::after {{ content:""; position:absolute; top:3px; left:3px; width:24px; height:24px; background:white; border-radius:50%; transition:transform .2s ease; }} .toggle input[type="checkbox"]:checked + .toggle-slider {{ background:var(--accent); }} .toggle input[type="checkbox"]:checked + .toggle-slider::after {{ transform:translateX(22px); }} .toggle-label {{ color:var(--muted); font-weight:500; }} .field-help {{ font-size:.9rem; }} .field-error {{ color:#ff8f8f; font-size:.92rem; font-weight:600; }} .preview-item {{ padding:10px 12px; border:1px solid rgba(77,212,255,0.08); border-radius:12px; background:rgba(255,255,255,0.03); margin-bottom:8px; }} .required-badge {{ display:inline-block; margin-left:8px; padding:2px 8px; border-radius:999px; font-size:.78rem; background:rgba(77,212,255,.16); color:var(--accent-2); vertical-align:middle; }}
    label {{ font-weight:600; font-size:0.95rem; }}
    input, textarea, select {{ width:100%; border-radius:14px; border:1px solid rgba(77,212,255,0.10); background:var(--panel-deep); color:var(--text); padding:12px; font:inherit; }}
    input:focus, textarea:focus, select:focus {{ outline:none; border-color: rgba(77,212,255,0.46); box-shadow: 0 0 0 4px var(--glow); }}
    textarea {{ min-height: 110px; resize: vertical; }}
    .dropzone {{ border:2px dashed rgba(77,212,255,0.16); border-radius:20px; padding:28px; text-align:center; background:linear-gradient(180deg, rgba(255,255,255,0.03), rgba(255,255,255,0.02)); }}
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
    .quick-create-forms {{ display:grid; gap:16px; }}
    .quick-create-form {{ transition:opacity .18s ease, transform .18s ease; }}
    .quick-create-form.is-muted {{ opacity:.4; transform:scale(.995); }}
    .accordion {{ margin-top:16px; border:1px solid rgba(77,212,255,0.12); border-radius:18px; background:rgba(255,255,255,0.02); overflow:hidden; }}
    .accordion summary {{ list-style:none; cursor:pointer; display:flex; align-items:center; justify-content:space-between; gap:12px; padding:14px 16px; font-weight:700; }}
    .accordion summary::-webkit-details-marker {{ display:none; }}
    .accordion summary::after {{ content:'+'; color:var(--accent-2); font-size:1.1rem; line-height:1; }}
    .accordion[open] summary::after {{ content:'–'; }}
    .accordion-body {{ padding:0 16px 16px; display:grid; gap:16px; }}
    .service-card::before {{ content:""; position:absolute; inset:0; background: linear-gradient(135deg, rgba(79,140,255,0.10), rgba(77,212,255,0.03) 55%, transparent 80%); pointer-events:none; }}
    .service-header {{ display:flex; justify-content:space-between; gap:12px; align-items:flex-start; position:relative; z-index:1; }}
    .service-badge {{ display:inline-flex; align-items:center; gap:8px; padding:6px 10px; border-radius:999px; background:rgba(110,231,183,0.10); border:1px solid rgba(110,231,183,0.18); color:#d6fff0; font-size:.85rem; }}
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
        <p class="hero-subtitle">Ein moderner ECM-Arbeitsraum für Aufnahme, Vorschau, Klassifikation und Wiederfinden. Archiva verbindet Struktur mit Bewegung, nicht nur Ablage mit Formularen.</p>
        <div class="flow-lanes">
          <div class="flow-lane"><strong>Intake</strong><div class="muted">Dokumente aufnehmen, typisieren und mit Metadaten anreichern.</div></div>
          <div class="flow-lane"><strong>Preview</strong><div class="muted">Asynchrone Vorschau über Queue, Worker und Artefakte.</div></div>
          <div class="flow-lane"><strong>Retrieve</strong><div class="muted">Objekte suchen, filtern und direkt weiterbearbeiten.</div></div>
        </div>
        <form method="get" action="/ui/app" class="overview-toolbar" id="search-form" style="margin-top:18px; position:relative; z-index:1;">
          {f'<input type="hidden" name="node_kind" value="{_escape(selected_node["kind"])}">' if selected_node else ''}
          {f'<input type="hidden" name="node_id" value="{_escape(selected_node["id"])}">' if selected_node else ''}
          {f'<input type="hidden" name="selected_document_type_id" value="{_escape(str(selected_document_type.id))}">' if selected_document_type else ''}
          <div class="search-row">
            <input type="search" name="q" value="{_escape(search_query)}" placeholder="Volltextsuche in Archiva, z. B. Titel, Metadaten, Tags, Typen">
            <button class="primary" type="submit">Suchen</button>
          </div>
        </form>
        <div class="hero-cta-strip">
          <a class="cta-card" href="#intake-form">
            <div><strong>Neu erfassen</strong><span>Dokument hochladen und direkt typisieren</span></div>
            <div class="cta-icon">+</div>
          </a>
          <a class="cta-card" href="#quick-create">
            <div><strong>Struktur anlegen</strong><span>Cabinet oder Register ohne Admin-Umweg</span></div>
            <div class="cta-icon">▦</div>
          </a>
          <a class="cta-card" href="#filters">
            <div><strong>Schnell finden</strong><span>Suche, Filter und letzte Objekte öffnen</span></div>
            <div class="cta-icon">⌕</div>
          </a>
        </div>
        <div class="pillbar"><a class="pill" href="/ui/app">Objektübersicht</a><a class="pill" href="#search-form">Volltextsuche</a><a class="pill" href="#filters">Schnellfilter</a><a class="pill" href="#intake-form">Upload & Intake</a></div>
      </div>
      <div class="panel hero-status">
        <h3>System</h3>
        <div class="status-stack">
          <div class="status-chip"><span>Preview</span><span class="service-badge"><span class="status-dot"></span> async</span></div>
          <div class="status-chip"><span>Suche</span><span class="service-badge"><span class="status-dot"></span> bereit</span></div>
        </div>
      </div>
    </section>
    {message_html}
    <section class="panel">
      <div class="section-head">
        <div>
          <h2 style="margin:0;">Objekte</h2>
          <div class="muted">Sofort sichtbare Übersicht, Suche ohne Extraklicks, leichte Filter und zuletzt genutzte Einträge.</div>
        </div>
        <div class="actions" style="margin-top:0;">
          <a class="chip" href="#intake-form">Neues Dokument</a>
          <a class="chip" href="#quick-create">Cabinet / Register</a>
          <a class="chip" href="#recent-documents">Zuletzt erfasste</a>
          <a class="chip" href="/ui/admin">Zur Admin-Oberfläche</a>
        </div>
      </div>
      <div class="workspace-grid">
        <div class="panel" style="margin-bottom:0;">
          <h2 style="margin-top:0;">Archivbaum</h2>
          <p class="muted">Strukturansicht von Cabinettypen, Cabinets, Registern und Dokumenttypen. Über das Kontextmenü kannst du direkt neue Elemente anlegen.</p>
          {archive_tree_html}
          <div id="tree-context-menu" class="context-menu" aria-hidden="true"></div>
        </div>
        <div style="display:block;">
          {node_header_html}
          {node_results_html}
          <div style="margin-top:20px;">{object_summary_html}</div>
          {object_overview_html}
        </div>
        <div>
          {_render_context_panel(selected_node, cabinets)}
        </div>
      </div>
    </section>
    <section class="main-grid">
      <div>
        <div class="panel" id="intake-form">
          <h2>Dokument aufnehmen</h2>
          <p class="muted">Starte einen neuen Content-Flow. Erst Datei aufnehmen, dann Typ zuordnen und Felder sauber erfassen.</p>
          <div class="dropzone" id="file-dropzone"><strong>Datei per Drag & Drop</strong><p class="muted" id="dropzone-hint">oder Datei auswählen und einem Dokumenttyp zuordnen.</p></div>
          <form method="post" action="/ui/app/intake" enctype="multipart/form-data">
            <div class="field-grid" style="margin-top:16px;">
              <div class="field full"><label>Datei</label><input id="file-input" type="file" name="file" required></div>
              <div class="field"><label>Cabinet</label><select disabled>{cabinet_options}</select></div>
              <div class="field"><label>Register</label><select disabled>{register_options}</select></div>
              <div class="field full"><label>Dokumenttyp</label><select id="document-type-select" name="document_type_id" required>{document_type_options}</select></div>
              {capture_fields_html}
              <div class="field full"><label>Aktuelle Felddefinition</label><div class="dropzone" style="text-align:left;">{capture_preview}</div></div>
            </div>
            <div class="actions"><button class="primary" type="submit">Dokument speichern</button></div>
          </form>
        </div>
        <div class="panel" id="recent-documents"><h2>Zuletzt erfasste Dokumente</h2>{recent_documents_html}</div>
      </div>
      <div>
        <div class="panel"><h2>Recent / Favoriten</h2>{recent_favorites_html}</div>
        <div class="panel service-card" id="filters">
          <div class="service-header"><div><h2>Schnellzugriffe</h2><p class="muted">Direkte Wege in die wichtigsten Objektansichten.</p></div><span class="service-badge">Navigator</span></div>
          <div class="actions">
            <a class="chip" href="{all_filter_link}">Alle Objekte</a>
            <a class="chip" href="{typed_filter_link}">Mit Typ</a>
            <a class="chip" href="{untyped_filter_link}">Ohne Typ</a>
            <a class="chip" href="{recent_filter_link}">Zuletzt verwendet</a>
            <a class="chip" href="{clear_filter_link}">Filter zurücksetzen</a>
          </div>
        </div>
        <div class="panel service-card"><div class="service-header"><div><h2>Preview-Service</h2><p class="muted">Asynchroner Preview-Dienst mit Queue, Worker und Artefakten.</p></div><span class="service-badge"><span class="status-dot"></span> Flow aktiv</span></div><div class="actions"><a class="chip" href="/ui/workflows">Workflows ansehen</a></div></div>
        <div class="panel service-card"><div class="service-header"><div><h2>Indexing-Service</h2><p class="muted">Für OCR, Text-Extraktion und Volltextindex vorgesehen.</p></div><span class="service-badge">geplant</span></div><div class="actions"><a class="chip" href="/ui/admin">Dokumenttypen prüfen</a></div></div>
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
        treeContextMenu.innerHTML = actions.map((action) => `<button type="button" data-action="${{action.action}}" data-kind="${{action.kind || ''}}" data-id="${{action.id || ''}}" data-label="${{action.label || ''}}">${{action.title}}</button>`).join('');
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
        if (action === 'new-cabinet') openQuickCreate('cabinet', kind, id, label);
        if (action === 'new-register') openQuickCreate('register', kind, id, label);
        if (action === 'new-document') window.location.hash = 'intake-form';
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

    if (fileDropzone && fileInput) {{
      const setFile = (fileList) => {{
        if (!fileList || !fileList.length) return;
        fileInput.files = fileList;
        if (dropzoneHint) {{
          dropzoneHint.textContent = `Ausgewählt: ${'{'}fileList[0].name{'}'}`;
        }}
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
    if document.document_type:
        field_html_parts: list[str] = []
        for field in sorted(document.document_type.fields, key=lambda item: item.order):
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
    .page {{ max-width: 1320px; margin: 0 auto; padding: 30px; }}
    .panel {{ background: linear-gradient(180deg, rgba(18,25,51,0.96), rgba(15,22,48,0.96)); border:1px solid rgba(77,212,255,0.10); border-radius:20px; padding:20px; margin-bottom:20px; box-shadow:var(--shadow); }}
    .success-banner {{ background:rgba(110,231,183,.10); border-color:rgba(110,231,183,.26); color:#d9ffec; }}
    .error-banner {{ background:rgba(255,120,120,.12); border-color:rgba(255,120,120,.28); color:#ffd0d0; }}
    .hero {{ display:grid; grid-template-columns: minmax(0, 1.1fr) minmax(300px, 0.9fr); gap:20px; align-items:stretch; margin-bottom:20px; }}
    .hero-main {{ position:relative; overflow:hidden; }}
    .hero-main::before {{ content:""; position:absolute; inset:0; background: linear-gradient(135deg, rgba(79,140,255,0.12), rgba(77,212,255,0.05) 45%, transparent 80%); pointer-events:none; }}
    .hero-brand {{ display:flex; gap:14px; align-items:center; position:relative; z-index:1; }}
    .brand-mark {{ width:52px; height:52px; border-radius:18px; display:grid; place-items:center; background: linear-gradient(135deg, rgba(79,140,255,0.28), rgba(77,212,255,0.18)); border:1px solid rgba(77,212,255,0.25); }}
    .brand-mark svg {{ width:32px; height:32px; }}
    .eyebrow {{ letter-spacing:.12em; text-transform:uppercase; font-size:.78rem; color:var(--accent-2); font-weight:700; }}
    .pillbar {{ display:flex; flex-wrap:wrap; gap:10px; margin-top:12px; position:relative; z-index:1; }}
    .pill {{ background: rgba(255,255,255,0.04); border:1px solid rgba(77,212,255,0.10); border-radius:999px; padding:8px 12px; color:var(--text); }}
    .actions {{ display:flex; gap:12px; flex-wrap:wrap; margin-top:16px; position:relative; z-index:1; }}
    .button {{ display:inline-flex; align-items:center; border-radius:999px; padding:12px 16px; border:1px solid rgba(77,212,255,0.10); background:rgba(255,255,255,0.03); color:var(--text); }}
    .button.primary {{ background: linear-gradient(135deg, var(--accent), var(--accent-2)); border:none; color:white; box-shadow: 0 8px 24px rgba(77,212,255,0.22); }}
    .detail-grid {{ display:grid; grid-template-columns: 1.1fr 0.9fr; gap:20px; }}
    .detail-row {{ display:grid; grid-template-columns: 220px 1fr; gap:12px; padding:12px 0; border-bottom:1px solid rgba(255,255,255,.06); }}
    .detail-key {{ color:var(--muted); font-weight:600; }}
    .detail-value {{ color:var(--text); word-break:break-word; }}
    .field-grid {{ display:grid; grid-template-columns: repeat(2, minmax(0,1fr)); gap:12px; }}
    .field {{ display:grid; gap:6px; }}
    .field.full {{ grid-column: 1 / -1; }}
    .field.error label {{ color:#ff8f8f; }}
    .field.error input, .field.error textarea, .field.error select, .field.error .checkbox-group {{ border-color:#ff8f8f; box-shadow:0 0 0 1px rgba(255,143,143,.25) inset; }}
    label {{ font-weight:600; font-size:0.95rem; }}
    input, textarea, select {{ width:100%; border-radius:14px; border:1px solid rgba(77,212,255,0.10); background:var(--panel-deep); color:var(--text); padding:12px; font:inherit; }}
    textarea {{ min-height:110px; resize:vertical; }}
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
    .preview-shell {{ border:1px solid rgba(77,212,255,.10); border-radius:18px; overflow:hidden; background:#09101f; }}
    .preview-frame {{ width:100%; min-height:720px; border:0; background:#0a1224; }}
    .preview-image-wrap {{ display:flex; justify-content:center; align-items:center; padding:16px; background:#09101f; min-height:420px; }}
    .preview-image {{ max-width:100%; max-height:75vh; border-radius:12px; box-shadow:0 12px 36px rgba(0,0,0,.25); }}
    .preview-placeholder {{ padding:20px; border:1px dashed rgba(77,212,255,.18); border-radius:14px; background:rgba(255,255,255,.03); }}
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
            <h1 style="margin:6px 0 8px;">{_escape(document.title or document.name)}</h1>
            <div class="muted">Dateiname: {_escape(document.name)}</div>
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
          <a class="button" href="/ui/app">Zurück zur Übersicht</a>
        </div>
      </div>
      <div class="panel">
        <div style="display:flex; justify-content:space-between; gap:12px; align-items:flex-start;">
          <div>
            <h3 style="margin-top:0;">Dokumentinfo</h3>
            <p class="muted">Kontext, Herkunft und technische Einordnung dieses Objekts.</p>
          </div>
          <span class="service-badge"><span class="status-dot"></span> aktiv</span>
        </div>
        <div class="detail-row"><div class="detail-key">Erstellt</div><div class="detail-value">{_escape(str(document.created_at))}</div></div>
        <div class="detail-row"><div class="detail-key">Aktualisiert</div><div class="detail-value">{_escape(str(document.updated_at))}</div></div>
        <div class="detail-row"><div class="detail-key">MIME-Type</div><div class="detail-value">{_escape(document.mime_type or 'unbekannt')}</div></div>
        <div class="detail-row"><div class="detail-key">Storage-Pfad</div><div class="detail-value"><pre>{_escape(document.storage_path)}</pre></div></div>
      </div>
    </div>

    <div class="detail-grid">
      <div>
        <div class="panel">
          <h2 style="margin-top:0;">Metadaten</h2>
          {metadata_rows}
        </div>
        <div class="panel">
          <h2 style="margin-top:0;">Metadaten bearbeiten</h2>
          <p class="muted">Pflege die strukturierte Beschreibung dieses Dokuments direkt im Flow.</p>
          <form method="post" action="/ui/app/documents/{document.id}/metadata">
            <div class="field-grid">
              {edit_fields_html}
            </div>
            <div class="actions">
              <button class="button primary" type="submit">Metadaten speichern</button>
            </div>
          </form>
        </div>
      </div>
      <div>
        <div class="panel">
          <h2 style="margin-top:0;">Vorschau</h2>
          {preview_html}
        </div>
        <div class="panel">
          <h2 style="margin-top:0;">Einordnung</h2>
          <div class="detail-row"><div class="detail-key">Dokumenttyp</div><div class="detail-value">{_escape(document_type_label)}</div></div>
          <div class="detail-row"><div class="detail-key">Interner Dateityp</div><div class="detail-value">{_escape(str(document.doc_type))}</div></div>
          <div class="detail-row"><div class="detail-key">Dokument-ID</div><div class="detail-value"><pre>{_escape(str(document.id))}</pre></div></div>
        </div>
        <div class="panel">
          <h2 style="margin-top:0;">Nächste sinnvolle Schritte</h2>
          <p class="muted">Als nächstes bietet sich das Bearbeiten der Metadaten, OCR oder eine erweiterte Mehrseitenvorschau an.</p>
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
        '<div style="margin-top:12px;" class="muted">Die Vorschau wird asynchron über einen separaten Preview-Dienst gerendert.</div>'
        f'<div class="actions"><a class="button" href="{download_link}">Original herunterladen</a></div>'
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


def _render_workflows_page() -> str:
    return """
<!doctype html>
<html lang=\"de\">
<head>
  <meta charset=\"utf-8\">
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">
  <title>Archiva Workflows</title>
  <link rel="icon" type="image/svg+xml" href="/assets/archiva-favicon.svg">
  <style>
    :root { color-scheme: dark; }
    body { font-family: Inter, ui-sans-serif, system-ui, sans-serif; background: radial-gradient(circle at top left, rgba(77,212,255,0.08), transparent 30%), #0b1020; color: #eef2ff; margin: 0; }
    .page { max-width: 1200px; margin: 0 auto; padding: 28px; }
    .panel { background: linear-gradient(180deg, rgba(18,25,51,0.96), rgba(15,22,48,0.96)); border: 1px solid rgba(77,212,255,0.10); border-radius: 20px; padding: 22px; margin-bottom: 20px; box-shadow: 0 18px 48px rgba(0,0,0,0.28); }
    a { color: #4dd4ff; text-decoration: none; }
    .muted { color: #a8b2d1; line-height: 1.6; }
    .eyebrow { letter-spacing:.12em; text-transform:uppercase; font-size:.78rem; color:#4dd4ff; font-weight:700; }
    .pillbar { display:flex; gap:12px; flex-wrap:wrap; margin-top:16px; }
    .pill { background: rgba(255,255,255,0.04); border:1px solid rgba(77,212,255,0.12); border-radius:999px; padding:9px 14px; color:#eef2ff; }
  </style>
</head>
<body>
  <div class=\"page\">
    <div class=\"panel\">
      <div class=\"eyebrow\">Orchestration Layer</div>
      <h1>Archiva Workflows</h1>
      <p class=\"muted\">Diese Fläche ist für Objekt-Workflows vorgesehen. Hier werden später Regeln, Übergänge, Freigaben und prozessuale Schritte auf die gleiche Flow-Archiv-Logik aufsetzen wie Intake, Preview und Retrieve.</p>
      <div class=\"pillbar\">
        <a class=\"pill\" href=\"/ui/app\">Zur ECM-App</a>
        <a class=\"pill\" href=\"/ui/admin\">Zur Admin-Oberfläche</a>
      </div>
    </div>
  </div>
</body>
</html>
"""


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
    return None


def _render_archive_tree(
    cabinets: list[Cabinet],
    selected_node: dict[str, Any] | None,
    search_query: str,
) -> str:
    if not cabinets:
        return "<div class='archive-tree'><div class='tree-node depth-0' data-menu='[{&quot;title&quot;:&quot;Cabinettyp im Admin anlegen&quot;,&quot;action&quot;:&quot;new-cabinet-type&quot;}]'><div class='tree-link'><strong>Leere Struktur</strong></div><div class='tree-actions'><button type='button' class='tree-menu-button'>⋯</button></div></div></div>"

    selected_kind = selected_node.get("kind") if selected_node else None
    selected_id = selected_node.get("id") if selected_node else None

    def node_link(kind: str, node_id: str, label: str, depth: int = 0, menu: list[dict[str, str]] | None = None) -> str:
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

    grouped: dict[tuple[str, str], list[Cabinet]] = {}
    for cabinet in cabinets:
        cabinet_type = getattr(cabinet, 'cabinet_type', None)
        if cabinet_type:
            type_key = (str(cabinet_type.id), cabinet_type.name)
        else:
            type_key = ("", 'Ohne Cabinettyp')
        grouped.setdefault(type_key, []).append(cabinet)

    promoted_bestand = len(grouped) == 1 and next(iter(grouped.keys()))[1].strip().lower() == "bestand"

    chunks: list[str] = ['<div class="archive-tree">']
    chunks.append("<div class='tree-node depth-0' data-menu='[{&quot;title&quot;:&quot;Cabinettyp im Admin anlegen&quot;,&quot;action&quot;:&quot;new-cabinet-type&quot;}]'><div class='tree-link'><strong>Wurzel</strong></div><div class='tree-actions'><button type='button' class='tree-menu-button'>⋯</button></div></div>")
    for (type_id, type_name), typed_cabinets in sorted(grouped.items(), key=lambda item: item[0][1].lower()):
        if not promoted_bestand:
            type_menu = [{"title": "Cabinet anlegen", "action": "new-cabinet", "kind": "cabinet_type", "id": type_id, "label": type_name}]
            href_id = type_id or quote_plus(type_name)
            chunks.append(node_link("cabinet_type", href_id, f"🧩 {type_name}", 0, type_menu))
            cabinet_depth = 1
        else:
            cabinet_depth = 0

        for cabinet in sorted(typed_cabinets, key=lambda item: item.order):
            cabinet_menu = [{"title": "Cabinet anlegen", "action": "new-cabinet", "kind": "cabinet_type", "id": str(cabinet.id), "label": cabinet.name}] if promoted_bestand else [{"title": "Register anlegen", "action": "new-register", "kind": "cabinet", "id": str(cabinet.id), "label": cabinet.name}]
            cabinet_kind = "cabinet_type" if promoted_bestand else "cabinet"
            cabinet_icon = "🧩" if promoted_bestand else "🗂️"
            chunks.append(node_link(cabinet_kind, str(cabinet.id), f"{cabinet_icon} {cabinet.name}", cabinet_depth, cabinet_menu))
            child_depth = cabinet_depth + 1
            for register in sorted(cabinet.registers, key=lambda item: item.order):
                child_kind = "cabinet" if promoted_bestand else "register"
                child_label = f"🗂️ {register.name}" if promoted_bestand else f"📑 {register.name}"
                child_menu = [{"title": "Register anlegen", "action": "new-register", "kind": "cabinet", "id": str(register.id), "label": register.name}] if promoted_bestand else [{"title": "Dokument erfassen", "action": "new-document", "kind": "register", "id": str(register.id), "label": register.name}]
                chunks.append(node_link(child_kind, str(register.id), child_label, child_depth, child_menu))
                for doc_type in sorted(register.document_types, key=lambda item: item.order):
                    chunks.append(node_link("document_type", str(doc_type.id), f"📄 {doc_type.name}", child_depth + 1))
            for doc_type in sorted(cabinet.document_types, key=lambda item: item.order):
                chunks.append(node_link("document_type", str(doc_type.id), f"📄 {doc_type.name}", child_depth))
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
                f"<a class='object-card' href='/ui/app/documents/{document.id}'><strong>🔎 {_escape(document.title or document.name)}</strong><div class='muted'>{_escape(document.document_type.name if document.document_type else 'Ohne Dokumenttyp')}</div></a>"
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

    if selected_kind == "cabinet":
        cabinet = next((cab for cab in cabinets if str(cab.id) == selected_id), None)
        if cabinet:
            subtitle = "Register und direkte Dokumenttypen dieses Cabinets"
            for register in sorted(cabinet.registers, key=lambda item: item.order):
                results.append(f"<div class='object-card'><strong>📑 {_escape(register.name)}</strong><div class='muted'>Register in {_escape(cabinet.name)}</div></div>")
            for doc_type in sorted(cabinet.document_types, key=lambda item: item.order):
                results.append(f"<div class='object-card'><strong>📄 {_escape(doc_type.name)}</strong><div class='muted'>Direkter Dokumenttyp im Cabinet</div></div>")
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
            subtitle = "Dokumenttypen und Dokumente dieses Registers"
            for doc_type in sorted(register.document_types, key=lambda item: item.order):
                results.append(f"<div class='object-card'><strong>📄 {_escape(doc_type.name)}</strong><div class='muted'>Dokumenttyp in {_escape(register.name)}</div></div>")
            matching_documents = [doc for doc in all_documents if doc.document_type and doc.document_type.register_id and str(doc.document_type.register_id) == selected_id]
            for document in matching_documents[:20]:
                results.append(f"<a class='object-card' href='/ui/app/documents/{document.id}'><strong>📄 {_escape(document.title or document.name)}</strong><div class='muted'>{_escape(parent_cabinet.name if parent_cabinet else '')}</div></a>")
    elif selected_kind == "document_type":
        subtitle = "Dokumente dieses Dokumenttyps"
        matching_documents = [doc for doc in all_documents if doc.document_type_id and str(doc.document_type_id) == selected_id]
        for document in matching_documents[:30]:
            results.append(f"<a class='object-card' href='/ui/app/documents/{document.id}'><strong>📄 {_escape(document.title or document.name)}</strong><div class='muted'>{_escape(document.name)}</div></a>")

    if search_query.strip():
        subtitle = (subtitle + " · " if subtitle else "") + f"Suche aktiv: {_escape(search_query)}"

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


def _render_context_panel(selected_node: dict[str, Any] | None, cabinets: list[Cabinet]) -> str:
    selected_kind = selected_node.get("kind") if selected_node else None
    selected_id = selected_node.get("id") if selected_node else None

    selected_cabinet_id = ""
    selected_register_id = ""
    selected_cabinet_label = ""
    selected_register_label = ""
    selected_cabinet_type_id = ""
    selected_cabinet_type_label = ""

    cabinet_type_map: dict[str, str] = {}
    for cabinet in cabinets:
        if cabinet.cabinet_type:
            cabinet_type_map[str(cabinet.cabinet_type.id)] = cabinet.cabinet_type.name

    if selected_kind == "cabinet" and selected_id:
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
    show_register_form = selected_kind == "cabinet"
    cabinet_form_style = "margin-bottom:0; padding:16px;" if show_cabinet_form else "display:none; margin-bottom:0; padding:16px;"
    register_form_style = "margin-bottom:0; padding:16px;" if show_register_form else "display:none; margin-bottom:0; padding:16px;"
    summary_parts: list[str] = []
    if show_cabinet_form:
        summary_parts.append(cabinet_cta_label)
    if show_register_form:
        summary_parts.append(register_cta_label)
    summary_label = " / ".join(summary_parts) if summary_parts else "Schnell anlegen"

    quick_create_html = f"""
    <div class='panel' id='quick-create'>
      <h2 style='margin-top:0;'>Schnell anlegen</h2>
      <p class='muted'>Cabinets und Register sind hier direkt greifbar, ohne Umweg über die tiefe Admin-Fläche.</p>
      <div class='mini-cta-row'>
        {contextual_cta_html}
      </div>
      <input type='hidden' id='quick-create-mode' value=''>
      <input type='hidden' id='quick-create-node-kind' value=''>
      <input type='hidden' id='quick-create-node-id' value=''>
      <div id='quick-create-context-hint' class='context-note'>Kein Kontext gewählt</div>
      <details class='accordion'{accordion_open}>
        <summary>{_escape(summary_label)}</summary>
        <div class='accordion-body'>
          <div class='quick-create-forms'>
          <form method='post' action='/ui/admin/cabinets' class='panel quick-create-form' id='quick-create-cabinet-form' style='{cabinet_form_style}'>
            <input type='hidden' name='return_to' value='app'>
            <h3 style='margin-top:0;'>{_escape(cabinet_cta_label)}</h3>
            <p class='muted' style='margin-top:0;'>{_escape(cabinet_type_hint)}</p>
            <div class='field-grid'>
              <div class='field full'><label>Cabinettyp</label><select name='cabinet_type_id' required>{cabinet_type_options}</select></div>
              <div class='field full'><label>Name</label><input type='text' name='name' placeholder='z. B. 2026' required></div>
              <div class='field full'><label>Beschreibung</label><textarea name='description' placeholder='Kurz beschreiben, wofür dieses Cabinet gedacht ist'></textarea></div>
            </div>
            <div class='actions'><button class='primary' type='submit'>{_escape(cabinet_cta_label)}</button></div>
          </form>
          <form method='post' action='/ui/admin/registers' class='panel quick-create-form' id='quick-create-register-form' style='{register_form_style}'>
            <input type='hidden' name='return_to' value='app'>
            <h3 style='margin-top:0;'>{_escape(register_cta_label)}</h3>
            <p class='muted' style='margin-top:0;'>{_escape(cabinet_hint)}</p>
            <div class='field-grid'>
              <div class='field full'><label>Cabinet</label><select name='cabinet_id' required>{cabinet_options}</select></div>
              <div class='field full'><label>Name</label><input type='text' name='name' placeholder='z. B. Eingangsrechnungen' required></div>
              <div class='field full'><label>Beschreibung</label><textarea name='description' placeholder='Kurz beschreiben, welche Objekte hier landen'></textarea></div>
            </div>
            <div class='actions'><button class='primary' type='submit'>{_escape(register_cta_label)}</button></div>
          </form>
          </div>
        </div>
      </details>
    </div>
    """

    if not selected_node:
        return f"""
        {quick_create_html}
        <div class='panel'>
          <h2 style='margin-top:0;'>Kontext</h2>
          <p class='muted'>Wähle links einen Knoten, um hier Kurzinfos, Status und nächste Aktionen zu sehen.</p>
        </div>
        """

    kind_labels = {
        "cabinet": "Cabinet",
        "register": "Register",
        "document_type": "Dokumenttyp",
    }
    label = selected_node.get("label", "Auswahl")
    kind = kind_labels.get(selected_node.get("kind"), selected_node.get("kind", "Objekt"))
    node_id = selected_node.get("id", "")
    create_hint = "Dieses Cabinet ist ausgewählt, du kannst direkt darunter weitere Register ergänzen." if selected_node.get("kind") == "cabinet" else (
        "Dieses Register ist ausgewählt, die Struktur bleibt hier im Blick und neue Register/Cabinets sind direkt anlegbar." if selected_node.get("kind") == "register" else "Dokumenttyp-Kontext aktiv, Strukturaktionen bleiben trotzdem direkt verfügbar."
    )
    return f"""
    {quick_create_html}
    <div class='panel'>
      <h2 style='margin-top:0;'>Kontext</h2>
      <div class='detail-row'><div class='detail-key'>Auswahl</div><div class='detail-value'>{_escape(label)}</div></div>
      <div class='detail-row'><div class='detail-key'>Art</div><div class='detail-value'>{_escape(kind)}</div></div>
      <div class='detail-row'><div class='detail-key'>ID</div><div class='detail-value'><pre>{_escape(node_id)}</pre></div></div>
      <div class='actions'>
        <a class='chip' href='/ui/app?node_kind={_escape(selected_node.get("kind", ""))}&node_id={_escape(node_id)}' target='_blank' rel='noopener noreferrer'>In neuem Tab ↗</a>
        <a class='chip' href='/ui/admin'>Admin</a>
      </div>
    </div>
    <div class='panel'>
      <h2 style='margin-top:0;'>Nächste Schritte</h2>
      <p class='muted'>{_escape(create_hint)}</p>
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


def _render_admin_create_panel(cabinet_types: list[CabinetType], cabinets: list[Cabinet], selected_document_type: DocumentType | None) -> str:
    cabinet_options = _option_list([(str(cabinet.id), f"{cabinet.cabinet_type.name if cabinet.cabinet_type else 'Ohne Typ'} → {cabinet.name}") for cabinet in cabinets], include_blank="Bitte wählen")
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
    return f"""
      <div class="cols">
        <div class="stack">
          <form method="post" action="/ui/admin/cabinet-types" class="panel" style="margin-bottom:0;"><h3>Cabinettyp anlegen</h3><p class="muted">Definiere die fachliche Klasse von Cabinets, z. B. ERB, Personal oder Verträge.</p><div class="field-grid"><div class="field"><label>Name</label><input type="text" name="name" required></div><div class="field"><label>Reihenfolge</label><input type="number" name="order" value="0"></div><div class="field full"><label>Beschreibung</label><textarea name="description"></textarea></div></div><div class="actions"><button class="primary" type="submit">Cabinettyp speichern</button></div></form>
          <form method="post" action="/ui/admin/cabinets" class="panel" style="margin-bottom:0;"><h3>Cabinet anlegen</h3><p class="muted">Lege ein konkretes Cabinet innerhalb eines Cabinettyps an, z. B. 2025 oder 2026 unter ERB.</p><div class="field-grid"><div class="field"><label>Cabinettyp</label><select name="cabinet_type_id" required>{cabinet_type_options}</select></div><div class="field"><label>Name</label><input type="text" name="name" required></div><div class="field"><label>Reihenfolge</label><input type="number" name="order" value="0"></div><div class="field full"><label>Beschreibung</label><textarea name="description"></textarea></div></div><div class="actions"><button class="primary" type="submit">Cabinet speichern</button></div></form>
          <form method="post" action="/ui/admin/registers" class="panel" style="margin-bottom:0;"><h3>Register anlegen</h3><p class="muted">Ordne Register innerhalb eines konkreten Cabinets ein.</p><div class="field-grid"><div class="field"><label>Cabinet</label><select name="cabinet_id" required>{cabinet_options}</select></div><div class="field"><label>Name</label><input type="text" name="name" required></div><div class="field"><label>Reihenfolge</label><input type="number" name="order" value="0"></div><div class="field full"><label>Beschreibung</label><textarea name="description"></textarea></div></div><div class="actions"><button class="primary" type="submit">Register speichern</button></div></form>
        </div>
        <div class="stack">
          <form method="post" action="/ui/admin/document-types" class="panel" style="margin-bottom:0;"><h3>Dokumenttyp anlegen</h3><p class="muted">Definiere Objekttypen für Intake, Metadaten und spätere Verarbeitung.</p><div class="field-grid"><div class="field"><label>Zieltyp</label><select name="target_kind"><option value="cabinet">Cabinet</option><option value="register">Register</option></select></div><div class="field"><label>Cabinet</label><select name="cabinet_id"><option value="">Bitte wählen</option>{cabinet_type_options}</select></div><div class="field"><label>Register</label><select name="register_id"><option value="">Bitte wählen</option>{register_options}</select></div><div class="field"><label>Name</label><input type="text" name="name" required></div><div class="field"><label>Icon</label><input type="text" name="icon" placeholder="optional"></div><div class="field"><label>Reihenfolge</label><input type="number" name="order" value="0"></div><div class="field full"><label>Beschreibung</label><textarea name="description"></textarea></div></div><div class="actions"><button class="primary" type="submit">Dokumenttyp speichern</button></div></form>
          <form method="post" action="/ui/admin/metadata-fields" class="panel" style="margin-bottom:0;"><h3>Metadatenfeld anlegen</h3><p class="muted">Lege strukturierte Felder für Cabinet, Register oder Dokumenttyp fest.</p><div class="field-grid"><div class="field"><label>Zieltyp</label><select name="target_kind"><option value="cabinet">Cabinet</option><option value="register">Register</option><option value="document_type">Dokumenttyp</option></select></div><div class="field"><label>Cabinet</label><select name="cabinet_id"><option value="">Bitte wählen</option>{cabinet_options}</select></div><div class="field"><label>Register</label><select name="register_id"><option value="">Bitte wählen</option>{register_options}</select></div><div class="field"><label>Dokumenttyp</label><select name="document_type_id"><option value="">Bitte wählen</option>{document_type_field_options}</select></div><div class="field"><label>Name</label><input type="text" name="name" required></div><div class="field"><label>Label</label><input type="text" name="label"></div><div class="field"><label>Feldtyp</label><select name="field_type">{field_type_options}</select></div><div class="field"><label>Breite</label><select name="width">{width_options}</select></div><div class="field"><label>Reihenfolge</label><input type="number" name="order" value="0"></div><div class="field"><label>Placeholder</label><input type="text" name="placeholder"></div><div class="field"><label>Default</label><input type="text" name="default_value"></div><div class="field"><label><input type="checkbox" name="is_required"> Pflichtfeld</label></div><div class="field"><label><input type="checkbox" name="is_unique"> Eindeutig</label></div><div class="field full"><label>Beschreibung</label><textarea name="description"></textarea></div></div><div class="actions"><button class="primary" type="submit">Feld speichern</button></div></form>
        </div>
      </div>
    """
