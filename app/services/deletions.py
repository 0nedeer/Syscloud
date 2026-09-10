import logging
from datetime import UTC, datetime

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import ApiError
from app.models import Recording
from app.storage import LocalStorage, StorageError

logger = logging.getLogger("app.deletions")


async def delete_recording(session: AsyncSession, storage: LocalStorage, recording_id: str) -> None:
    async with session.begin():
        recording = await session.scalar(
            select(Recording).where(Recording.id == recording_id).with_for_update()
        )
        if recording is None:
            raise ApiError(404, "recording_not_found", "Recording not found.")
        if recording.deleted_at is None:
            recording.deleted_at = datetime.now(UTC).replace(tzinfo=None)
        storage_key = recording.storage_key
        # Commit the tombstone before touching the filesystem. No new task or result
        # may treat this recording as live after this transaction commits.
    try:
        await __import__("anyio").to_thread.run_sync(storage.delete, storage_key)
    except StorageError:
        logger.error(
            "recording_delete_storage_failed",
            extra={"recording_id": recording_id, "error_code": "storage_unavailable"},
        )
        raise ApiError(503, "storage_unavailable", "Audio storage is unavailable.") from None
    async with session.begin():
        recording = await session.scalar(
            select(Recording).where(Recording.id == recording_id).with_for_update()
        )
        if recording is None:
            return
        # Recheck the tombstone so this cleanup cannot delete a replacement row.
        if recording.deleted_at is None or recording.storage_key != storage_key:
            return
        await session.execute(delete(Recording).where(Recording.id == recording_id))
    logger.info("recording_deleted", extra={"recording_id": recording_id})
