# tests/test_backpressure.py
"""Регрессии 501 страницы: независимые HTTP-бюджеты, OCR backpressure и обязательный review."""

import asyncio
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from pathlib import Path

import httpx
import pymupdf
import pytest
from pydantic import SecretStr

from app.domain.errors import IntakeError, LeaseLost
from app.domain.jobs import utcnow
from app.infrastructure.docling import DoclingOCR, retry_delay
from app.worker import process
from tests.support import pdf_bytes
from tests.test_adapters import multipart_fields

KEY = {"X-API-Key": "e" * 48}


async def test_501_thumbnails_do_not_exhaust_status_or_control_budget(
    client, container, monkeypatch
):
    """Настоящие 501 PNG скачиваются при обычных лимитах; команды сохраняют свой лимит 5/min."""
    container.settings.rate_limit_per_minute = 5
    now = utcnow()
    monkeypatch.setattr("app.infrastructure.database.utcnow", lambda: now)
    response = await client.post(
        "/api/v1/uploads",
        headers=KEY,
        files={"files": ("501.pdf", pdf_bytes(501), "application/pdf")},
    )
    identifier = response.json()["job_id"]
    await process(container, identifier)
    job = await container.database.get(identifier)
    file_id = job.sources[0].file_id
    for page in range(1, 502):
        response = await client.get(
            f"/api/v1/jobs/{identifier}/pages/{file_id}/{page}/thumbnail", headers=KEY
        )
        assert response.status_code == 200
        assert response.content.startswith(b"\x89PNG")
    response = await client.get(
        f"/api/v1/jobs/{identifier}/snapshot?include_pages=false", headers=KEY
    )
    assert response.status_code == 200 and response.json()["pages_ready"] == 501
    for _ in range(4):
        assert (
            await client.post(f"/api/v1/jobs/{identifier}/start", headers=KEY)
        ).status_code == 202
    rejected = await client.post(f"/api/v1/jobs/{identifier}/start", headers=KEY)
    assert rejected.status_code == 429 and rejected.json()["error"]["code"] == "rate_limit"
    assert rejected.headers["Retry-After"] == "60"
    assert (await client.get(f"/api/v1/jobs/{identifier}", headers=KEY)).status_code == 200


async def test_read_and_preview_limits_remain_enforced(client, container):
    """Бюджеты остаются конечными и общими между разными URL/query одного класса запросов."""
    container.settings.read_rate_limit_per_minute = 5
    for index in range(5):
        response = await client.get(f"/api/v1/session/prompts?nonce={index}", headers=KEY)
        assert response.status_code == 200
    response = await client.get("/api/v1/session/prompts?nonce=extra", headers=KEY)
    assert response.status_code == 429
    assert (
        await client.put("/api/v1/session/prompts", headers=KEY, json=container.prompts.base)
    ).status_code == 200


async def test_501_docling_pages_retry_overload_without_losing_checkpoints(settings, tmp_path):
    """26 настоящих PDF-окон и два отказа 429/503 сохраняют физический порядок всех 501 страниц."""
    settings.docling_api_key = SecretStr("synthetic-docling-key")
    settings.docling_request_interval_seconds = 0
    source = tmp_path / "501.pdf"
    source.write_bytes(pdf_bytes(501))
    pages = [
        {
            "file_id": "source",
            "page_number": n,
            "pdf": str(source),
            "preview": str(tmp_path / f"page-{n}.png"),
        }
        for n in range(1, 502)
    ]
    calls, events = [], []

    async def transport(request):
        """Отказывает одному окну, затем подтверждает реальный состав полученного PDF."""
        calls.append(request)
        if len(calls) in {3, 4}:
            return httpx.Response(429 if len(calls) == 3 else 503, headers={"Retry-After": "0"})
        with pymupdf.open(stream=multipart_fields(request)["files"][0], filetype="pdf") as document:
            count = len(document)
        return httpx.Response(
            200,
            json={
                "status": "success",
                "document": {"json_content": {"pages": {str(n): {} for n in range(1, count + 1)}}},
            },
        )

    async def emit(kind, data):
        """Сохраняет только технические события ожидания OCR."""
        events.append((kind, data))

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as shared:
        result = await DoclingOCR(settings, shared).recognize(pages, emit)
        assert len(calls) == 28
        cached = await DoclingOCR(settings, shared).recognize(pages, emit)
    assert len(calls) == 28 and cached == result
    assert [p["page_number"] for p in result] == list(range(1, 502))
    assert len(events) == 2 and all(event[0] == "ocr_retry" for event in events)


