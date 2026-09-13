"""为失败任务创建幂等后继，保留原任务的处理历史。"""

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import ApiError
from app.models import Recording, Task
from app.schemas import RetryResponse

logger = logging.getLogger("app.retries")


# 按录音→任务的统一顺序加行锁，再检查来源状态和既有后继。
# 重复请求返回已创建的直接后继，数据库唯一约束进一步限制同一来源只能重试出一个任务。
async def retry_task(session: AsyncSession, task_id: str) -> tuple[RetryResponse, bool]:
    async with session.begin():
        recording_id = await session.scalar(select(Task.recording_id).where(Task.id == task_id))
        if recording_id is None:
            raise ApiError(404, "task_not_found", "Task not found.")
        # 所有变更先锁定父记录，再锁定任务记录；加锁查询使用当前读，避免复用发现查询的旧快照。
        recording = await session.scalar(
            select(Recording).where(Recording.id == recording_id).with_for_update()
        )
        if recording is None or recording.deleted_at is not None:
            raise ApiError(404, "task_not_found", "Task not found.")
        source = await session.scalar(select(Task).where(Task.id == task_id).with_for_update())
        if source is None:
            raise ApiError(404, "task_not_found", "Task not found.")
        if source.status != "failed":
            raise ApiError(409, "task_not_retryable", "Only failed tasks can be retried.")
        successor = await session.scalar(
            select(Task).where(Task.retry_of_task_id == task_id).with_for_update()
        )
        created = successor is None
        # 新任务使用独立 ID，attempt_no 加一，自动重试次数从默认 0 开始。
        # 这里不复制旧转写，因此手动重试会重新走转写和摘要流程。
        if created:
            successor = Task(
                recording_id=recording_id,
                attempt_no=source.attempt_no + 1,
                retry_of_task_id=task_id,
            )
            session.add(successor)
            await session.flush()
        result = RetryResponse(
            recording_id=recording_id,
            task_id=successor.id,
            status=successor.status,
            attempt_no=successor.attempt_no,
        )
    logger.info(
        "task_retried" if created else "task_retry_replayed",
        extra={
            "recording_id": recording_id,
            "task_id": str(result.task_id),
            "new_status": result.status,
        },
    )
    return result, created
