# tests/test_extraction_concurrency.py
"""Общий пул документов и настоящий VLM adapter: REST/DB/PDF, AI-ответы явно synthetic."""

import asyncio
import io
import json
from collections import Counter
from contextlib import closing
from copy import deepcopy
from datetime import timedelta
from zipfile import ZipFile

import httpx
import pymupdf
import pytest
from openpyxl import load_workbook

from app.application.analysis import Analyze, reusable_document
from app.domain.boundaries import FixedGroup
from app.domain.errors import LeaseLost
from app.domain.jobs import utcnow
from app.infrastructure.vision import SharedVision
from app.worker import process
from tests.test_boundary_api import KEY, confirm, create, segmentation
from tests.test_extractions_v2 import extracted, selected

# Ожидание реальной сборки PDF в subprocess до первого VLM-вызова, включая три пакета.
PREPARATION_TIMEOUT_SECONDS = 30


class ContextVLM:
    """HTTP fixture выбирает ответ по собственному документу, без общей очереди фактов."""

    def __init__(self, *, failure=None, gated=False, out_of_order=False):
        """Наблюдает реальные пересечения; gate нужен для отмены/lease и восстановления."""
        self.active = self.maximum = 0
        self.calls = Counter()
        self.finished = []
        self.failure = failure
        self.out_of_order = out_of_order
        self.other_finished = asyncio.Event()
        self.entered, self.release = asyncio.Event(), asyncio.Event()
        self.three_active = asyncio.Event()
        if not gated:
            self.release.set()

    async def __call__(self, request):
        """Сохраняет только технические счётчики, отвечает корректным OpenAI-compatible SSE."""
        payload = json.loads(request.content)
        context = json.loads(payload["messages"][1]["content"][0]["text"])
        task = context.get("task")
        index = (
            int(context["part"]["block_id"].rsplit(":", 1)[-1])
            if task == "classify_document"
            else context.get("group", {}).get("document_index", 0)
        )
        phase = "classify" if task == "classify_document" else "extract"
        ref = context.get("pages", [{"file_id": "comparison", "page_number": 1}])[0]
        key = ref["file_id"], index, phase
        self.calls[key] += 1
        self.active += 1
        self.maximum = max(self.maximum, self.active)
        if self.active >= 3:
            self.three_active.set()
        self.entered.set()
        try:
            await self.release.wait()
            if self.out_of_order and index == 1:
                await self.other_finished.wait()
            await asyncio.sleep(0.07 if index == 1 else 0.01)
            selection = selected()
            raw = (
                selection.model_dump(mode="json")
                if phase == "classify"
                else extracted(
                    selection,
                    number="0007/А" if index in (1, 2) else str(index),
                    ref={k: ref[k] for k in ("file_id", "page_number")},
                    details={
                        "profile": "D03",
                        "primary_document_type": "Счёт на оплату",
                        "primary_document_role": "payment_request",
                    },
                )
            )
            if phase == "extract" and index == self.failure:
                raw = {}
            text = json.dumps(raw, ensure_ascii=False)
            events = [
                {"choices": [{"delta": {"content": text}, "finish_reason": None}]},
                {"choices": [{"delta": {}, "finish_reason": "stop"}]},
                {
                    "choices": [],
                    "usage": {"prompt_tokens": 100 + index, "completion_tokens": 40 + index},
                },
            ]
            self.finished.append(key)
            if index > 1:
                self.other_finished.set()
            return httpx.Response(
                200,
                text="".join("data: " + json.dumps(e) + "\n\n" for e in events)
                + "data: [DONE]\n\n",
            )
        finally:
            self.active -= 1


async def approved(client, container, count=5):
    """Готовит и утверждает реальные одностраничные группы через общий REST редактор."""
    identifier = (await create(client, endpoint="uploads", pages=count))["job_id"]
    await process(container, identifier)
    async with asyncio.timeout(10):
        while not (await container.database.get(identifier)).preview_complete:  # noqa: ASYNC110 — внешний worker.
            await asyncio.sleep(0.03)
    plan = await segmentation(client, identifier)
    refs = plan["draft_plan"]["documents"][0]["pages"]
    response = await client.put(
        f"/api/v1/jobs/{identifier}/segmentation",
        headers=KEY,
        json={
            "expected_revision": plan["plan_revision"],
            "documents": [{"client_id": f"d{i}", "pages": [p]} for i, p in enumerate(refs)],
        },
    )
    assert response.status_code == 200
    await confirm(client, identifier, response.json()["plan_revision"])
    return identifier


