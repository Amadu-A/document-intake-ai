# app/application/intake.py
"""Общие сценарии UI и headless API: загрузка, запуск, доступ и отмена."""

import hashlib
import json
from datetime import timedelta
from uuid import uuid4

from app.application.ports import JobRepository, Storage, Upload
from app.application.prompts import Prompts
from app.core.observability import timed
from app.domain.errors import IntakeError
from app.domain.jobs import Job, utcnow


class Intake:
    """Оркестрирует приём документов через порты без SQL и HTTP-зависимостей."""

    def __init__(self, repository: JobRepository, storage: Storage, prompts: Prompts) -> None:
        """Принимает уже собранные зависимости из composition root."""
        self.repository = repository
        self.storage = storage
        self.prompts = prompts

    @timed("upload_documents")
    async def upload(
        self,
        owner: str,
        uploads: list[Upload],
        organization: str | None,
        key: str | None,
        start: bool,
    ) -> Job:
        """Сохраняет оригиналы и атомарно создаёт job/outbox с owner-idempotency."""
        if key is not None and not 1 <= len(key) <= 128:
            raise IntakeError("invalid_idempotency_key", "Неверная длина Idempotency-Key")
        if organization and len(organization) > 1000:
            raise IntakeError("invalid_organization", "Слишком длинный контекст организации")
        job_id = str(uuid4())
        try:
            sources = await self.storage.ingest(job_id, uploads)
            prompts = (await self.prompts.get(owner))["prompts"]
            digest = hashlib.sha256(
                json.dumps(
                    {
                        "sources": [(s.file_name, s.mime_type, s.sha256) for s in sources],
                        "organization": organization,
                        "start": start,
                        "prompts": prompts if start else {},
                    },
                    sort_keys=True,
                    ensure_ascii=False,
                ).encode()
            ).hexdigest()
            now = utcnow()
            job = Job(
                id=job_id,
                owner=owner,
                status="queued" if start else "uploaded",
                created_at=now,
                expires_at=None if start else now + timedelta(hours=1),
                sources=sources,
                prompts=dict(prompts) if start else {},
                organization_hint=organization,
            )
            existing, created = await self.repository.create(job, key, digest)
            if not created:
                await self.storage.remove(job_id)
            return existing
        except IntakeError:
            await self.storage.remove(job_id)
            raise
        # При отмене/обрыве БД исход commit может быть неизвестен: удаление могло бы
        # повредить уже созданное задание. Непривязанный каталог уберёт reaper через час.

    async def get(self, job_id: str, owner: str) -> Job:
        """Проверяет owner и TTL перед каждым пользовательским чтением."""
        job = await self.repository.get(job_id)
        job.authorize(owner)
        return job

    @timed("start_analysis")
    async def start(self, job_id: str, owner: str) -> Job:
        """Запускает ранее загруженный пакет со снимками обоих промптов."""
        prompts = (await self.prompts.get(owner))["prompts"]
        return await self.repository.start(job_id, owner, prompts)

    async def result(self, job_id: str, owner: str) -> dict:
        """Отдаёт только проверенный опубликованный JSON, иначе явную ошибку."""
        job = await self.get(job_id, owner)
        if job.status not in {"completed", "completed_with_warnings"} or job.result is None:
            raise IntakeError("result_not_ready", "Проверенный результат недоступен", 409)
        return job.result
