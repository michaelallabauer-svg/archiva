"""Database connection and session management."""

import hashlib
from collections.abc import Generator
from contextlib import contextmanager

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from archiva.config import Settings, load_settings
from archiva.models import Base


_engine = None
_SessionLocal = None


def init_db(settings: Settings | None = None) -> None:
    """Initialize database engine and session factory."""
    global _engine, _SessionLocal

    if settings is None:
        settings = load_settings()

    _engine = create_engine(
        settings.database.url,
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=10,
    )
    _SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=_engine)


def create_tables() -> None:
    """Create all database tables."""
    if _engine is None:
        raise RuntimeError("Database not initialized. Call init_db() first.")
    with _engine.begin() as conn:
        bootstrap_lock_key = int(hashlib.sha1(b"archiva:create_tables:v1").hexdigest()[:15], 16)
        conn.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": bootstrap_lock_key})
        Base.metadata.create_all(bind=conn)
        _ensure_identity_tables(conn)
        _ensure_document_cabinet_column(conn)
        _ensure_structure_metadata_value_columns(conn)
        _ensure_definition_model_columns(conn)
        _ensure_search_indexing_columns(conn)


def _ensure_identity_tables(conn) -> None:
    conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS roles (
                    id UUID PRIMARY KEY,
                    name VARCHAR(255) NOT NULL UNIQUE,
                    description TEXT NULL,
                    is_system BOOLEAN NOT NULL DEFAULT FALSE,
                    permissions_json TEXT NULL,
                    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMP NOT NULL DEFAULT NOW()
                )
                """
            )
        )
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_roles_name ON roles (name)"))

    conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id UUID PRIMARY KEY,
                    email VARCHAR(255) NOT NULL UNIQUE,
                    display_name VARCHAR(255) NOT NULL,
                    auth_source VARCHAR(50) NOT NULL DEFAULT 'local',
                    status VARCHAR(50) NOT NULL DEFAULT 'active',
                    password_hash VARCHAR(255) NULL,
                    external_subject VARCHAR(255) NULL,
                    external_tenant_id VARCHAR(255) NULL,
                    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMP NOT NULL DEFAULT NOW()
                )
                """
            )
        )
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_users_email ON users (email)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_users_status ON users (status)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_users_auth_source ON users (auth_source)"))

    conn.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS user_role_assignments (
                    id UUID PRIMARY KEY,
                    user_id UUID NOT NULL REFERENCES users(id),
                    role_id UUID NOT NULL REFERENCES roles(id),
                    created_at TIMESTAMP NOT NULL DEFAULT NOW()
                )
                """
            )
        )
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_user_role_assignments_user_id ON user_role_assignments (user_id)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_user_role_assignments_role_id ON user_role_assignments (role_id)"))
    conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS ix_user_role_assignments_unique ON user_role_assignments (user_id, role_id)"))

    conn.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS teams (
                id UUID PRIMARY KEY,
                name VARCHAR(255) NOT NULL UNIQUE,
                description TEXT NULL,
                created_at TIMESTAMP NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMP NOT NULL DEFAULT NOW()
            )
            """
        )
    )
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_teams_name ON teams (name)"))

    conn.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS team_memberships (
                id UUID PRIMARY KEY,
                team_id UUID NOT NULL REFERENCES teams(id),
                user_id UUID NOT NULL REFERENCES users(id),
                created_at TIMESTAMP NOT NULL DEFAULT NOW()
            )
            """
        )
    )
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_team_memberships_team_id ON team_memberships (team_id)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_team_memberships_user_id ON team_memberships (user_id)"))
    conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS ix_team_memberships_unique ON team_memberships (team_id, user_id)"))

    conn.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS assignment_targets (
                id UUID PRIMARY KEY,
                target_type VARCHAR(50) NOT NULL,
                user_id UUID NULL REFERENCES users(id),
                role_id UUID NULL REFERENCES roles(id),
                team_id UUID NULL REFERENCES teams(id),
                label VARCHAR(255) NULL,
                description TEXT NULL,
                created_at TIMESTAMP NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMP NOT NULL DEFAULT NOW()
            )
            """
        )
    )
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_assignment_targets_target_type ON assignment_targets (target_type)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_assignment_targets_user_id ON assignment_targets (user_id)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_assignment_targets_role_id ON assignment_targets (role_id)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_assignment_targets_team_id ON assignment_targets (team_id)"))

    conn.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS workflow_definitions (
                id UUID PRIMARY KEY,
                name VARCHAR(255) NOT NULL UNIQUE,
                description TEXT NULL,
                is_active BOOLEAN NOT NULL DEFAULT TRUE,
                version INTEGER NOT NULL DEFAULT 1,
                created_at TIMESTAMP NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMP NOT NULL DEFAULT NOW()
            )
            """
        )
    )
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_workflow_definitions_name ON workflow_definitions (name)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_workflow_definitions_is_active ON workflow_definitions (is_active)"))

    conn.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS workflow_step_definitions (
                id UUID PRIMARY KEY,
                workflow_definition_id UUID NOT NULL REFERENCES workflow_definitions(id),
                name VARCHAR(255) NOT NULL,
                description TEXT NULL,
                step_key VARCHAR(100) NOT NULL,
                "order" INTEGER NOT NULL DEFAULT 0,
                assignment_target_id UUID NULL REFERENCES assignment_targets(id),
                due_in_days INTEGER NULL,
                created_at TIMESTAMP NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMP NOT NULL DEFAULT NOW()
            )
            """
        )
    )
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_workflow_step_definitions_workflow_definition_id ON workflow_step_definitions (workflow_definition_id)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_workflow_step_definitions_step_key ON workflow_step_definitions (step_key)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_workflow_step_definitions_order ON workflow_step_definitions (\"order\")"))

    conn.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS workflow_transition_definitions (
                id UUID PRIMARY KEY,
                workflow_definition_id UUID NOT NULL REFERENCES workflow_definitions(id),
                from_step_id UUID NOT NULL REFERENCES workflow_step_definitions(id),
                to_step_id UUID NOT NULL REFERENCES workflow_step_definitions(id),
                label VARCHAR(255) NOT NULL DEFAULT 'Weiter',
                is_default BOOLEAN NOT NULL DEFAULT FALSE,
                created_at TIMESTAMP NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMP NOT NULL DEFAULT NOW()
            )
            """
        )
    )
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_workflow_transition_definitions_workflow_definition_id ON workflow_transition_definitions (workflow_definition_id)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_workflow_transition_definitions_from_step_id ON workflow_transition_definitions (from_step_id)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_workflow_transition_definitions_to_step_id ON workflow_transition_definitions (to_step_id)"))

    advisory_lock_key = int(hashlib.sha1(b"archiva:seed:roles").hexdigest()[:15], 16)
    lock_acquired = conn.execute(
        text("SELECT pg_try_advisory_xact_lock(:key)"),
        {"key": advisory_lock_key},
    ).scalar()
    if not lock_acquired:
        return

    seeded_roles = [
        ("Admin", "Voller Zugriff auf Administration und Systemkonfiguration", True, '["admin:*", "app:*", "workflow:*", "identity:*"]'),
        ("Editor", "Operative Arbeit in der App mit Dokumenten und Metadaten", True, '["app:read", "app:write", "documents:write"]'),
        ("Viewer", "Nur lesender Zugriff auf Inhalte", True, '["app:read", "documents:read"]'),
    ]
    for name, description, is_system, permissions_json in seeded_roles:
        conn.execute(
            text(
                """
                INSERT INTO roles (id, name, description, is_system, permissions_json, created_at, updated_at)
                VALUES (gen_random_uuid(), :name, :description, :is_system, :permissions_json, NOW(), NOW())
                ON CONFLICT (name) DO NOTHING
                """
            ),
            {
                "name": name,
                "description": description,
                "is_system": is_system,
                "permissions_json": permissions_json,
            },
        )

    conn.execute(
        text(
            """
            INSERT INTO assignment_targets (id, target_type, user_id, label, description, created_at, updated_at)
            SELECT gen_random_uuid(), 'user', u.id, u.display_name, u.email, NOW(), NOW()
            FROM users u
            WHERE NOT EXISTS (
                SELECT 1 FROM assignment_targets at
                WHERE at.user_id = u.id AND at.target_type = 'user'
            )
            """
        )
    )
    conn.execute(
        text(
            """
            INSERT INTO assignment_targets (id, target_type, role_id, label, description, created_at, updated_at)
            SELECT gen_random_uuid(), 'role', r.id, r.name, r.description, NOW(), NOW()
            FROM roles r
            WHERE NOT EXISTS (
                SELECT 1 FROM assignment_targets at
                WHERE at.role_id = r.id AND at.target_type = 'role'
            )
            """
        )
    )
    conn.execute(
        text(
            """
            INSERT INTO assignment_targets (id, target_type, team_id, label, description, created_at, updated_at)
            SELECT gen_random_uuid(), 'team', t.id, t.name, t.description, NOW(), NOW()
            FROM teams t
            WHERE NOT EXISTS (
                SELECT 1 FROM assignment_targets at
                WHERE at.team_id = t.id AND at.target_type = 'team'
            )
            """
        )
    )


def _ensure_document_cabinet_column(conn) -> None:
    conn.execute(text("ALTER TABLE documents ADD COLUMN IF NOT EXISTS cabinet_id UUID NULL"))
    fk_exists = conn.execute(
            text(
                """
                SELECT 1
                FROM information_schema.table_constraints
                WHERE table_name = 'documents'
                  AND constraint_name = 'fk_documents_cabinet_id'
                LIMIT 1
                """
            )
        ).first()
    if not fk_exists:
        conn.execute(
            text(
                "ALTER TABLE documents ADD CONSTRAINT fk_documents_cabinet_id FOREIGN KEY (cabinet_id) REFERENCES cabinets(id)"
            )
        )
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_documents_cabinet_id ON documents (cabinet_id)"))


def _ensure_structure_metadata_value_columns(conn) -> None:
    conn.execute(text("ALTER TABLE cabinets ADD COLUMN IF NOT EXISTS metadata_json TEXT NULL"))
    conn.execute(text("ALTER TABLE registers ADD COLUMN IF NOT EXISTS metadata_json TEXT NULL"))


def _ensure_search_indexing_columns(conn) -> None:
    conn.execute(text("ALTER TABLE documents ADD COLUMN IF NOT EXISTS index_status VARCHAR(50) NOT NULL DEFAULT 'pending'"))
    conn.execute(text("ALTER TABLE documents ADD COLUMN IF NOT EXISTS index_revision INTEGER NOT NULL DEFAULT 0"))
    conn.execute(text("ALTER TABLE documents ADD COLUMN IF NOT EXISTS content_hash VARCHAR(255) NULL"))
    conn.execute(text("ALTER TABLE documents ADD COLUMN IF NOT EXISTS index_error TEXT NULL"))
    conn.execute(text("ALTER TABLE documents ADD COLUMN IF NOT EXISTS index_engine VARCHAR(255) NULL"))
    conn.execute(text("ALTER TABLE documents ADD COLUMN IF NOT EXISTS extracted_text_preview TEXT NULL"))
    conn.execute(text("ALTER TABLE documents ADD COLUMN IF NOT EXISTS extracted_text_length INTEGER NULL"))
    conn.execute(text("ALTER TABLE documents ADD COLUMN IF NOT EXISTS index_ocr_used BOOLEAN NOT NULL DEFAULT FALSE"))
    conn.execute(text("ALTER TABLE documents ADD COLUMN IF NOT EXISTS file_hash VARCHAR(32) NULL"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_documents_file_hash ON documents (file_hash)"))
    conn.execute(text("ALTER TABLE document_types ADD COLUMN IF NOT EXISTS md5_duplicate_check BOOLEAN NOT NULL DEFAULT TRUE"))
    conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS index_jobs (
                    id UUID PRIMARY KEY,
                    document_id UUID NOT NULL REFERENCES documents(id),
                    job_type VARCHAR(50) NOT NULL DEFAULT 'index_document',
                    status VARCHAR(50) NOT NULL DEFAULT 'pending',
                    priority INTEGER NOT NULL DEFAULT 100,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    max_attempts INTEGER NOT NULL DEFAULT 5,
                    scheduled_at TIMESTAMP NOT NULL DEFAULT NOW(),
                    started_at TIMESTAMP NULL,
                    finished_at TIMESTAMP NULL,
                    worker_id VARCHAR(255) NULL,
                    error_message TEXT NULL,
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMP NOT NULL DEFAULT NOW()
                )
                """
            )
        )
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_index_jobs_document_id ON index_jobs (document_id)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_index_jobs_status ON index_jobs (status)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_index_jobs_scheduled_at ON index_jobs (scheduled_at)"))


