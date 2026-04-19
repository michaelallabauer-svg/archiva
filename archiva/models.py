"""SQLAlchemy models for Archiva ECM."""

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    Column,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import ENUM as PGEnum
from sqlalchemy.dialects.postgresql import TSVECTOR, UUID
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import Mapped, mapped_column, relationship
import enum

Base = declarative_base()


# --- Field Types ---

class FieldType(str, enum.Enum):
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


class DisplayWidth(str, enum.Enum):
    """Form layout width."""

    FULL = "full"
    HALF = "half"
    THIRD = "third"
    QUARTER = "quarter"


# --- ECM Hierarchy Models ---

class CabinetType(Base):
    """Fachlicher Cabinet-Typ, z. B. ERB oder Personal."""

    __tablename__ = "cabinet_types"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    cabinets: Mapped[list["Cabinet"]] = relationship(
        "Cabinet", back_populates="cabinet_type", cascade="all, delete-orphan"
    )
    register_types: Mapped[list["RegisterType"]] = relationship(
        "RegisterType", back_populates="cabinet_type", cascade="all, delete-orphan"
    )
    document_type_definitions: Mapped[list["DocumentType"]] = relationship(
        "DocumentType",
        back_populates="cabinet_type_definition",
        foreign_keys="DocumentType.cabinet_type_id",
    )
    metadata_fields: Mapped[list["MetadataField"]] = relationship(
        "MetadataField",
        back_populates="cabinet_type_definition",
        foreign_keys="MetadataField.cabinet_type_id",
    )

    __table_args__ = (Index("ix_cabinet_types_order", "order"),)


class Cabinet(Base):
    """Konkretes Cabinet innerhalb eines Cabinet-Typs, z. B. 2025 oder 2026."""

    __tablename__ = "cabinets"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    cabinet_type_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("cabinet_types.id"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    cabinet_type: Mapped["CabinetType"] = relationship("CabinetType", back_populates="cabinets")
    registers: Mapped[list["Register"]] = relationship(
        "Register", back_populates="cabinet", cascade="all, delete-orphan"
    )
    document_types: Mapped[list["DocumentType"]] = relationship(
        "DocumentType", back_populates="cabinet", cascade="all, delete-orphan"
    )
    metadata_fields: Mapped[list["MetadataField"]] = relationship(
        "MetadataField", back_populates="cabinet", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("ix_cabinets_order", "order"),
        Index("ix_cabinets_cabinet_type_id", "cabinet_type_id"),
    )


class Register(Base):
    """Register - middle level inside a cabinet."""

    __tablename__ = "registers"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    cabinet_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("cabinets.id"), nullable=False
    )
    register_type_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("register_types.id"), nullable=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    cabinet: Mapped["Cabinet"] = relationship("Cabinet", back_populates="registers")
    register_type: Mapped[Optional["RegisterType"]] = relationship("RegisterType", back_populates="registers")
    document_types: Mapped[list["DocumentType"]] = relationship(
        "DocumentType", back_populates="register", cascade="all, delete-orphan"
    )
    metadata_fields: Mapped[list["MetadataField"]] = relationship(
        "MetadataField", back_populates="register", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("ix_registers_cabinet_id", "cabinet_id"),
        Index("ix_registers_register_type_id", "register_type_id"),
    )


