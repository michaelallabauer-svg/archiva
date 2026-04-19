"""Standalone preview worker service for Archiva."""

from __future__ import annotations

import logging
import time

from archiva.config import load_settings
from archiva.database import create_tables, get_session, init_db
from archiva.preview_queue import process_pending_preview_jobs
from archiva.storage import StorageManager

logger = logging.getLogger("archiva.preview_worker")


def run_worker(poll_interval_seconds: float = 2.0) -> None:
    settings = load_settings()
    init_db(settings)
    create_tables()
    storage = StorageManager(settings.storage.base_path)

    logger.info("Preview worker started, poll_interval_seconds=%s", poll_interval_seconds)
    while True:
        try:
            with get_session() as db:
                processed = process_pending_preview_jobs(db, storage)
            if processed:
                logger.info("Processed %s preview job(s)", processed)
            else:
                time.sleep(poll_interval_seconds)
        except KeyboardInterrupt:
            logger.info("Preview worker stopped")
            raise
        except Exception:
            logger.exception("Preview worker loop failed")
            time.sleep(max(poll_interval_seconds, 5.0))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    run_worker()