def _ensure_definition_model_columns(conn) -> None:
    conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS register_types (
                    id UUID PRIMARY KEY,
                    cabinet_type_id UUID NOT NULL REFERENCES cabinet_types(id),
                    name VARCHAR(255) NOT NULL,
                    description TEXT NULL,
                    "order" INTEGER NOT NULL DEFAULT 0,
                    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMP NOT NULL DEFAULT NOW()
                )
                """
            )
        )
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_register_types_cabinet_type_id ON register_types (cabinet_type_id)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_register_types_order ON register_types (\"order\")"))

    conn.execute(text("ALTER TABLE registers ADD COLUMN IF NOT EXISTS register_type_id UUID NULL"))
    register_type_fk_exists = conn.execute(
            text(
                """
                SELECT 1 FROM information_schema.table_constraints
                WHERE table_name = 'registers' AND constraint_name = 'fk_registers_register_type_id'
                LIMIT 1
                """
            )
        ).first()
    if not register_type_fk_exists:
        conn.execute(text("ALTER TABLE registers ADD CONSTRAINT fk_registers_register_type_id FOREIGN KEY (register_type_id) REFERENCES register_types(id)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_registers_register_type_id ON registers (register_type_id)"))

    conn.execute(text("ALTER TABLE document_types ADD COLUMN IF NOT EXISTS cabinet_type_id UUID NULL"))
    conn.execute(text("ALTER TABLE document_types ADD COLUMN IF NOT EXISTS register_type_id UUID NULL"))

    document_type_cabinet_type_fk_exists = conn.execute(
        text(
            """
            SELECT 1 FROM information_schema.table_constraints
            WHERE table_name = 'document_types' AND constraint_name = 'fk_document_types_cabinet_type_id'
            LIMIT 1
            """
        )
    ).first()
    if not document_type_cabinet_type_fk_exists:
        conn.execute(text("ALTER TABLE document_types ADD CONSTRAINT fk_document_types_cabinet_type_id FOREIGN KEY (cabinet_type_id) REFERENCES cabinet_types(id)"))

    document_type_register_type_fk_exists = conn.execute(
        text(
            """
            SELECT 1 FROM information_schema.table_constraints
            WHERE table_name = 'document_types' AND constraint_name = 'fk_document_types_register_type_id'
            LIMIT 1
            """
        )
    ).first()
    if not document_type_register_type_fk_exists:
        conn.execute(text("ALTER TABLE document_types ADD CONSTRAINT fk_document_types_register_type_id FOREIGN KEY (register_type_id) REFERENCES register_types(id)"))

    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_document_types_cabinet_type_id ON document_types (cabinet_type_id)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_document_types_register_type_id ON document_types (register_type_id)"))

    conn.execute(text("ALTER TABLE metadata_fields ADD COLUMN IF NOT EXISTS cabinet_type_id UUID NULL"))
    conn.execute(text("ALTER TABLE metadata_fields ADD COLUMN IF NOT EXISTS register_type_id UUID NULL"))

    metadata_field_cabinet_type_fk_exists = conn.execute(
        text(
            """
            SELECT 1 FROM information_schema.table_constraints
            WHERE table_name = 'metadata_fields' AND constraint_name = 'fk_metadata_fields_cabinet_type_id'
            LIMIT 1
            """
        )
    ).first()
    if not metadata_field_cabinet_type_fk_exists:
        conn.execute(text("ALTER TABLE metadata_fields ADD CONSTRAINT fk_metadata_fields_cabinet_type_id FOREIGN KEY (cabinet_type_id) REFERENCES cabinet_types(id)"))

    metadata_field_register_type_fk_exists = conn.execute(
        text(
            """
            SELECT 1 FROM information_schema.table_constraints
            WHERE table_name = 'metadata_fields' AND constraint_name = 'fk_metadata_fields_register_type_id'
            LIMIT 1
            """
        )
    ).first()
    if not metadata_field_register_type_fk_exists:
        conn.execute(text("ALTER TABLE metadata_fields ADD CONSTRAINT fk_metadata_fields_register_type_id FOREIGN KEY (register_type_id) REFERENCES register_types(id)"))

    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_metadata_fields_cabinet_type_id ON metadata_fields (cabinet_type_id)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_metadata_fields_register_type_id ON metadata_fields (register_type_id)"))


@contextmanager
def get_session() -> Generator[Session, None, None]:
    """Get a database session as a context manager."""
    if _SessionLocal is None:
        raise RuntimeError("Database not initialized. Call init_db() first.")

    session = _SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency for database session."""
    if _SessionLocal is None:
        raise RuntimeError("Database not initialized. Call init_db() first.")

    session = _SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
