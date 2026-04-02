"""Schema definitions for Archiva ECM."""

from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, Field

from archiva.models import DocumentType


# --- Enums ---

class FieldType(str):
    """Metadata field types."""

    TEXT = "text"  # Single line
    NUMBER = "number"
    CURRENCY = "currency"
    DATE = "date"
    DATETIME = "datetime"
    SELECTION = "selection"  # Dropdown single choice
    MULTI_SELECTION = "multi_selection"  # Multiple choices
    BOOLEAN = "boolean"  # Yes/No
    LONG_TEXT = "long_text"  # Multi-line
    URL = "url"
    EMAIL = "email"
    PHONE = "phone"


class DisplayWidth(str):
    """Form layout width."""

    FULL = "full"  # 100%
    HALF = "half"  # 50%
    THIRD = "third"  # 33%
    QUARTER = "quarter"  # 25%


class FieldDisplayConfig(BaseModel):
    """Display configuration for a field."""

    width: DisplayWidth = DisplayWidth.HALF
    order: int = 0
    show_label: bool = True
    placeholder: Optional[str] = None


# --- Cabinet (Schrank) ---

class CabinetBase(BaseModel):
    """Base cabinet schema."""

    name: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = None
    order: int = Field(default=0)


class CabinetCreate(CabinetBase):
    """Schema for creating a cabinet."""

    pass


class CabinetUpdate(BaseModel):
    """Schema for updating a cabinet."""

    name: Optional[str] = Field(None, min_length=1, max_length=255)
    description: Optional[str] = None
    order: Optional[int] = None


class CabinetResponse(CabinetBase):
    """Schema for cabinet response."""

    id: UUID
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class CabinetWithRegisters(CabinetResponse):
    """Cabinet with nested registers."""

    registers: list["RegisterWithDocumentTypes"] = []


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


# --- DocumentType (Dokumenttyp) ---

class DocumentTypeBase(BaseModel):
    """Base document type schema."""

    name: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = None
    order: int = Field(default=0)
    icon: Optional[str] = None  # Emoji or icon name


class DocumentTypeCreate(DocumentTypeBase):
    """Schema for creating a document type."""

    register_id: UUID


class DocumentTypeUpdate(BaseModel):
    """Schema for updating a document type."""

    name: Optional[str] = Field(None, min_length=1, max_length=255)
    description: Optional[str] = None
    order: Optional[int] = None
    icon: Optional[str] = None
    register_id: Optional[UUID] = None


class DocumentTypeResponse(DocumentTypeBase):
    """Schema for document type response."""

    id: UUID
    register_id: UUID
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
    field_type: str = Field(..., description="Field type from FieldType enum")
    label: Optional[str] = None  # Display label, defaults to name
    description: Optional[str] = None  # Help text
    placeholder: Optional[str] = None
    default_value: Optional[str] = None
    is_required: bool = Field(default=False)
    is_unique: bool = Field(default=False)
    order: int = Field(default=0)
    width: DisplayWidth = Field(default=DisplayWidth.HALF)
    config_json: Optional[str] = None  # JSON for type-specific config

    # For selection/multi_selection types
    options: Optional[list[str]] = None  # Available choices

    # Validation
    min_value: Optional[float] = None
    max_value: Optional[float] = None
    min_length: Optional[int] = None
    max_length: Optional[int] = None
    pattern: Optional[str] = None  # Regex pattern


class MetadataFieldCreate(MetadataFieldBase):
    """Schema for creating a metadata field."""

    document_type_id: UUID


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
    width: Optional[DisplayWidth] = None
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
    document_type_id: UUID
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
    width: DisplayWidth


class GeneratedLayout(BaseModel):
    """Generated form layout for a document type."""

    document_type_id: UUID
    document_type_name: str
    rows: list[LayoutRow]
    total_fields: int


# --- CRUD Schemas ---

class DocumentTypeDetail(DocumentTypeWithFields):
    """Full document type with layout."""

    layout: GeneratedLayout


# Update forward refs
CabinetWithRegisters.model_rebuild()
RegisterWithDocumentTypes.model_rebuild()
DocumentTypeWithFields.model_rebuild()
LayoutRow.model_rebuild()