@pytest.mark.parametrize("retry_after,expected_calls", [("0", 5), ("120", 1)])
async def test_docling_overload_is_bounded_and_does_not_leak_body(
    settings, tmp_path, retry_after, expected_calls
):
    """Постоянная перегрузка или слишком длинный Retry-After завершаются безопасной диагностикой."""
    settings.docling_api_key = SecretStr("synthetic-docling-key")
    settings.docling_request_interval_seconds = 0
    source = tmp_path / "source.pdf"
    source.write_bytes(pdf_bytes())
    page = {
        "file_id": "source",
        "page_number": 1,
        "pdf": str(source),
        "preview": str(tmp_path / "1.png"),
    }
    calls = []

    async def transport(request):
        """Возвращает приватное upstream-сообщение, запрещённое для API/логов."""
        calls.append(request)
        return httpx.Response(429, headers={"Retry-After": retry_after}, text="PRIVATE")

    async def emit(kind, data):
        """Принимает технический retry без приватного текста."""
        assert "PRIVATE" not in str(data)

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as shared:
        with pytest.raises(IntakeError) as error:
            await DoclingOCR(settings, shared).recognize([page], emit)
    assert error.value.code == "docling_busy" and "PRIVATE" not in error.value.message
    assert len(calls) == expected_calls
    assert not Path(page["preview"]).with_suffix(".ocr.json").exists()


def test_retry_after_seconds_date_and_invalid_header():
    """Учитывает обе формы Retry-After и конечный backoff для невалидного заголовка."""
    assert retry_delay(httpx.Response(429, headers={"Retry-After": "7"}), 1) == 7
    date = format_datetime(datetime.now(UTC) + timedelta(seconds=30))
    assert 28 <= retry_delay(httpx.Response(503, headers={"Retry-After": date}), 1) <= 30
    assert retry_delay(httpx.Response(429, headers={"Retry-After": "invalid"}), 3) == 4


async def test_docling_cancellation_interrupts_retry_wait(settings, tmp_path):
    """Отмена задания прерывает паузу 60 секунд, не создавая следующий OCR-запрос."""
    settings.docling_api_key = SecretStr("synthetic-docling-key")
    source = tmp_path / "source.pdf"
    source.write_bytes(pdf_bytes())
    page = {
        "file_id": "source",
        "page_number": 1,
        "pdf": str(source),
        "preview": str(tmp_path / "1.png"),
    }
    waiting = asyncio.Event()
    calls = []

    async def transport(request):
        """Сигнализирует продолжительную, но разрешённую паузу."""
        calls.append(request)
        return httpx.Response(429, headers={"Retry-After": "60"})

    async def emit(kind, data):
        """Сообщает, что запрос перешёл к ожиданию слота."""
        waiting.set()

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as shared:
        task = asyncio.create_task(DoclingOCR(settings, shared).recognize([page], emit))
        await asyncio.wait_for(waiting.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 1)
    assert len(calls) == 1


