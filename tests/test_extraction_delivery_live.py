# tests/test_extraction_delivery_live.py
"""SIGKILL настоящего worker между документами: реальный RabbitMQ/PG, OCR/VLM явно synthetic."""

import asyncio
import json

import pytest

from app.domain.errors import LeaseLost
from tests.test_delivery_live import delivery_lab as delivery_lab
from tests.test_delivery_live import docker_lab as docker_lab

pytestmark = pytest.mark.live

DOCUMENT_WORKER = '''# fixture/worker.py
"""Настоящий production pipeline/consumer; только внешние OCR/VLM заменены ответчиками."""
import asyncio
import json
from pathlib import Path

import httpx
import pymupdf

from app.application.analysis import Analyze
from app.core.container import Container
from app.core.observability import configure_logging
from app.core.settings import Settings
from app.domain.boundaries import ManualPlan
from app.domain.schema import Classification, Extracted, OneC, Recognition, Requisites
from app.infrastructure.vision import SharedVision
from app.worker import process, run

ROOT = Path("/test-gates")

class Upload:
    """Только синтетический трёхстраничный PDF."""
    filename, content_type = "synthetic.pdf", "application/pdf"
    def __init__(self):
        with pymupdf.open() as pdf:
            for i in range(3):
                pdf.new_page().insert_text((30, 50), f"SYNTHETIC {i + 1}")
            self.data = pdf.tobytes()
    async def read(self, size):
        chunk, self.data = self.data[:size], self.data[size:]
        return chunk

class OCR:
    """Подменяет только внешний OCR, сохраняя реальные рендеры/страницы/PDF."""
    async def recognize(self, pages, emit):
        return [{**p, "ocr": "SYNTHETIC", "ocr_full": "SYNTHETIC"} for p in pages]

def record(index, phase):
    """Записывает только технические номера тестовых запросов."""
    with (ROOT / "requests.jsonl").open("a") as target:
        target.write(json.dumps([index, phase]) + "\\n")

async def main():
    """Создаёт один пакет, затем использует штатные run/ACK/lease/checkpoints."""
    settings = Settings()
    configure_logging(settings.log_level)
    root = Container(settings)
    marker = ROOT / "packet.id"
    packet = marker.read_text() if marker.exists() else None
    classification = Classification(document_root="Входящая/Исходящая корреспонденция",
        direction=None, is_letter=True, template_path=[], document_kind=None)

    async def transport(request):
        """Два документа ждут gate; первый успевает сохранить настоящий checkpoint."""
        payload = json.loads(request.content)
        context = json.loads(payload["messages"][1]["content"][0]["text"])
        phase = context["task"]
        if context.get("validation_errors"):
            await asyncio.to_thread((ROOT / "feedback.json").write_text,
                                    json.dumps(context["validation_errors"]), "utf-8")
        index = (int(context["part"]["block_id"].rsplit(":", 1)[-1])
                 if phase == "classify_document" else context["group"]["document_index"])
        await asyncio.to_thread(record, index, phase)
        if phase == "extract_document" and index > 1:
            while not (ROOT / packet).exists():
                await asyncio.sleep(0.1)
        if phase == "classify_document":
            value = {"classification": classification.model_dump(mode="json"),
                     "continuation_summary": None, "warnings": ["Нет организации"]}
        else:
            value = Extracted(classification=classification,
                requisites=Requisites(document_number=str(index)), one_c=OneC(),
                recognition=Recognition(), warnings=["Нет организации"]).model_dump(mode="json")
            value.update(kind_code=None, details=None, field_evidence={
                "requisites.document_number": [{
                    "file_id": context["pages"][0]["file_id"],
                    "page_number": context["pages"][0]["page_number"],
                    "raw": str(index), "status": "found", "source": "image", "confidence": None,
                }],
            }, primary_readability={"document_number": "legible", "document_date": "missing",
                                    "handwritten_fields": []})
        chunks = [{"choices": [{"delta": {"content": json.dumps(value)}, "finish_reason": "stop"}]}]
        return httpx.Response(200, text="".join("data: " + json.dumps(c) + "\\n\\n" for c in chunks)
                              + "data: [DONE]\\n\\n")

    await root.http.aclose()
    root.http = httpx.AsyncClient(transport=httpx.MockTransport(transport))
    root.vision = SharedVision(settings, root.http)
    root.analysis = Analyze(root.database, root.storage, root.vision, OCR(), settings)
    try:
        if packet is None:
            count_path = ROOT / "packet_count"
            count = int(count_path.read_text()) if count_path.exists() else 1
            packets = []
            for _ in range(count):
                job = await root.intake.upload("integration:fixture", [Upload()], None, None, False)
                await process(root, job.id)
                job = await root.database.get(job.id)
                plan = ManualPlan(documents=[{"client_id": str(i), "pages": [p]}
                                             for i, p in enumerate(job.page_refs())])
                saved = await root.database.save_draft(
                    job.id, job.owner, job.plan_revision, plan, "edit")
                await root.database.confirm(
                    job.id, job.owner, saved.plan_revision, root.prompts.base)
                packets.append(job.id)
            packet = packets[0]
            marker.write_text(packet)
            (ROOT / "packets.json").write_text(json.dumps(packets))
        await run(root)
    finally:
        await root.close()

asyncio.run(main())
'''


