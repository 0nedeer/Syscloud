import asyncio
import logging

from anyio import to_thread
from starlette.datastructures import UploadFile

from app.db import Database
from app.errors import ApiError
from app.models import Recording, Task
from app.schemas import RecordingCreateResponse
from app.storage import AudioTooLarge, LocalStorage, StorageError, validate_extension

logger = logging.getLogger("app.uploads")


async def create_recording(database: Database, storage: LocalStorage, file: UploadFile):
    # Finish the bounded file/DB operation if a client disconnects during an await.
    # A committed upload remains valid even if its response can no longer be delivered.
    operation = asyncio.create_task(_create_recording(database, storage, file))
    try:
        return await asyncio.shield(operation)
    except asyncio.CancelledError:
        try:
            await asyncio.shield(operation)
        finally:
            raise


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
    commit_attempted = False
    try:
        async with database.sessions() as session, session.begin():
            recording = Recording(
                original_filename=filename,
                storage_key=stored.storage_key,
                size_bytes=stored.size_bytes,
            )
            session.add(recording)
            await session.flush()
            task = Task(recording_id=recording.id)
            session.add(task)
            await session.flush()
            result = RecordingCreateResponse(recording_id=recording.id, task_id=task.id)
            commit_attempted = True
    except BaseException:
        if not commit_attempted:
            try:
                await to_thread.run_sync(storage.delete, stored.storage_key)
            except StorageError:
                logger.error("upload_cleanup_failed", extra={"error_code": "storage_unavailable"})
        else:
            # A lost COMMIT acknowledgement is ambiguous: never delete a possibly referenced file.
            logger.error("upload_commit_uncertain", extra={"error_code": "commit_uncertain"})
        raise
    logger.info("recording_uploaded", extra={"recording_id": recording.id, "task_id": task.id})
    logger.info(
        "task_created",
        extra={"recording_id": recording.id, "task_id": task.id, "new_status": "pending"},
    )
    return result
