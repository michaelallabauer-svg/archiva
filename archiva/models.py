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

class Cabinet(Base):
    """Schrank (Cabinet) - top level container."""

    __tablename__ = "cabinets"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
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

    registers: Mapped[list["Register"]] = relationship(
        "Register", back_populates="cabinet", cascade="all, delete-orphan"
    )

    __table_args__ = (Index("ix_cabinets_order", "order"),)


class Register(Base):
    """Register - middle level inside a cabinet."""

    __tablename__ = "registers"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    cabinet_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("cabinets.id"), nullable=False
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
    document_types: Mapped[list["DocumentType"]] = relationship(
        "DocumentType", back_populates="register", cascade="all, delete-orphan"
    )

    __table_args__ = (Index("ix_registers_cabinet_id", "cabinet_id"),)


class DocumentType(Base):
    """Document Type - defines metadata structure for a type of document."""

    __tablename__ = "document_types"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    register_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("registers.id"), nullable=False
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

    register: Mapped["Register"] = relationship("Register", back_populates="document_types")
    fields: Mapped[list["MetadataField"]] = relationship(
        "MetadataField", back_populates="document_type", cascade="all, delete-orphan"
    )
    documents: Mapped[list["Document"]] = relationship(
        "Document", back_populates="document_type"
    )

    __table_args__ = (Index("ix_document_types_register_id", "register_id"),)


class MetadataField(Base):
    """Metadata field definition for a document type."""

    __tablename__ = "metadata_fields"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    document_type_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("document_types.id"), nullable=False
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

    document_type: Mapped["DocumentType"] = relationship(
        "DocumentType", back_populates="fields"
    )

    __table_args__ = (
        Index("ix_metadata_fields_document_type_id", "document_type_id"),
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

    # Relationships
    document_type: Mapped[Optional["DocumentType"]] = relationship(
        "DocumentType", back_populates="documents"
    )
    versions: Mapped[list["DocumentVersion"]] = relationship(
        "DocumentVersion", back_populates="document", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("ix_documents_content_vector", "content_vector", postgresql_using="gin"),
        Index("ix_documents_name", "name"),
        Index("ix_documents_doc_type", "doc_type"),
        Index("ix_documents_created_at", "created_at"),
        Index("ix_documents_document_type_id", "document_type_id"),
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
