"""Main entry point for Archiva ECM."""

import asyncio
import contextlib
import logging

import uvicorn
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

from archiva.api import router as admin_router
from archiva.api_documents import init_router, router as documents_router
from archiva.internal_index_api import router as internal_index_router
from archiva.search_api import router as search_router
from archiva.config import load_settings
from archiva.database import create_tables, get_session, init_db
from archiva.indexer.opensearch_client import OpenSearchClient
from archiva.indexer.worker import process_pending_index_jobs
from archiva.preview_queue import process_pending_preview_jobs
from archiva.storage import StorageManager
from archiva.ui import router as ui_router

logger = logging.getLogger("archiva.main")


def _process_pending_preview_jobs_once(storage: StorageManager) -> int:
    with get_session() as db:
        return process_pending_preview_jobs(db, storage)


async def _queue_worker_loop(settings) -> None:
    storage = StorageManager(settings.storage.base_path)
    client = OpenSearchClient()
    while True:
        try:
            preview_count = await asyncio.to_thread(_process_pending_preview_jobs_once, storage)
            index_count = await asyncio.to_thread(process_pending_index_jobs, storage, client, "app-worker")
            if preview_count or index_count:
                logger.info("Queue worker processed preview=%s index=%s", preview_count, index_count)
                await asyncio.sleep(0.2)
            else:
                await asyncio.sleep(2.0)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Queue worker failed")
            await asyncio.sleep(5.0)


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

    @app.on_event("startup")
    async def start_queue_worker() -> None:
        app.state.queue_worker_task = asyncio.create_task(_queue_worker_loop(settings))

    @app.on_event("shutdown")
    async def stop_queue_worker() -> None:
        task = getattr(app.state, "queue_worker_task", None)
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

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
