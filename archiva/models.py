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


class UserAuthSource(str, enum.Enum):
    """Identity source for admin/app users."""

    LOCAL = "local"
    ENTRA_ID = "entra_id"


class UserStatus(str, enum.Enum):
    """Lifecycle status for users."""

    ACTIVE = "active"
    INVITED = "invited"
    DISABLED = "disabled"


class AssignmentTargetType(str, enum.Enum):
    """Supported assignment target kinds for workflow definitions and tasks."""

    USER = "user"
    ROLE = "role"
    TEAM = "team"


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
    md5_duplicate_check: Mapped[bool] = mapped_column(default=True, nullable=False)
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


class Role(Base):
    """Native role definition for Archiva admin/app access."""

    __tablename__ = "roles"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    is_system: Mapped[bool] = mapped_column(default=False, nullable=False)
    permissions_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    assignments: Mapped[list["UserRoleAssignment"]] = relationship(
        "UserRoleAssignment", back_populates="role", cascade="all, delete-orphan"
    )

    __table_args__ = (Index("ix_roles_name", "name"),)


class User(Base):
    """Native user record, later extensible with external identity providers."""

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    email: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    auth_source: Mapped[str] = mapped_column(String(50), nullable=False, default=UserAuthSource.LOCAL.value)
    status: Mapped[str] = mapped_column(String(50), nullable=False, default=UserStatus.ACTIVE.value)
    password_hash: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    external_subject: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    external_tenant_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    role_assignments: Mapped[list["UserRoleAssignment"]] = relationship(
        "UserRoleAssignment", back_populates="user", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("ix_users_email", "email"),
        Index("ix_users_status", "status"),
        Index("ix_users_auth_source", "auth_source"),
    )


class UserRoleAssignment(Base):
    """Role assignments between native users and roles."""

    __tablename__ = "user_role_assignments"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    role_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("roles.id"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )

    user: Mapped["User"] = relationship("User", back_populates="role_assignments")
    role: Mapped["Role"] = relationship("Role", back_populates="assignments")

    __table_args__ = (
        Index("ix_user_role_assignments_user_id", "user_id"),
        Index("ix_user_role_assignments_role_id", "role_id"),
        Index("ix_user_role_assignments_unique", "user_id", "role_id", unique=True),
    )


class Team(Base):
    """Native team/group for workflow assignments and shared responsibility."""

    __tablename__ = "teams"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    memberships: Mapped[list["TeamMembership"]] = relationship(
        "TeamMembership", back_populates="team", cascade="all, delete-orphan"
    )

    __table_args__ = (Index("ix_teams_name", "name"),)


class TeamMembership(Base):
    """Membership relation between users and teams."""

    __tablename__ = "team_memberships"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    team_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("teams.id"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )

    team: Mapped["Team"] = relationship("Team", back_populates="memberships")
    user: Mapped["User"] = relationship("User")

    __table_args__ = (
        Index("ix_team_memberships_team_id", "team_id"),
        Index("ix_team_memberships_user_id", "user_id"),
        Index("ix_team_memberships_unique", "team_id", "user_id", unique=True),
    )


class AssignmentTarget(Base):
    """Generic assignment target abstraction over user, role, and team."""

    __tablename__ = "assignment_targets"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    target_type: Mapped[str] = mapped_column(String(50), nullable=False)
    user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    role_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("roles.id"), nullable=True
    )
    team_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("teams.id"), nullable=True
    )
    label: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    user: Mapped[Optional["User"]] = relationship("User")
    role: Mapped[Optional["Role"]] = relationship("Role")
    team: Mapped[Optional["Team"]] = relationship("Team")

    __table_args__ = (
        Index("ix_assignment_targets_target_type", "target_type"),
        Index("ix_assignment_targets_user_id", "user_id"),
        Index("ix_assignment_targets_role_id", "role_id"),
        Index("ix_assignment_targets_team_id", "team_id"),
    )


class WorkflowDefinition(Base):
    """Definition of a reusable workflow inside the workflow designer."""

    __tablename__ = "workflow_definitions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(default=True, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    steps: Mapped[list["WorkflowStepDefinition"]] = relationship(
        "WorkflowStepDefinition", back_populates="workflow_definition", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("ix_workflow_definitions_name", "name"),
        Index("ix_workflow_definitions_is_active", "is_active"),
    )


class WorkflowStepDefinition(Base):
    """Single step inside a workflow definition."""

    __tablename__ = "workflow_step_definitions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    workflow_definition_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workflow_definitions.id"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    step_key: Mapped[str] = mapped_column(String(100), nullable=False)
    order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    assignment_target_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("assignment_targets.id"), nullable=True
    )
    due_in_days: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    workflow_definition: Mapped["WorkflowDefinition"] = relationship("WorkflowDefinition", back_populates="steps")
    assignment_target: Mapped[Optional["AssignmentTarget"]] = relationship("AssignmentTarget")
    outgoing_transitions: Mapped[list["WorkflowTransitionDefinition"]] = relationship(
        "WorkflowTransitionDefinition",
        foreign_keys="WorkflowTransitionDefinition.from_step_id",
        back_populates="from_step",
        cascade="all, delete-orphan",
    )
    incoming_transitions: Mapped[list["WorkflowTransitionDefinition"]] = relationship(
        "WorkflowTransitionDefinition",
        foreign_keys="WorkflowTransitionDefinition.to_step_id",
        back_populates="to_step",
    )

    __table_args__ = (
        Index("ix_workflow_step_definitions_workflow_definition_id", "workflow_definition_id"),
        Index("ix_workflow_step_definitions_step_key", "step_key"),
        Index("ix_workflow_step_definitions_order", "order"),
    )


class WorkflowTransitionDefinition(Base):
    """Directed transition between two workflow steps."""

    __tablename__ = "workflow_transition_definitions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    workflow_definition_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workflow_definitions.id"), nullable=False
    )
    from_step_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workflow_step_definitions.id"), nullable=False
    )
    to_step_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workflow_step_definitions.id"), nullable=False
    )
    label: Mapped[str] = mapped_column(String(255), nullable=False, default="Weiter")
    is_default: Mapped[bool] = mapped_column(default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    from_step: Mapped["WorkflowStepDefinition"] = relationship(
        "WorkflowStepDefinition", foreign_keys=[from_step_id], back_populates="outgoing_transitions"
    )
    to_step: Mapped["WorkflowStepDefinition"] = relationship(
        "WorkflowStepDefinition", foreign_keys=[to_step_id], back_populates="incoming_transitions"
    )

    __table_args__ = (
        Index("ix_workflow_transition_definitions_workflow_definition_id", "workflow_definition_id"),
        Index("ix_workflow_transition_definitions_from_step_id", "from_step_id"),
        Index("ix_workflow_transition_definitions_to_step_id", "to_step_id"),
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
    index_engine: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    extracted_text_preview: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    extracted_text_length: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    index_ocr_used: Mapped[bool] = mapped_column(default=False, nullable=False)

    file_hash: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)

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
    values_callable=lambda enum_cls: [item.value for item in enum_cls],
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
