# tests/test_adjacent_migration.py
"""Обновление алгоритма не переисполняет старые задания и не теряет готовые результаты."""

import importlib

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations

from app.domain.jobs import Job
from app.infrastructure.database import JobRow
from app.worker import process
from tests.support import MemoryUpload, pdf_bytes


async def migrate(container):
    """Применяет настоящую ревизию данных к SQLite, созданной тестовым repository."""
    module = importlib.import_module("migrations.versions.0002_adjacent_ocr")

    def upgrade(connection):
        """Подключает стандартный Alembic Operations к собственной тестовой БД."""
        with Operations.context(MigrationContext.configure(connection)):
            module.upgrade()

    async with container.database.engine.begin() as connection:
        await connection.run_sync(upgrade)


@pytest.mark.parametrize("completed", [False, True])
async def test_legacy_jobs_are_retired_without_reinterpreting_results(container, completed):
    """Активное задание завершается диагностикой, terminal сохраняет JSON и артефакты."""
    job = await container.intake.upload(
        "integration:onec", [MemoryUpload(pdf_bytes(2))], None, None, True, auto_split=True
    )
    if completed:
        await process(container, job.id)
    original = await container.database.get(job.id)
    async with container.database.sessions.begin() as session:
        row = await session.get(JobRow, job.id)
        legacy = dict(row.data)
        legacy.pop("algorithm_version")
        legacy.pop("comparisons")
        legacy["proposed_plan"] = {
            "documents": [
                {
                    "document_index": 1,
                    "pages": [{"file_id": job.sources[0].file_id, "page_number": 1}],
                }
            ]
        }
        row.data = legacy
    await migrate(container)
    upgraded = await container.database.get(job.id)
    assert upgraded.algorithm_version == "retired-window-v3"
    Job.model_validate(upgraded.model_dump())
    if completed:
        assert upgraded.result == original.result
        assert upgraded.manifest == original.manifest
        assert upgraded.expires_at == original.expires_at
        assert upgraded.status == original.status
    else:
        assert upgraded.status == "failed" and upgraded.error["code"] == "algorithm_version_changed"
        assert upgraded.fence > original.fence
        assert job.id not in await container.database.pending()
        assert await container.database.claim(job.id) is None
    first = upgraded.model_dump()
    await migrate(container)
    assert (await container.database.get(job.id)).model_dump() == first


async def test_session_upgrade_resets_only_old_segmentation_prompt(container):
    """Старый формат границ не попадает в новый запрос; пользовательский extraction сохраняется."""
    extraction = container.prompts.base["extraction"] + "\nСинтетическая правка пользователя"
    await container.database.save_prompts(
        "synthetic", {"segmentation": "Старый формат групп", "extraction": extraction}
    )
    values = (await container.prompts.get("synthetic"))["prompts"]
    assert values == {
        "segmentation": container.prompts.base["segmentation"],
        "extraction": extraction,
    }
    values["segmentation"] += "\nСинтетическая новая правка"
    await container.prompts.save("synthetic", values)
    assert (await container.prompts.get("synthetic"))["prompts"] == values


def test_pair_settings_follow_catalog_private_environment_precedence(tmp_path, monkeypatch):
    """Параллелизм и повторы настраиваются тем же Settings, что и остальные параметры."""
    from app.core.settings import Settings

    catalog, private = tmp_path / "catalog", tmp_path / "private"
    catalog.write_text("PAIR_CONCURRENCY=4\nPAIR_MAX_ATTEMPTS=3\n", "utf-8")
    private.write_text("PAIR_CONCURRENCY=2\n", "utf-8")
    assert Settings(_env_file=(catalog, private)).pair_concurrency == 2
    monkeypatch.setenv("PAIR_CONCURRENCY", "8")
    assert Settings(_env_file=(catalog, private)).pair_concurrency == 8
    assert Settings(_env_file=(catalog, private)).pair_max_attempts == 3
