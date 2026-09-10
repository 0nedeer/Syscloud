"""Persist the shared automatic retry budget and scheduling deadline."""

import sqlalchemy as sa
from alembic import op

revision = "0002_task_auto_retries"
down_revision = "0001_recordings_tasks"
branch_labels = None
depends_on = None


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


def downgrade():
    op.drop_index("ix_tasks_status_next_attempt", table_name="tasks")
    op.drop_constraint(op.f("ck_tasks_auto_retry_count"), "tasks", type_="check")
    for name in ("last_error_message", "last_error_code", "next_attempt_at", "auto_retry_count"):
        op.drop_column("tasks", name)
