# tests/test_delivery_handoff.py
"""Ранний ACK, закрытые каналы, бюджеты и heartbeat проверяются без реальных зависимостей."""

import asyncio
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.domain.jobs import Job, utcnow
from app.infrastructure.database import JobRow
from app.infrastructure.health import Health
from app.infrastructure.messaging import NoConsumer
from app.worker import _settle, process
from tests.support import MemoryUpload, pdf_bytes


@pytest.mark.parametrize("operation", ["ack", "nack", "reject"])
async def test_closed_channel_is_never_used_for_ack_or_nack(operation):
    """Заведомо закрытый канал не получает повторный delivery tag или NACK."""
    message = SimpleNamespace(
        channel=SimpleNamespace(is_closed=True),
        ack=AsyncMock(),
        nack=AsyncMock(),
        reject=AsyncMock(),
    )
    assert not await _settle(message, operation)
    message.ack.assert_not_awaited()
    message.nack.assert_not_awaited()
    message.reject.assert_not_awaited()


async def test_uncertain_ack_does_not_trigger_nack():
    """Разрыв после отправки ACK оставляет ответственность lease, а не NACK на новом канале."""
    message = SimpleNamespace(
        channel=SimpleNamespace(is_closed=False),
        ack=AsyncMock(side_effect=ConnectionError()),
        nack=AsyncMock(),
    )
    assert not await _settle(message, "ack")
    message.nack.assert_not_awaited()


@pytest.mark.parametrize("active", [True, False])
async def test_busy_health_requires_live_lease_when_subscription_is_paused(active):
    """Нулевой consumer допустим только с живым lease; ошибка подключения не скрывается."""
    database = SimpleNamespace(has_active_work=AsyncMock(return_value=active))
    rabbit = SimpleNamespace(require_consumer=AsyncMock(side_effect=NoConsumer()))
    health = Health(database, rabbit, None, None, None)
    if active:
        await health._consumer()
    else:
        with pytest.raises(NoConsumer):
            await health._consumer()
    rabbit.require_consumer.side_effect = ConnectionError()
    with pytest.raises(ConnectionError):
        await health._consumer()


@pytest.mark.parametrize(
    "phase,code",
    [
        ("queue", "queue_timeout"),
        ("preview", "preview_timeout"),
        ("analysis", "job_timeout"),
        ("retries", "worker_attempts_exhausted"),
    ],
)
async def test_timeout_reasons_keep_independent_budgets_and_attempts(container, phase, code):
    """Ожидание не считается попыткой обработки, прошлый work_stage и TTL сохраняются честно."""
    job = await container.intake.upload(
        "integration:onec", [MemoryUpload(pdf_bytes())], None, None, True
    )
    if phase != "queue":
        job = await container.database.claim(job.id)
    async with container.database.sessions.begin() as session:
        row = await session.get(JobRow, job.id)
        data = dict(row.data)
        data["work_stage"] = "docling" if phase == "analysis" else None
        if phase == "analysis":
            data.update(preview_complete=True)
        if phase == "retries":
            data["attempt"] = container.settings.worker_max_attempts
            row.lease_until = utcnow() - timedelta(seconds=1)
        else:
            data["deadline_at"] = (utcnow() - timedelta(seconds=1)).isoformat()
        row.data = data
    await container.database.recover()
    failed = await container.database.get(job.id)
    assert failed.error["code"] == code
    assert failed.attempt == (
        0
        if phase == "queue"
        else container.settings.worker_max_attempts
        if phase == "retries"
        else 1
    )
    if phase == "analysis":
        assert failed.work_stage == "docling"
    assert failed.expires_at > failed.completed_at


async def test_failed_lease_check_cancels_business_before_releasing_slot(container):
    """Ошибка БД не оставляет незарегистрированную бизнес-корутину параллельно следующему job."""
    entered, stopped = asyncio.Event(), asyncio.Event()

    class Waiting:
        """Моделирует длительный OCR/VLM с явным наблюдением cancellation cleanup."""

        async def execute(self, job):
            """Не завершает бизнес-операцию до отмены после неудачной проверки lease."""
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

    container.analysis = Waiting()
    container.database.is_active = AsyncMock(side_effect=ConnectionError())
    job = await container.intake.upload(
        "integration:onec", [MemoryUpload(pdf_bytes())], None, None, True
    )
    running = asyncio.create_task(process(container, job.id))
    await asyncio.wait_for(entered.wait(), 2)
    await asyncio.wait_for(running, 2)
    assert stopped.is_set()


async def test_three_jobs_wait_past_old_queue_budget_without_losing_deadline(
    container, monkeypatch
):
    """Управляемые часы: 31 мин ожидания при 4 ч бюджете; heartbeat сохраняет один рабочий lease."""
    point = utcnow()
    monkeypatch.setattr("app.infrastructure.database.utcnow", lambda: point)
    jobs = []
    for _ in range(4):
        job = Job(
            id=str(uuid4()),
            owner="synthetic",
            status="queued",
            created_at=point,
            expires_at=None,
            sources=[],
            prompts={},
            preview_complete=True,
        )
        jobs.append((await container.database.create(job, None, job.id))[0])
    assert container.settings.queue_timeout_seconds == 14400
    first = await container.database.claim(jobs[0].id)
    for _ in range(62):
        point += timedelta(seconds=30)
        assert await container.database.heartbeat(first.id, first.fence)
        await container.database.recover()
    for waiting in jobs[1:]:
        saved = await container.database.get(waiting.id)
        assert saved.status == "queued" and saved.attempt == 0
        assert saved.queue_deadline_at == waiting.queue_deadline_at
    await container.database.finish(
        first.id,
        first.fence,
        {"schema_version": "1.0", "batch_id": first.id, "files": []},
        {},
        None,
        False,
    )
    for waiting in jobs[1:]:
        claimed = await container.database.claim(waiting.id)
        assert claimed.attempt == 1
        await container.database.finish(
            claimed.id,
            claimed.fence,
            {"schema_version": "1.0", "batch_id": claimed.id, "files": []},
            {},
            None,
            False,
        )
    for job in jobs:
        assert (await container.database.get(job.id)).status == "completed"
        assert sum(e["event"] == "started" for e in await container.database.events(job.id, 0)) == 1
