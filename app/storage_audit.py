"""Read-only storage/database reconciliation for maintenance and incident review."""

from dataclasses import dataclass

from app.storage import LocalStorage


@dataclass(frozen=True)
class StorageAudit:
    referenced_missing: tuple[str, ...]
    unreferenced_files: tuple[str, ...]
    temporary_files: tuple[str, ...]


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
