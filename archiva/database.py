"""Database connection and session management."""

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
    Base.metadata.create_all(bind=_engine)
    _ensure_document_cabinet_column()
    _ensure_definition_model_columns()
    _ensure_search_indexing_columns()


def _ensure_document_cabinet_column() -> None:
    if _engine is None:
        return
    with _engine.begin() as conn:
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


def _ensure_search_indexing_columns() -> None:
    if _engine is None:
        return
    with _engine.begin() as conn:
        conn.execute(text("ALTER TABLE documents ADD COLUMN IF NOT EXISTS index_status VARCHAR(50) NOT NULL DEFAULT 'pending'"))
        conn.execute(text("ALTER TABLE documents ADD COLUMN IF NOT EXISTS index_revision INTEGER NOT NULL DEFAULT 0"))
        conn.execute(text("ALTER TABLE documents ADD COLUMN IF NOT EXISTS content_hash VARCHAR(255) NULL"))
        conn.execute(text("ALTER TABLE documents ADD COLUMN IF NOT EXISTS index_error TEXT NULL"))
        conn.execute(text("ALTER TABLE documents ADD COLUMN IF NOT EXISTS index_engine VARCHAR(100) NULL"))
        conn.execute(text("ALTER TABLE documents ADD COLUMN IF NOT EXISTS index_ocr_used BOOLEAN NOT NULL DEFAULT FALSE"))
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


def _ensure_definition_model_columns() -> None:
    if _engine is None:
        return
    with _engine.begin() as conn:
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
