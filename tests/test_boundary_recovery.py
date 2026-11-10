# tests/test_boundary_recovery.py
"""Конкурентные редакции, конечное ожидание, fencing и независимые бюджеты этапов."""

import asyncio
from datetime import timedelta

import pytest

from app.domain.errors import IntakeError, LeaseLost
from app.domain.jobs import utcnow
from app.infrastructure.database import JobRow
from app.worker import process
from tests.support import MemoryUpload, pdf_bytes


async def prepared(container):
    """Готовит реальные рендеры без единого OCR/VLM-вызова."""
    job = await container.intake.upload(
        "integration:onec", [MemoryUpload(pdf_bytes(4))], None, None, False
    )
    await process(container, job.id)
    return await container.database.get(job.id)


async def test_parallel_edits_accept_only_one_revision(container):
    """Две независимые транзакции одной revision не могут обе перезаписать draft."""
    job = await prepared(container)
    results = await asyncio.gather(
        *[
            container.database.save_draft(
                job.id, job.owner, job.plan_revision, job.draft_plan, "edit"
            )
            for _ in range(2)
        ],
        return_exceptions=True,
    )
    assert sum(not isinstance(r, Exception) for r in results) == 1
    failure = next(r for r in results if isinstance(r, IntakeError))
    assert failure.code == "plan_version_conflict"
    assert (await container.database.get(job.id)).plan_revision == job.plan_revision + 1


async def test_waiting_expires_without_worker_and_edit_does_not_extend_ttl(container):
    """Ожидание сохраняет свой час, не удерживает lease и удаляется обычным reaper."""
    job = await prepared(container)
    assert job.expires_at is not None
    edited = await container.database.save_draft(
        job.id, job.owner, job.plan_revision, job.draft_plan, "manual"
    )
    assert edited.expires_at == job.expires_at
    assert not await container.database.heartbeat(job.id, job.fence)
    async with container.database.sessions.begin() as session:
        row = await session.get(JobRow, job.id)
        row.data = {**row.data, "expires_at": (utcnow() - timedelta(seconds=1)).isoformat()}
    assert job.id in await container.database.recover()
    with pytest.raises(IntakeError) as error:
        await container.intake.get(job.id, job.owner)
    assert error.value.code == "job_expired"


async def test_preparation_retry_keeps_budget_and_stale_worker_cannot_write(container):
    """Рестарт не даёт новый бюджет превью и не принимает checkpoint старого fence."""
    job = await container.intake.upload(
        "integration:onec", [MemoryUpload(pdf_bytes())], None, None, False
    )
    first = await container.database.claim(job.id)
    async with container.database.sessions.begin() as session:
        row = await session.get(JobRow, job.id)
        row.lease_until = utcnow() - timedelta(seconds=1)
    assert job.id not in await container.database.recover()
    second = await container.database.claim(job.id)
    assert second.preview_deadline_at == first.preview_deadline_at
    with pytest.raises(LeaseLost):
        await container.database.checkpoint(job.id, first.fence, "page_ready", {"pages": []})
    await container.analysis.prepare(second)
    ready = await container.database.get(job.id)
    assert ready.preview_complete and ready.expires_at > utcnow()
    started = await container.intake.start(job.id, job.owner)
    assert started.queue_deadline_at and started.processing_deadline_at is None
    claimed = await container.database.claim(job.id)
    assert (
        claimed.processing_deadline_at > claimed.queue_deadline_at
        if claimed.queue_deadline_at
        else claimed.processing_deadline_at > utcnow()
    )


async def test_group_page_limit_is_independent_of_source_limit(container):
    """Несколько допустимых источников не позволяют превысить отдельный лимит комплекта."""
    job = await prepared(container)
    container.settings.document_max_pages = 3
    with pytest.raises(IntakeError) as error:
        await container.database.save_draft(
            job.id, job.owner, job.plan_revision, job.draft_plan, "edit"
        )
    assert error.value.code == "page_limit"
