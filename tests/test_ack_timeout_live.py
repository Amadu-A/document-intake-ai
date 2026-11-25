# tests/test_ack_timeout_live.py
"""Настоящий consumer timeout на отдельном брокере: короткий ACK и три ожидающих пакета."""

import asyncio

import pytest

from app.infrastructure.health import Health
from tests.delivery_support import DeliveryLab, docker
from tests.test_delivery_live import delivery_lab as delivery_lab
from tests.test_delivery_live import docker_lab as docker_lab

pytestmark = pytest.mark.live


async def test_long_work_ack_and_three_waiting_jobs(tmp_path, request):
    """Долгая операция не удерживает AMQP delivery и не мешает трём пакетам после неё."""
    lab = DeliveryLab(tmp_path, request.config.getoption("--delivery-image"), 60000)
    opened = False
    try:
        await asyncio.to_thread(lab.setup)
        actual = await asyncio.to_thread(
            docker,
            "exec",
            lab.prefix + "-mq",
            "rabbitmqctl",
            "eval",
            "application:get_env(rabbit, consumer_timeout).",
        )
        assert "60000" in actual
        await lab.open()
        opened = True
        first = await lab.job()
        await asyncio.to_thread(lab.worker)
        await lab.wait_status(first.id, "running")
        waiting = [await lab.job() for _ in range(3)]
        await lab.ready(3)
        for _ in range(13):
            await asyncio.sleep(10)
        logs = await asyncio.to_thread(docker, "logs", lab.prefix + "-mq")
        state = await lab.info()
        print(
            "ack_timeout",
            {
                "ready": state["messages_ready"],
                "unacked": state["messages_unacknowledged"],
                "broker_timeout": "delivery acknowledgement on channel" in logs,
            },
        )
        assert "delivery acknowledgement on channel" not in logs
        assert state["messages_unacknowledged"] == 0 and state["messages_ready"] == 3
        assert await lab.database.has_active_work()
        await Health(lab.database, lab.rabbit, None, None, None)._consumer()
        for queued in waiting:
            saved = await lab.database.get(queued.id)
            assert saved.attempt == 0 and saved.status == "queued"
        await lab.release(first.id)
        for queued in waiting:
            await lab.release(queued.id)
        for queued in [first, *waiting]:
            assert (await lab.wait_status(queued.id, "completed", seconds=60)).attempt == 1
    finally:
        if opened:
            await lab.close()
        await asyncio.to_thread(lab.cleanup)


@pytest.mark.parametrize("boundary", ["before_claim", "before_ack", "after_ack"])
async def test_sigkill_at_claim_ack_boundary_has_no_lost_work(delivery_lab, boundary):
    """Crash до claim, между commit/ACK и после ACK восстанавливает работу из брокера/lease."""
    lab = delivery_lab
    job = await lab.job()
    worker = await asyncio.to_thread(lab.worker, boundary)
    marker = lab.root / (boundary + "-" + job.id)
    async with asyncio.timeout(40):
        while not await asyncio.to_thread(marker.is_file):  # noqa: ASYNC110 — внешний worker.
            await asyncio.sleep(0.1)
    original = await lab.database.get(job.id)
    assert original.attempt == (0 if boundary == "before_claim" else 1)
    if boundary == "after_ack":
        async with asyncio.timeout(20):
            while True:
                counts = await lab.info()
                if counts.get("messages", 1) == 0:
                    break
                await asyncio.sleep(0.2)
    await asyncio.to_thread(lab.command, "kill", worker)
    await lab.release(job.id)
    await asyncio.to_thread(lab.command, "start", worker)
    completed = await lab.wait_status(job.id, "completed", seconds=60)
    assert completed.attempt == (1 if boundary == "before_claim" else 2)
    assert completed.fence > original.fence


async def test_completed_pg_checkpoint_survives_ack_and_worker_crash(delivery_lab):
    """Принятая durable страница/checkpoint не вычисляется заново при lease recovery."""
    lab = delivery_lab
    job = await lab.job()
    worker = await asyncio.to_thread(lab.worker)
    async with asyncio.timeout(30):
        while not (await lab.database.get(job.id)).manifest.get("delivery_checkpoint"):  # noqa: ASYNC110
            await asyncio.sleep(0.1)
    await asyncio.to_thread(lab.command, "kill", worker)
    await lab.release(job.id)
    await asyncio.to_thread(lab.command, "start", worker)
    completed = await lab.wait_status(job.id, "completed", seconds=60)
    events = await lab.database.events(job.id, 0)
    assert completed.attempt == 2
    assert sum(e["event"] == "normalized" for e in events) == 1
    assert any(e["data"].get("stage") == "delivery_test_resume" for e in events)


async def test_channel_closed_before_ack_keeps_claim_and_never_nacks_closed_channel(delivery_lab):
    """Настоящее закрытие канала между commit/ACK не теряет lease и не роняет callback/task."""
    lab = delivery_lab
    job = await lab.job()
    worker = await asyncio.to_thread(lab.worker, "before_ack")
    marker = lab.root / ("before_ack-" + job.id)
    async with asyncio.timeout(30):
        while not await asyncio.to_thread(marker.is_file):  # noqa: ASYNC110 — файл child.
            await asyncio.sleep(0.1)
    await lab.close_delivery_channel()
    await lab.release(job.id)
    await asyncio.to_thread((lab.root / ("continue-before_ack-" + job.id)).touch)
    completed = await lab.wait_status(job.id, "completed", seconds=60)
    assert completed.attempt == 1
    events = await lab.database.events(job.id, 0)
    assert any(
        e["event"] == "delivery"
        and e["data"].get("operation") == "ack"
        and e["data"]["outcome"] == "uncertain"
        for e in events
    )
    logs = await asyncio.to_thread(docker, "logs", worker)
    assert "unhandled_error" not in logs and "Task exception was never retrieved" not in logs