async def test_sigkill_parallel_extraction_recovers_real_document_checkpoints(delivery_lab):
    """После раннего ACK crash не теряет пакет и не отправляет готовый документ в VLM повторно."""
    lab = delivery_lab
    await asyncio.to_thread((lab.root / "worker.py").write_text, DOCUMENT_WORKER, "utf-8")
    worker = await asyncio.to_thread(lab.worker)
    async with asyncio.timeout(40):
        while True:
            if await asyncio.to_thread((lab.root / "packet.id").exists):
                identifier = await asyncio.to_thread((lab.root / "packet.id").read_text)
                before = await lab.database.get(identifier)
                if "1" in before.document_results:
                    break
            await asyncio.sleep(0.1)
    diagnostic = lab.root / "feedback.json"
    feedback = (
        await asyncio.to_thread(diagnostic.read_text)
        if await asyncio.to_thread(diagnostic.exists)
        else ""
    )
    assert before.document_results["1"].processing.status == "recognized", feedback
    identities = set(before.manifest["documents"])
    assert len(identities) == 3
    await asyncio.to_thread(lab.command, "kill", worker)
    await lab.ready(0)
    await lab.release(identifier)
    await asyncio.to_thread(lab.command, "start", worker)
    done = await lab.wait_status(identifier, "completed_with_warnings", seconds=60)
    assert done.attempt == 2 and done.fence > before.fence
    assert set(done.manifest["documents"]) == identities
    assert len(done.document_results) == len(done.document_processing) == 3
    requests = await asyncio.to_thread((lab.root / "requests.jsonl").read_text)
    requests = [json.loads(line) for line in requests.splitlines()]
    assert requests.count([1, "classify_document"]) == requests.count([1, "extract_document"]) == 1
    progress = [
        e["data"]["completed"]
        for e in await lab.database.events(identifier, 0)
        if e["event"] == "extraction_progress"
    ]
    assert progress == sorted(progress) and progress[-1] == 3
    with pytest.raises(LeaseLost):
        await lab.database.finish(identifier, before.fence, {}, {}, None, False)


async def test_three_real_worker_slots_share_one_document_pool(delivery_lab):
    """Три AMQP claim работают одновременно, а сумма активных документов PG не превышает 3."""
    from sqlalchemy import select

    from app.infrastructure.database import JobRow

    lab = delivery_lab
    environment = await asyncio.to_thread(lab.env.read_text, "utf-8")
    await asyncio.to_thread(
        lab.env.write_text,
        environment.replace("WORKER_CONCURRENCY=1", "WORKER_CONCURRENCY=3"),
        "utf-8",
    )
    await asyncio.to_thread((lab.root / "packet_count").write_text, "3", "utf-8")
    await asyncio.to_thread((lab.root / "worker.py").write_text, DOCUMENT_WORKER, "utf-8")
    await asyncio.to_thread(lab.worker)
    async with asyncio.timeout(40):
        while not await asyncio.to_thread((lab.root / "packets.json").exists):  # noqa: ASYNC110 — внешний worker.
            await asyncio.sleep(0.1)
    identifiers = json.loads(await asyncio.to_thread((lab.root / "packets.json").read_text))
    peak = 0
    async with asyncio.timeout(30):
        while True:
            async with lab.database.sessions() as session:
                rows = list(await session.scalars(select(JobRow).where(JobRow.id.in_(identifiers))))
            active = sum(row.data.get("extraction_active", 0) for row in rows)
            peak = max(peak, active)
            assert active <= 3
            if len(rows) == 3 and all(row.status == "running" for row in rows) and peak == 3:
                break
            await asyncio.sleep(0.1)
    await asyncio.sleep(6)
    info = await lab.info()
    assert info["messages_ready"] == info["messages_unacknowledged"] == 0
    await lab.release(identifiers[0])
    for identifier in identifiers:
        job = await lab.wait_status(identifier, "completed_with_warnings", seconds=60)
        assert job.attempt == 1
        assert len(job.document_results) == len(job.manifest["documents"]) == 3
