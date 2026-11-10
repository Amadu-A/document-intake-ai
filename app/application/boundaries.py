# app/application/boundaries.py
"""Сценарии редактора обращаются к атомарному порту; SQL и переключения UI здесь отсутствуют."""

from app.application.intake import Intake
from app.application.ports import BoundaryRepository
from app.application.prompts import Prompts
from app.core.observability import timed
from app.domain.boundaries import ManualPlan, validate_mode
from app.domain.jobs import Job


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