def parallel(container, limit, vlm):
    """Использует production Analyze/SharedVision и настраиваемые общие semaphore одного root."""
    container.settings.document_extraction_concurrency = limit
    client = httpx.AsyncClient(transport=httpx.MockTransport(vlm))
    vision = SharedVision(container.settings, client)
    container.analysis = Analyze(
        container.database, container.storage, vision, container.analysis.ocr, container.settings
    )
    return client


async def representations(client, identifier):
    """Читает обе схемы, настоящие PDF/ZIP/XLSX; удаляет только межзаданные ID/URLs."""
    prefix = f"/api/v1/jobs/{identifier}"
    one = (await client.get(prefix + "/result", headers=KEY)).json()
    two = (await client.get(prefix + "/extractions", headers=KEY)).json()
    pdf_text = []
    for source in one["files"]:
        source.pop("file_id")
        source.pop("file_url")
        for document in source["documents"]:
            response = await client.get(document["attachment_file"]["file_url"])
            with pymupdf.open(stream=response.content) as pdf:
                pdf_text.append([p.get_text() for p in pdf])
            document.pop("document_id")
            document["attachment_file"].pop("file_url")
    one.pop("batch_id")
    two.pop("batch_id")
    for document in two["documents"]:
        document.pop("document_id")
        document["attachment_file"].pop("file_url")
        for ref in document["source_pages"]:
            ref.pop("file_id")
        for records in document["field_evidence"].values():
            for ref in records:
                ref.pop("file_id")
    archive = await client.get(prefix + "/archive", headers=KEY)
    with ZipFile(io.BytesIO(archive.content)) as zipped:
        entries = zipped.namelist()
        for name, expected in zip(entries, pdf_text, strict=True):
            with pymupdf.open(stream=zipped.read(name)) as pdf:
                assert [p.get_text() for p in pdf] == expected
    xlsx = await client.get(prefix + "/registry.xlsx", headers=KEY)
    with closing(load_workbook(io.BytesIO(xlsx.content))) as workbook:
        rows = list(workbook.active.values)
    return one, two, pdf_text, entries, rows


@pytest.mark.parametrize("limit", [1, 2, 3])
async def test_out_of_order_documents_keep_all_formats_and_unique_progress(
    client, container, limit
):
    """Одинаковые модельные факты дают одинаковые JSON/PDF/ZIP/XLSX при любом лимите."""
    reference = None
    for concurrency in (1, limit):
        vlm = ContextVLM(out_of_order=concurrency > 1)
        async with parallel(container, concurrency, vlm):
            identifier = await approved(client, container)
            await process(container, identifier)
            saved = await container.database.get(identifier)
            assert saved.status == "completed_with_warnings"
            assert len(saved.document_results) == len(saved.manifest["documents"]) == 5
            assert vlm.maximum <= min(concurrency, 4)
            if concurrency > 1:
                assert vlm.finished[0][1] != 1
            events = await container.database.events(identifier, 0)
            completed = [
                e["data"]["completed"] for e in events if e["event"] == "extraction_progress"
            ]
            assert completed == sorted(completed) and completed[-1] == 5
            done = [e["data"]["document_id"] for e in events if e["event"] == "document_completed"]
            assert len(done) == len(set(done)) == 5
            assert all(
                0 <= e["data"].get("active", 0) <= concurrency
                for e in events
                if e["event"] == "extraction_progress"
            )
            assert saved.extraction_active == 0
            value = await representations(client, identifier)
            if reference is None:
                reference = value
            else:
                assert value == reference


