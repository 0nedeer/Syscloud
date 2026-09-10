"""Constrained local storage operations for uploaded audio."""

from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile
from uuid import uuid4

from anyio import to_thread

MAX_AUDIO_BYTES = 50 * 1024 * 1024
ALLOWED_EXTENSIONS = frozenset({".wav", ".mp3", ".m4a", ".aac"})
CHUNK_SIZE = 1024 * 1024
logger = logging.getLogger("app.storage")


class StorageError(Exception):
    """A filesystem operation failed without exposing its path to callers."""


class AudioTooLarge(ValueError):
    pass


@dataclass(frozen=True)
class StoredUpload:
    storage_key: str
    size_bytes: int
    content_sha256: str


def validate_extension(filename: str | None) -> str:
    suffix = Path(filename or "").suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        raise ValueError("Audio extension must be one of wav, mp3, m4a or aac.")
    return suffix


class LocalStorage:
    def __init__(self, root: Path):
        self.root = root.resolve()

    def _safe_path(self, storage_key: str) -> Path:
        candidate = (self.root / storage_key).resolve()
        if self.root != candidate.parent or candidate.name != storage_key:
            raise StorageError("Invalid storage key.")
        return candidate

    async def save(self, source, extension: str) -> StoredUpload:
        """Disk work runs off the event loop; the caller owns the uploaded spool."""
        return await to_thread.run_sync(self._save, source.file, extension)

    def _save(self, source, extension: str) -> StoredUpload:
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

    def delete(self, storage_key: str) -> None:
        try:
            self._safe_path(storage_key).unlink(missing_ok=True)
        except OSError as exc:
            raise StorageError("Unable to delete audio file.") from exc

    def exists(self, storage_key: str) -> bool:
        try:
            return self._safe_path(storage_key).is_file()
        except OSError as exc:
            raise StorageError("Unable to inspect audio file.") from exc
