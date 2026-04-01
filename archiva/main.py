"""Main entry point for Archiva ECM."""

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from archiva.api import init_router, router
from archiva.config import load_settings
from archiva.database import create_tables, init_db
from archiva.storage import StorageManager


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    settings = load_settings()

    # Initialize database
    init_db(settings)
    create_tables()

    # Initialize storage
    storage = StorageManager(settings.storage.base_path)

    # Create FastAPI app
    app = FastAPI(
        title="Archiva",
        description="Lightweight Enterprise Content Management with Full-Text Search",
        version="0.1.0",
    )

    # CORS middleware
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Initialize router with storage
    init_router(storage)

    # Include API routes
    app.include_router(router)

    return app


app = create_app()


if __name__ == "__main__":
    settings = load_settings()
    uvicorn.run(
        "archiva.main:app",
        host=settings.app.host,
        port=settings.app.port,
        reload=settings.app.debug,
    )