async def test_three_packets_share_document_limit_and_keep_facts_separate(client, container):
    """Три параллельных процесса обработки используют один лимит документов, не 3×лимит."""
    vlm = ContextVLM(gated=True)
    async with parallel(container, 3, vlm):
        identifiers = [await approved(client, container, 3) for _ in range(3)]
        tasks = [asyncio.create_task(process(container, i)) for i in identifiers]
        try:
            await asyncio.wait_for(vlm.entered.wait(), PREPARATION_TIMEOUT_SECONDS)
            await asyncio.wait_for(vlm.three_active.wait(), PREPARATION_TIMEOUT_SECONDS)
            await asyncio.sleep(0.05)
            assert vlm.active == 3
            vlm.release.set()
            await asyncio.gather(*tasks)
        finally:
            vlm.release.set()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        assert vlm.maximum == 3
        for identifier in identifiers:
            job = await container.database.get(identifier)
            assert len(job.result["files"][0]["documents"]) == 3
            assert len(job.document_results) == len(job.document_processing) == 3
        assert all(count == 1 for count in vlm.calls.values())


async def test_one_failed_document_does_not_cancel_siblings(client, container):
    """Ошибка реквизитов одного блока сохраняет остальные и все PDF без повторной регистрации."""
    vlm = ContextVLM(failure=2)
    async with parallel(container, 3, vlm):
        identifier = await approved(client, container, 3)
        await process(container, identifier)
        job = await container.database.get(identifier)
        assert job.status == "completed_with_warnings"
        assert [c.processing.status for _, c in sorted(job.document_results.items())] == [
            "recognized",
            "extraction_failed",
            "recognized",
        ]
        assert len(job.manifest["documents"]) == 3
        assert (
            sum(
                count
                for (_, index, phase), count in vlm.calls.items()
                if index == 2 and phase == "extract"
            )
            == container.settings.model_attempts
        )


@pytest.mark.parametrize("cause", ["cancel", "deadline", "lease"])
async def test_cancel_drains_documents_and_http_requests(client, container, cause):
    """Отмена отзывает fence, дожидается дочерних задач и освобождает оба общих semaphore."""
    vlm = ContextVLM(gated=True)
    async with parallel(container, 3, vlm):
        identifier = await approved(client, container, 5)
        task = asyncio.create_task(process(container, identifier))
        try:
            # До первого VLM вызова штатно собираются пять PDF в отдельных процессах.
            # Это ожидание подготовки на CI, а не допустимое время остановки ниже.
            await asyncio.wait_for(vlm.entered.wait(), PREPARATION_TIMEOUT_SECONDS)
            if cause == "cancel":
                await container.intake.cancel(identifier, "integration:onec")
            else:
                # Имитируем время под той же блокировкой агрегата, что использует repository.
                # Прямая запись старого row.data могла затереть cursor соседнего checkpoint.
                async with container.database._transaction() as session:
                    row, _ = await container.database._locked(session, identifier)
                    if cause == "deadline":
                        data = dict(row.data)
                        data["deadline_at"] = (utcnow() - timedelta(seconds=1)).isoformat()
                        row.data = data
                    else:
                        row.lease_until = utcnow() - timedelta(seconds=1)
                await container.database.recover()
            await asyncio.wait_for(task, 5)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        assert vlm.active == 0
        saved = await container.database.get(identifier)
        assert (
            saved.status == {"cancel": "cancelled", "deadline": "failed", "lease": "queued"}[cause]
        )
        assert saved.extraction_active == 0
        if cause == "deadline":
            assert saved.error["code"] == "job_timeout"
        before = deepcopy(saved.model_dump(mode="json"))
        vlm.release.set()
        await asyncio.sleep(0.1)
        assert (await container.database.get(identifier)).model_dump(mode="json") == before
        replacement = await approved(client, container, 1)
        await asyncio.wait_for(process(container, replacement), 10)
        assert (await container.database.get(replacement)).status == "completed_with_warnings"


async def test_second_cancel_does_not_interrupt_child_cleanup(client, container):
    """Отмена задания и последующий shutdown ждут cleanup всех HTTP корутин, не только первой."""
    vlm = ContextVLM(gated=True)
    cleaning, release_cleanup = asyncio.Event(), asyncio.Event()

    async def transport(request):
        """Моделирует асинхронное закрытие внешнего ресурса после первой отмены."""
        try:
            return await vlm(request)
        finally:
            cleaning.set()
            await release_cleanup.wait()

    async with parallel(container, 3, transport):
        identifier = await approved(client, container, 3)
        driver = asyncio.create_task(process(container, identifier))
        try:
            await asyncio.wait_for(vlm.three_active.wait(), PREPARATION_TIMEOUT_SECONDS)
            await container.intake.cancel(identifier, "integration:onec")
            await asyncio.wait_for(cleaning.wait(), 5)
            driver.cancel()
            await asyncio.sleep(0.05)
            assert not driver.done()
            release_cleanup.set()
            await asyncio.wait_for(asyncio.gather(driver, return_exceptions=True), 5)
            assert vlm.active == 0
        finally:
            vlm.release.set()
            release_cleanup.set()
            driver.cancel()
            await asyncio.gather(driver, return_exceptions=True)


