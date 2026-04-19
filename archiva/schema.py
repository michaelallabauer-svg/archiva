"""Schema definitions for Archiva ECM."""

from datetime import datetime
from enum import Enum
from typing import Any, Optional
from uuid import UUID

from pydantic import BaseModel, Field


# --- Field Types (mirrors models.FieldType) ---


class FieldType(str, Enum):
    """Metadata field types."""

    TEXT = "text"
    NUMBER = "number"
    CURRENCY = "currency"
    DATE = "date"
    DATETIME = "datetime"
    SELECTION = "selection"
    MULTI_SELECTION = "multi_selection"
    BOOLEAN = "boolean"
    LONG_TEXT = "long_text"
    URL = "url"
    EMAIL = "email"
    PHONE = "phone"


class DisplayWidth(str, Enum):
    """Form layout width."""

    FULL = "full"
    HALF = "half"
    THIRD = "third"
    QUARTER = "quarter"


# --- CabinetType (fachlicher Typ) ---

class CabinetTypeBase(BaseModel):
    """Base cabinet type schema."""

    name: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = None
    order: int = Field(default=0)


class CabinetTypeCreate(CabinetTypeBase):
    """Schema for creating a cabinet type."""

    pass


class CabinetTypeUpdate(BaseModel):
    """Schema for updating a cabinet type."""

    name: Optional[str] = Field(None, min_length=1, max_length=255)
    description: Optional[str] = None
    order: Optional[int] = None


class CabinetTypeResponse(CabinetTypeBase):
    """Schema for cabinet type response."""

    id: UUID
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


# --- Cabinet (Schrank) ---

class CabinetBase(BaseModel):
    """Base cabinet schema."""

    name: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = None
    order: int = Field(default=0)


class CabinetCreate(CabinetBase):
    """Schema for creating a cabinet."""

    cabinet_type_id: UUID


class CabinetUpdate(BaseModel):
    """Schema for updating a cabinet."""

    name: Optional[str] = Field(None, min_length=1, max_length=255)
    description: Optional[str] = None
    order: Optional[int] = None
    cabinet_type_id: Optional[UUID] = None


class CabinetResponse(CabinetBase):
    """Schema for cabinet response."""

    id: UUID
    cabinet_type_id: UUID
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class CabinetWithRegisters(CabinetResponse):
    """Cabinet with nested registers."""

    cabinet_type: Optional["CabinetTypeResponse"] = None
    registers: list["RegisterWithDocumentTypes"] = []
    metadata_fields: list["MetadataFieldResponse"] = []


# --- Register (Register) ---

class RegisterBase(BaseModel):
    """Base register schema."""

    name: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = None
    order: int = Field(default=0)


class RegisterCreate(RegisterBase):
    """Schema for creating a register."""

    cabinet_id: UUID


class RegisterUpdate(BaseModel):
    """Schema for updating a register."""

    name: Optional[str] = Field(None, min_length=1, max_length=255)
    description: Optional[str] = None
    order: Optional[int] = None
    cabinet_id: Optional[UUID] = None


class RegisterResponse(RegisterBase):
    """Schema for register response."""

    id: UUID
    cabinet_id: UUID
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class RegisterWithDocumentTypes(RegisterResponse):
    """Register with nested document types."""

    document_types: list["DocumentTypeWithFields"] = []
    metadata_fields: list["MetadataFieldResponse"] = []


# --- DocumentType (Dokumenttyp) ---

class DocumentTypeBase(BaseModel):
    """Base document type schema."""

    name: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = None
    order: int = Field(default=0)
    icon: Optional[str] = None


class DocumentTypeCreate(DocumentTypeBase):
    """Schema for creating a document type."""

    register_id: Optional[UUID] = None
    cabinet_id: Optional[UUID] = None


class DocumentTypeUpdate(BaseModel):
    """Schema for updating a document type."""

    name: Optional[str] = Field(None, min_length=1, max_length=255)
    description: Optional[str] = None
    order: Optional[int] = None
    icon: Optional[str] = None
    register_id: Optional[UUID] = None
    cabinet_id: Optional[UUID] = None


class DocumentTypeResponse(DocumentTypeBase):
    """Schema for document type response."""

    id: UUID
    register_id: Optional[UUID] = None
    cabinet_id: Optional[UUID] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class DocumentTypeWithFields(DocumentTypeResponse):
    """Document type with nested metadata fields."""

    fields: list["MetadataFieldResponse"] = []


