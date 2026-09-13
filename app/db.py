"""异步连接池、请求会话与迁移就绪检查。"""

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from fastapi import Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import Settings


# 按源码位置定位 Alembic 配置，避免迁移脚本路径依赖调用目录。
def migration_config() -> Config:
    root = Path(__file__).resolve().parent.parent
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "migrations"))
    return config


# 数据库可以连接但迁移版本不匹配，与数据库断连分别返回不同错误码。
class SchemaNotReady(Exception):
    pass


# 一个 API/Worker 进程持有一个 Database，内部连接池复用连接。
class Database:
    def __init__(self, settings: Settings) -> None:
        self.engine = create_async_engine(
            settings.database_url.get_secret_value(),
            pool_pre_ping=True,
            pool_recycle=1800,
            pool_timeout=settings.db_pool_timeout_seconds,
            connect_args={
                "charset": "utf8mb4",
                "init_command": "SET time_zone = '+00:00'",
                "connect_timeout": settings.db_connect_timeout_seconds,
                "read_timeout": settings.db_read_timeout_seconds,
            },
            hide_parameters=True,
            isolation_level="REPEATABLE READ",
            echo=False,
        )
        # 提交后不自动使 ORM 属性过期，便于在异步代码中读取结果，避免隐式数据库 I/O。
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        self.heads = set(ScriptDirectory.from_config(migration_config()).get_heads())

    # 在总体超时内执行 SELECT 1，并比较数据库与源码的 Alembic head。
    # 仅检查连接和迁移版本，不自动迁移，也不检查 Worker、磁盘或模型。
    async def check_ready(self, deadline_seconds: float) -> None:
        async with asyncio.timeout(deadline_seconds):
            async with self.engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
                heads = await connection.run_sync(
                    lambda conn: set(MigrationContext.configure(conn).get_current_heads())
                )
                if not self.heads or heads != self.heads:
                    raise SchemaNotReady

    async def close(self) -> None:
        await self.engine.dispose()


# FastAPI 的 yield 依赖：每次请求借出独立 Session，请求结束自动关闭。
# 不同请求不能共用同一个可变 Session；是否提交由调用它的业务函数决定。
async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    async with request.app.state.database.sessions() as session:
        yield session
