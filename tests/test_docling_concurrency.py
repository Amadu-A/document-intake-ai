# tests/test_docling_concurrency.py
"""Окна OCR: общий лимит, физический порядок, single-flight, отмена и durable восстановление."""

import asyncio
import json
import logging
import re
import threading
from time import monotonic

import httpx
import pymupdf
import pytest

from app.domain.errors import IntakeError
from app.infrastructure.docling import DoclingOCR
from app.infrastructure.docling_tasks import DoclingTasks, persist_checkpoint
from tests.test_adapters import multipart_fields
from tests.test_docling_tasks import discard, page_data
from tests.test_live_docling_concurrency import content_hash


def input_numbers(request):
    """Читает номера из настоящего синтетического PDF, независимо от порядка запросов."""
    with pymupdf.open(stream=multipart_fields(request)["files"][0], filetype="pdf") as pdf:
        return [int(re.search(r"PAGE (\d+)", p.get_text())[1]) for p in pdf]


def document_result(numbers):
    """Возвращает локальные номера Docling и уникальный текст каждой исходной страницы."""
    return httpx.Response(
        200,
        json={
            "status": "success",
            "document": {
                "json_content": {
                    "pages": {str(i): {} for i in range(1, len(numbers) + 1)},
                    "texts": [
                        {"text": f"SYNTHETIC PAGE {n}", "prov": [{"page_no": i}]}
                        for i, n in enumerate(numbers, 1)
                    ],
                }
            },
        },
    )


def test_content_comparison_keeps_text_and_table_values():
    """Геометрический шум допустим; изменение цифры или ячейки обязательно обнаруживается."""
    blocks = [
        {"kind": "texts", "text": "Номер 42", "position": [{"bbox": {"l": 10.0}}]},
        {"kind": "tables", "cells": [["Стоимость", "500"]], "position": []},
    ]
    original = content_hash(json.dumps(blocks))
    blocks[0]["position"][0]["bbox"]["l"] += 0.00002
    assert content_hash(json.dumps(blocks)) == original
    blocks[0]["text"] = "Номер 43"
    assert content_hash(json.dumps(blocks)) != original
    blocks[0]["text"] = "Номер 42"
    blocks[1]["cells"][0][1] = "501"
    assert content_hash(json.dumps(blocks)) != original


@pytest.mark.parametrize("asynchronous", [True, False])
async def test_173_pages_finish_out_of_order_but_return_original_sequence(
    settings, tmp_path, asynchronous
):
    """Девять реальных PDF-окон выполняются максимум по два; кеш повторно не вызывает HTTP."""
    pages = page_data(settings, tmp_path, 173)
    settings.docling_async = asynchronous
    second_finished, both_started = asyncio.Event(), asyncio.Event()
    tasks, completions, progress = {}, [], []
    active, maximum, posts = 0, 0, 0

    async def finish(numbers):
        """Принудительно завершает второе окно раньше первого, не измеряя скорость mock."""
        nonlocal active
        if numbers[0] == 1:
            await asyncio.wait_for(second_finished.wait(), 5)
        if numbers[0] == 21:
            await asyncio.wait_for(both_started.wait(), 5)
        completions.append(numbers[0])
        active -= 1
        if numbers[0] == 21:
            second_finished.set()
        return document_result(numbers)

    async def transport(request):
        """Считает активные задания от POST до result, включая длительный sync POST."""
        nonlocal active, maximum, posts
        if request.method == "POST":
            posts += 1
            numbers = input_numbers(request)
            active += 1
            maximum = max(maximum, active)
            if active == 2:
                both_started.set()
            if not asynchronous:
                return await finish(numbers)
            identifier = f"task-{numbers[0]}"
            tasks[identifier] = numbers
            return httpx.Response(200, json={"task_id": identifier})
        identifier = request.url.path.rsplit("/", 1)[-1]
        if "/poll/" in request.url.path:
            if tasks[identifier][0] == 1:
                await asyncio.wait_for(second_finished.wait(), 5)
            return httpx.Response(200, json={"task_id": identifier, "task_status": "success"})
        return await finish(tasks[identifier])

    async def emit(kind, data):
        """Проверяет монотонность checkpoint прогресса при перестановке завершений."""
        if kind == "ocr_progress":
            progress.append(data["completed"])

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
        adapter = DoclingOCR(settings, http)
        recognized = await asyncio.wait_for(adapter.recognize(pages, emit), 20)
        assert await adapter.recognize(pages, emit) == recognized
    assert posts == 9 and maximum == 2 and active == 0
    assert completions.index(21) < completions.index(1)
    assert [p["page_number"] for p in recognized] == list(range(1, 174))
    assert all(f'SYNTHETIC PAGE {p["page_number"]}"' in p["ocr_full"] for p in recognized)
    assert progress == sorted(progress) and progress[-1] == 173
    assert not list(tmp_path.glob(".docling-*.task.json"))


