# app/worker.py
"""Ограниченный AMQP worker с outbox, recovery, lease heartbeat и TTL-очисткой."""

import asyncio
import hashlib
import json
import logging
import os
import tempfile
from pathlib import Path
from time import monotonic
from uuid import UUID

from app.core.container import Container
from app.core.observability import configure_logging
from app.core.settings import Settings
from app.domain.errors import IntakeError, LeaseLost
from app.domain.jobs import Job, utcnow


async def process(container: Container, job_id: str) -> None:
    """Сохраняет прямой драйвер use-case для проверок; AMQP подтверждается в delivery loop."""
    job = await container.database.claim(job_id)
    if not job:
        return
    await process_claimed(container, job)


async def process_claimed(container: Container, job: Job) -> None:
    """Выполняет уже закоммиченный claim; heartbeat/fencing действуют независимо от ACK канала."""
    operation = (
        container.analysis.prepare if job.work_kind == "prepare" else container.analysis.execute
    )
    analysis = asyncio.create_task(operation(job))
    heartbeat_at = monotonic() + container.settings.worker_lease_seconds / 3
    try:
        while not analysis.done():
            done, _ = await asyncio.wait({analysis}, timeout=0.25)
            if done:
                break
            active = await container.database.is_active(job.id, job.fence)
            if active and monotonic() >= heartbeat_at:
                active = await container.database.heartbeat(job.id, job.fence)
                heartbeat_at = monotonic() + container.settings.worker_lease_seconds / 3
            if not active:
                analysis.cancel()
                break
        await analysis
    except (asyncio.CancelledError, LeaseLost):
        analysis.cancel()
        await asyncio.gather(analysis, return_exceptions=True)
        if asyncio.current_task().cancelling():
            # Внешняя остановка worker должна прерывать и вызывающий consumer loop.
            raise asyncio.CancelledError from None
    except Exception:
        analysis.cancel()
        await asyncio.gather(analysis, return_exceptions=True)
        logging.getLogger("intake").error('{"event":"worker_failure","status":"error"}')


async def maintenance(container: Container) -> None:
    """Независимо публикует outbox, восстанавливает lease и удаляет истёкшие данные."""
    while True:
        try:
            await asyncio.to_thread((Path(tempfile.gettempdir()) / "intake-worker-heartbeat").touch)
            expired = await container.database.recover()
            for job_id in expired:
                await container.storage.remove(job_id)
                await container.database.purge(job_id)
            for job_id, fence in await container.database.pending_publications():
                snapshot = await container.database.get(job_id)
                snapshot = snapshot if snapshot.fence == fence else None
                started = monotonic()
                try:
                    await container.rabbit.publish(job_id)
                except Exception as error:
                    _delivery_log("publish", "uncertain", snapshot, type(error).__name__)
                    raise
                await container.database.published(job_id, fence)
                await container.database.delivery_event(job_id, fence, "publish", "confirmed")
                _delivery_log(
                    "publish",
                    "confirmed",
                    snapshot,
                    duration_ms=round((monotonic() - started) * 1000, 2),
                )
            # После crash upload может остаться без DB-строки; активные uploads младше часа.
            for directory in container.storage.root.iterdir():
                try:
                    UUID(directory.name)
                except ValueError:
                    continue
                if directory.is_dir() and utcnow().timestamp() - directory.stat().st_mtime > 3600:
                    try:
                        await container.database.get(directory.name)
                    except IntakeError as exc:
                        if exc.code == "job_not_found":
                            await container.storage.remove(directory.name)
        except Exception:
            logging.getLogger("intake").warning('{"event":"maintenance_retry","status":"error"}')
        await asyncio.sleep(5)


async def run(container: Container) -> None:
    """Каждый slot получает одну доставку, фиксирует claim/ACK и затем выполняет работу.

    Подписка отменяется ДО ACK при prefetch=1, поэтому следующая доставка не
    удерживается unacked во время OCR. После claim ответственность несёт lease PG.
    Число slot tasks фиксировано; ожидания semaphore и RAM-очереди заданий нет.
    """
    await container.rabbit.connect()
    background = asyncio.create_task(maintenance(container))
    tasks = [
        asyncio.create_task(_consume_slot(container))
        for _ in range(container.settings.worker_concurrency)
    ]
    try:
        await asyncio.gather(background, *tasks)
    finally:
        background.cancel()
        for task in tasks:
            task.cancel()
        await asyncio.gather(background, *tasks, return_exceptions=True)


def _delivery_log(
    operation: str,
    outcome: str,
    job: Job | None = None,
    reason=None,
    duration_ms: float | None = None,
) -> None:
    """Пишет безопасную корреляцию/UTC без ID владельца, файлов, секретов и AMQP payload."""
    logging.getLogger("intake").info(
        json.dumps(
            {
                "event": "job_delivery",
                "timestamp": utcnow().isoformat(),
                "operation": operation,
                "outcome": outcome,
                "reason": reason,
                "duration_ms": duration_ms,
                "job_ref": hashlib.sha256(job.id.encode()).hexdigest()[:12] if job else None,
                "fence": job.fence if job else None,
                "attempt": job.attempt if job else None,
                "phase": job.work_kind if job else None,
            }
        )
    )


async def _settle(message, operation: str, job: Job | None = None) -> bool:
    """Подтверждает только исходный живой канал; после неопределённого ACK NACK не посылает."""
    started = monotonic()
    try:
        if message.channel.is_closed:
            raise ConnectionError("closed channel")
        if operation == "ack":
            await message.ack()
        elif operation == "reject":
            await message.reject(requeue=False)
        else:
            await message.nack(requeue=True)
    except Exception as error:
        _delivery_log(operation, "channel_lost", job, type(error).__name__)
        return False
    _delivery_log(operation, "sent", job, duration_ms=round((monotonic() - started) * 1000, 2))
    return True


async def _consume_slot(container: Container) -> None:
    """Получает работу только при свободном slot; сбой транспорта не отменяет committed lease."""
    while True:
        job = None
        try:
            async with container.rabbit.delivery() as message:
                try:
                    job_id = message.body.decode("ascii")
                    UUID(job_id)
                except (ValueError, UnicodeError):
                    await _settle(message, "reject")
                    continue
                try:
                    job = await container.database.claim(job_id)
                except Exception as error:
                    _delivery_log("claim", "retry", reason=type(error).__name__)
                    await _settle(message, "nack")
                    await asyncio.sleep(1)
                    continue
                _delivery_log("claim", "acquired" if job else "duplicate_or_terminal", job)
                acknowledged = await _settle(message, "ack", job)
                if job:
                    await container.database.delivery_event(
                        job.id, job.fence, "ack", "sent" if acknowledged else "uncertain"
                    )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            _delivery_log("consumer", "channel_lost", job, type(error).__name__)
            if not job:
                await asyncio.sleep(1)
        if job:
            await process_claimed(container, job)


async def main() -> None:
    """Запускает собственный worker и освобождает ресурсы при остановке процесса."""
    os.umask(0o077)
    settings = Settings()
    configure_logging(settings.log_level)
    container = Container(settings)
    try:
        await run(container)
    finally:
        await container.close()


if __name__ == "__main__":
    asyncio.run(main())
