# app/core/container.py
"""Единственный composition root для API, worker и эксплуатационных проверок."""

from pathlib import Path

import httpx
from sqlalchemy.ext.asyncio import create_async_engine

from app.application.analysis import Analyze
from app.application.boundaries import ManageBoundaries
from app.application.exports import Exports
from app.application.intake import Intake
from app.application.prompts import Prompts
from app.core.settings import Settings
from app.infrastructure.database import Database
from app.infrastructure.docling import DoclingOCR
from app.infrastructure.health import Health
from app.infrastructure.messaging import Rabbit
from app.infrastructure.security import Capabilities
from app.infrastructure.storage import FileStorage
from app.infrastructure.vision import SharedVision


class Container:
    """Собирает адаптеры и сценарии один раз на процесс; route handlers их не создают."""

    def __init__(self, settings: Settings) -> None:
        """Валидирует собственные секреты до соединений и подготовки приватного хранилища."""
        settings.require_secrets()
        self.settings = settings
        self.engine = create_async_engine(
            settings.database_url(), pool_pre_ping=True, pool_size=5, max_overflow=5
        )
        self.database = Database(self.engine, settings)
        self.storage = FileStorage(settings)
        self.http = httpx.AsyncClient(
            trust_env=False,
            follow_redirects=False,
            limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
        )
        self.capabilities = Capabilities(settings)
        self.prompts = Prompts(self.database, Path(__file__).resolve().parents[2] / "prompts")
        self.intake = Intake(self.database, self.storage, self.prompts)
        self.boundaries = ManageBoundaries(self.database, self.intake, self.prompts)
        self.exports = Exports(self.intake, self.capabilities, settings.document_max_pages)
        self.vision = SharedVision(settings, self.http)
        self.ocr = DoclingOCR(settings, self.http)
        self.rabbit = Rabbit(settings)
        self.analysis = Analyze(self.database, self.storage, self.vision, self.ocr, settings)
        self.health = Health(self.database, self.rabbit, self.vision, self.ocr, settings)

    async def close(self) -> None:
        """Освобождает только соединения данного процесса; не останавливает shared-сервисы."""
        await self.rabbit.close()
        await self.http.aclose()
        await self.engine.dispose()
