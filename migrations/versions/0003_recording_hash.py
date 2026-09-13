# 第三版迁移：校验已有音频并回填 SHA-256，最后建立上传内容唯一约束。
# 必须停写并挂载原音频目录；缺失、大小不符或重复时终止，不擅自合并既有录音。

"""Backfill verified audio hashes before enforcing upload deduplication.

Stop API and Worker first. Re-running after interrupted MySQL DDL is supported.
"""

import hashlib
from pathlib import Path

import sqlalchemy as sa
from alembic import op

from app.config import Settings

revision = "0003_recording_hash"
down_revision = "0002_task_auto_retries"
branch_labels = None
depends_on = None


# 先完整核验文件，再执行 DDL；根据已存在的列/索引继续执行，以应对迁移中途断开。
# 哈希来自实际文件，不能用占位值；因此本迁移不支持纯离线 SQL 生成。
def upgrade():
    context = op.get_context()
    if context.as_sql:
        raise RuntimeError("Hash migration requires an online database and its audio volume.")
    conn = op.get_bind()
    root = Path(context.config.attributes.get("upload_dir") or Settings().upload_dir).resolve()
    rows = conn.execute(sa.text("SELECT id,storage_key,size_bytes FROM recordings")).all()
    hashes, seen = [], {}
    # Validate every source before any DDL; never delete or merge existing recordings.
    for recording_id, key, expected_size in rows:
        try:
            path = (root / key).resolve()
            if path.parent != root or path.name != key:
                raise ValueError
            digest, size = hashlib.sha256(), 0
            with path.open("rb") as source:
                while chunk := source.read(1024 * 1024):
                    size += len(chunk)
                    digest.update(chunk)
            if size != expected_size:
                raise ValueError
        except (OSError, ValueError):
            raise RuntimeError(f"Cannot verify audio for recording {recording_id}.") from None
        value = digest.hexdigest()
        if value in seen:
            raise RuntimeError(
                f"Duplicate audio: recordings {seen[value]} and {recording_id}; "
                "resolve existing data explicitly before retrying migration."
            )
        seen[value] = recording_id
        hashes.append({"id": recording_id, "hash": value})
    inspector = sa.inspect(conn)
    columns = {column["name"] for column in inspector.get_columns("recordings")}
    if "content_sha256" not in columns:
        op.execute(sa.text("ALTER TABLE recordings ADD COLUMN content_sha256 CHAR(64) NULL"))
    if hashes:
        conn.execute(sa.text("UPDATE recordings SET content_sha256=:hash WHERE id=:id"), hashes)
    # MySQL implicitly commits DDL. A restart recomputes the hashes and resumes safely.
    indexes = {index["name"] for index in sa.inspect(conn).get_indexes("recordings")}
    clause = (
        ""
        if "uq_recordings_content_sha256" in indexes
        else (", ADD CONSTRAINT uq_recordings_content_sha256 UNIQUE (content_sha256)")
    )
    op.execute(
        sa.text("ALTER TABLE recordings MODIFY COLUMN content_sha256 CHAR(64) NOT NULL" + clause)
    )


# 删除哈希列和唯一索引，录音及音频仍保留，但不再有数据库级内容去重。
def downgrade():
    op.execute(
        sa.text(
            "ALTER TABLE recordings DROP INDEX uq_recordings_content_sha256, "
            "DROP COLUMN content_sha256"
        )
    )
