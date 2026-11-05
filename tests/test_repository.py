# tests/test_repository.py
"""Функциональные проверки lease/fencing, recovery, outbox, cancellation и TTL."""

import asyncio
from datetime import timedelta

import pytest

from app.application.intake import Intake
from app.domain.errors import IntakeError, LeaseLost
from app.domain.jobs import utcnow
from app.infrastructure.database import JobRow
from tests.support import MemoryUpload, pdf_bytes


async def create(container):
    """Создаёт queued-задание через application use-case, включая outbox."""
    return await container.intake.upload(
        "integration:onec", [MemoryUpload(pdf_bytes())], None, None, True
    )


async def test_outbox_survives_published_signal_until_claim(container):
    """Потеря подтверждения AMQP не теряет работу: durable outbox остаётся до claim."""
    job = await create(container)
    assert await container.database.pending() == [job.id]
    await container.database.published(job.id)
    assert await container.database.pending() == []
    claimed = await container.database.claim(job.id)
    assert claimed.fence == 1
    assert await container.database.claim(job.id) is None


async def test_restart_recovery_fences_old_worker(container):
    """После истёкшего lease новое поколение получает задачу, старое не публикует результат."""
    job = await create(container)
    first = await container.database.claim(job.id)
    async with container.database.sessions.begin() as session:
        row = await session.get(JobRow, job.id)
        row.lease_until = utcnow() - timedelta(seconds=1)
    await container.database.recover()
    second = await container.database.claim(job.id)
    assert second.fence > first.fence
    assert second.attempt == 2
    with pytest.raises(LeaseLost):
        await container.database.finish(job.id, first.fence, {}, {}, None, False)
    assert not await container.database.heartbeat(job.id, first.fence)


async def test_cancel_fences_running_worker(container):
    """После отмены heartbeat и checkpoint прежнего worker не продлевают жизнь задания."""
    job = await create(container)
    running = await container.database.claim(job.id)
    cancelled = await container.database.cancel(job.id, job.owner)
    assert cancelled.status == "cancelled"
    assert cancelled.expires_at == cancelled.completed_at + timedelta(hours=1)
    assert not await container.database.heartbeat(job.id, running.fence)
    with pytest.raises(LeaseLost):
        await container.database.checkpoint(
            job.id, running.fence, "stage", {"stage": "late", "progress": 90}
        )


async def test_deadline_is_separate_from_retention(container):
    """Общее время обработки ограничено независимо; ошибка начинает новый полный час хранения."""
    job = await create(container)
    async with container.database.sessions.begin() as session:
        row = await session.get(JobRow, job.id)
        data = dict(row.data)
        data["deadline_at"] = (utcnow() - timedelta(seconds=1)).isoformat()
        row.data = data
    await container.database.recover()
    failed = await container.database.get(job.id)
    assert failed.status == "failed"
    assert failed.error["code"] == "job_timeout"
    assert failed.expires_at == failed.completed_at + timedelta(hours=1)


async def test_reaper_does_not_select_active_jobs(container):
    """Физическая очистка не получает идентификатор активного задания."""
    job = await create(container)
    await container.database.claim(job.id)
    assert job.id not in await container.database.recover()
    assert container.storage.directory(job.id).exists()


async def test_queue_and_owner_admission_limits(container):
    """Backpressure проверяется транзакционно до принятия нового задания."""
    container.settings.queue_max_depth = 1
    await create(container)
    with pytest.raises(IntakeError) as error:
        await create(container)
    assert error.value.code == "queue_full"
    assert len(list(container.storage.root.iterdir())) == 1


async def test_cancelled_response_after_commit_keeps_originals(container):
    """Разрыв HTTP после commit не должен удалить оригиналы уже принятого queued-задания."""

    class CommitThenCancel:
        """Моделирует потерянный ответ после реальной фиксации create/outbox."""

        async def create(self, job, key, digest):
            """Сначала сохраняет задание, затем теряет ответ вызывающему коду."""
            await container.database.create(job, key, digest)
            raise asyncio.CancelledError

    intake = Intake(CommitThenCancel(), container.storage, container.prompts)
    with pytest.raises(asyncio.CancelledError):
        await intake.upload("integration:onec", [MemoryUpload(pdf_bytes())], None, None, True)
    pending = await container.database.pending()
    assert len(pending) == 1
    job = await container.database.get(pending[0])
    assert container.storage.resolve(job.sources[0].path).is_file()
