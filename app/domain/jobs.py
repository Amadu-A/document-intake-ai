# app/domain/jobs.py
"""Состояния заданий и безопасные правила видимости/срока хранения."""

from datetime import UTC, datetime
from typing import Literal

from pydantic import Field

from app.domain.errors import IntakeError
from app.domain.schema import StrictModel

TERMINAL = frozenset({"completed", "completed_with_warnings", "failed", "cancelled"})
JobStatus = Literal[
    "uploaded", "queued", "running", "completed", "completed_with_warnings", "failed", "cancelled"
]


def utcnow() -> datetime:
    """Возвращает aware UTC независимо от часового пояса сервера."""
    return datetime.now(UTC)


class Source(StrictModel):
    """Приватный manifest оригинала с вычисленными размером и хешем."""

    file_id: str
    file_name: str
    mime_type: str
    path: str
    byte_size: int
    sha256: str
    page_count: int = 0


class Job(StrictModel):
    """Снимок задания, не привязанный к драйверу БД или HTTP."""

    id: str
    owner: str
    status: JobStatus
    created_at: datetime
    expires_at: datetime | None
    deadline_at: datetime | None = None
    completed_at: datetime | None = None
    stage: str = "upload"
    progress: int = 0
    attempt: int = 0
    fence: int = 0
    sources: list[Source]
    prompts: dict[str, str]
    organization_hint: str | None = None
    result: dict | None = None
    manifest: dict = Field(default_factory=dict)
    error: dict | None = None
    cursor: int = 0

    def authorize(self, owner: str) -> None:
        """Скрывает чужое задание и немедленно закрывает доступ по истечении TTL."""
        if self.owner != owner:
            raise IntakeError("access_denied", "Задание не найдено", 404)
        if self.expires_at and self.expires_at <= utcnow():
            raise IntakeError("job_expired", "Срок хранения задания истёк", 410)
