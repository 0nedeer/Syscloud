"""独立 Worker 的并发调度、处理流水线与恢复清理。"""

import asyncio
import logging
import signal
from time import perf_counter

from anyio import to_thread
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from app.config import WorkerSettings
from app.db import Database
from app.errors import ApiError
from app.logging import configure_logging
from app.models import Recording
from app.processing.llm import LLMProvider
from app.processing.providers import MockASR, ProcessingError
from app.processing.queue import Lease, TaskQueue
from app.services.deletions import delete_recording
from app.storage import LocalStorage, StorageError

logger = logging.getLogger("app.worker")


class Worker:
    def __init__(
        self,
        database: Database,
        storage: LocalStorage,
        settings: WorkerSettings,
        llm: LLMProvider,
        asr: MockASR | None = None,
    ) -> None:
        self.database, self.storage, self.settings = database, storage, settings
        self.llm, self.asr = llm, asr or MockASR()
        self.queue = TaskQueue(
            database, settings.worker_lease_seconds, settings.worker_retry_base_seconds
        )
        self.active: set[asyncio.Task[None]] = set()

    # 心跳丢失意味着所有权不再可靠，必须取消外部调用并回收协程。
    async def process(self, lease: Lease) -> None:
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

    async def _heartbeat(self, lease: Lease) -> None:
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

    # 从持久化阶段恢复；摘要阶段复用已存转写，避免重复调用 ASR。
    # 提交结果不确定时保留租约状态，由后续领取决定恢复，避免覆盖可能已成功的结果。
    async def _pipeline(self, lease: Lease) -> None:
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
        # 连接错误不代表事务一定回滚；保留状态交由后续领取确认。
        except SQLAlchemyError:
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

    # 可恢复错误交给队列重试；写库失败时只记录脱敏信息。
    async def _fail(self, lease: Lease, code: str, message: str) -> None:
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

    async def cleanup_once(self) -> None:
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

    async def _cleanup_loop(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await self.cleanup_once()
            except Exception:
                logger.error("cleanup_scan_failed", extra={"error_code": "database_unavailable"})
            await self._wait(stop, self.settings.worker_cleanup_seconds)

    @staticmethod
    async def _wait(stop: asyncio.Event, seconds: float) -> None:
        try:
            await asyncio.wait_for(stop.wait(), seconds)
        except TimeoutError:
            pass

    # 退出时停止领取并给予活动任务宽限期；未完成任务依赖租约恢复。
    async def run(self, stop: asyncio.Event) -> None:
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


# SIGINT/SIGTERM 只通知停止，实际协程回收由 Worker.run 完成，最后关闭外部资源。
async def serve() -> None:
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
