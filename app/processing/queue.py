"""Short MySQL transactions fence every write with a live lease and recording."""

import logging
from dataclasses import dataclass, replace
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import and_, func, or_, select

from app.db import Database
from app.models import Recording, Task
from app.schemas import SummaryResult

logger = logging.getLogger("app.queue")
ACTIVE = ("transcribing", "summarizing")
MAX_AUTO_RETRIES = 3


@dataclass(frozen=True)
class Lease:
    recording_id: str
    task_id: str
    token: str
    stage: str
    storage_key: str
    transcript: str | None


class TaskQueue:
    def __init__(self, database: Database, lease_seconds: int, retry_base_seconds: float = 2):
        self.database = database
        self.lease_seconds = lease_seconds
        self.retry_base_seconds = retry_base_seconds

    async def claim(self) -> Lease | None:
        eligible = and_(
            or_(Task.next_attempt_at.is_(None), Task.next_attempt_at <= func.utc_timestamp(6)),
            or_(
                Task.status == "pending",
                and_(
                    Task.status.in_(ACTIVE),
                    or_(
                        Task.lease_expires_at.is_(None),
                        Task.lease_expires_at <= func.utc_timestamp(6),
                    ),
                ),
            ),
        )
        # Discover without locking tasks first: retry/delete lock the recording first too.
        async with self.database.sessions() as session:
            candidates = (
                await session.execute(
                    select(Task.id, Task.recording_id)
                    .join(Recording)
                    .where(Recording.deleted_at.is_(None), eligible)
                    .order_by(Task.created_at, Task.id)
                    .limit(100)
                )
            ).all()
        for task_id, recording_id in candidates:
            async with self.database.sessions() as session, session.begin():
                recording = await session.scalar(
                    select(Recording)
                    .where(Recording.id == recording_id, Recording.deleted_at.is_(None))
                    .with_for_update(skip_locked=True)
                )
                if recording is None:
                    continue
                task = await session.scalar(
                    select(Task)
                    .where(Task.id == task_id, eligible)
                    .with_for_update(skip_locked=True)
                )
                if task is None:
                    continue
                now = await session.scalar(select(func.utc_timestamp(6)))
                previous = task.status
                retrying = task.next_attempt_at is not None
                if previous == "pending":
                    task.status = "transcribing"
                    task.started_at = now
                elif not retrying:
                    task.recovery_count += 1
                task.next_attempt_at = None
                task.lease_token = str(uuid4())
                task.lease_expires_at = now + timedelta(seconds=self.lease_seconds)
                lease = Lease(
                    recording.id,
                    task.id,
                    task.lease_token,
                    task.status,
                    recording.storage_key,
                    task.transcript,
                )
            logger.info(
                "task_retry_started"
                if retrying
                else ("task_recovered" if previous in ACTIVE else "task_claimed"),
                extra={
                    "recording_id": recording_id,
                    "task_id": task_id,
                    "previous_status": previous,
                    "new_status": lease.stage,
                },
            )
            return lease
        return None

    async def _owned(self, session, lease):
        recording = await session.scalar(
            select(Recording)
            .where(Recording.id == lease.recording_id, Recording.deleted_at.is_(None))
            .with_for_update()
        )
        if recording is None:
            return None, None
        task = await session.scalar(
            select(Task)
            .where(Task.id == lease.task_id, Task.recording_id == lease.recording_id)
            .with_for_update()
        )
        now = await session.scalar(select(func.utc_timestamp(6)))
        if (
            task is None
            or task.status not in ACTIVE
            or task.lease_token != lease.token
            or task.lease_expires_at is None
            or task.lease_expires_at <= now
        ):
            return None, now
        return task, now

    async def heartbeat(self, lease: Lease) -> bool:
        async with self.database.sessions() as session, session.begin():
            task, now = await self._owned(session, lease)
            if task is None:
                return False
            task.lease_expires_at = now + timedelta(seconds=self.lease_seconds)
        return True

    async def transcribed(self, lease: Lease, transcript: str) -> Lease | None:
        if not transcript.strip():
            raise ValueError("Transcript must not be empty")
        async with self.database.sessions() as session, session.begin():
            task, now = await self._owned(session, lease)
            if task is None or task.status != "transcribing":
                return None
            task.transcript = transcript
            task.last_error_code = task.last_error_message = None
            task.status = "summarizing"
            task.lease_expires_at = now + timedelta(seconds=self.lease_seconds)
        self._log_transition(lease, "summarizing")
        return replace(lease, stage="summarizing", transcript=transcript)

    async def complete(self, lease: Lease, result: SummaryResult) -> bool:
        validated = SummaryResult.model_validate(result.model_dump())
        async with self.database.sessions() as session, session.begin():
            task, now = await self._owned(session, lease)
            if task is None or task.status != "summarizing":
                return False
            task.summary_result = validated.model_dump()
            task.status = "done"
            task.error_code = task.error_message = None
            task.last_error_code = task.last_error_message = None
            self._finish(task, now)
        self._log_transition(lease, "done")
        return True

    async def fail(self, lease: Lease, code: str, message: str, *, retryable=False) -> bool:
        async with self.database.sessions() as session, session.begin():
            task, now = await self._owned(session, lease)
            if task is None or task.status != lease.stage:
                return False
            retry = retryable and task.auto_retry_count < MAX_AUTO_RETRIES
            if retry:
                delay = self.retry_base_seconds * 2**task.auto_retry_count
                task.auto_retry_count += 1
                task.last_error_code, task.last_error_message = code, message
                task.next_attempt_at = now + timedelta(seconds=delay)
                task.lease_token = task.lease_expires_at = None
                count = task.auto_retry_count
                deadline = task.next_attempt_at.isoformat() + "Z"
            else:
                task.status = "failed"
                task.error_code, task.error_message = code, message
                task.last_error_code = task.last_error_message = None
                self._finish(task, now)
        if retry:
            logger.info(
                "task_retry_scheduled",
                extra={
                    "recording_id": lease.recording_id,
                    "task_id": lease.task_id,
                    "error_code": code,
                    "auto_retry_count": count,
                    "next_attempt_at": deadline,
                },
            )
        else:
            self._log_transition(lease, "failed", code)
        return True

    @staticmethod
    def _finish(task, now):
        task.finished_at = now
        task.next_attempt_at = None
        task.lease_token = task.lease_expires_at = None

    @staticmethod
    def _log_transition(lease, status, code=None):
        logger.info(
            "task_transition",
            extra={
                "recording_id": lease.recording_id,
                "task_id": lease.task_id,
                "previous_status": lease.stage,
                "new_status": status,
                "error_code": code,
            },
        )
