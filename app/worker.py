"""Run with python -m app.worker; API and Worker share only database/storage."""

import asyncio
import logging
import signal

from app.db import Database
from app.logging import configure_logging
from app.processing.config import WorkerSettings
from app.processing.llm import LLMProvider
from app.processing.worker import Worker
from app.storage import LocalStorage


async def serve():
    settings = WorkerSettings()
    configure_logging(settings.log_level)
    database = Database(settings)
    provider = LLMProvider(settings)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    previous = {}
    for signum in (signal.SIGINT, signal.SIGTERM):
        previous[signum] = signal.getsignal(signum)
        signal.signal(signum, lambda *_: loop.call_soon_threadsafe(stop.set))
    try:
        await database.check_ready(settings.readiness_timeout_seconds)
        worker = Worker(database, LocalStorage(settings.upload_dir), settings, provider)
        await worker.run(stop)
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
        await provider.close()
        await database.close()


if __name__ == "__main__":
    configure_logging("INFO")
    try:
        asyncio.run(serve())
    except Exception as exc:
        logging.getLogger("app.worker").error(
            "worker_startup_failed", extra={"exception_type": type(exc).__name__}
        )
        raise SystemExit(1) from None
