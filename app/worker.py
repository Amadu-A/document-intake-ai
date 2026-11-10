# app/worker.py
"""Ограниченный AMQP worker с outbox, recovery, lease heartbeat и часовым reaper."""

import asyncio
import contextlib
import logging
import os
import tempfile
from pathlib import Path
from uuid import UUID

from app.core.container import Container
from app.core.observability import configure_logging
from app.core.settings import Settings
from app.domain.errors import IntakeError, LeaseLost
from app.domain.jobs import utcnow


async def process(container: Container, job_id: str) -> None:
    """Claim выделяет lease; heartbeat останавливает устаревшее поколение."""
    job = await container.database.claim(job_id)
    if not job:
        return
    operation = (
        container.analysis.prepare if job.work_kind == "prepare" else container.analysis.execute
    )
    analysis = asyncio.create_task(operation(job))
    try:
        while not analysis.done():
            done, _ = await asyncio.wait(
                {analysis}, timeout=container.settings.worker_lease_seconds / 3
            )
            if done:
                break
            if not await container.database.heartbeat(job.id, job.fence):
                analysis.cancel()
                break
        await analysis
    except (asyncio.CancelledError, LeaseLost):
        analysis.cancel()
        with contextlib.suppress(asyncio.CancelledError, LeaseLost):
            await analysis
        if asyncio.current_task().cancelling():
            # Внешняя остановка worker должна прерывать и вызывающий consumer loop.
            raise asyncio.CancelledError from None
    except Exception:
        logging.getLogger("intake").error('{"event":"worker_failure","status":"error"}')


async def maintenance(container: Container) -> None:
    """Независимо публикует outbox, восстанавливает lease и удаляет истёкшие данные."""
    while True:
        await asyncio.to_thread((Path(tempfile.gettempdir()) / "intake-worker-heartbeat").touch)
        try:
            expired = await container.database.recover()
            for job_id in expired:
                await container.storage.remove(job_id)
                await container.database.purge(job_id)
            for job_id in await container.database.pending():
                await container.rabbit.publish(job_id)
                await container.database.published(job_id)
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
    """Подписывает consumer и обрабатывает не более WORKER_CONCURRENCY заданий одновременно."""
    await container.rabbit.connect()
    background = asyncio.create_task(maintenance(container))
    semaphore = asyncio.Semaphore(container.settings.worker_concurrency)
    tasks = set()

    async def handle(message) -> None:
        """Подтверждает доставку после обработки; повтор безопасен благодаря claim/fencing."""
        async with semaphore:
            try:
                job_id = message.body.decode("ascii")
                UUID(job_id)
            except (ValueError, UnicodeError):
                await message.reject(requeue=False)
                return
            try:
                await process(container, job_id)
                await message.ack()
            except Exception:
                await message.nack(requeue=True)

    try:
        async with container.rabbit.queue.iterator() as iterator:
            async for message in iterator:
                task = asyncio.create_task(handle(message))
                tasks.add(task)
                task.add_done_callback(tasks.discard)
    finally:
        background.cancel()
        for task in tasks:
            task.cancel()
        await asyncio.gather(background, *tasks, return_exceptions=True)


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