class RegisterType(Base):
    """Definition of allowed registers beneath a cabinet type."""

    __tablename__ = "register_types"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    cabinet_type_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("cabinet_types.id"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    cabinet_type: Mapped["CabinetType"] = relationship("CabinetType", back_populates="register_types")
    document_type_definitions: Mapped[list["DocumentType"]] = relationship(
        "DocumentType",
        back_populates="register_type_definition",
        foreign_keys="DocumentType.register_type_id",
    )
    registers: Mapped[list["Register"]] = relationship("Register", back_populates="register_type")
    metadata_fields: Mapped[list["MetadataField"]] = relationship(
        "MetadataField",
        back_populates="register_type_definition",
        foreign_keys="MetadataField.register_type_id",
    )

    __table_args__ = (
        Index("ix_register_types_cabinet_type_id", "cabinet_type_id"),
        Index("ix_register_types_order", "order"),
    )


class DocumentType(Base):
    """Document Type - defines metadata structure for a type of document."""

    __tablename__ = "document_types"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    register_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("registers.id"), nullable=True
    )
    cabinet_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("cabinets.id"), nullable=True
    )
    cabinet_type_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("cabinet_types.id"), nullable=True
    )
    register_type_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("register_types.id"), nullable=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    icon: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    register: Mapped[Optional["Register"]] = relationship("Register", back_populates="document_types")
    cabinet: Mapped[Optional["Cabinet"]] = relationship("Cabinet", back_populates="document_types")
    cabinet_type_definition: Mapped[Optional["CabinetType"]] = relationship(
        "CabinetType",
        back_populates="document_type_definitions",
        foreign_keys=[cabinet_type_id],
    )
    register_type_definition: Mapped[Optional["RegisterType"]] = relationship(
        "RegisterType",
        back_populates="document_type_definitions",
        foreign_keys=[register_type_id],
    )
    fields: Mapped[list["MetadataField"]] = relationship(
        "MetadataField", back_populates="document_type", cascade="all, delete-orphan"
    )
    documents: Mapped[list["Document"]] = relationship(
        "Document", back_populates="document_type"
    )

    __table_args__ = (
        Index("ix_document_types_register_id", "register_id"),
        Index("ix_document_types_cabinet_id", "cabinet_id"),
        Index("ix_document_types_cabinet_type_id", "cabinet_type_id"),
        Index("ix_document_types_register_type_id", "register_type_id"),
    )


class MetadataField(Base):
    """Metadata field definition for a document type."""

    __tablename__ = "metadata_fields"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    document_type_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("document_types.id"), nullable=True
    )
    cabinet_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("cabinets.id"), nullable=True
    )
    register_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("registers.id"), nullable=True
    )
    cabinet_type_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("cabinet_types.id"), nullable=True
    )
    register_type_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("register_types.id"), nullable=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    field_type: Mapped[str] = mapped_column(String(50), nullable=False)
    label: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    placeholder: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    default_value: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    is_required: Mapped[bool] = mapped_column(default=False, nullable=False)
    is_unique: Mapped[bool] = mapped_column(default=False, nullable=False)
    order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    width: Mapped[str] = mapped_column(String(20), default="half", nullable=False)
    config_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    options: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    min_value: Mapped[Optional[float]] = mapped_column(Integer, nullable=True)
    max_value: Mapped[Optional[float]] = mapped_column(Integer, nullable=True)
    min_length: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    max_length: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    pattern: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    document_type: Mapped[Optional["DocumentType"]] = relationship(
        "DocumentType", back_populates="fields"
    )
    cabinet: Mapped[Optional["Cabinet"]] = relationship(
        "Cabinet", back_populates="metadata_fields"
    )
    register: Mapped[Optional["Register"]] = relationship(
        "Register", back_populates="metadata_fields"
    )
    cabinet_type_definition: Mapped[Optional["CabinetType"]] = relationship(
        "CabinetType",
        back_populates="metadata_fields",
        foreign_keys=[cabinet_type_id],
    )
    register_type_definition: Mapped[Optional["RegisterType"]] = relationship(
        "RegisterType",
        back_populates="metadata_fields",
        foreign_keys=[register_type_id],
    )

    __table_args__ = (
        Index("ix_metadata_fields_document_type_id", "document_type_id"),
        Index("ix_metadata_fields_cabinet_id", "cabinet_id"),
        Index("ix_metadata_fields_register_id", "register_id"),
        Index("ix_metadata_fields_cabinet_type_id", "cabinet_type_id"),
        Index("ix_metadata_fields_register_type_id", "register_type_id"),
        Index("ix_metadata_fields_order", "order"),
    )


# --- Document Models ---

class DocType(str, enum.Enum):
    """Document type enum (file-based)."""

    TEXT = "text"
    PDF = "pdf"
    DOC = "doc"
    IMAGE = "image"
    OTHER = "other"


