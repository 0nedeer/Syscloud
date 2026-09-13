"""用短事务和租约令牌管理任务领取、续租、结果及自动重试。"""

import logging
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from uuid import uuid4

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import Database
from app.models import Recording, Task
from app.schemas import SummaryResult

logger = logging.getLogger("app.queue")
ACTIVE = ("transcribing", "summarizing")
MAX_AUTO_RETRIES = 3


# 领取结果的不可变快照，不是长期持有的数据库锁。
# token 标识本次所有权；恢复领取会换新 token，使旧进程的延迟结果失效。
@dataclass(frozen=True)
class Lease:
    recording_id: str
    task_id: str
    token: str
    stage: str
    storage_key: str
    transcript: str | None


# 任务状态写入的统一入口；耗时 ASR/LLM 在事务外，只有领取/续租/提交时加锁。
class TaskQueue:
    def __init__(
        self, database: Database, lease_seconds: int, retry_base_seconds: float = 2
    ) -> None:
        self.database = database
        self.lease_seconds = lease_seconds
        self.retry_base_seconds = retry_base_seconds

    # 先发现候选，再逐个加锁复查：候选查询与真正领取之间可能已被其他进程处理。
    # 可领取 pending、到期自动重试或租约失效的处理中任务；SKIP LOCKED 跳过被占用行。
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
                # 每次领取都更换 token；即便是同一 Worker 再次领取，也不能沿用旧所有权。
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

    # 统一写入门禁：录音未删除、任务处于处理中、令牌匹配且租约未过期才可修改。
    # 返回 (任务, 数据库当前时间)；无所有权时返回空任务，让调用者停止旧执行。
    async def _owned(
        self, session: AsyncSession, lease: Lease
    ) -> tuple[Task | None, datetime | None]:
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

    # 续租也必须先通过所有权校验；过期租约不能靠旧 Worker 自行复活。
    async def heartbeat(self, lease: Lease) -> bool:
        async with self.database.sessions() as session, session.begin():
            task, now = await self._owned(session, lease)
            if task is None:
                return False
            task.lease_expires_at = now + timedelta(seconds=self.lease_seconds)
        return True

    # 先持久化非空转写，再把阶段推进到 summarizing，并返回更新后的租约快照。
    # 此事务成功后即使进程崩溃，恢复时也能复用 transcript，跳过 ASR。
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

    # 返回 False 表示租约或阶段已变，不能强行覆盖其他执行的结果。
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

    # 可重试错误在原任务上安排下次执行；转写和摘要共享最多 3 次自动重试。
    # 默认退避 2/4/8 秒，预算和到期时间落库，进程重启不会把它们清零。
    async def fail(self, lease: Lease, code: str, message: str, *, retryable: bool = False) -> bool:
        async with self.database.sessions() as session, session.begin():
            task, now = await self._owned(session, lease)
            if task is None or task.status != lease.stage:
                return False
            retry = retryable and task.auto_retry_count < MAX_AUTO_RETRIES
            if retry:
                delay = self.retry_base_seconds * 2**task.auto_retry_count
                task.auto_retry_count += 1
                task.last_error_code, task.last_error_message = code, message
                # 退避只存一个到期时间，不让处理协程 sleep 等待，因此不会占用并发处理槽。
                task.next_attempt_at = now + timedelta(seconds=delay)
                # 释放租约但保持失败时的阶段：摘要重试仍是 summarizing，转写结果得以复用。
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

    # 终态统一记录完成时间，清除租约和重试到期时间，使任务不再参与领取。
    @staticmethod
    def _finish(task: Task, now: datetime) -> None:
        task.finished_at = now
        task.next_attempt_at = None
        task.lease_token = task.lease_expires_at = None

    # 用录音 ID、任务 ID 和前后状态串联生命周期，不记录转写/摘要正文。
    @staticmethod
    def _log_transition(lease: Lease, status: str, code: str | None = None) -> None:
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
