"""Main entry point for Archiva ECM."""

import uvicorn
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

from archiva.api import router as admin_router
from archiva.api_documents import init_router, router as documents_router
from archiva.internal_index_api import router as internal_index_router
from archiva.search_api import router as search_router
from archiva.config import load_settings
from archiva.database import create_tables, init_db
from archiva.storage import StorageManager
from archiva.ui import router as ui_router


def create_app() -> FastAPI:
    settings = load_settings()

    init_db(settings)
    create_tables()

    storage = StorageManager(settings.storage.base_path)

    app = FastAPI(
        title="Archiva",
        description="Lightweight Enterprise Content Management with Full-Text Search",
        version="0.1.0",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    init_router(storage)
    app.mount("/assets", StaticFiles(directory="assets"), name="assets")
    app.include_router(ui_router, prefix="/ui")
    app.include_router(documents_router)
    app.include_router(admin_router)
    app.include_router(search_router)
    app.include_router(internal_index_router)

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