class Document(Base):
    """Core document model with full-text search and ECM metadata."""

    __tablename__ = "documents"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(500), nullable=False)
    doc_type: Mapped[DocType] = mapped_column(
        Enum(DocType), nullable=False, default=DocType.OTHER
    )
    mime_type: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    storage_path: Mapped[str] = mapped_column(String(1000), nullable=False)

    # ECM reference
    document_type_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("document_types.id"), nullable=True
    )
    cabinet_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("cabinets.id"), nullable=True
    )
    metadata_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Full-text search vector
    content_vector = Column(TSVECTOR, nullable=True)

    # Legacy metadata fields
    title: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    author: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    tags: Mapped[Optional[str]] = mapped_column(String(1000), nullable=True)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )
    indexed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    index_status: Mapped[str] = mapped_column(String(50), nullable=False, default="pending")
    index_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    content_hash: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    index_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    index_engine: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    index_ocr_used: Mapped[bool] = mapped_column(default=False, nullable=False)

    # Relationships
    document_type: Mapped[Optional["DocumentType"]] = relationship(
        "DocumentType", back_populates="documents"
    )
    cabinet: Mapped[Optional["Cabinet"]] = relationship("Cabinet")
    versions: Mapped[list["DocumentVersion"]] = relationship(
        "DocumentVersion", back_populates="document", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("ix_documents_content_vector", "content_vector", postgresql_using="gin"),
        Index("ix_documents_name", "name"),
        Index("ix_documents_doc_type", "doc_type"),
        Index("ix_documents_created_at", "created_at"),
        Index("ix_documents_document_type_id", "document_type_id"),
        Index("ix_documents_cabinet_id", "cabinet_id"),
    )


class IndexJob(Base):
    """Queue entry for asynchronous search indexing."""

    __tablename__ = "index_jobs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id"), nullable=False
    )
    job_type: Mapped[str] = mapped_column(String(50), nullable=False, default="index_document")
    status: Mapped[str] = mapped_column(String(50), nullable=False, default="pending")
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    scheduled_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    worker_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    document: Mapped["Document"] = relationship("Document")

    __table_args__ = (
        Index("ix_index_jobs_document_id", "document_id"),
        Index("ix_index_jobs_status", "status"),
        Index("ix_index_jobs_scheduled_at", "scheduled_at"),
    )


class PreviewJobStatus(str, enum.Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


preview_job_status_enum = PGEnum(
    PreviewJobStatus,
    name="previewjobstatus",
    create_type=False,
)


class PreviewJob(Base):
    __tablename__ = "preview_jobs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("documents.id"), nullable=False)
    status: Mapped[PreviewJobStatus] = mapped_column(preview_job_status_enum, nullable=False, default=PreviewJobStatus.PENDING)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    document: Mapped["Document"] = relationship("Document")

    __table_args__ = (
        Index("ix_preview_jobs_document_id", "document_id"),
        Index("ix_preview_jobs_status", "status"),
        Index("ix_preview_jobs_created_at", "created_at"),
    )


class PreviewArtifact(Base):
    __tablename__ = "preview_artifacts"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("documents.id"), nullable=False)
    kind: Mapped[str] = mapped_column(String(50), nullable=False, default="html")
    mime_type: Mapped[str] = mapped_column(String(100), nullable=False, default="text/html")
    storage_path: Mapped[str] = mapped_column(String(1000), nullable=False)
    status: Mapped[str] = mapped_column(String(50), nullable=False, default="ready")
    page_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    document: Mapped["Document"] = relationship("Document")

    __table_args__ = (
        Index("ix_preview_artifacts_document_id", "document_id"),
        Index("ix_preview_artifacts_status", "status"),
    )


class DocumentVersion(Base):
    """Version history for documents."""

    __tablename__ = "document_versions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id"), nullable=False
    )
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)

    storage_path: Mapped[str] = mapped_column(String(1000), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    checksum: Mapped[str] = mapped_column(String(64), nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )
    created_by: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    document: Mapped["Document"] = relationship("Document", back_populates="versions")

    __table_args__ = (
        Index("ix_document_versions_document_id", "document_id"),
        Index("ix_document_versions_version_number", "version_number"),
    )
