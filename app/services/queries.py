"""录音与任务查询，统一隐藏删除中的数据并选择最新任务。"""

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import ApiError
from app.models import Recording, Task
from app.schemas import (
    RecordingDetailResponse,
    RecordingListItem,
    RecordingListResponse,
    RecordingListTask,
    TaskError,
    TaskResponse,
)


# 把 ORM 任务映射成对外数据：等待重试看 next_attempt_at，终态失败看 error。
# 摘要仅在 done 时发布，过程中出现的文本不能作为通过校验的最终结果。
def task_response(task: Task) -> TaskResponse:
    return TaskResponse(
        task_id=task.id,
        recording_id=task.recording_id,
        status=task.status,
        attempt_no=task.attempt_no,
        retry_of_task_id=task.retry_of_task_id,
        auto_retry_count=task.auto_retry_count,
        retry_waiting=task.next_attempt_at is not None,
        next_attempt_at=task.next_attempt_at,
        last_error=TaskError(code=task.last_error_code, message=task.last_error_message)
        if task.last_error_code and task.last_error_message
        else None,
        transcript=task.transcript,
        summary_result=task.summary_result if task.status == "done" else None,
        error=TaskError(
            code=task.error_code or "processing_failed",
            message=task.error_message or "Task processing failed.",
        )
        if task.status == "failed"
        else None,
        created_at=task.created_at,
        started_at=task.started_at,
        finished_at=task.finished_at,
    )


# 任务必须关联一条未标记删除的录音；不存在和已删除都对外返回 404。
async def get_task(session: AsyncSession, task_id: str) -> TaskResponse:
    task = await session.scalar(
        select(Task).join(Recording).where(Task.id == task_id, Recording.deleted_at.is_(None))
    )
    if task is None:
        raise ApiError(404, "task_not_found", "Task not found.")
    return task_response(task)


# 列表和详情共用最新任务定义：按尝试序号选择，不依赖创建时间。
def _latest_task_id():
    return (
        select(Task.id)
        .where(Task.recording_id == Recording.id)
        .order_by(Task.attempt_no.desc())
        .limit(1)
        .correlate(Recording)
        .scalar_subquery()
    )


# 用单条 SQL 同时读取录音和最大 attempt_no 的任务，避免两个查询读到不同版本。
async def get_recording(session: AsyncSession, recording_id: str) -> RecordingDetailResponse:
    row = (
        await session.execute(
            select(Recording, Task)
            .outerjoin(Task, Task.id == _latest_task_id())
            .where(Recording.id == recording_id, Recording.deleted_at.is_(None))
        )
    ).first()
    if row is None:
        raise ApiError(404, "recording_not_found", "Recording not found.")
    recording, task = row
    return RecordingDetailResponse(
        recording_id=recording.id,
        original_filename=recording.original_filename,
        size_bytes=recording.size_bytes,
        created_at=recording.created_at,
        latest_task=task_response(task) if task else None,
        transcript=task.transcript if task else None,
        summary_result=task.summary_result if task and task.status == "done" else None,
    )


# 列表只取元数据和最新任务概况，不加载可能很大的转写、摘要，也不逐行额外查询。
async def list_recordings(
    session: AsyncSession, page: int, page_size: int
) -> RecordingListResponse:
    total = await session.scalar(
        select(func.count()).select_from(Recording).where(Recording.deleted_at.is_(None))
    )
    offset = (page - 1) * page_size
    if offset >= total:
        return RecordingListResponse(items=[], page=page, page_size=page_size, total=total)
    rows = (
        await session.execute(
            select(
                Recording.id,
                Recording.original_filename,
                Recording.size_bytes,
                Recording.created_at,
                Task.id.label("task_id"),
                Task.status,
                Task.attempt_no,
            )
            .outerjoin(Task, Task.id == _latest_task_id())
            .where(Recording.deleted_at.is_(None))
            .order_by(Recording.created_at.desc(), Recording.id.desc())
            .offset(offset)
            .limit(page_size)
        )
    ).all()
    items = [
        RecordingListItem(
            recording_id=row.id,
            original_filename=row.original_filename,
            size_bytes=row.size_bytes,
            created_at=row.created_at,
            latest_task=RecordingListTask(
                task_id=row.task_id, status=row.status, attempt_no=row.attempt_no
            )
            if row.task_id
            else None,
        )
        for row in rows
    ]
    return RecordingListResponse(items=items, page=page, page_size=page_size, total=total)