@pytest.mark.parametrize("writer_busy", [False, True], ids=["idle", "concurrent-checkpoint"])
async def test_restart_reuses_checkpoints_ids_and_monotonic_unique_progress(
    client, container, writer_busy
):
    """Прерывает executor после первого checkpoint, меняет fence и восстанавливает тот же пакет."""
    vlm = ContextVLM()
    paused, release = asyncio.Event(), asyncio.Event()
    original = vlm.__class__.__call__

    async def interrupted(request):
        """Два блока зависают на HTTP до аварийного завершения текущего executor."""
        context = json.loads(json.loads(request.content)["messages"][1]["content"][0]["text"])
        if context.get("task") == "extract_document" and context["group"]["document_index"] > 1:
            paused.set()
            await release.wait()
        return await original(vlm, request)

    async with parallel(container, 3, interrupted):
        identifier = await approved(client, container, 3)
        task = asyncio.create_task(process(container, identifier))
        try:
            await asyncio.wait_for(paused.wait(), PREPARATION_TIMEOUT_SECONDS)
            async with asyncio.timeout(5):
                while True:
                    saved = await container.database.get(identifier)
                    if "1" in saved.document_results:
                        break
                    await asyncio.sleep(0.02)
            before_calls = vlm.calls.copy()
            identities = set(saved.manifest["documents"])
            old_fence = saved.fence
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        async def expire_lease():
            """Меняет только тестовый lease в общей очереди записей repository."""
            async with container.database._transaction() as session:
                row, _ = await container.database._locked(session, identifier)
                row.lease_until = utcnow() - timedelta(seconds=1)

        if writer_busy:
            expiring = None
            try:
                async with container.database._transaction() as session:
                    row, snapshot = await container.database._locked(session, identifier)
                    # Незавершённый checkpoint удерживает настоящий SQLite write lock.
                    container.database._write(
                        session,
                        row,
                        snapshot,
                        "stage",
                        {"stage": snapshot.stage, "progress": snapshot.progress},
                    )
                    await session.flush()
                    expiring = asyncio.create_task(expire_lease())
                    done_tasks, _ = await asyncio.wait({expiring}, timeout=0.05)
                    assert not done_tasks
                await asyncio.wait_for(expiring, 5)
            finally:
                if expiring is not None:
                    expiring.cancel()
                    await asyncio.gather(expiring, return_exceptions=True)
        else:
            await expire_lease()
        await container.database.recover()
        release.set()
        await process(container, identifier)
        done = await container.database.get(identifier)
        assert done.fence > old_fence and done.status == "completed_with_warnings"
        assert set(done.manifest["documents"]) == identities
        assert len(done.document_results) == len(done.document_processing) == 3
        assert all(vlm.calls[k] == count for k, count in before_calls.items() if k[1] == 1)
        events = await container.database.events(identifier, 0)
        progress = [e["data"]["completed"] for e in events if e["event"] == "extraction_progress"]
        assert progress == sorted(progress) and progress[-1] == 3
        with pytest.raises(LeaseLost):
            await container.database.checkpoint(
                identifier, old_fence, "extraction_progress", {"completed": 3, "total": 3}
            )
        group = FixedGroup(document_index=1, pages=list(done.approved_plan.documents[0].pages))
        assert reusable_document(done, group) is not None
        changed = done.model_copy(deep=True)
        changed.document_results["1"].fingerprint = "invalid"
        assert reusable_document(changed, group) is None
        changed = done.model_copy(deep=True)
        changed.document_results["1"].group.pages.reverse()
        changed.document_results["1"].processing.segment_index = 2
        assert reusable_document(changed, group) is None
