import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.health import router as health_router
from app.api.recordings import router as recordings_router
from app.config import Settings
from app.db import Database
from app.errors import register_error_handlers
from app.logging import configure_logging
from app.middleware import RequestContextMiddleware
from app.storage import LocalStorage

logger = logging.getLogger("app.lifecycle")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    configure_logging(settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        database = Database(settings)
        app.state.settings = settings
        app.state.database = database
        app.state.storage = LocalStorage(settings.upload_dir)
        logger.info("api_started")
        try:
            yield
        finally:
            await database.close()
            logger.info("api_stopped")

    app = FastAPI(title="Recording Transcription API", version="0.1.0", lifespan=lifespan)
    app.add_middleware(RequestContextMiddleware)
    register_error_handlers(app)
    app.include_router(health_router)
    app.include_router(recordings_router)
    return app
