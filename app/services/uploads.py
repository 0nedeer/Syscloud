import asyncio
import logging

from anyio import to_thread
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from starlette.datastructures import UploadFile

from app.db import Database
from app.errors import ApiError
from app.models import Recording, Task
from app.schemas import RecordingCreateResponse
from app.storage import AudioTooLarge, LocalStorage, StorageError, validate_extension

logger = logging.getLogger("app.uploads")


class HashConflict(Exception):
    """The recording hash unique constraint selected another upload."""


async def _find_duplicate(database, content_hash):
    async with database.sessions() as session, session.begin():
        recording = await session.scalar(
            select(Recording).where(Recording.content_sha256 == content_hash).with_for_update()
        )
        if recording is None:
            return None  # Deleted after the conflicting INSERT: retry creation with our file.
        if recording.deleted_at is not None:
            raise ApiError(409, "recording_deleting", "Recording deletion is in progress.")
        task = await session.scalar(
            select(Task)
            .where(Task.recording_id == recording.id, Task.attempt_no == 1)
            .with_for_update()
        )
        if task is None:
            raise ApiError(409, "upload_conflict", "Upload state changed; please retry.")
        return RecordingCreateResponse(
            recording_id=recording.id, task_id=task.id, status=task.status
        )


async def _discard_duplicate(storage, key):
    try:
        await to_thread.run_sync(storage.delete, key)
    except StorageError:
        logger.error("upload_cleanup_failed", extra={"storage_key": key})
        raise ApiError(503, "storage_unavailable", "Upload cleanup is unavailable.") from None


async def create_recording(database: Database, storage: LocalStorage, file: UploadFile):
    # Keep ownership of the upload spool until disk/DB work has actually stopped.
    # Cancellation of an await cannot safely stop an OS write in another thread.
    operation = asyncio.create_task(_create_recording(database, storage, file))
    cancelled = False
    while True:
        try:
            result = await asyncio.shield(operation)
        except asyncio.CancelledError:
            cancelled = True
            if operation.cancelled():
                raise
        except Exception:
            if cancelled:
                raise asyncio.CancelledError from None
            raise
        else:
            if cancelled:
                raise asyncio.CancelledError
            return result


async def _find_committed_upload(database, result, storage_key) -> bool:
    # A fresh session avoids the failed transaction's identity map/snapshot.
    # Absence is not proof of rollback: an in-flight COMMIT may still complete.
    try:
        async with database.sessions() as session:
            task_id = await session.scalar(
                select(Task.id)
                .join(Recording)
                .where(
                    Recording.id == str(result.recording_id),
                    Recording.storage_key == storage_key,
                    Recording.deleted_at.is_(None),
                    Task.id == str(result.task_id),
                    Task.attempt_no == 1,
                )
            )
            return task_id is not None
    except Exception:
        return False


async def _create_recording(database: Database, storage: LocalStorage, file: UploadFile):
    try:
        extension = validate_extension(file.filename)
    except ValueError as exc:
        raise ApiError(400, "invalid_audio", str(exc)) from None
    try:
        stored = await storage.save(file, extension)
    except AudioTooLarge:
        raise ApiError(413, "audio_too_large", "Audio file must not exceed 50 MB.") from None
    except ValueError:
        raise ApiError(400, "empty_audio", "Audio file must not be empty.") from None
    except StorageError:
        logger.error("upload_storage_failed", extra={"error_code": "storage_unavailable"})
        raise ApiError(503, "storage_unavailable", "Audio storage is unavailable.") from None

    # Filenames are display metadata only. Normalize both Windows and POSIX separators.
    filename = (file.filename or "audio").replace("\\", "/").rsplit("/", 1)[-1]
    filename = "".join(char for char in filename if char.isprintable())[:255]
    for _ in range(3):
        try:
            return await _commit_upload(database, storage, stored, filename), True
        except HashConflict:
            try:
                result = await _find_duplicate(database, stored.content_sha256)
            except BaseException:
                await _discard_duplicate(storage, stored.storage_key)
                raise
            if result is not None:
                await _discard_duplicate(storage, stored.storage_key)
                logger.info(
                    "upload_deduplicated",
                    extra={
                        "recording_id": str(result.recording_id),
                        "task_id": str(result.task_id),
                    },
                )
                return result, False
    await _discard_duplicate(storage, stored.storage_key)
    raise ApiError(409, "upload_conflict", "Concurrent upload changed; please retry.")


async def _commit_upload(database, storage, stored, filename):
    commit_attempted = False
    try:
        async with database.sessions() as session, session.begin():
            recording = Recording(
                original_filename=filename,
                storage_key=stored.storage_key,
                size_bytes=stored.size_bytes,
                content_sha256=stored.content_sha256,
            )
            session.add(recording)
            try:
                await session.flush()
            except IntegrityError as exc:
                if exc.orig.args[0] == 1062 and "uq_recordings_content_sha256" in str(exc.orig):
                    raise HashConflict from None
                raise
            task = Task(recording_id=recording.id)
            session.add(task)
            await session.flush()
            result = RecordingCreateResponse(recording_id=recording.id, task_id=task.id)
            commit_attempted = True
    except BaseException as exc:
        if isinstance(exc, HashConflict):
            raise  # Caller still owns this file and may retry after a concurrent deletion.
        if not commit_attempted:
            try:
                await to_thread.run_sync(storage.delete, stored.storage_key)
            except StorageError:
                logger.error(
                    "upload_cleanup_failed",
                    extra={"storage_key": stored.storage_key, "error_code": "storage_unavailable"},
                )
        else:
            context = {
                "recording_id": str(result.recording_id),
                "task_id": str(result.task_id),
                "storage_key": stored.storage_key,
            }
            confirmed = isinstance(exc, Exception) and await _find_committed_upload(
                database, result, stored.storage_key
            )
            if confirmed:
                logger.warning("upload_commit_reconciled", extra=context)
            else:
                logger.error(
                    "upload_commit_uncertain", extra={**context, "error_code": "commit_uncertain"}
                )
                raise
        if not commit_attempted:
            raise
    logger.info("recording_uploaded", extra={"recording_id": recording.id, "task_id": task.id})
    logger.info(
        "task_created",
        extra={"recording_id": recording.id, "task_id": task.id, "new_status": "pending"},
    )
    return result