@pytest.mark.parametrize("concurrency", [1, 2])
async def test_two_documents_share_process_limit(settings, tmp_path, concurrency):
    """Два вызова одного адаптера используют общий semaphore, а не по два слота каждый."""
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    inputs = [page_data(settings, first, 40), page_data(settings, second, 40)]
    settings.docling_window_concurrency = concurrency
    settings.docling_async = False
    active, maximum = 0, 0
    owners = []

    async def transport(request):
        """Держит HTTP-запрос открытым и учитывает весь суммарный параллелизм процесса."""
        nonlocal active, maximum
        active += 1
        maximum = max(active, maximum)
        await asyncio.sleep(0.01)
        active -= 1
        return document_result(input_numbers(request))

    async def run(adapter, index):
        """Отмечает завершения разных документов без смешивания их результатов."""
        value = await adapter.recognize(inputs[index], discard)
        owners.append(index)
        return value

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
        adapter = DoclingOCR(settings, http)
        results = await asyncio.gather(run(adapter, 0), run(adapter, 1))
    assert maximum == concurrency and set(owners) == {0, 1}
    for index, output in enumerate(results):
        assert [p["page_number"] for p in output] == list(range(1, 41))
        assert all(p["preview"].startswith(str([first, second][index])) for p in output)


async def test_two_sources_in_one_batch_keep_source_page_pairs(settings, tmp_path):
    """Пачка из двух файлов использует два окна, сохраняя повторяющиеся физические номера."""
    pages = []
    for name in ("first", "second"):
        folder = tmp_path / name
        folder.mkdir()
        pages.extend({**p, "file_id": name} for p in page_data(settings, folder, 20))
    settings.docling_async = False
    entered, release = 0, asyncio.Event()

    async def transport(request):
        """Оба окна должны начать обработку; иначе проверка завершится по конечному timeout."""
        nonlocal entered
        entered += 1
        if entered == 2:
            release.set()
        await asyncio.wait_for(release.wait(), 5)
        return document_result(input_numbers(request))

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
        output = await DoclingOCR(settings, http).recognize(pages, discard)
    assert [(p["file_id"], p["page_number"]) for p in output] == [
        (name, n) for name in ("first", "second") for n in range(1, 21)
    ]


async def test_identical_inflight_window_is_submitted_once(settings, tmp_path):
    """Дублирующий вызов ждёт публикации кеша первого и не создаёт повторный POST."""
    pages = page_data(settings, tmp_path, 2)
    entered, release = asyncio.Event(), asyncio.Event()
    posts = 0

    async def transport(request):
        """Блокирует первый poll, пока второй recognize ожидает тот же window lock."""
        nonlocal posts
        if request.method == "POST":
            posts += 1
            return httpx.Response(200, json={"task_id": "one-task"})
        if "/poll/" in request.url.path:
            entered.set()
            await release.wait()
            return httpx.Response(200, json={"task_id": "one-task", "task_status": "success"})
        return document_result([1, 2])

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
        adapter = DoclingOCR(settings, http)
        first = asyncio.create_task(adapter.recognize(pages, discard))
        await asyncio.wait_for(entered.wait(), 5)
        second = asyncio.create_task(adapter.recognize(pages, discard))
        await asyncio.sleep(0.01)
        release.set()
        results = await asyncio.wait_for(asyncio.gather(first, second), 5)
    assert posts == 1 and results[0] == results[1]


