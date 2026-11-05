# migrations/env.py
"""Применяет миграции через общий Settings без вывода DSN/пароля в Alembic logs."""

import asyncio

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.settings import Settings
from app.infrastructure.database import Base


def migrate(connection) -> None:
    """Выполняет ревизии в одной управляемой Alembic-транзакции."""
    context.configure(connection=connection, target_metadata=Base.metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run() -> None:
    """Открывает собственную DB и не выполняет create_all вместо версионированных миграций."""
    engine = create_async_engine(Settings().database_url())
    try:
        async with engine.connect() as connection:
            await connection.run_sync(migrate)
    finally:
        await engine.dispose()


asyncio.run(run())
