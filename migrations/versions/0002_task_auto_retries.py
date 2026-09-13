# 第二版迁移：增加单任务自动重试预算、下次执行时间及最近错误字段。
# 旧任务默认预算使用量为 0，保留已有转写、摘要和手动尝试记录。

"""Persist the shared automatic retry budget and scheduling deadline."""

import sqlalchemy as sa
from alembic import op

revision = "0002_task_auto_retries"
down_revision = "0001_recordings_tasks"
branch_labels = None
depends_on = None


# 用一条 ALTER TABLE 添加字段、约束及调度索引，利用 MySQL 8 的单条 DDL 原子性。
def upgrade():
    # One atomic MySQL 8 ALTER: a failed DDL does not leave partially added columns.
    op.execute(
        sa.text("""
        ALTER TABLE tasks
        ADD COLUMN auto_retry_count INTEGER NOT NULL DEFAULT 0,
        ADD COLUMN next_attempt_at DATETIME(6) NULL,
        ADD COLUMN last_error_code VARCHAR(64) NULL,
        ADD COLUMN last_error_message TEXT NULL,
        ADD CONSTRAINT ck_tasks_auto_retry_count CHECK (auto_retry_count BETWEEN 0 AND 3),
        ADD INDEX ix_tasks_status_next_attempt (status, next_attempt_at)
    """)
    )


# 回退移除新增索引/约束/列，自动重试历史随这些列一起丢失。
def downgrade():
    op.drop_index("ix_tasks_status_next_attempt", table_name="tasks")
    op.drop_constraint(op.f("ck_tasks_auto_retry_count"), "tasks", type_="check")
    for name in ("last_error_message", "last_error_code", "next_attempt_at", "auto_retry_count"):
        op.drop_column("tasks", name)
