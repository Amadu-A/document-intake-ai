# tests/test_docling_tasks.py
"""Async Docling: реальные PDF-окна, восстановление task ID, отсутствие дублирующих POST."""

import asyncio
from pathlib import Path

import httpx
import pymupdf
import pytest
from pydantic import SecretStr

from app.domain.errors import IntakeError
from app.infrastructure.docling import DoclingOCR
from tests.support import pdf_bytes
from tests.test_adapters import multipart_fields


def page_data(settings, tmp_path, count=1):
    """Готовит физические номера и отдельные checkpoint paths без пользовательских файлов."""
    settings.docling_api_key = SecretStr("synthetic-docling-key")
    settings.docling_async = True
    settings.docling_request_interval_seconds = 0
    settings.docling_poll_interval_seconds = 0
    source = tmp_path / "source.pdf"
    source.write_bytes(pdf_bytes(count))
    return [
        {
            "file_id": "source",
            "page_number": n,
            "pdf": str(source),
            "preview": str(tmp_path / f"page-{n}.png"),
        }
        for n in range(1, count + 1)
    ]


async def discard(kind, data):
    """Принимает только технические события OCR."""


def result(count):
    """Возвращает полный Docling JSON с подтверждением каждого физического листа окна."""
    return httpx.Response(
        200,
        json={
            "status": "success",
            "document": {"json_content": {"pages": {str(n): {} for n in range(1, count + 1)}}},
        },
    )


async def test_501_pages_async_waits_for_exact_task_and_reuses_all_success(settings, tmp_path):
    """27 POST с одним 429 дают 26 результатов/501 страницу, а повтор не делает HTTP-запросов."""
    pages = page_data(settings, tmp_path, 501)
    tasks, calls = {}, []
    posts = 0

    async def transport(request):
        """Имитирует проверенный runtime-контракт async → poll → result на настоящих PDF."""
        nonlocal posts
        calls.append(request.url.path)
        if request.method == "POST":
            assert request.url.path == "/v1/convert/file/async"
            posts += 1
            if posts == 3:
                return httpx.Response(429, headers={"Retry-After": "0"})
            with pymupdf.open(stream=multipart_fields(request)["files"][0], filetype="pdf") as doc:
                count = len(doc)
            identifier = f"task-{posts}"
            tasks[identifier] = {"count": count, "polls": 0}
            return httpx.Response(200, json={"task_id": identifier, "task_status": "pending"})
        identifier = request.url.path.rsplit("/", 1)[-1]
        if "/status/poll/" in request.url.path:
            tasks[identifier]["polls"] += 1
            return httpx.Response(
                200,
                json={
                    "task_id": identifier,
                    "task_status": "started" if tasks[identifier]["polls"] == 1 else "success",
                },
            )
        return result(tasks[identifier]["count"])

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as shared:
        recognized = await DoclingOCR(settings, shared).recognize(pages, discard)
        count = len(calls)
        assert await DoclingOCR(settings, shared).recognize(pages, discard) == recognized
    assert posts == 27 and len(tasks) == 26 and len(calls) == count
    assert sum(task["polls"] for task in tasks.values()) == 52
    assert [p["page_number"] for p in recognized] == list(range(1, 502))
    assert not list(tmp_path.glob(".docling-*.task.json"))


async def test_async_timeout_resumes_same_task_without_second_post(settings, tmp_path):
    """Restart после timeout продолжает ту же shared task, затем удаляет pending checkpoint."""
    pages = page_data(settings, tmp_path)
    settings.docling_task_timeout_seconds = 0.03
    settings.docling_poll_interval_seconds = 0.005
    calls, complete = [], False

    async def transport(request):
        """Сначала задача длительная, затем следующий экземпляр получает готовый результат."""
        calls.append(request.method + " " + request.url.path)
        if request.method == "POST":
            return httpx.Response(200, json={"task_id": "same-task", "task_status": "pending"})
        if "/status/poll/" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "task_id": "same-task",
                    "task_status": "success" if complete else "started",
                },
            )
        return result(1)

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as shared:
        with pytest.raises(IntakeError) as error:
            await DoclingOCR(settings, shared).recognize(pages, discard)
        assert error.value.code == "docling_task_timeout"
        assert len(list(tmp_path.glob(".docling-*.task.json"))) == 1
        complete = True
        settings.docling_task_timeout_seconds = 1
        assert len(await DoclingOCR(settings, shared).recognize(pages, discard)) == 1
    assert sum(call.startswith("POST") for call in calls) == 1
    assert not list(tmp_path.glob(".docling-*.task.json"))


@pytest.mark.parametrize(
    "response_status,task_status,expected_code",
    [
        (404, None, "docling_task_result_unavailable"),
        (200, "failure", "docling_incomplete"),
        (200, "partial_success", "docling_incomplete"),
    ],
)
async def test_terminal_failure_never_reposts_the_conversion(
    settings, tmp_path, response_status, task_status, expected_code
):
    """404/ошибка/частичный успех не запускают повторную конвертацию и не теряют диагностику."""
    pages = page_data(settings, tmp_path)
    posts = 0

    async def transport(request):
        """Отказ upstream может содержать приватное сообщение, которое не отражается в API."""
        nonlocal posts
        if request.method == "POST":
            posts += 1
            return httpx.Response(200, json={"task_id": "failed-task"})
        return httpx.Response(
            response_status,
            json={
                "task_id": "failed-task",
                "task_status": task_status,
                "error_message": "PRIVATE",
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as shared:
        for _ in range(2):
            with pytest.raises(IntakeError) as error:
                await DoclingOCR(settings, shared).recognize(pages, discard)
            assert error.value.code == expected_code and "PRIVATE" not in error.value.message
    assert posts == 1 and not Path(pages[0]["preview"]).with_suffix(".ocr.json").exists()


async def test_bad_task_id_cannot_change_upstream_path(settings, tmp_path):
    """Ответ с путём вместо ID задачи не вызывает ни одного GET и не утекает в ошибку."""
    pages = page_data(settings, tmp_path)
    calls = []

    async def transport(request):
        """Подставляет недопустимый путь в недоверенное поле upstream."""
        calls.append(request)
        return httpx.Response(200, json={"task_id": "../../PRIVATE"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as shared:
        with pytest.raises(IntakeError) as error:
            await DoclingOCR(settings, shared).recognize(pages, discard)
    assert len(calls) == 1 and "PRIVATE" not in error.value.message


async def test_cancelled_poll_keeps_task_checkpoint(settings, tmp_path):
    """Отмена ожидания освобождает coroutine и сохраняет ссылку на уже отправленную задачу."""
    pages = page_data(settings, tmp_path)
    entered = asyncio.Event()

    async def transport(request):
        """Блокирует только polling, а не повторно передаёт исходный PDF."""
        if request.method == "POST":
            return httpx.Response(200, json={"task_id": "cancel-task"})
        entered.set()
        await asyncio.sleep(60)

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as shared:
        task = asyncio.create_task(DoclingOCR(settings, shared).recognize(pages, discard))
        await asyncio.wait_for(entered.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 1)
    assert len(list(tmp_path.glob(".docling-*.task.json"))) == 1
