"""FastAPI routes for Archiva ECM."""

import json
from datetime import datetime
from pathlib import Path
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from archiva.config import Settings, load_settings
from archiva.database import get_db
from archiva.layout import generate_layout
from archiva.models import (
    Cabinet,
    Document,
    DocumentType,
    DocumentVersion,
    MetadataField,
    Register,
)
from archiva.schema import (
    CabinetCreate,
    CabinetResponse,
    CabinetUpdate,
    CabinetWithRegisters,
    DocumentTypeCreate,
    DocumentTypeResponse,
    DocumentTypeUpdate,
    DocumentTypeWithFields,
    GeneratedLayout,
    LayoutRow,
    MessageResponse,
    MetadataFieldCreate,
    MetadataFieldResponse,
    MetadataFieldUpdate,
    RegisterCreate,
    RegisterResponse,
    RegisterUpdate,
    RegisterWithDocumentTypes,
)

router = APIRouter(prefix="/api/v1", tags=["admin"])

# Re-export document routes for convenience
from archiva.api_documents import router as documents_router
router.include_router(documents_router)


# =============================================================================
# Cabinet Routes (Schrank)
# =============================================================================

@router.get("/cabinets", response_model=list[CabinetWithRegisters])
async def list_cabinets(db: Session = Depends(get_db)) -> list[CabinetWithRegisters]:
    """List all cabinets with nested registers."""
    cabinets = db.query(Cabinet).order_by(Cabinet.order).all()
    result = []
    for c in cabinets:
        cabinet_dict = {
            "id": c.id,
            "name": c.name,
            "description": c.description,
            "order": c.order,
            "created_at": c.created_at,
            "updated_at": c.updated_at,
            "registers": []
        }
        for r in sorted(c.registers, key=lambda x: x.order):
            reg_dict = {
                "id": r.id,
                "cabinet_id": r.cabinet_id,
                "name": r.name,
                "description": r.description,
                "order": r.order,
                "created_at": r.created_at,
                "updated_at": r.updated_at,
                "document_types": []
            }
            for dt in sorted(r.document_types, key=lambda x: x.order):
                doc_type_dict = {
                    "id": dt.id,
                    "register_id": dt.register_id,
                    "name": dt.name,
                    "description": dt.description,
                    "icon": dt.icon,
                    "order": dt.order,
                    "created_at": dt.created_at,
                    "updated_at": dt.updated_at,
                    "fields": []
                }
                for f in sorted(dt.fields, key=lambda x: x.order):
                    field_dict = _field_to_response(f)
                    doc_type_dict["fields"].append(field_dict)
                reg_dict["document_types"].append(doc_type_dict)
            cabinet_dict["registers"].append(reg_dict)
        result.append(CabinetWithRegisters(**cabinet_dict))
    return result


@router.post("/cabinets", response_model=CabinetResponse)
async def create_cabinet(data: CabinetCreate, db: Session = Depends(get_db)) -> Cabinet:
    """Create a new cabinet."""
    cabinet = Cabinet(name=data.name, description=data.description, order=data.order)
    db.add(cabinet)
    db.commit()
    db.refresh(cabinet)
    return cabinet


@router.get("/cabinets/{cabinet_id}", response_model=CabinetWithRegisters)
async def get_cabinet(cabinet_id: UUID, db: Session = Depends(get_db)) -> CabinetWithRegisters:
    """Get a cabinet with all nested registers and document types."""
    cabinet = db.query(Cabinet).where(Cabinet.id == cabinet_id).first()
    if not cabinet:
        raise HTTPException(status_code=404, detail="Cabinet not found")
    return _cabinet_to_response(cabinet)


