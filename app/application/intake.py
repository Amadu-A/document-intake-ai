# app/application/intake.py
"""Общие сценарии UI и headless API: загрузка, запуск, доступ и отмена."""

import hashlib
import json
from uuid import uuid4

from app.application.ports import JobRepository, Storage, Upload
from app.application.prompts import Prompts
from app.core.observability import timed
from app.domain.boundaries import validate_mode
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
        auto_split: bool = True,
        skip_segmentation: bool = False,
    ) -> Job:
        """Сохраняет оригиналы и атомарно создаёт job/outbox с owner-idempotency."""
        validate_mode(auto_split, skip_segmentation)
        if key is not None and not 1 <= len(key) <= 128:
            raise IntakeError("invalid_idempotency_key", "Неверная длина Idempotency-Key")
        if organization and len(organization) > 1000:
            raise IntakeError("invalid_organization", "Слишком длинный контекст организации")
        job_id = str(uuid4())
        may_have_committed = False
        try:
            sources = await self.storage.ingest(job_id, uploads)
            prompts = (await self.prompts.get(owner))["prompts"]
            identity = {
                "sources": [(s.file_name, s.mime_type, s.sha256) for s in sources],
                "organization": organization,
                "start": start,
                "prompts": prompts if start else {},
            }
            # Default-запрос сохраняет прежний hash: retry старой 1С после обновления допустим.
            if not auto_split or skip_segmentation:
                identity["mode"] = {
                    "auto_split": auto_split,
                    "skip_segmentation": skip_segmentation,
                }
            digest = hashlib.sha256(
                json.dumps(
                    identity,
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
                expires_at=None,
                sources=sources,
                prompts=dict(prompts) if start else {},
                organization_hint=organization,
                auto_split=auto_split,
                skip_segmentation=skip_segmentation,
                work_kind="analyze" if start else "prepare",
                processing_requested=start,
                stage="queued" if start else "preparing_preview",
            )
            may_have_committed = True
            existing, created = await self.repository.create(job, key, digest)
            if not created:
                await self.storage.remove(job_id)
            return existing
        except IntakeError:
            await self.storage.remove(job_id)
            raise
        except BaseException:
            if not may_have_committed:
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

    @timed("cancel_analysis")
    async def cancel(self, job_id: str, owner: str) -> Job:
        """Отменяет задание через общий порт с атомарной проверкой owner, TTL и fence."""
        return await self.repository.cancel(job_id, owner)
