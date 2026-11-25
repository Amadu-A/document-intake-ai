# tests/test_delivery_live.py
"""Opt-in: реальные локальные RabbitMQ/PG и Docker worker; shared стек не изменяется."""

import asyncio

import pytest
import pytest_asyncio
from aio_pika.exceptions import CONNECTION_EXCEPTIONS

from app.domain.errors import LeaseLost
from app.infrastructure.messaging import Rabbit
from tests.delivery_support import DeliveryLab, LostConfirmProxy

pytestmark = pytest.mark.live


@pytest.fixture
def docker_lab(tmp_path, request):
    """Создаёт отдельный стенд; teardown ограничен контейнерами этого теста."""
    lab = DeliveryLab(tmp_path, request.config.getoption("--delivery-image"))
    try:
        lab.setup()
        yield lab
    finally:
        lab.cleanup()


@pytest_asyncio.fixture
async def delivery_lab(docker_lab):
    """Подключает реальные repository/AMQP и всегда закрывает соединения."""
    await docker_lab.open()
    try:
        yield docker_lab
    finally:
        await docker_lab.close()


async def test_confirmed_backlog_does_not_grow_while_worker_is_busy(delivery_lab):
    """Подтверждённое ожидающее задание не публикуется повторно каждые 30 секунд."""
    lab = delivery_lab
    first = await lab.job()
    await asyncio.to_thread(lab.worker)
    await lab.wait_status(first.id, "running")
    second = await lab.job()
    await lab.ready()
    before = await lab.info()
    await asyncio.sleep(38)
    after = await lab.info()
    print(
        {
            "before_ready": before["messages_ready"],
            "after_ready": after["messages_ready"],
            "unacked": after["messages_unacknowledged"],
        }
    )
    assert after["messages_ready"] == 1
    assert after["messages_unacknowledged"] == 0
    await lab.release(first.id)
    await lab.wait_status(first.id, "completed")
    await lab.wait_status(second.id, "running")
    await lab.release(second.id)
    assert (await lab.wait_status(second.id, "completed")).attempt == 1


async def test_lost_actual_publisher_confirm_preserves_retry_and_one_business_claim(delivery_lab):
    """Потеря настоящего confirm в TCP допускает повтор, сохраняя задание."""
    lab = delivery_lab
    job = await lab.job()
    async with LostConfirmProxy(lab.mq_port) as proxy:
        settings = lab.settings.model_copy(update={"rabbitmq_port": proxy.port})
        publisher = Rabbit(settings)
        await publisher.connect()
        try:
            with pytest.raises((*CONNECTION_EXCEPTIONS, TimeoutError)):
                async with asyncio.timeout(8):
                    await publisher.publish(job.id)
            assert proxy.dropped.is_set()
            assert await lab.database.pending() == [job.id]
        finally:
            await publisher.close()
    await lab.ready()
    signal = (await lab.database.pending_publications())[0]
    await lab.rabbit.publish(job.id)
    await lab.database.published(*signal)
    await lab.ready(2)
    await asyncio.to_thread(lab.worker)
    await lab.wait_status(job.id, "running")
    await lab.release(job.id)
    completed = await lab.wait_status(job.id, "completed")
    assert completed.attempt == 1
    assert sum(e["event"] == "started" for e in await lab.database.events(job.id, 0)) == 1


async def test_connection_failure_and_broker_restart_preserve_confirmed_and_pending_jobs(
    delivery_lab,
):
    """Durable брокер хранит confirmed сообщение; ошибка отправки сохраняет outbox."""
    lab = delivery_lab
    first = await lab.job()
    await lab.rabbit.publish(first.id)
    await lab.database.published(*(await lab.database.pending_publications())[0])
    await lab.ready()
    await asyncio.to_thread(lab.command, "stop", lab.prefix + "-mq")
    second = await lab.job()
    with pytest.raises((*CONNECTION_EXCEPTIONS, TimeoutError)):
        async with asyncio.timeout(7):
            await lab.rabbit.publish(second.id)
    assert await lab.database.pending() == [second.id]
    await asyncio.to_thread(lab.command, "start", lab.prefix + "-mq")
    await asyncio.to_thread(lab.wait_ready)
    await lab.ready()
    await lab.release(first.id)
    await lab.release(second.id)
    await asyncio.to_thread(lab.worker)
    assert (await lab.wait_status(first.id, "completed")).attempt == 1
    assert (await lab.wait_status(second.id, "completed")).attempt == 1


async def test_worker_sigkill_and_all_container_restarts_recover_without_lost_jobs(delivery_lab):
    """SIGKILL после claim/ACK и restart контейнеров сохраняют backlog и fencing recovery."""
    lab = delivery_lab
    first = await lab.job()
    worker = await asyncio.to_thread(lab.worker)
    original = await lab.wait_status(first.id, "running")
    second = await lab.job()
    await lab.ready()
    await asyncio.to_thread(lab.command, "kill", worker)
    await asyncio.to_thread(lab.command, "stop", lab.prefix + "-mq", lab.prefix + "-pg")
    await asyncio.to_thread(lab.command, "start", lab.prefix + "-pg", lab.prefix + "-mq")
    await asyncio.to_thread(lab.wait_ready)
    assert (await lab.database.get(first.id)).fence == original.fence
    await lab.ready()
    # Восстановление должно работать и когда lease истёк до старта нового worker.
    await asyncio.sleep(lab.settings.worker_lease_seconds + 1)
    await lab.release(second.id)
    # Повторная доставка не обязана сохранить FIFO: обе операции после crash разрешены.
    await lab.release(first.id)
    await asyncio.to_thread(lab.command, "start", worker)
    try:
        await lab.wait_status(second.id, "completed", seconds=60)
    except TimeoutError:
        states = [await lab.database.get(identifier) for identifier in (first.id, second.id)]
        print("recovery_states", [(j.status, j.attempt, j.fence) for j in states])
        raise
    recovered = await lab.wait_status(first.id, "completed")
    assert recovered.attempt == 2 and recovered.fence > original.fence
    with pytest.raises(LeaseLost):
        await lab.database.finish(first.id, original.fence, {}, {}, None, False)
