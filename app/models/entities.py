"""Persistent recording metadata and processing state."""

import enum
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.mysql import BIGINT, CHAR, DATETIME, JSON, LONGTEXT
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utc_now() -> datetime:
    # MySQL DATETIME is timezone-naive; all application and server sessions use UTC.
    return datetime.now(UTC).replace(tzinfo=None)


def new_id() -> str:
    return str(uuid4())


class Base(DeclarativeBase):
    # MySQL lacks INSERT ... RETURNING. Fetch server defaults inside async flush,
    # rather than triggering implicit I/O when serializing a newly inserted object.
    __mapper_args__ = {"eager_defaults": True}
    metadata = MetaData(
        naming_convention={
            "ix": "ix_%(table_name)s_%(column_0_name)s",
            "uq": "uq_%(table_name)s_%(column_0_name)s",
            "ck": "ck_%(table_name)s_%(constraint_name)s",
            "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
            "pk": "pk_%(table_name)s",
        }
    )


class TaskStatus(enum.StrEnum):
    PENDING = "pending"
    TRANSCRIBING = "transcribing"
    SUMMARIZING = "summarizing"
    DONE = "done"
    FAILED = "failed"


class Timestamps:
    created_at: Mapped[datetime] = mapped_column(
        DATETIME(fsp=6), server_default=text("CURRENT_TIMESTAMP(6)")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DATETIME(fsp=6), server_default=text("CURRENT_TIMESTAMP(6)"), onupdate=utc_now
    )


class Recording(Timestamps, Base):
    __tablename__ = "recordings"
    __table_args__ = (
        CheckConstraint("size_bytes > 0 AND size_bytes <= 52428800", name="size_bytes"),
        Index("ix_recordings_visible_created", "deleted_at", "created_at", "id"),
        {
            "mysql_engine": "InnoDB",
            "mysql_charset": "utf8mb4",
            "mysql_collate": "utf8mb4_unicode_ci",
        },
    )

    id: Mapped[str] = mapped_column(CHAR(36), primary_key=True, default=new_id)
    original_filename: Mapped[str] = mapped_column(String(255))
    storage_key: Mapped[str] = mapped_column(String(255), unique=True)
    size_bytes: Mapped[int] = mapped_column(BIGINT)
    content_sha256: Mapped[str] = mapped_column(CHAR(64), unique=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DATETIME(fsp=6))


class Task(Timestamps, Base):
    __tablename__ = "tasks"
    __table_args__ = (
        UniqueConstraint("recording_id", "attempt_no", name="uq_tasks_recording_attempt"),
        CheckConstraint(
            "status IN ('pending','transcribing','summarizing','done','failed')", name="status"
        ),
        CheckConstraint("attempt_no >= 1", name="attempt_no"),
        CheckConstraint("recovery_count >= 0", name="recovery_count"),
        CheckConstraint("auto_retry_count BETWEEN 0 AND 3", name="auto_retry_count"),
        Index("ix_tasks_status_created", "status", "created_at", "id"),
        Index("ix_tasks_status_lease", "status", "lease_expires_at"),
        Index("ix_tasks_status_next_attempt", "status", "next_attempt_at"),
        {
            "mysql_engine": "InnoDB",
            "mysql_charset": "utf8mb4",
            "mysql_collate": "utf8mb4_unicode_ci",
        },
    )

    id: Mapped[str] = mapped_column(CHAR(36), primary_key=True, default=new_id)
    recording_id: Mapped[str] = mapped_column(
        CHAR(36), ForeignKey("recordings.id", ondelete="CASCADE")
    )
    attempt_no: Mapped[int] = mapped_column(Integer, server_default=text("1"))
    retry_of_task_id: Mapped[str | None] = mapped_column(CHAR(36), unique=True)
    status: Mapped[str] = mapped_column(String(20), server_default=text("'pending'"))
    transcript: Mapped[str | None] = mapped_column(LONGTEXT)
    summary_result: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(Text)
    lease_token: Mapped[str | None] = mapped_column(CHAR(36))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DATETIME(fsp=6))
    recovery_count: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    auto_retry_count: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    next_attempt_at: Mapped[datetime | None] = mapped_column(DATETIME(fsp=6))
    last_error_code: Mapped[str | None] = mapped_column(String(64))
    last_error_message: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DATETIME(fsp=6))
    finished_at: Mapped[datetime | None] = mapped_column(DATETIME(fsp=6))