@router.put("/cabinets/{cabinet_id}", response_model=CabinetResponse)
async def update_cabinet(cabinet_id: UUID, data: CabinetUpdate, db: Session = Depends(get_db)) -> Cabinet:
    """Update a cabinet."""
    cabinet = db.query(Cabinet).where(Cabinet.id == cabinet_id).first()
    if not cabinet:
        raise HTTPException(status_code=404, detail="Cabinet not found")
    if data.name is not None:
        cabinet.name = data.name
    if data.description is not None:
        cabinet.description = data.description
    if data.order is not None:
        cabinet.order = data.order
    db.commit()
    db.refresh(cabinet)
    return cabinet


@router.delete("/cabinets/{cabinet_id}", response_model=MessageResponse)
async def delete_cabinet(cabinet_id: UUID, db: Session = Depends(get_db)) -> MessageResponse:
    """Delete a cabinet and all its contents."""
    cabinet = db.query(Cabinet).where(Cabinet.id == cabinet_id).first()
    if not cabinet:
        raise HTTPException(status_code=404, detail="Cabinet not found")
    db.delete(cabinet)
    db.commit()
    return MessageResponse(message="Cabinet deleted successfully")


# =============================================================================
# Register Routes (Register)
# =============================================================================

@router.post("/registers", response_model=RegisterResponse)
async def create_register(data: RegisterCreate, db: Session = Depends(get_db)) -> Register:
    """Create a new register in a cabinet."""
    cabinet = db.query(Cabinet).where(Cabinet.id == data.cabinet_id).first()
    if not cabinet:
        raise HTTPException(status_code=404, detail="Cabinet not found")
    register = Register(
        cabinet_id=data.cabinet_id,
        name=data.name,
        description=data.description,
        order=data.order
    )
    db.add(register)
    db.commit()
    db.refresh(register)
    return register


@router.get("/registers/{register_id}", response_model=RegisterWithDocumentTypes)
async def get_register(register_id: UUID, db: Session = Depends(get_db)) -> RegisterWithDocumentTypes:
    """Get a register with all nested document types."""
    register = db.query(Register).where(Register.id == register_id).first()
    if not register:
        raise HTTPException(status_code=404, detail="Register not found")
    return _register_to_response(register)


@router.put("/registers/{register_id}", response_model=RegisterResponse)
async def update_register(register_id: UUID, data: RegisterUpdate, db: Session = Depends(get_db)) -> Register:
    """Update a register."""
    register = db.query(Register).where(Register.id == register_id).first()
    if not register:
        raise HTTPException(status_code=404, detail="Register not found")
    if data.name is not None:
        register.name = data.name
    if data.description is not None:
        register.description = data.description
    if data.order is not None:
        register.order = data.order
    if data.cabinet_id is not None:
        register.cabinet_id = data.cabinet_id
    db.commit()
    db.refresh(register)
    return register


@router.delete("/registers/{register_id}", response_model=MessageResponse)
async def delete_register(register_id: UUID, db: Session = Depends(get_db)) -> MessageResponse:
    """Delete a register and all its contents."""
    register = db.query(Register).where(Register.id == register_id).first()
    if not register:
        raise HTTPException(status_code=404, detail="Register not found")
    db.delete(register)
    db.commit()
    return MessageResponse(message="Register deleted successfully")


# =============================================================================
# DocumentType Routes (Dokumenttyp)
# =============================================================================

@router.post("/document-types", response_model=DocumentTypeResponse)
async def create_document_type(data: DocumentTypeCreate, db: Session = Depends(get_db)) -> DocumentType:
    """Create a new document type in a register."""
    register = db.query(Register).where(Register.id == data.register_id).first()
    if not register:
        raise HTTPException(status_code=404, detail="Register not found")
    doc_type = DocumentType(
        register_id=data.register_id,
        name=data.name,
        description=data.description,
        icon=data.icon,
        order=data.order
    )
    db.add(doc_type)
    db.commit()
    db.refresh(doc_type)
    return doc_type


