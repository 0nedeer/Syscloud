# 运维入口：从数据库读取所有 storage_key，再与磁盘比较，仅输出分类结果。
# 命令 python scripts/audit-storage.py 使用当前目录的 .env，不会自动删除孤立文件。

"""Print a read-only database/file reconciliation report; never deletes files."""

import argparse
import asyncio
import json
import sys
from pathlib import Path

# Direct script invocation otherwise searches scripts/ rather than the project root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


# 从录音表读取引用（包含删除中的记录），在线程中做磁盘扫描，最后关闭连接池。
async def main() -> None:
    from sqlalchemy import select

    from app.config import Settings
    from app.db import Database
    from app.models import Recording
    from app.storage import LocalStorage, audit_storage

    settings = Settings()
    database = Database(settings)
    try:
        async with database.sessions() as session:
            keys = set((await session.scalars(select(Recording.storage_key))).all())
        report = await asyncio.to_thread(audit_storage, LocalStorage(settings.upload_dir), keys)
        print(json.dumps(report.__dict__, ensure_ascii=False, indent=2))
    finally:
        await database.close()


if __name__ == "__main__":
    argparse.ArgumentParser(description=__doc__).parse_args()
    try:
        asyncio.run(main())
    except Exception as exc:
        # Driver/configuration exceptions can contain credentials and local paths.
        print(
            json.dumps({"error": "storage_audit_failed", "type": type(exc).__name__}),
            file=sys.stderr,
        )
        sys.exit(1)
