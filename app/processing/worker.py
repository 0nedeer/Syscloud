import asyncio
import logging
from time import perf_counter

from anyio import to_thread
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from app.errors import ApiError
from app.models import Recording
from app.processing.providers import MockASR, ProcessingError
from app.processing.queue import TaskQueue
from app.services.deletions import delete_recording
from app.storage import StorageError

logger = logging.getLogger("app.worker")


class Worker:
    def __init__(self, database, storage, settings, llm, asr=None):
        self.database, self.storage, self.settings = database, storage, settings
        self.llm, self.asr = llm, asr or MockASR()
        self.queue = TaskQueue(
            database, settings.worker_lease_seconds, settings.worker_retry_base_seconds
        )
        self.active: set[asyncio.Task] = set()

    async def process(self, lease):
        work = asyncio.create_task(self._pipeline(lease))
        heartbeat = asyncio.create_task(self._heartbeat(lease))
        try:
            finished, _ = await asyncio.wait({work, heartbeat}, return_when=asyncio.FIRST_COMPLETED)
            if heartbeat in finished:
                logger.warning(
                    "task_lease_lost",
                    extra={
                        "recording_id": lease.recording_id,
                        "task_id": lease.task_id,
                    },
                )
                work.cancel()
            else:
                await work
        finally:
            work.cancel()
            heartbeat.cancel()
            await asyncio.gather(work, heartbeat, return_exceptions=True)

    async def _heartbeat(self, lease):
        try:
            while True:
                await asyncio.sleep(self.settings.worker_heartbeat_seconds)
                if not await self.queue.heartbeat(lease):
                    return
        except Exception:
            logger.error(
                "heartbeat_failed",
                extra={
                    "recording_id": lease.recording_id,
                    "task_id": lease.task_id,
                    "error_code": "database_unavailable",
                },
            )

    async def _pipeline(self, lease):
        started = perf_counter()
        try:
            if lease.stage == "transcribing":
                if not await to_thread.run_sync(self.storage.exists, lease.storage_key):
                    raise ProcessingError("audio_missing", "Audio file is unavailable.")
                transcript = await self.asr.transcribe()
                lease = await self.queue.transcribed(lease, transcript)
                if lease is None:
                    return
            if not lease.transcript:
                raise ProcessingError("transcript_missing", "Transcript is unavailable.")
            logger.info(
                "summary_started",
                extra={
                    "recording_id": lease.recording_id,
                    "task_id": lease.task_id,
                },
            )
            result = await self.llm.summarize(lease.transcript)
            await self.queue.complete(lease, result)
        except StorageError:
            await self._fail(lease, "storage_unavailable", "Audio storage is unavailable.")
        except ProcessingError as exc:
            await self._fail(lease, exc.code, exc.message)
        except SQLAlchemyError:
            # Ambiguous DB commits must recover through leases, not be overwritten as failed.
            logger.error(
                "task_execution_interrupted",
                extra={
                    "recording_id": lease.recording_id,
                    "task_id": lease.task_id,
                    "error_code": "execution_interrupted",
                },
            )
        except Exception:
            await self._fail(lease, "processing_failed", "Task processing failed.")
        finally:
            if lease is not None:
                logger.info(
                    "task_execution_finished",
                    extra={
                        "recording_id": lease.recording_id,
                        "task_id": lease.task_id,
                        "duration_ms": round((perf_counter() - started) * 1000, 2),
                    },
                )

    async def _fail(self, lease, code, message):
        try:
            await self.queue.fail(lease, code, message, retryable=True)
        except Exception:
            logger.error(
                "task_failure_write_interrupted",
                extra={
                    "recording_id": lease.recording_id,
                    "task_id": lease.task_id,
                    "error_code": "database_unavailable",
                },
            )

    async def cleanup_once(self):
        async with self.database.sessions() as session:
            ids = (
                await session.scalars(
                    select(Recording.id)
                    .where(Recording.deleted_at.is_not(None))
                    .order_by(Recording.updated_at, Recording.id)
                    .limit(100)
                )
            ).all()
        for recording_id in ids:
            try:
                async with self.database.sessions() as session:
                    await delete_recording(session, self.storage, recording_id)
            except ApiError as exc:
                if exc.status_code != 404:
                    logger.warning(
                        "cleanup_deferred",
                        extra={
                            "recording_id": recording_id,
                            "error_code": exc.code,
                        },
                    )

    async def _cleanup_loop(self, stop):
        while not stop.is_set():
            try:
                await self.cleanup_once()
            except Exception:
                logger.error("cleanup_scan_failed", extra={"error_code": "database_unavailable"})
            await self._wait(stop, self.settings.worker_cleanup_seconds)

    @staticmethod
    async def _wait(stop, seconds):
        try:
            await asyncio.wait_for(stop.wait(), seconds)
        except TimeoutError:
            pass

    async def run(self, stop: asyncio.Event):
        cleanup = asyncio.create_task(self._cleanup_loop(stop))
        logger.info("worker_started")
        try:
            while not stop.is_set():
                for task in tuple(self.active):
                    if task.done():
                        self.active.remove(task)
                        await asyncio.gather(task, return_exceptions=True)
                while len(self.active) < self.settings.worker_concurrency and not stop.is_set():
                    try:
                        lease = await self.queue.claim()
                    except Exception:
                        logger.error(
                            "task_claim_failed", extra={"error_code": "database_unavailable"}
                        )
                        break
                    if lease is None:
                        break
                    if stop.is_set():
                        break  # The unstarted claim recovers when its lease expires.
                    self.active.add(asyncio.create_task(self.process(lease)))
                await self._wait(stop, self.settings.worker_poll_seconds)
        finally:
            cleanup.cancel()
            if self.active:
                _, pending = await asyncio.wait(
                    self.active, timeout=self.settings.worker_shutdown_seconds
                )
                for task in pending:
                    task.cancel()
            await asyncio.gather(cleanup, *self.active, return_exceptions=True)
            self.active.clear()
            logger.info("worker_stopped")