@router.get("/document-types/{document_type_id}", response_model=DocumentTypeWithFields)
async def get_document_type(document_type_id: UUID, db: Session = Depends(get_db)) -> DocumentTypeWithFields:
    """Get a document type with all its metadata fields."""
    doc_type = db.query(DocumentType).where(DocumentType.id == document_type_id).first()
    if not doc_type:
        raise HTTPException(status_code=404, detail="Document type not found")
    return _document_type_to_response(doc_type)


@router.get("/document-types/{document_type_id}/layout", response_model=GeneratedLayout)
async def get_document_type_layout(
    document_type_id: UUID,
    db: Session = Depends(get_db)
) -> GeneratedLayout:
    """Get the auto-generated form layout for a document type."""
    doc_type = db.query(DocumentType).where(DocumentType.id == document_type_id).first()
    if not doc_type:
        raise HTTPException(status_code=404, detail="Document type not found")
    
    fields = [_field_to_response(f) for f in sorted(doc_type.fields, key=lambda x: x.order)]
    layout = generate_layout(fields, str(doc_type.id), doc_type.name)
    return layout


@router.put("/document-types/{document_type_id}", response_model=DocumentTypeResponse)
async def update_document_type(
    document_type_id: UUID,
    data: DocumentTypeUpdate,
    db: Session = Depends(get_db)
) -> DocumentType:
    """Update a document type."""
    doc_type = db.query(DocumentType).where(DocumentType.id == document_type_id).first()
    if not doc_type:
        raise HTTPException(status_code=404, detail="Document type not found")
    if data.name is not None:
        doc_type.name = data.name
    if data.description is not None:
        doc_type.description = data.description
    if data.icon is not None:
        doc_type.icon = data.icon
    if data.order is not None:
        doc_type.order = data.order
    if data.register_id is not None:
        doc_type.register_id = data.register_id
    db.commit()
    db.refresh(doc_type)
    return doc_type


@router.delete("/document-types/{document_type_id}", response_model=MessageResponse)
async def delete_document_type(document_type_id: UUID, db: Session = Depends(get_db)) -> MessageResponse:
    """Delete a document type and all its fields."""
    doc_type = db.query(DocumentType).where(DocumentType.id == document_type_id).first()
    if not doc_type:
        raise HTTPException(status_code=404, detail="Document type not found")
    db.delete(doc_type)
    db.commit()
    return MessageResponse(message="Document type deleted successfully")


# =============================================================================
# MetadataField Routes (Metadatenfeld)
# =============================================================================

@router.post("/metadata-fields", response_model=MetadataFieldResponse)
async def create_metadata_field(data: MetadataFieldCreate, db: Session = Depends(get_db)) -> MetadataField:
    """Create a new metadata field for a document type."""
    doc_type = db.query(DocumentType).where(DocumentType.id == data.document_type_id).first()
    if not doc_type:
        raise HTTPException(status_code=404, detail="Document type not found")
    
    options_json = json.dumps(data.options) if data.options else None
    
    field = MetadataField(
        document_type_id=data.document_type_id,
        name=data.name,
        field_type=data.field_type,
        label=data.label or data.name,
        description=data.description,
        placeholder=data.placeholder,
        default_value=data.default_value,
        is_required=data.is_required,
        is_unique=data.is_unique,
        order=data.order,
        width=data.width.value if data.width else "half",
        config_json=data.config_json,
        options=options_json,
        min_value=data.min_value,
        max_value=data.max_value,
        min_length=data.min_length,
        max_length=data.max_length,
        pattern=data.pattern,
    )
    db.add(field)
    db.commit()
    db.refresh(field)
    return field


@router.get("/metadata-fields/{field_id}", response_model=MetadataFieldResponse)
async def get_metadata_field(field_id: UUID, db: Session = Depends(get_db)) -> MetadataField:
    """Get a metadata field by ID."""
    field = db.query(MetadataField).where(MetadataField.id == field_id).first()
    if not field:
        raise HTTPException(status_code=404, detail="Metadata field not found")
    return _field_to_response(field)


