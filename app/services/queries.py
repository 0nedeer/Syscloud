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


def task_response(task: Task) -> TaskResponse:
    return TaskResponse(
        task_id=task.id,
        recording_id=task.recording_id,
        status=task.status,
        attempt_no=task.attempt_no,
        retry_of_task_id=task.retry_of_task_id,
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


async def get_task(session: AsyncSession, task_id: str) -> TaskResponse:
    task = await session.scalar(
        select(Task).join(Recording).where(Task.id == task_id, Recording.deleted_at.is_(None))
    )
    if task is None:
        raise ApiError(404, "task_not_found", "Task not found.")
    return task_response(task)


async def get_recording(session: AsyncSession, recording_id: str) -> RecordingDetailResponse:
    latest = (
        select(Task.id)
        .where(Task.recording_id == Recording.id)
        .order_by(Task.attempt_no.desc())
        .limit(1)
        .correlate(Recording)
        .scalar_subquery()
    )
    row = (
        await session.execute(
            select(Recording, Task)
            .outerjoin(Task, Task.id == latest)
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


async def list_recordings(session: AsyncSession, page: int, page_size: int):
    total = await session.scalar(
        select(func.count()).select_from(Recording).where(Recording.deleted_at.is_(None))
    )
    offset = (page - 1) * page_size
    if offset >= total:
        return RecordingListResponse(items=[], page=page, page_size=page_size, total=total)
    latest = (
        select(Task.id)
        .where(Task.recording_id == Recording.id)
        .order_by(Task.attempt_no.desc())
        .limit(1)
        .correlate(Recording)
        .scalar_subquery()
    )
    # Select metadata only: list pages must not load audio transcripts or model output.
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
            .outerjoin(Task, Task.id == latest)
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
