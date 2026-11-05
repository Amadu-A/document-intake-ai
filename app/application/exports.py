# app/application/exports.py
"""Выдача безопасных статусов, manifest и реальных временных download URLs."""

from copy import deepcopy
from datetime import timedelta

from app.application.intake import Intake
from app.application.ports import DownloadSigner
from app.domain.errors import IntakeError
from app.domain.jobs import Job, utcnow
from app.domain.schema import BatchResult


class Exports:
    """Единые правила выдачи UI/1С без экспорта локальных путей, OCR и промптов."""

    def __init__(self, intake: Intake, capabilities: DownloadSigner) -> None:
        """Подключает сценарий доступа и signer из composition root."""
        self.intake, self.capabilities = intake, capabilities

    def source_path(self, job_id: str, file_id: str) -> str:
        """Возвращает стабильный REST-путь оригинала."""
        return f"/api/v1/jobs/{job_id}/source-files/{file_id}"

    def document_path(self, job_id: str, document_id: str) -> str:
        """Возвращает стабильный REST-путь единого PDF-комплекта."""
        return f"/api/v1/jobs/{job_id}/documents/{document_id}/file"

    def signed(self, job: Job, path: str) -> str:
        """Активному заданию выдаёт короткую ссылку; terminal-ссылку ограничивает итоговым TTL."""
        expires = job.expires_at or utcnow() + timedelta(minutes=5)
        return self.capabilities.sign(path, job.owner, expires)

    def status(self, job: Job) -> dict:
        """Формирует публичный snapshot без приватных полей persistence."""
        pages = [
            {
                "file_id": p["file_id"],
                "page_number": p["page_number"],
                "preview_url": self.signed(
                    job, f"/api/v1/jobs/{job.id}/pages/{p['file_id']}/{p['page_number']}/preview"
                ),
            }
            for p in job.manifest.get("pages", [])
        ]
        sources = [
            {
                "file_id": s.file_id,
                "file_name": s.file_name,
                "mime_type": s.mime_type,
                "byte_size": s.byte_size,
                "sha256": s.sha256,
                "page_count": s.page_count,
                "file_url": self.signed(job, self.source_path(job.id, s.file_id)),
            }
            for s in job.sources
        ]
        artifacts = [
            {
                "document_id": document_id,
                "file_name": item["file_name"],
                "byte_size": item["byte_size"],
                "sha256": item["sha256"],
                "file_url": self.signed(job, self.document_path(job.id, document_id)),
            }
            for document_id, item in job.manifest.get("documents", {}).items()
        ]
        return {
            "job_id": job.id,
            "status": job.status,
            "stage": job.stage,
            "progress": job.progress,
            "created_at": job.created_at,
            "completed_at": job.completed_at,
            "expires_at": job.expires_at,
            "cursor": job.cursor,
            "error": job.error,
            "sources": sources,
            "pages": pages,
            "artifacts": artifacts,
            "plan": job.manifest.get("plan"),
            "links": {
                "status": f"/api/v1/jobs/{job.id}",
                "result": f"/api/v1/jobs/{job.id}/result",
                "events": f"/api/v1/jobs/{job.id}/events",
            },
        }

    async def result(self, job_id: str, owner: str) -> dict:
        """Добавляет настоящие ссылки, не изменяя утверждённое дерево JSON 1.0."""
        result = deepcopy(await self.intake.result(job_id, owner))
        job = await self.intake.get(job_id, owner)
        for source in result["files"]:
            source["file_url"] = self.signed(job, self.source_path(job.id, source["file_id"]))
            for document in source["documents"]:
                document["attachment_file"]["file_url"] = self.signed(
                    job, self.document_path(job.id, document["document_id"])
                )
        return BatchResult.model_validate(result).model_dump(mode="json")

    async def artifact(
        self,
        job_id: str,
        kind: str,
        item_id: str,
        owner: str | None,
        token: str | None,
        page_number: int | None = None,
    ) -> tuple[Job, dict]:
        """Разрешает только owner либо capability конкретного скачивания; TTL проверяется всегда."""
        job = await self.intake.repository.get(job_id)
        if kind == "source":
            path = self.source_path(job_id, item_id)
        elif kind == "document":
            path = self.document_path(job_id, item_id)
        else:
            path = f"/api/v1/jobs/{job_id}/pages/{item_id}/{page_number}/preview"
        expires = job.expires_at or utcnow() + timedelta(minutes=5)
        signed = self.capabilities.verify(
            path,
            job.owner,
            expires,
            token,
            allow_expired=bool(job.expires_at and job.expires_at <= utcnow()),
        )
        job.authorize(job.owner if signed else (owner or ""))
        if kind == "source":
            source = next((s for s in job.sources if s.file_id == item_id), None)
            if source:
                return job, source.model_dump()
        elif kind == "document":
            artifact = job.manifest.get("documents", {}).get(item_id)
            if artifact and job.status in {"completed", "completed_with_warnings"}:
                return job, artifact
        else:
            preview = next(
                (
                    p
                    for p in job.manifest.get("pages", [])
                    if p["file_id"] == item_id and p["page_number"] == page_number
                ),
                None,
            )
            if preview:
                return job, {
                    "path": preview["preview"],
                    "file_name": "page.png",
                    "mime_type": "image/png",
                }
        raise IntakeError("artifact_not_found", "Артефакт не найден", 404)
