"""保存音频、原子创建录音和任务，并处理去重与提交结果确认。"""

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
from app.storage import AudioTooLarge, LocalStorage, StorageError, StoredUpload, validate_extension

logger = logging.getLogger("app.uploads")


# 只表示录音内容哈希的唯一约束冲突；其他数据库完整性错误不能当作重复上传。
class HashConflict(Exception):
    """The recording hash unique constraint selected another upload."""


# 冲突后用新事务查询胜出的录音，并返回它的初始任务（attempt_no=1）。
# 删除标记仍在时返回 409，防止旧文件尚未清理就复用相同内容。
async def _find_duplicate(database: Database, content_hash: str) -> RecordingCreateResponse | None:
    # 使用独立事务读取冲突记录，避免继续使用刚刚失败的 INSERT 所在事务的快照。
    async with database.sessions() as session, session.begin():
        # 按内容哈希加行锁：并发上传相同音频时，所有请求最终都观察同一条“胜出”记录。
        recording = await session.scalar(
            select(Recording).where(Recording.content_sha256 == content_hash).with_for_update()
        )
        if recording is None:
            # 冲突记录可能在查询前已被删除；返回 None 让上层重新尝试创建，而不是误报重复。
            return None  # Deleted after the conflicting INSERT: retry creation with our file.
        if recording.deleted_at is not None:
            # 删除流程尚未完成时不能复用同一哈希，否则新文件可能与旧文件清理互相覆盖。
            raise ApiError(409, "recording_deleting", "Recording deletion is in progress.")
        # 只取首次尝试的任务；去重接口必须返回最初任务，而不是后续手动重试任务。
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


# 仅清理本次上传产生的多余文件；绝不能删除去重胜出录音的文件。
async def _discard_duplicate(storage: LocalStorage, key: str) -> None:
    try:
        await to_thread.run_sync(storage.delete, key)
    except StorageError:
        logger.error("upload_cleanup_failed", extra={"storage_key": key})
        raise ApiError(503, "storage_unavailable", "Upload cleanup is unavailable.") from None


# 保护整个保存操作，使客户端断开后仍等磁盘写入和事务结束再释放上传文件。
# shield 只阻止取消传播给内部任务；外层仍记住取消，完成善后后再向上传播。
async def create_recording(
    database: Database, storage: LocalStorage, file: UploadFile
) -> tuple[RecordingCreateResponse, bool]:
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


# COMMIT 应答丢失时，换会话查询是否其实已提交成功。
# 查到可确认成功；查不到不能证明回滚，所以调用者必须保留可能已被引用的文件。
async def _find_committed_upload(
    database: Database, result: RecordingCreateResponse, storage_key: str
) -> bool:
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


# 先验证并保存文件，再处理去重与数据库事务；文件名只作展示元数据。
# 三次循环是应对“冲突对象随即被删除”的上传竞态，不是 Worker 的自动重试。
async def _create_recording(
    database: Database, storage: LocalStorage, file: UploadFile
) -> tuple[RecordingCreateResponse, bool]:
    try:
        extension = validate_extension(file.filename)
    except ValueError as exc:
        raise ApiError(400, "invalid_audio", str(exc)) from None
    try:
        # save 会分块读取上传内容、计算 SHA-256 并写入临时/最终 key。
        stored = await storage.save(file, extension)
    except AudioTooLarge:
        raise ApiError(413, "audio_too_large", "Audio file must not exceed 50 MB.") from None
    except ValueError:
        raise ApiError(400, "empty_audio", "Audio file must not be empty.") from None
    except StorageError:
        logger.error("upload_storage_failed", extra={"error_code": "storage_unavailable"})
        raise ApiError(503, "storage_unavailable", "Audio storage is unavailable.") from None

    # 文件名仅保存到数据库供界面展示；去掉路径部分，防止把客户端路径带入业务数据。
    filename = (file.filename or "audio").replace("\\", "/").rsplit("/", 1)[-1]
    filename = "".join(char for char in filename if char.isprintable())[:255]
    # 哈希唯一键冲突后重新读取胜出记录；最多重试三轮以覆盖“冲突记录刚被删除”的竞态。
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


# 提交前失败可删本次文件；提交结果不确定时保留文件并尝试核实，交由审计处理残留。
async def _commit_upload(
    database: Database, storage: LocalStorage, stored: StoredUpload, filename: str
) -> RecordingCreateResponse:
    # False 表示事务仍可确定回滚；True 表示 COMMIT 可能已发出，失败后不能贸然删文件。
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
                # flush 将 INSERT 发到数据库并获取默认字段，但尚未提交；哈希冲突在这里被识别。
                await session.flush()
            except IntegrityError as exc:
                if exc.orig.args[0] == 1062 and "uq_recordings_content_sha256" in str(exc.orig):
                    raise HashConflict from None
                raise
            # 初始任务与录音同事务创建，保证不会出现“有文件/录音却没有可处理任务”。
            task = Task(recording_id=recording.id)
            session.add(task)
            await session.flush()
            result = RecordingCreateResponse(recording_id=recording.id, task_id=task.id)
            # 退出 session.begin() 才会提交；先记录已进入提交阶段，以区分可回滚和结果不明两种错误。
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
