# tests/conftest.py
# tests/conftest.py
"""Изолированная SQLite test DB и doubles; live/browser требуют явных флагов."""

from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine

from app.application.analysis import Analyze
from app.application.boundaries import ManageBoundaries
from app.application.exports import Exports
from app.application.intake import Intake
from app.application.prompts import Prompts
from app.core.settings import Settings
from app.infrastructure.database import Base, Database
from app.infrastructure.security import Capabilities
from app.infrastructure.storage import FileStorage
from app.main import create_app
from tests.support import ScriptedVision, SyntheticHealth, SyntheticOCR, simple_container


def pytest_addoption(parser):
    """Отделяет opt-in реальные системы от обязательных offline-проверок."""
    parser.addoption("--live", action="store_true", help="Настоящие shared зависимости/HTTP")
    parser.addoption(
        "--browser",
        action="store_true",
        help="Настоящий Chromium, предварительно установленный Playwright",
    )


def pytest_collection_modifyitems(config, items):
    """Пропуск отмечается SKIPPED; включённые live-тесты без credentials завершаются ошибкой."""
    for item in items:
        for marker, option in (("live", "--live"), ("browser", "--browser")):
            if marker in item.keywords and not config.getoption(option):
                item.add_marker(pytest.mark.skip(reason=f"NOT_RUN: требуется {option}"))


@pytest.fixture
def settings(tmp_path):
    """Настройки используют только несекретные тестовые значения, без чтения настоящего .env."""
    return Settings(
        _env_file=None,
        data_dir=tmp_path / "storage",
        app_public_base_url="http://testserver",
        database_password="a" * 48,
        rabbitmq_password="b" * 48,
        session_secret="c" * 48,
        download_secret="d" * 48,
        api_keys={"onec": "e" * 48, "telegram": "f" * 48},
        model_attempts=2,
        # Старые adapter fixtures проверяют sync-контракт; async имеет отдельные регрессии.
        docling_async=False,
        rate_limit_per_minute=1000,
    )


@pytest_asyncio.fixture
async def container(settings):
    """Production repository на SQLite; fixture не доказывает PostgreSQL concurrency."""
    # HTTP и тестовая доставка идут параллельно: им нельзя делить одну SQLite connection.
    engine = create_async_engine(f"sqlite+aiosqlite:///{settings.data_dir.parent / 'state.sqlite'}")
    async with engine.begin() as connection:
        if engine.dialect.name == "sqlite":
            # SSE/preview читают параллельно writer: rollback journal блокирует его commit.
            await connection.exec_driver_sql("PRAGMA journal_mode=WAL")
        await connection.run_sync(Base.metadata.create_all)
    database = Database(engine, settings)
    storage = FileStorage(settings)
    capabilities = Capabilities(settings)
    prompts = Prompts(database, Path("prompts"))
    intake = Intake(database, storage, prompts)
    exports = Exports(intake, capabilities)
    vision = ScriptedVision()
    ocr = SyntheticOCR()
    root = simple_container(
        settings=settings,
        database=database,
        storage=storage,
        capabilities=capabilities,
        prompts=prompts,
        intake=intake,
        boundaries=ManageBoundaries(database, intake, prompts),
        exports=exports,
        vision=vision,
        health=SyntheticHealth(),
    )
    root.analysis = Analyze(database, storage, vision, ocr, settings)
    yield root
    await engine.dispose()


@pytest_asyncio.fixture
async def client(container):
    """Открывает transport без сети; тестовые API-ключи позволяют проверить реальный REST flow."""
    app = create_app(container)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as session:
            yield session
