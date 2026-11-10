# tests/test_worker.py
"""Worker orchestration с реальными repository/storage и synthetic внешними адаптерами."""

import asyncio
from contextlib import suppress
from datetime import timedelta

import pytest

from app.domain.jobs import utcnow
from app.infrastructure.database import JobRow
from app.worker import maintenance, process
from tests.support import MemoryUpload, pdf_bytes


async def test_worker_process_and_duplicate_delivery(container):
    """Повторная доставка готового задания не вызывает второй VLM-анализ."""
    job = await container.intake.upload(
        "integration:onec", [MemoryUpload(pdf_bytes())], None, None, True
    )
    await process(container, job.id)
    assert (await container.database.get(job.id)).status == "completed_with_warnings"
    count = len(container.vision.calls)
    await process(container, job.id)
    assert len(container.vision.calls) == count


async def test_worker_stops_analysis_after_lease_revocation(container):
    """Отмена прекращает зависший analysis; heartbeat не возвращает ему право записи."""
    started, stopped = asyncio.Event(), asyncio.Event()

    class WaitingAnalysis:
        """Моделирует внешний долгий вызов, поддерживающий coroutine cancellation."""

        async def execute(self, job):
            """Ждёт отмены без настоящего VLM-запроса."""
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

    container.analysis = WaitingAnalysis()
    container.settings.worker_lease_seconds = 0.15
    job = await container.intake.upload(
        "integration:onec", [MemoryUpload(pdf_bytes())], None, None, True
    )
    task = asyncio.create_task(process(container, job.id))
    await asyncio.wait_for(started.wait(), timeout=2)
    await container.database.cancel(job.id, job.owner)
    await asyncio.wait_for(task, timeout=2)
    assert stopped.is_set()
    assert (await container.database.get(job.id)).status == "cancelled"


async def test_external_worker_cancel_propagates_to_consumer_loop(container):
    """Остановка consumer не поглощается process после отмены дочернего analysis."""
    started, stopped, continued = asyncio.Event(), asyncio.Event(), asyncio.Event()

    class WaitingAnalysis:
        """Даёт отменить worker именно во время активного use-case."""

        async def execute(self, job):
            """Фиксирует cleanup дочерней операции перед выходом из consumer."""
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

    container.analysis = WaitingAnalysis()
    job = await container.intake.upload(
        "integration:onec", [MemoryUpload(pdf_bytes())], None, None, True
    )

    async def consumer():
        """После внешней отмены следующий виток доставки недопустим."""
        await process(container, job.id)
        continued.set()

    task = asyncio.create_task(consumer())
    await asyncio.wait_for(started.wait(), 3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 3)
    assert task.cancelled() and stopped.is_set() and not continued.is_set()


async def test_real_reaper_cycle_removes_expired_payload(container, monkeypatch):
    """Maintenance удаляет истёкший оригинал и DB payload, сохраняя active job."""
    monkeypatch.setattr(
        "app.worker.tempfile.gettempdir", lambda: str(container.storage.root.parent)
    )
    expired = await container.intake.upload(
        "integration:onec", [MemoryUpload(pdf_bytes())], None, None, False
    )
    await process(container, expired.id)
    active = await container.intake.upload(
        "integration:onec", [MemoryUpload(pdf_bytes())], None, None, False
    )
    async with container.database.sessions.begin() as session:
        row = await session.get(JobRow, expired.id)
        data = dict(row.data)
        data["expires_at"] = (utcnow() - timedelta(seconds=1)).isoformat()
        row.data = data
    removed = asyncio.Event()
    original_purge = container.database.purge

    async def purge(job_id):
        """Отмечает завершение настоящей очистки, не подменяя её результат."""
        await original_purge(job_id)
        removed.set()

    container.database.purge = purge
    task = asyncio.create_task(maintenance(container))
    try:
        await asyncio.wait_for(removed.wait(), timeout=2)
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
    assert not container.storage.directory(expired.id).exists()
    assert container.storage.directory(active.id).exists()