@router.put("/metadata-fields/{field_id}", response_model=MetadataFieldResponse)
async def update_metadata_field(
    field_id: UUID,
    data: MetadataFieldUpdate,
    db: Session = Depends(get_db)
) -> MetadataField:
    """Update a metadata field."""
    field = db.query(MetadataField).where(MetadataField.id == field_id).first()
    if not field:
        raise HTTPException(status_code=404, detail="Metadata field not found")
    
    update_data = data.model_dump(exclude_unset=True)
    
    if "width" in update_data and update_data["width"]:
        update_data["width"] = update_data["width"].value
    if "options" in update_data and update_data["options"]:
        update_data["options"] = json.dumps(update_data["options"])
    
    for key, value in update_data.items():
        setattr(field, key, value)
    
    db.commit()
    db.refresh(field)
    return _field_to_response(field)


@router.delete("/metadata-fields/{field_id}", response_model=MessageResponse)
async def delete_metadata_field(field_id: UUID, db: Session = Depends(get_db)) -> MessageResponse:
    """Delete a metadata field."""
    field = db.query(MetadataField).where(MetadataField.id == field_id).first()
    if not field:
        raise HTTPException(status_code=404, detail="Metadata field not found")
    db.delete(field)
    db.commit()
    return MessageResponse(message="Metadata field deleted successfully")


# =============================================================================
# Helpers
# =============================================================================

def _field_to_response(field: MetadataField) -> MetadataFieldResponse:
    """Convert a MetadataField model to response schema."""
    options = None
    if field.options:
        try:
            options = json.loads(field.options)
        except json.JSONDecodeError:
            options = None
    
    return MetadataFieldResponse(
        id=field.id,
        document_type_id=field.document_type_id,
        name=field.name,
        field_type=field.field_type,
        label=field.label,
        description=field.description,
        placeholder=field.placeholder,
        default_value=field.default_value,
        is_required=field.is_required,
        is_unique=field.is_unique,
        order=field.order,
        width=field.width,
        config_json=field.config_json,
        options=options,
        min_value=field.min_value,
        max_value=field.max_value,
        min_length=field.min_length,
        max_length=field.max_length,
        pattern=field.pattern,
        created_at=field.created_at,
        updated_at=field.updated_at,
    )


def _register_to_response(register: Register) -> RegisterWithDocumentTypes:
    """Convert a Register model to response schema with nested document types."""
    return RegisterWithDocumentTypes(
        id=register.id,
        cabinet_id=register.cabinet_id,
        name=register.name,
        description=register.description,
        order=register.order,
        created_at=register.created_at,
        updated_at=register.updated_at,
        document_types=[
            _document_type_to_response(dt) for dt in sorted(register.document_types, key=lambda x: x.order)
        ]
    )


def _document_type_to_response(doc_type: DocumentType) -> DocumentTypeWithFields:
    """Convert a DocumentType model to response schema with nested fields."""
    return DocumentTypeWithFields(
        id=doc_type.id,
        register_id=doc_type.register_id,
        name=doc_type.name,
        description=doc_type.description,
        icon=doc_type.icon,
        order=doc_type.order,
        created_at=doc_type.created_at,
        updated_at=doc_type.updated_at,
        fields=[_field_to_response(f) for f in sorted(doc_type.fields, key=lambda x: x.order)]
    )


def _cabinet_to_response(cabinet: Cabinet) -> CabinetWithRegisters:
    """Convert a Cabinet model to response schema with nested registers."""
    return CabinetWithRegisters(
        id=cabinet.id,
        name=cabinet.name,
        description=cabinet.description,
        order=cabinet.order,
        created_at=cabinet.created_at,
        updated_at=cabinet.updated_at,
        registers=[
            _register_to_response(r) for r in sorted(cabinet.registers, key=lambda x: x.order)
        ]
    )
