from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import ApiError
from app.models import Recording, Task
from app.schemas import RecordingDetailResponse, TaskError, TaskResponse


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
