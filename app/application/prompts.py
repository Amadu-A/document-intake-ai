# app/application/prompts.py
"""Два независимых редактируемых промпта; базовые версии не меняются сессией."""

import hashlib
from pathlib import Path

from app.application.ports import PromptRepository
from app.domain.errors import IntakeError


class Prompts:
    """Читает базовые тексты, применяет owner overrides и отдаёт immutable копии."""

    def __init__(self, repository: PromptRepository, directory: Path) -> None:
        """Подключает repository и каталог утверждённых промптов при сборке приложения."""
        self.repository = repository
        self.versions = {"segmentation": "v2", "extraction": "v1"}
        self.base = {
            stage: (directory / f"{stage}-{self.versions[stage]}.txt").read_text("utf-8")
            for stage in ("segmentation", "extraction")
        }

    async def get(self, owner: str) -> dict:
        """Возвращает оба текста и стабильные хеши базовых версий."""
        saved = await self.repository.get_prompts(owner)
        return {
            "prompts": saved or dict(self.base),
            "base_prompts": dict(self.base),
            "versions": {
                k: self.versions[k] + ":" + hashlib.sha256(v.encode()).hexdigest()[:12]
                for k, v in self.base.items()
            },
        }

    async def save(self, owner: str, prompts: dict[str, str]) -> dict:
        """Валидирует размер и сохраняет оба текста только для текущего owner."""
        if set(prompts) != set(self.base) or any(
            not 20 <= len(v) <= 30000 for v in prompts.values()
        ):
            raise IntakeError("invalid_prompts", "Нужны два промпта длиной от 20 до 30000 символов")
        await self.repository.save_prompts(owner, dict(prompts))
        return await self.get(owner)

    async def restore(self, owner: str, stage: str) -> dict:
        """Восстанавливает только выбранную вкладку, сохраняя другую."""
        if stage not in self.base:
            raise IntakeError("invalid_stage", "Неизвестный этап промпта")
        values = (await self.get(owner))["prompts"]
        values[stage] = self.base[stage]
        return await self.save(owner, values)
