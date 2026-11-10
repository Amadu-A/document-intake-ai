# app/infrastructure/boundary_transactions.py
"""Короткие транзакции плана: owner/revision/fence проверяются вместе с записью outbox."""

from datetime import timedelta

from app.domain.boundaries import (
    ApprovedPlan,
    ManualPlan,
    draft_from_vlm,
    source_file_plan,
    validate_assignment,
    validate_mode,
)
from app.domain.errors import IntakeError, LeaseLost
from app.domain.jobs import TERMINAL, Job, utcnow
from app.domain.segmentation import Plan


class BoundaryTransactions:
    """Расширяет собственный repository переходами редактора без длинного файлового/VLM I/O."""

    def _editable(self, job: Job, owner: str, revision: int) -> None:
        """Скрывает чужой агрегат, проверяет TTL, запрет после lock и optimistic concurrency."""
        job.authorize(owner)
        if job.plan_locked or job.status in TERMINAL:
            raise IntakeError("plan_locked", "Границы уже заблокированы для извлечения", 409)
        if job.plan_revision != revision:
            raise IntakeError("plan_version_conflict", "Черновик изменён; перечитайте план", 409)

    def _waiting(self, job: Job) -> None:
        """Начинает конечное ожидание готового плана; обычные правки не продлевают его TTL."""
        if job.expires_at is None:
            job.expires_at = (job.preview_ready_at or utcnow()) + timedelta(
                seconds=self.settings.confirmation_timeout_seconds
            )
        job.deadline_at = None
        job.queue_deadline_at = None
        job.processing_deadline_at = None

    async def complete_preview(self, job_id: str, fence: int, manifest: dict) -> Job:
        """Фиксирует превью и исходные группы; освобождает prepare либо продолжает анализ."""
        async with self.sessions.begin() as session:
            await self._serialize_admission(session)
            row, job = await self._locked(session, job_id)
            self._assert_worker(row, job, fence)
            job.manifest = manifest
            for source in job.sources:
                source.page_count = manifest["source_counts"][source.file_id]
            job.preview_complete = True
            job.preview_ready_at = job.preview_ready_at or utcnow()
            job.preview_deadline_at = None
            if job.draft_plan is None or job.segmentation_source == "source_files":
                job.draft_plan = source_file_plan(job.page_refs())
                job.plan_revision += 1
            if job.work_kind == "prepare" or not job.processing_requested:
                row.lease_until = None
                manual = job.segmentation_source in {"manual", "manual_override"}
                job.status, job.stage = (
                    ("awaiting_confirmation", "awaiting_confirmation")
                    if manual
                    else ("uploaded", "preview_ready")
                )
                self._waiting(job)
                if job.processing_requested and not manual:
                    await self._queue_work(session, row, job)
            else:
                job.stage = "preview_ready"
                job.processing_deadline_at = utcnow() + timedelta(
                    seconds=self.settings.job_timeout_seconds
                )
                job.deadline_at = job.processing_deadline_at
            self._write(session, row, job, "preview_ready", {"page_count": len(job.page_refs())})
            return job

    async def change_mode(
        self, job_id: str, owner: str, revision: int, auto_split: bool, skip_segmentation: bool
    ) -> Job:
        """Меняет режим под row lock, не включая скрытый повторный запуск анализа."""
        validate_mode(auto_split, skip_segmentation)
        async with self.sessions.begin() as session:
            row, job = await self._locked(session, job_id)
            self._editable(job, owner, revision)
            job.auto_split, job.skip_segmentation = auto_split, skip_segmentation
            job.processing_requested = False
            job.plan_revision += 1
            job.draft_plan = (
                draft_from_vlm(job.proposed_plan)
                if job.proposed_plan and not skip_segmentation
                else source_file_plan(job.page_refs())
            )
            job.segmentation_source = (
                ("vlm_auto" if auto_split else "vlm_proposed")
                if job.proposed_plan and not skip_segmentation
                else "source_files"
            )
            if job.preview_complete:
                job.fence += 1
                row.lease_until = None
                job.status, job.stage = "uploaded", "preview_ready"
                self._waiting(job)
                await self._drop_signal(session, job.id)
            else:
                job.work_kind = "prepare"
                if job.status in {"queued", "running"}:
                    job.status, job.stage = "uploaded", "preparing_preview"
            self._write(
                session,
                row,
                job,
                "mode_changed",
                {
                    "action": "mode",
                    "revision": job.plan_revision,
                    "actor": owner,
                    "at": utcnow().isoformat(),
                },
            )
            return job

    async def save_draft(
        self, job_id: str, owner: str, revision: int, plan: ManualPlan, action: str
    ) -> Job:
        """Одной записью сохраняет ручные границы и переводит режим в skip, без вызова VLM."""
        async with self.sessions.begin() as session:
            row, job = await self._locked(session, job_id)
            self._editable(job, owner, revision)
            validate_assignment(
                plan,
                job.page_refs(),
                self.settings.document_max_pages,
                complete=job.preview_complete,
            )
            job.auto_split, job.skip_segmentation = False, True
            job.segmentation_source = "manual_override" if job.proposed_plan else "manual"
            job.processing_requested = False
            job.draft_plan = plan.model_copy(deep=True)
            job.plan_revision += 1
            if job.preview_complete:
                job.fence += 1
                row.lease_until = None
                job.status, job.stage = "awaiting_confirmation", "awaiting_confirmation"
                self._waiting(job)
                await self._drop_signal(session, job.id)
            else:
                job.work_kind = "prepare"
                if job.status in {"queued", "running"}:
                    job.status, job.stage = "uploaded", "preparing_preview"
            self._write(
                session,
                row,
                job,
                "draft_saved",
                {
                    "action": action,
                    "revision": job.plan_revision,
                    "actor": owner,
                    "at": utcnow().isoformat(),
                },
            )
            return job

    async def propose(self, job_id: str, fence: int, plan: Plan) -> Job:
        """Сохраняет предложение единожды; review освобождает lease до подтверждения."""
        async with self.sessions.begin() as session:
            row, job = await self._locked(session, job_id)
            self._assert_worker(row, job, fence)
            if job.skip_segmentation or job.approved_plan:
                raise LeaseLost()
            if job.proposed_plan is None:
                job.proposed_plan = plan.model_copy(deep=True)
            job.draft_plan = draft_from_vlm(job.proposed_plan)
            job.plan_revision += 1
            job.manifest = {**job.manifest, "plan": job.proposed_plan.model_dump(mode="json")}
            job.segmentation_source = "vlm_auto" if job.auto_split else "vlm_proposed"
            if not job.auto_split:
                job.status, job.stage = "awaiting_confirmation", "awaiting_confirmation"
                row.lease_until = None
                job.expires_at = (job.preview_ready_at or utcnow()) + timedelta(
                    seconds=self.settings.confirmation_timeout_seconds
                )
                self._waiting(job)
                await self._drop_signal(session, job.id)
            self._write(
                session, row, job, "segmentation", job.proposed_plan.model_dump(mode="json")
            )
            return job

    async def confirm(self, job_id: str, owner: str, revision: int, prompts: dict[str, str]) -> Job:
        """Утверждает одну ревизию один раз и атомарно создаёт единственный сигнал extraction."""
        async with self.sessions.begin() as session:
            await self._serialize_admission(session)
            row, job = await self._locked(session, job_id)
            job.authorize(owner)
            if job.approved_revision == revision and job.approved_plan is not None:
                return job
            self._editable(job, owner, revision)
            if not job.preview_complete:
                raise IntakeError(
                    "preview_not_ready", "Подготовка всех страниц ещё не завершена", 409
                )
            if not job.skip_segmentation and not job.proposed_plan:
                raise IntakeError(
                    "segmentation_confirmation_required", "Сначала получите предложение VLM", 409
                )
            if job.draft_plan is None:
                raise IntakeError("invalid_segmentation", "Черновик отсутствует")
            validate_assignment(
                job.draft_plan,
                job.page_refs(),
                self.settings.document_max_pages,
                require_documents=True,
            )
            job.approved_plan = ApprovedPlan.from_draft(job.draft_plan)
            job.approved_revision = revision
            job.plan_locked = True
            if job.segmentation_source == "vlm_proposed":
                job.segmentation_source = "vlm_confirmed"
            job.fence += 1
            row.lease_until = None
            job.processing_requested = True
            if not job.prompts:
                job.prompts = dict(prompts)
            await self._queue_work(session, row, job)
            self._write(
                session,
                row,
                job,
                "plan_approved",
                {
                    "action": "approve",
                    "revision": revision,
                    "actor": owner,
                    "at": utcnow().isoformat(),
                },
            )
            return job

    async def lock_plan(self, job_id: str, fence: int) -> Job:
        """Блокирует auto/source plan перед extraction с проверкой актуального fence."""
        async with self.sessions.begin() as session:
            row, job = await self._locked(session, job_id)
            self._assert_worker(row, job, fence)
            if job.approved_plan is None:
                if not job.auto_split and not (
                    job.skip_segmentation and job.segmentation_source == "source_files"
                ):
                    raise IntakeError(
                        "segmentation_confirmation_required", "Границы ожидают подтверждения", 409
                    )
                if job.draft_plan is None:
                    raise IntakeError("invalid_segmentation", "Нет границ для извлечения")
                validate_assignment(
                    job.draft_plan,
                    job.page_refs(),
                    self.settings.document_max_pages,
                    require_documents=True,
                )
                job.approved_plan = ApprovedPlan.from_draft(job.draft_plan)
                job.approved_revision = job.plan_revision
            job.plan_locked = True
            job.expires_at = None
            self._write(session, row, job, "plan_locked", {"revision": job.approved_revision})
            return job
