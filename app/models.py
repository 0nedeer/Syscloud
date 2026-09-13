"""录音元数据、处理任务及数据库约束。"""

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


# Python 侧生成 UTC 时间后去掉时区标记，以适配 MySQL DATETIME；接口输出时再补 Z。
def utc_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


# UUID 在应用侧生成，适合独立 API/Worker 写入；字符串形式存为 CHAR(36)。
def new_id() -> str:
    return str(uuid4())


# 统一元数据和约束命名，使 ORM 与 Alembic 可稳定对照。
# eager_defaults 在 flush 时取回数据库默认值，避免之后读取属性触发隐式异步 I/O。
class Base(DeclarativeBase):
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


# 五个公开状态；failed/done 是旧任务终态，手动重试通过新任务表达。
class TaskStatus(enum.StrEnum):
    PENDING = "pending"
    TRANSCRIBING = "transcribing"
    SUMMARIZING = "summarizing"
    DONE = "done"
    FAILED = "failed"


# 共享时间字段：数据库默认值负责插入时间，ORM onupdate 负责常规更新。
# 直接写原生 SQL 时需自行维护 updated_at。
class Timestamps:
    created_at: Mapped[datetime] = mapped_column(
        DATETIME(fsp=6), server_default=text("CURRENT_TIMESTAMP(6)")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DATETIME(fsp=6), server_default=text("CURRENT_TIMESTAMP(6)"), onupdate=utc_now
    )


# 录音元数据及文件引用；content_sha256 唯一约束实现按内容去重。
# deleted_at 是删除意图，尚未硬删除时查询和领取也必须过滤它。
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


# 一次手动处理尝试的持久化状态，包含结果、错误、租约及自动重试预算。
# attempt_no 是手动尝试序号，auto_retry_count 是本任务内部自动重试次数。
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
    # 来源失败任务只能产生一个直接后继；关系由服务事务保证，不设自引用外键。
    retry_of_task_id: Mapped[str | None] = mapped_column(CHAR(36), unique=True)
    status: Mapped[str] = mapped_column(String(20), server_default=text("'pending'"))
    transcript: Mapped[str | None] = mapped_column(LONGTEXT)
    # None 存 SQL NULL，避免与 JSON 文档中的 null 混淆；仅 done 时向用户发布。
    summary_result: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(Text)
    # 令牌与到期时间共同表示短期所有权；新领取换令牌，旧执行不能覆盖新结果。
    lease_token: Mapped[str | None] = mapped_column(CHAR(36))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DATETIME(fsp=6))
    recovery_count: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    auto_retry_count: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    next_attempt_at: Mapped[datetime | None] = mapped_column(DATETIME(fsp=6))
    last_error_code: Mapped[str | None] = mapped_column(String(64))
    last_error_message: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DATETIME(fsp=6))
    finished_at: Mapped[datetime | None] = mapped_column(DATETIME(fsp=6))
