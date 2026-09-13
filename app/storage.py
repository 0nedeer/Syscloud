"""受控音频文件存储、内容哈希及只读引用对账。"""

from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import BinaryIO
from uuid import uuid4

from anyio import to_thread
from starlette.datastructures import UploadFile

MAX_AUDIO_BYTES = 50 * 1024 * 1024
ALLOWED_EXTENSIONS = frozenset({".wav", ".mp3", ".m4a", ".aac"})
CHUNK_SIZE = 1024 * 1024
logger = logging.getLogger("app.storage")


# 把底层磁盘错误包装成不含绝对路径的异常，由业务层映射到 503。
class StorageError(Exception):
    """A filesystem operation failed without exposing its path to callers."""


# 单独区分超限，上传业务据此返回 413，而非一般非法文件的 400。
class AudioTooLarge(ValueError):
    pass


# 文件保存结果只返回相对存储键、实际字节数和哈希，供录音事务落库。
@dataclass(frozen=True)
class StoredUpload:
    storage_key: str
    size_bytes: int
    content_sha256: str


# 扩展名仅用于限制存储类型，不作为音频真实性证明。
def validate_extension(filename: str | None) -> str:
    suffix = Path(filename or "").suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        raise ValueError("Audio extension must be one of wav, mp3, m4a or aac.")
    return suffix


# 根目录启动时转绝对路径；所有业务文件访问都通过 storage_key 限制在根目录下。
class LocalStorage:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    # 拒绝 ../、绝对路径及子目录，要求解析后的父目录恰好等于存储根目录。
    def _safe_path(self, storage_key: str) -> Path:
        candidate = (self.root / storage_key).resolve()
        if self.root != candidate.parent or candidate.name != storage_key:
            raise StorageError("Invalid storage key.")
        return candidate

    # 把同步磁盘 I/O 放到线程，避免堵住 asyncio；调用方仍拥有上传临时流的生命周期。
    async def save(self, source: UploadFile, extension: str) -> StoredUpload:
        """Disk work runs off the event loop; the caller owns the uploaded spool."""
        return await to_thread.run_sync(self._save, source.file, extension)

    def _save(self, source: BinaryIO, extension: str) -> StoredUpload:
        if extension not in ALLOWED_EXTENSIONS:
            raise ValueError("Invalid audio extension.")
        storage_key = f"{uuid4()}{extension}"
        final_path = self._safe_path(storage_key)
        size = 0
        digest = hashlib.sha256()
        temporary_path: Path | None = None
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            with NamedTemporaryFile(dir=self.root, prefix=".upload-", delete=False) as temporary:
                temporary_path = Path(temporary.name)
                while True:
                    chunk = source.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > MAX_AUDIO_BYTES:
                        raise AudioTooLarge("Audio file must not exceed 50 MB.")
                    digest.update(chunk)
                    temporary.write(chunk)
            if size == 0:
                raise ValueError("Audio file must not be empty.")
            # 同一目录内原子替换，让最终文件名只对应完整内容；这不等于与数据库一起原子提交。
            os.replace(temporary_path, final_path)
            temporary_path = None
            return StoredUpload(storage_key, size, digest.hexdigest())
        except ValueError:
            raise
        except Exception as exc:
            raise StorageError("Unable to save audio file.") from exc
        finally:
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    logger.error(
                        "upload_cleanup_failed", extra={"error_code": "storage_unavailable"}
                    )

    # 文件不存在也算完成，方便删除请求和 Worker 补偿反复执行。
    def delete(self, storage_key: str) -> None:
        try:
            self._safe_path(storage_key).unlink(missing_ok=True)
        except OSError as exc:
            raise StorageError("Unable to delete audio file.") from exc

    # 领取后、转写前确认音频仍在；文件系统异常与普通不存在分别处理。
    def exists(self, storage_key: str) -> bool:
        try:
            return self._safe_path(storage_key).is_file()
        except OSError as exc:
            raise StorageError("Unable to inspect audio file.") from exc


# 不自动删除任何内容；在线上传期间看到的差异可能只是暂时状态。


# 三个分类：被引用但缺失、未引用最终文件、以 .upload- 开头的上传临时文件。
@dataclass(frozen=True)
class StorageAudit:
    referenced_missing: tuple[str, ...]
    unreferenced_files: tuple[str, ...]
    temporary_files: tuple[str, ...]


# 扫描存储目录的直接文件，通过集合差集找不一致，并排序以方便复核。
def audit_storage(storage: LocalStorage, referenced_keys: set[str]) -> StorageAudit:
    """Compare committed database keys with files without deleting anything."""
    files: set[str] = set()
    temporary: set[str] = set()
    if storage.root.exists():
        for path in storage.root.iterdir():
            if not path.is_file():
                continue
            if path.name.startswith(".upload-"):
                temporary.add(path.name)
            else:
                files.add(path.name)
    return StorageAudit(
        referenced_missing=tuple(sorted(referenced_keys - files)),
        unreferenced_files=tuple(sorted(files - referenced_keys)),
        temporary_files=tuple(sorted(temporary)),
    )
