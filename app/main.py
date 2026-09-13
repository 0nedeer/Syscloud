"""API 应用工厂，负责依赖生命周期、路由和异常处理。"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api import router
from app.config import Settings
from app.db import Database
from app.errors import register_error_handlers
from app.logging import RequestContextMiddleware, configure_logging
from app.storage import LocalStorage

logger = logging.getLogger("app.lifecycle")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    configure_logging(settings.log_level)

    # yield 前初始化进程级共享对象，yield 后在退出时关闭连接池。
    # 通过 app.state 提供给路由，不把数据库连接或文件存储做成跨进程共享内存。
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
    app.include_router(router)
    return app
