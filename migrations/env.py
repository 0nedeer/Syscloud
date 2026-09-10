"""Read credentials from Settings instead of storing them in alembic.ini."""

import asyncio

from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import Settings
from app.models import Base


def configure_and_run(connection):
    context.configure(connection=connection, target_metadata=Base.metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


async def run_online():
    settings = Settings()
    engine = create_async_engine(
        settings.database_url.get_secret_value(),
        poolclass=pool.NullPool,
        hide_parameters=True,
        connect_args={
            "charset": "utf8mb4",
            "init_command": "SET time_zone = '+00:00'",
            "connect_timeout": settings.db_connect_timeout_seconds,
            "read_timeout": settings.db_read_timeout_seconds,
        },
    )
    try:
        async with engine.connect() as connection:
            await connection.run_sync(configure_and_run)
    finally:
        await engine.dispose()


if context.is_offline_mode():
    context.configure(
        dialect_name="mysql",
        target_metadata=Base.metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()
elif context.config.attributes.get("connection") is not None:
    configure_and_run(context.config.attributes["connection"])
else:
    asyncio.run(run_online())