async def test_cancel_two_windows_then_resume_same_tasks(settings, tmp_path):
    """Оба polling отменяются; новый адаптер восстанавливает два task ID без новых POST."""
    pages = page_data(settings, tmp_path, 40)
    entered = asyncio.Event()
    tasks, polling, cancelled = {}, set(), set()
    resumed = False

    async def transport(request):
        """После отмены upstream задачи доступны; повторное создание было бы ошибкой."""
        if request.method == "POST":
            numbers = input_numbers(request)
            identifier = f"task-{numbers[0]}"
            assert identifier not in tasks
            tasks[identifier] = numbers
            return httpx.Response(200, json={"task_id": identifier})
        identifier = request.url.path.rsplit("/", 1)[-1]
        if "/poll/" in request.url.path:
            if not resumed:
                polling.add(identifier)
                if len(polling) == 2:
                    entered.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    cancelled.add(identifier)
                    raise
            return httpx.Response(200, json={"task_id": identifier, "task_status": "success"})
        return document_result(tasks[identifier])

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
        task = asyncio.create_task(DoclingOCR(settings, http).recognize(pages, discard))
        await asyncio.wait_for(entered.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        assert len(cancelled) == 2 and len(list(tmp_path.glob(".docling-*.task.json"))) == 2
        resumed = True
        output = await DoclingOCR(settings, http).recognize(pages, discard)
    assert len(tasks) == 2 and [p["page_number"] for p in output] == list(range(1, 41))
    assert not list(tmp_path.glob(".docling-*.task.json"))


async def test_partial_cache_retains_original_pending_window(settings, tmp_path, monkeypatch):
    """Отмена между записями страниц не меняет fingerprint pending окна при восстановлении."""
    pages = page_data(settings, tmp_path, 3)
    written, release = threading.Event(), threading.Event()
    original = DoclingOCR._save_cache
    posts = 0

    def save(adapter, page, raw):
        """Оставляет одну готовую страницу и удерживает thread до запроса отмены."""
        original(adapter, page, raw)
        if page["page_number"] == 1:
            written.set()
            assert release.wait(5)

    async def transport(request):
        """Возвращает одну полноценную task независимо от частичной публикации локального кеша."""
        nonlocal posts
        if request.method == "POST":
            posts += 1
            return httpx.Response(200, json={"task_id": "partial-cache-task"})
        if "/poll/" in request.url.path:
            return httpx.Response(
                200, json={"task_id": "partial-cache-task", "task_status": "success"}
            )
        return document_result([1, 2, 3])

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
        monkeypatch.setattr(DoclingOCR, "_save_cache", save)
        task = asyncio.create_task(DoclingOCR(settings, http).recognize(pages, discard))
        try:
            assert await asyncio.to_thread(written.wait, 5)
            task.cancel()
            await asyncio.sleep(0.01)
            assert not task.done()
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(list(tmp_path.glob("*.ocr.json"))) == 1
        monkeypatch.setattr(DoclingOCR, "_save_cache", original)
        output = await DoclingOCR(settings, http).recognize(pages, discard)
    assert posts == 1 and len(output) == 3


async def test_cancel_after_submit_finishes_task_id_checkpoint(settings, tmp_path, monkeypatch):
    """Принятый task ID фиксируется до возврата отмены; восстановление не отправляет новый PDF."""
    pages = page_data(settings, tmp_path)
    entered, release = threading.Event(), threading.Event()
    original = DoclingTasks.save
    posts = 0

    def save(tasks, path, fingerprint, identifier):
        """Задерживает запись принятого задания в проверяемом месте гонки с отменой."""
        entered.set()
        assert release.wait(5)
        original(tasks, path, fingerprint, identifier)

    async def transport(request):
        """Считает новые задания отдельно от повторного ожидания известного ID."""
        nonlocal posts
        if request.method == "POST":
            posts += 1
            return httpx.Response(200, json={"task_id": "accepted-task"})
        if "/poll/" in request.url.path:
            return httpx.Response(200, json={"task_id": "accepted-task", "task_status": "success"})
        return document_result([1])

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
        monkeypatch.setattr(DoclingTasks, "save", save)
        task = asyncio.create_task(DoclingOCR(settings, http).recognize(pages, discard))
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            task.cancel()
            await asyncio.sleep(0.01)
            assert not task.done()
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(list(tmp_path.glob(".docling-*.task.json"))) == 1
        monkeypatch.setattr(DoclingTasks, "save", original)
        assert len(await DoclingOCR(settings, http).recognize(pages, discard)) == 1
    assert posts == 1


async def test_repeated_cancellation_still_finishes_checkpoint_write():
    """Одновременная отмена watchdog/gather не оставляет пишущий thread после возврата задачи."""
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()

    def write():
        """Имитирует короткую атомарную запись с управляемым местом ожидания."""
        entered.set()
        assert release.wait(5)
        finished.set()

    task = asyncio.create_task(persist_checkpoint(write))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        await asyncio.sleep(0.01)
        task.cancel()
        await asyncio.sleep(0.01)
        assert not task.done() and not finished.is_set()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finished.is_set()


async def test_window_failure_cancels_other_polling(settings, tmp_path):
    """Отказ одного окна не оставляет фоновых HTTP-получателей после завершения recognize."""
    pages = page_data(settings, tmp_path, 40)
    polling, cancelled = asyncio.Event(), asyncio.Event()

    async def transport(request):
        """Второе окно отказывает только после начала ожидания первого."""
        if request.method == "POST":
            return httpx.Response(200, json={"task_id": f"task-{input_numbers(request)[0]}"})
        identifier = request.url.path.rsplit("/", 1)[-1]
        if identifier == "task-1":
            polling.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise
        await polling.wait()
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
        with pytest.raises(IntakeError, match="отсутствует"):
            await asyncio.wait_for(DoclingOCR(settings, http).recognize(pages, discard), 5)
    assert cancelled.is_set() and len(list(tmp_path.glob(".docling-*.task.json"))) == 2


async def test_shared_cooldown_and_request_interval(settings, tmp_path, monkeypatch):
    """Окна соблюдают общий Retry-After и интервал POST, включая продолжение после отказа."""
    pages = page_data(settings, tmp_path, 40)
    settings.docling_async = False
    settings.docling_request_interval_seconds = 0.01
    monkeypatch.setattr("app.infrastructure.docling.retry_delay", lambda *_: 0.05)
    submitted = []

    async def transport(request):
        """Первый POST перегружен; оба окна должны выдержать его cooldown."""
        submitted.append(monotonic())
        if len(submitted) == 1:
            return httpx.Response(429)
        return document_result(input_numbers(request))

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
        output = await DoclingOCR(settings, http).recognize(pages, discard)
    assert len(output) == 40 and len(submitted) == 3
    assert submitted[1] - submitted[0] >= 0.045
    assert submitted[2] - submitted[1] >= 0.009


@pytest.mark.parametrize("resumed,started", [(False, True), (True, True), (False, False)])
async def test_poll_timings_are_observed_and_never_invented(
    settings, caplog, monkeypatch, resumed, started
):
    """Нет timestamps сервера: интервалы помечены client_poll, неизвестные значения null."""
    statuses = iter(["pending", "started", "success"] if started else ["success"])
    ticks = iter([1.0, 4.0, 9.0, 10.0] if started else [1.0, 9.0, 10.0])
    monkeypatch.setattr("app.infrastructure.docling_tasks.monotonic", lambda: next(ticks))
    settings.docling_poll_interval_seconds = 0

    async def transport(request):
        """Источник времён тестовый; в логи запрещено переносить payload и настоящий task ID."""
        if "/poll/" in request.url.path:
            return httpx.Response(
                200, json={"task_id": "PRIVATE_TASK", "task_status": next(statuses)}
            )
        return document_result([1])

    with caplog.at_level(logging.INFO, logger="intake"):
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
            await DoclingTasks(settings, http).wait("PRIVATE_TASK", discard, resumed=resumed)
    metrics = [json.loads(r.message) for r in caplog.records if "ocr_task_timing" in r.message]
    assert len(metrics) == 1 and "PRIVATE" not in json.dumps(metrics)
    assert metrics[0]["timing_source"] == "client_poll" and metrics[0]["status"] == "success"
    assert metrics[0]["wait_ms"] == 9000
    assert metrics[0]["observed_queue_wait_ms"] == (3000 if started and not resumed else None)
    assert metrics[0]["observed_execution_ms"] == (5000 if started else None)
