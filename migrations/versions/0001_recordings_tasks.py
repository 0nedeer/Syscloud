# 第一版表结构：建立录音、任务、外键和初始状态/租约字段。
# 历史迁移描述当时的 schema；后续字段通过追加迁移引入，不以当前模型重写旧版本。

"""Create recording metadata and durable processing tasks.

Revision ID: 0001_recordings_tasks
Revises: None
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "0001_recordings_tasks"
down_revision = None
branch_labels = None
depends_on = None


# 按父表 recordings→子表 tasks 的顺序创建，建立文件大小、任务状态及重试来源约束。
def upgrade() -> None:
    op.create_table(
        "recordings",
        sa.Column("id", mysql.CHAR(36), nullable=False),
        sa.Column("original_filename", sa.String(255), nullable=False),
        sa.Column("storage_key", sa.String(255), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("deleted_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column(
            "created_at",
            mysql.DATETIME(fsp=6),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
        ),
        sa.Column(
            "updated_at",
            mysql.DATETIME(fsp=6),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
        ),
        sa.PrimaryKeyConstraint("id", name="pk_recordings"),
        sa.UniqueConstraint("storage_key", name="uq_recordings_storage_key"),
        sa.CheckConstraint(
            "size_bytes > 0 AND size_bytes <= 52428800", name=op.f("ck_recordings_size_bytes")
        ),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_unicode_ci",
    )
    op.create_index(
        "ix_recordings_visible_created", "recordings", ["deleted_at", "created_at", "id"]
    )
    op.create_table(
        "tasks",
        sa.Column("id", mysql.CHAR(36), nullable=False),
        sa.Column("recording_id", mysql.CHAR(36), nullable=False),
        sa.Column("attempt_no", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("retry_of_task_id", mysql.CHAR(36), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("transcript", mysql.LONGTEXT(), nullable=True),
        sa.Column("summary_result", mysql.JSON(none_as_null=True), nullable=True),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("lease_token", mysql.CHAR(36), nullable=True),
        sa.Column("lease_expires_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column("recovery_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("started_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column("finished_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column(
            "created_at",
            mysql.DATETIME(fsp=6),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
        ),
        sa.Column(
            "updated_at",
            mysql.DATETIME(fsp=6),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
        ),
        sa.PrimaryKeyConstraint("id", name="pk_tasks"),
        sa.ForeignKeyConstraint(
            ["recording_id"],
            ["recordings.id"],
            ondelete="CASCADE",
            name="fk_tasks_recording_id_recordings",
        ),
        sa.UniqueConstraint("recording_id", "attempt_no", name="uq_tasks_recording_attempt"),
        sa.UniqueConstraint("retry_of_task_id", name="uq_tasks_retry_of_task_id"),
        sa.CheckConstraint(
            "status IN ('pending','transcribing','summarizing','done','failed')",
            name=op.f("ck_tasks_status"),
        ),
        sa.CheckConstraint("attempt_no >= 1", name=op.f("ck_tasks_attempt_no")),
        sa.CheckConstraint("recovery_count >= 0", name=op.f("ck_tasks_recovery_count")),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_unicode_ci",
    )
    op.create_index("ix_tasks_status_created", "tasks", ["status", "created_at", "id"])
    op.create_index("ix_tasks_status_lease", "tasks", ["status", "lease_expires_at"])


def downgrade() -> None:
    op.drop_table("tasks")
    op.drop_table("recordings")