# --- MetadataField (Metadatenfeld) ---

class MetadataFieldBase(BaseModel):
    """Base metadata field schema."""

    name: str = Field(..., min_length=1, max_length=255)
    field_type: str = Field(...)
    label: Optional[str] = None
    description: Optional[str] = None
    placeholder: Optional[str] = None
    default_value: Optional[str] = None
    is_required: bool = Field(default=False)
    is_unique: bool = Field(default=False)
    order: int = Field(default=0)
    width: str = Field(default=DisplayWidth.HALF.value)
    config_json: Optional[str] = None
    options: Optional[list[str]] = None
    min_value: Optional[float] = None
    max_value: Optional[float] = None
    min_length: Optional[int] = None
    max_length: Optional[int] = None
    pattern: Optional[str] = None


class MetadataFieldCreate(MetadataFieldBase):
    """Schema for creating a metadata field."""

    document_type_id: Optional[UUID] = None
    cabinet_id: Optional[UUID] = None
    register_id: Optional[UUID] = None


class MetadataFieldUpdate(BaseModel):
    """Schema for updating a metadata field."""

    name: Optional[str] = Field(None, min_length=1, max_length=255)
    field_type: Optional[str] = None
    label: Optional[str] = None
    description: Optional[str] = None
    placeholder: Optional[str] = None
    default_value: Optional[str] = None
    is_required: Optional[bool] = None
    is_unique: Optional[bool] = None
    order: Optional[int] = None
    width: Optional[str] = None
    config_json: Optional[str] = None
    options: Optional[list[str]] = None
    min_value: Optional[float] = None
    max_value: Optional[float] = None
    min_length: Optional[int] = None
    max_length: Optional[int] = None
    pattern: Optional[str] = None


class MetadataFieldResponse(MetadataFieldBase):
    """Schema for metadata field response."""

    id: UUID
    document_type_id: Optional[UUID] = None
    cabinet_id: Optional[UUID] = None
    register_id: Optional[UUID] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


# --- Layout Engine ---

class LayoutRow(BaseModel):
    """A single row in the generated form layout."""

    order: int
    columns: list["LayoutColumn"]


class LayoutColumn(BaseModel):
    """A column in a layout row."""

    field: MetadataFieldResponse
    width: str


class GeneratedLayout(BaseModel):
    """Generated form layout for a document type."""

    document_type_id: UUID
    document_type_name: str
    rows: list[LayoutRow]
    total_fields: int


# --- Capture / Document Intake ---

class DocumentTypeSummary(BaseModel):
    id: UUID
    name: str
    icon: Optional[str] = None
    register_id: UUID

    class Config:
        from_attributes = True


class CapturedDocumentBase(BaseModel):
    title: Optional[str] = None
    author: Optional[str] = None
    description: Optional[str] = None
    tags: Optional[str] = None
    document_type_id: Optional[UUID] = None
    metadata: Optional[dict[str, Any]] = None


class CapturedDocumentResponse(CapturedDocumentBase):
    id: UUID
    name: str
    doc_type: str
    mime_type: Optional[str]
    size_bytes: int
    document_type: Optional[DocumentTypeSummary] = None
    created_at: datetime
    updated_at: datetime


class CaptureDefinitionField(BaseModel):
    id: UUID
    name: str
    label: str
    field_type: str
    description: Optional[str] = None
    placeholder: Optional[str] = None
    default_value: Optional[str] = None
    is_required: bool
    is_unique: bool
    order: int
    width: str
    options: Optional[list[str]] = None
    min_value: Optional[float] = None
    max_value: Optional[float] = None
    min_length: Optional[int] = None
    max_length: Optional[int] = None
    pattern: Optional[str] = None


class CaptureDefinitionResponse(BaseModel):
    document_type: DocumentTypeSummary
    fields: list[CaptureDefinitionField]


class MessageResponse(BaseModel):
    message: str


# Update forward refs
CabinetTypeResponse.model_rebuild()
CabinetWithRegisters.model_rebuild()
RegisterWithDocumentTypes.model_rebuild()
DocumentTypeWithFields.model_rebuild()
LayoutRow.model_rebuild()
