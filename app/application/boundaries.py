# app/application/boundaries.py
"""Сценарии редактора обращаются к атомарному порту; SQL и переключения UI здесь отсутствуют."""

from app.application.intake import Intake
from app.application.ports import BoundaryRepository
from app.application.prompts import Prompts
from app.core.observability import timed
from app.domain.boundaries import ManualPlan, source_file_plan, validate_mode
from app.domain.errors import IntakeError
from app.domain.jobs import TERMINAL, Job
from app.domain.plan_edits import PlanEdit, edit_plan


class ManageBoundaries:
    """Даёт одинаковые операции границ web-клиенту и независимым интеграциям."""

    def __init__(self, repository: BoundaryRepository, intake: Intake, prompts: Prompts) -> None:
        """Получает зависимости; атомарный порт проверяет owner, TTL, revision и fencing."""
        self.repository, self.intake, self.prompts = repository, intake, prompts

    @timed("change_segmentation_mode")
    async def mode(self, job_id: str, owner: str, revision: int, auto: bool, skip: bool) -> Job:
        """Меняет допустимый режим до lock; транзакция порта защищает от конкурентной правки."""
        validate_mode(auto, skip)
        return await self.repository.change_mode(job_id, owner, revision, auto, skip)

    @timed("save_segmentation_draft")
    async def save(
        self, job_id: str, owner: str, revision: int, plan: ManualPlan, action: str
    ) -> Job:
        """Сохраняет ручной план одной атомарной операцией вместе с режимом skip."""
        return await self.repository.save_draft(job_id, owner, revision, plan, action)

    @timed("confirm_segmentation")
    async def confirm(self, job_id: str, owner: str, revision: int) -> Job:
        """Подтверждает ревизию; повтор сохраняет snapshots и не запускает дубли."""
        prompts = (await self.prompts.get(owner))["prompts"]
        return await self.repository.confirm(job_id, owner, revision, prompts)

    @timed("edit_segmentation")
    async def edit(self, job_id: str, owner: str, command: PlanEdit) -> Job:
        """Общий серверный редактор; гонка чтения/записи защищена expected_revision в DB."""
        job = await self.intake.get(job_id, owner)
        if job.plan_locked or job.status in TERMINAL:
            raise IntakeError("plan_locked", "План уже утверждён или задание завершено", 409)
        if command.expected_revision != job.plan_revision:
            raise IntakeError("plan_conflict", "Редакция плана изменилась", 409)
        initial = job.proposed_plan or source_file_plan(job.page_refs())
        plan = (
            initial
            if command.action == "reset"
            else edit_plan(job.draft_plan or initial, command, job.page_refs())
        )
        action = {"move": "page_move", "create": "restore"}.get(command.action, command.action)
        return await self.save(job_id, owner, command.expected_revision, plan, action)
