# tests/test_checkpoint_cancellation.py
"""Отмена начатой SQL-записи дожидается транзакции и сохраняет проверку fencing."""

import asyncio
from uuid import uuid4

import pytest

from app.domain.errors import LeaseLost
from app.domain.jobs import Job, utcnow


@pytest.mark.parametrize("cancellations", [1, 2])
async def test_checkpoint_cancellation_drains_sql_transaction(
    container, monkeypatch, cancellations
):
    """Удерживает настоящий SQLite write lock во время отмены; следующая запись не блокируется."""
    database = container.database
    job = Job(
        id=str(uuid4()),
        owner="integration:onec",
        status="queued",
        created_at=utcnow(),
        expires_at=None,
        sources=[],
        prompts={},
    )
    await database.create(job, None, "0" * 64)
    claimed = await database.claim(job.id)
    entered, release = asyncio.Event(), asyncio.Event()
    original = database._locked
    paused = False

    async def locked(session, identifier):
        """Пауза после BEGIN IMMEDIATE/SELECT удерживает настоящую write-транзакцию."""
        nonlocal paused
        value = await original(session, identifier)
        if not paused:
            paused = True
            entered.set()
            await release.wait()
        return value

    monkeypatch.setattr(database, "_locked", locked)
    driver = asyncio.create_task(
        database.checkpoint(
            job.id, claimed.fence, "stage", {"stage": "extraction_vlm", "progress": 60}
        )
    )
    try:
        await asyncio.wait_for(entered.wait(), 5)
        for _ in range(cancellations):
            driver.cancel()
            await asyncio.sleep(0)
            assert not driver.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(driver, 5)
    finally:
        release.set()
        driver.cancel()
        await asyncio.gather(driver, return_exceptions=True)

    saved = await database.get(job.id)
    assert saved.stage == "extraction_vlm" and saved.progress == 60
    assert await database.heartbeat(job.id, claimed.fence)
    await database.cancel(job.id, job.owner)
    with pytest.raises(LeaseLost):
        await database.checkpoint(
            job.id, claimed.fence, "stage", {"stage": "extraction_vlm", "progress": 90}
        )
    saved = await database.get(job.id)
    assert saved.status == "cancelled" and saved.progress == 60
