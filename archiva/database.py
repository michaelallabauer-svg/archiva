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
