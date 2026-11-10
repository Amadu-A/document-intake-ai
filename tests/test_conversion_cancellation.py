# tests/test_conversion_cancellation.py
"""Реальный subprocess и заполненный stdout не должны блокировать отмену большого preview."""

import asyncio
import sys

import pytest

from tests.support import MemoryUpload, pdf_bytes


async def test_cancel_preview_drains_filled_subprocess_pipe(container, monkeypatch):
    """Медленный checkpoint заполняет pipe; отмена завершает дочерний процесс за конечное время."""
    job = await container.intake.upload(
        "integration:onec", [MemoryUpload(pdf_bytes())], None, None, False
    )
    job = await container.database.claim(job.id)
    original = asyncio.create_subprocess_exec
    processes = []
    # Тестовый дочерний процесс печатает только synthetic metadata, не рендерит документ.
    child = (
        "import json,sys,time; "
        "print(json.dumps({'event':'source_discovered','data':{'page_count':1000}}),flush=True); "
        "sys.stdout.write('x'*1048576); sys.stdout.flush(); time.sleep(30)"
    )

    async def spawn(*arguments, **options):
        """Сохраняет настоящие pipe/process-group параметры production нормализации."""
        process = await original(sys.executable, "-c", child, **options)
        processes.append(process)
        return process

    discovered = asyncio.Event()

    async def blocked_checkpoint(kind, data):
        """Останавливает потребление stdout, пока дочерний процесс заполняет pipe."""
        if kind == "source_discovered":
            discovered.set()
            await asyncio.Event().wait()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    preparing = asyncio.create_task(container.storage.normalize(job, blocked_checkpoint))
    try:
        await asyncio.wait_for(discovered.wait(), 5)
        await asyncio.sleep(0.2)
        preparing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(preparing, 5)
        assert processes[0].returncode is not None
    finally:
        if not preparing.done():
            preparing.cancel()
        for process in processes:
            if process.returncode is None:
                process.kill()
                await process.communicate()
