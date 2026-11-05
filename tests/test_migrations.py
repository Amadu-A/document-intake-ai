# tests/test_migrations.py
"""Функциональная проверка Alembic DDL отдельно от реального PostgreSQL deploy."""

import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect

from app.infrastructure.database import Base


def test_initial_migration_matches_repository_metadata():
    """Миграция создаёт все таблицы/колонки repository, не опираясь на create_all в production."""
    path = Path("migrations/versions/0001_initial.py")
    spec = importlib.util.spec_from_file_location("initial_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            module.upgrade()
        schema = inspect(connection)
        assert set(schema.get_table_names()) == set(Base.metadata.tables)
        for name, table in Base.metadata.tables.items():
            assert {column["name"] for column in schema.get_columns(name)} == set(
                table.columns.keys()
            )
        with Operations.context(context):
            module.downgrade()
        assert inspect(connection).get_table_names() == []
