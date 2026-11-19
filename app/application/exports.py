# app/application/exports.py
"""Выдача безопасных статусов, manifest и реальных временных download URLs."""

from copy import deepcopy
from datetime import timedelta

from app.application.intake import Intake
from app.application.ports import DownloadSigner
from app.core.observability import timed
from app.domain.boundaries import (
    source_file_plan,
    validate_assignment,
)
from app.domain.comparisons import ALGORITHM_VERSION, adjacent_comparisons
from app.domain.document_kinds import kind_metadata
from app.domain.document_names import document_archive_name
from app.domain.errors import IntakeError
from app.domain.extractions import DetailedDocument, DetailedFacts, DetailedResult
from app.domain.jobs import TERMINAL, Job, utcnow
from app.domain.schema import BatchResult


class Exports:
    """Единые правила выдачи UI/1С без экспорта локальных путей, OCR и промптов."""

    def __init__(
        self, intake: Intake, capabilities: DownloadSigner, max_group_pages: int = 1000
    ) -> None:
        """Подключает сценарий доступа и signer из composition root."""
        self.intake, self.capabilities = intake, capabilities
        self.max_group_pages = max_group_pages

    def source_path(self, job_id: str, file_id: str) -> str:
        """Возвращает стабильный REST-путь оригинала."""
        return f"/api/v1/jobs/{job_id}/source-files/{file_id}"

    def document_path(self, job_id: str, document_id: str) -> str:
        """Возвращает стабильный REST-путь единого PDF-комплекта."""
        return f"/api/v1/jobs/{job_id}/documents/{document_id}/file"

    def archive_path(self, job_id: str) -> str:
        """Даёт независимый от web путь ZIP всех регистраций пачки."""
        return f"/api/v1/jobs/{job_id}/archive"

    async def _archive(self, job: Job) -> dict:
        """Берёт проверенные факты и целые PDF в глобальном порядке утверждённых блоков."""
        if job.status not in {"completed", "completed_with_warnings"} or not job.result:
            raise IntakeError(
                "archive_not_ready", "Архив доступен после обработки утверждённых блоков", 409
            )
        result = BatchResult.model_validate(job.result)
        documents = sorted(
            (d for source in result.files for d in source.documents), key=lambda d: d.segment_index
        )
        entries = []
        for document in documents:
            item = job.manifest.get("documents", {}).get(document.document_id)
            if not item:
                raise IntakeError("artifact_not_found", "Не найден PDF утверждённого блока", 404)
            entries.append(
                {**item, "file_name": document_archive_name(document.segment_index, document)}
            )
        if not entries:
            raise IntakeError("archive_not_ready", "Нет готовых документов для архива", 409)
        artifact = await self.intake.storage.archive(job, entries)
        job.authorize(job.owner)
        return artifact

    @timed("prepare_document_archive")
    async def prepare_archive(self, job_id: str, owner: str) -> dict:
        """Подготавливает ZIP без повторных VLM-вызовов; возвращает только безопасную ссылку."""
        job = await self.intake.get(job_id, owner)
        artifact = await self._archive(job)
        return {
            **{key: artifact[key] for key in ("file_name", "byte_size", "sha256")},
            "file_url": self.signed(job, self.archive_path(job_id)),
        }

    def signed(self, job: Job, path: str) -> str:
        """Активному заданию выдаёт короткую ссылку; terminal-ссылку ограничивает итоговым TTL."""
        short = utcnow() + timedelta(minutes=5)
        expires = min(job.expires_at, short) if job.expires_at else short
        return self.capabilities.sign(path, job.owner, expires)

    def segmentation(self, job: Job) -> dict:
        """Отдаёт авторизованному клиенту режим/revision и планы без OCR или приватных путей."""
        physical = job.page_refs()
        draft = job.draft_plan
        proposal = job.proposed_plan
        can_confirm = False
        if job.preview_complete and not job.plan_locked and draft is not None:
            try:
                validate_assignment(draft, physical, self.max_group_pages, require_documents=True)
                can_confirm = job.status in {"uploaded", "awaiting_confirmation"} and (
                    job.skip_segmentation or job.proposed_plan is not None
                )
            except IntakeError:
                pass
        assigned = sum(len(d.pages) for d in draft.documents) if draft else 0
        excluded = len(draft.excluded_pages) if draft else 0
        initial = proposal or source_file_plan(physical)
        return {
            "auto_split": job.auto_split,
            "skip_segmentation": job.skip_segmentation,
            "segmentation_source": job.segmentation_source,
            "plan_revision": job.plan_revision,
            "approved_revision": job.approved_revision,
            "locked": job.plan_locked or job.status in TERMINAL,
            "preview_complete": job.preview_complete,
            "can_confirm": can_confirm,
            "assigned_count": assigned,
            "excluded_count": excluded,
            "unassigned_count": max(0, len(physical) - assigned - excluded),
            "document_count": len(draft.documents) if draft else 0,
            "proposed_plan": proposal.model_dump(mode="json") if proposal else None,
            "draft_plan": draft.model_dump(mode="json") if draft else None,
            "approved_plan": job.approved_plan.model_dump(mode="json")
            if job.approved_plan
            else None,
            "initial_plan": initial.model_dump(mode="json"),
            "comparisons": self.comparisons(job),
        }

    def comparisons(self, job: Job) -> dict:
        """Показывает прогресс и неизвестные переходы без OCR и его отпечатков."""
        expected = (
            adjacent_comparisons(job.page_refs())
            if job.algorithm_version == ALGORITHM_VERSION
            and (not job.skip_segmentation or job.comparisons)
            else []
        )
        saved = {pair.index: pair for pair in job.comparisons}
        pairs = [saved.get(pair.index, pair) for pair in expected]
        return {
            "total": len(pairs),
            "completed": sum(p.status == "completed" for p in pairs),
            "errors": sum(p.status == "unresolved" for p in pairs),
            "needs_review": job.segmentation_source == "vlm_partial",
            "transitions": [p.model_dump(mode="json", exclude={"fingerprint"}) for p in pairs],
        }

    def page_slice(self, job: Job, offset: int = 0, limit: int = 60) -> dict:
        """Пагинирует метаданные; неготовый PNG обозначается pending без фиктивной ссылки."""
        refs = job.page_refs()
        ready = {(p["file_id"], p["page_number"]): p for p in job.manifest.get("pages", [])}
        orders = {s.file_id: i for i, s in enumerate(job.sources)}
        pages = []
        for position, ref in enumerate(refs[offset : offset + limit], offset + 1):
            exists = (ref.file_id, ref.page_number) in ready
            pages.append(
                {
                    **ref.model_dump(),
                    "global_position": position,
                    "source_order": orders[ref.file_id],
                    "render_status": "ready" if exists else "pending",
                    "preview_url": self.signed(
                        job, f"/api/v1/jobs/{job.id}/pages/{ref.file_id}/{ref.page_number}/preview"
                    )
                    if exists
                    else None,
                    "thumbnail_url": self.signed(
                        job,
                        f"/api/v1/jobs/{job.id}/pages/{ref.file_id}/{ref.page_number}/thumbnail",
                    )
                    if exists
                    else None,
                }
            )
        return {
            "pages": pages,
            "offset": offset,
            "limit": limit,
            "total": len(refs),
            "preview_complete": job.preview_complete,
            "ready_count": len(ready),
        }

    def status(self, job: Job, include_pages: bool = True) -> dict:
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
            if include_pages
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
                "processing": job.document_processing[document_id].model_dump()
                if document_id in job.document_processing
                else None,
            }
            for document_id, item in job.manifest.get("documents", {}).items()
        ]
        return {
            "job_id": job.id,
            "status": job.status,
            "stage": job.stage,
            "progress": job.progress,
            "work_stage": job.work_stage,
            "document_processing": {
                identifier: processing.model_dump()
                for identifier, processing in job.document_processing.items()
            },
            "work_progress": {
                stage: counter.model_dump() for stage, counter in job.work_progress.items()
            },
            "created_at": job.created_at,
            "completed_at": job.completed_at,
            "expires_at": job.expires_at,
            "cursor": job.cursor,
            "error": job.error,
            "organization_hint": job.organization_hint,
            "comparisons": self.comparisons(job),
            "sources": sources,
            "pages": pages,
            "artifacts": artifacts,
            "plan": job.manifest.get("plan") if include_pages else None,
            "mode": {"auto_split": job.auto_split, "skip_segmentation": job.skip_segmentation},
            "segmentation_source": job.segmentation_source,
            "plan_revision": job.plan_revision,
            "plan_locked": job.plan_locked,
            "awaiting_confirmation": job.status == "awaiting_confirmation",
            "preview_complete": job.preview_complete,
            "pages_total": len(job.page_refs()),
            "pages_ready": len(job.manifest.get("pages", [])),
            "deadlines": {
                "queue": job.queue_deadline_at,
                "preview": job.preview_deadline_at,
                "processing": job.processing_deadline_at,
            },
            "counts": {
                "documents": len(job.draft_plan.documents) if job.draft_plan else 0,
                "assigned": sum(len(d.pages) for d in job.draft_plan.documents)
                if job.draft_plan
                else 0,
                "excluded": len(job.draft_plan.excluded_pages) if job.draft_plan else 0,
            },
            "links": {
                "status": f"/api/v1/jobs/{job.id}",
                "result": f"/api/v1/jobs/{job.id}/result",
                "extractions": f"/api/v1/jobs/{job.id}/extractions",
                "events": f"/api/v1/jobs/{job.id}/events",
                "pages": f"/api/v1/jobs/{job.id}/pages",
                "segmentation": f"/api/v1/jobs/{job.id}/segmentation",
                "archive": self.archive_path(job.id),
            },
        }

    def event_data(self, job: Job, kind: str, data: dict, include_pages: bool = True) -> dict:
        """Удаляет приватные пути из событий; новый SSE клиент не получает полный реестр PNG."""
        if kind in {"pair_started", "pair_result"}:
            return {k: v for k, v in data.items() if k != "fingerprint"}
        if kind == "document_completed":
            return {
                "document_id": data["document_id"],
                "processing": data["checkpoint"]["processing"],
            }
        if kind == "normalized":
            return {
                "pages": self.status(job, include_pages)["pages"],
                "page_count": len(job.page_refs()),
            }
        if kind == "page_ready":
            return {
                "pages": [
                    {
                        "file_id": p["file_id"],
                        "page_number": p["page_number"],
                        "preview_url": self.signed(
                            job,
                            f"/api/v1/jobs/{job.id}/pages/{p['file_id']}/{p['page_number']}/preview",
                        ),
                    }
                    for p in data["pages"]
                ]
            }
        return data

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

    async def extractions(self, job_id: str, owner: str) -> DetailedResult:
        """Выдаёт только реально сохранённый v2; старые результаты v1 не конвертируются молча."""
        legacy = await self.result(job_id, owner)
        job = await self.intake.get(job_id, owner)
        documents = []
        for document in sorted(
            (d for f in legacy["files"] for d in f["documents"]), key=lambda d: d["segment_index"]
        ):
            checkpoint = job.document_results.get(str(document["segment_index"]))
            if (
                not checkpoint
                or checkpoint.extraction_schema_version != "2.0"
                or not isinstance(checkpoint.facts, DetailedFacts)
            ):
                raise IntakeError(
                    "extractions_unavailable",
                    "Задание создано старой версией; JSON 1.0 доступен",
                    409,
                )
            facts = checkpoint.facts
            name, path = kind_metadata(facts.kind_code)
            documents.append(
                DetailedDocument(
                    document_id=document["document_id"],
                    segment_index=document["segment_index"],
                    source_pages=checkpoint.group.pages,
                    classification=facts.classification,
                    kind_code=facts.kind_code,
                    kind_name=name,
                    kind_path=path,
                    requisites=facts.requisites,
                    details=facts.details,
                    field_evidence=facts.field_evidence,
                    one_c=facts.one_c,
                    recognition=facts.recognition,
                    warnings=facts.warnings,
                    attachment_file=document["attachment_file"],
                )
            )
        return DetailedResult(batch_id=job.id, documents=documents)

    async def list_jobs(self, owner: str, offset: int, limit: int) -> dict:
        """Список содержит только безопасную сводку, без OCR, путей, prompts или чужих totals."""
        jobs = await self.intake.list_jobs(owner, offset, min(limit + 1, 100))
        return {
            "jobs": [
                {
                    "job_id": job.id,
                    "status": job.status,
                    "stage": job.stage,
                    "created_at": job.created_at,
                    "completed_at": job.completed_at,
                    "expires_at": job.expires_at,
                    "plan_revision": job.plan_revision,
                    "files": [source.file_name for source in job.sources],
                    "document_count": len(job.draft_plan.documents) if job.draft_plan else 0,
                }
                for job in jobs[:limit]
            ],
            "offset": offset,
            "limit": limit,
            "has_more": len(jobs) > limit,
        }

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
        elif kind == "archive":
            path = self.archive_path(job_id)
        else:
            suffix = "thumbnail" if kind == "thumbnail" else "preview"
            path = f"/api/v1/jobs/{job_id}/pages/{item_id}/{page_number}/{suffix}"
        expires = job.expires_at or utcnow() + timedelta(minutes=5)
        signed = self.capabilities.verify(
            path,
            job.owner,
            expires,
            token,
            allow_expired=bool(job.expires_at and job.expires_at <= utcnow()),
        )
        job.authorize(job.owner if signed else (owner or ""))
        if kind == "archive":
            return job, await self._archive(job)
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
                    "path": preview.get("thumbnail", preview["preview"])
                    if kind == "thumbnail"
                    else preview["preview"],
                    "file_name": "page.png",
                    "mime_type": "image/png",
                }
        raise IntakeError("artifact_not_found", "Артефакт не найден", 404)
