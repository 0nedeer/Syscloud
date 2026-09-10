"""Database lifetime, bounded readiness checks and request-scoped sessions."""

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


def migration_config() -> Config:
    root = Path(__file__).resolve().parent.parent
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "migrations"))
    return config


class SchemaNotReady(Exception):
    pass


class Database:
    def __init__(self, settings: Settings):
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
            echo=False,
        )
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        self.heads = set(ScriptDirectory.from_config(migration_config()).get_heads())

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


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    async with request.app.state.database.sessions() as session:
        yield session