async def test_invalid_segmentation_becomes_reviewable_pages_without_extraction(client, container):
    """Ошибки VLM не выдаются за успех: одиночные страницы ждут сохранённого подтверждения."""
    original = container.vision.request

    async def invalid(prompt, context, pages, schema, emit):
        """Повторяемая ошибка покрытия воспроизводит отказ автоматического разделения."""
        if schema["title"] == "Plan":
            return {"documents": [], "unassigned_pages": [], "warnings": []}
        return await original(prompt, context, pages, schema, emit)

    container.vision.request = invalid
    response = await client.post(
        "/api/v1/jobs", headers=KEY, files={"files": ("three.pdf", pdf_bytes(3), "application/pdf")}
    )
    identifier = response.json()["job_id"]
    await process(container, identifier)
    job = await container.database.get(identifier)
    assert job.status == "awaiting_confirmation" and job.segmentation_source == "page_fallback"
    assert not job.plan_locked and job.result is None and len(job.draft_plan.documents) == 3
    assert not container.vision.calls and not await container.database.pending()
    assert (await client.get(f"/api/v1/jobs/{identifier}/result", headers=KEY)).status_code == 409
    assert (await client.post(f"/api/v1/jobs/{identifier}/start", headers=KEY)).status_code == 409
    with pytest.raises(LeaseLost):
        await container.database.fallback_pages(job.id, job.fence - 1, "invalid_segmentation")
    response = await client.post(
        f"/api/v1/jobs/{identifier}/segmentation/confirm",
        headers=KEY,
        json={"expected_revision": job.plan_revision},
    )
    assert response.status_code == 202
    await process(container, identifier)
    completed = await container.database.get(identifier)
    assert completed.status == "completed_with_warnings"
    assert len(completed.manifest["documents"]) == 3
    assert all(call["schema"]["title"] == "Extracted" for call in container.vision.calls)


async def test_segmentation_time_budget_and_review_do_not_require_docling(client, container):
    """Недоступный OCR не блокирует разметку; бюджет VLM заканчивается черновиком, а не успехом."""
    container.settings.vlm_segmentation_timeout_seconds = 0.02

    async def unavailable(pages, emit):
        """Любой OCR-вызов до утверждения является нарушением этапов."""
        raise AssertionError("OCR before plan confirmation")

    async def slow(prompt, context, pages, schema, emit):
        """Длинный vision-запрос должен отмениться по отдельному бюджету разделения."""
        await asyncio.sleep(60)

    container.analysis.ocr.recognize = unavailable
    container.vision.request = slow
    response = await client.post(
        "/api/v1/jobs",
        headers=KEY,
        files={"files": ("two.pdf", pdf_bytes(2), "application/pdf")},
    )
    identifier = response.json()["job_id"]
    await asyncio.wait_for(process(container, identifier), 10)
    job = await container.database.get(identifier)
    assert job.status == "awaiting_confirmation"
    assert job.manifest["segmentation_fallback"]["code"] == "segmentation_timeout"
    assert len(job.draft_plan.documents) == 2 and not job.result


async def test_noncontiguous_manual_pdf_excludes_foreign_middle_page(client, container):
    """Комплект 1,3 физически содержит два нужных листа; лист 2 скачивается другим документом."""
    response = await client.post(
        "/api/v1/uploads",
        headers=KEY,
        files={"files": ("three.pdf", pdf_bytes(3), "application/pdf")},
    )
    identifier = response.json()["job_id"]
    await process(container, identifier)
    job = await container.database.get(identifier)
    refs = [page.model_dump() for page in job.page_refs()]
    edited = await client.put(
        f"/api/v1/jobs/{identifier}/segmentation",
        headers=KEY,
        json={
            "expected_revision": job.plan_revision,
            "documents": [
                {"client_id": "connected", "pages": [refs[0], refs[2]]},
                {"client_id": "foreign", "pages": [refs[1]]},
            ],
            "excluded_pages": [],
        },
    )
    assert edited.status_code == 200
    await client.post(
        f"/api/v1/jobs/{identifier}/segmentation/confirm",
        headers=KEY,
        json={"expected_revision": edited.json()["plan_revision"]},
    )
    await process(container, identifier)
    result = (await client.get(f"/api/v1/jobs/{identifier}/result", headers=KEY)).json()
    documents = result["files"][0]["documents"]
    assert [d["pages"] for d in documents] == [[1, 3], [2]]
    response = await client.get(documents[0]["attachment_file"]["file_url"])
    with pymupdf.open(stream=response.content, filetype="pdf") as pdf:
        assert len(pdf) == 2
        assert "PAGE 1" in pdf[0].get_text() and "PAGE 3" in pdf[1].get_text()
