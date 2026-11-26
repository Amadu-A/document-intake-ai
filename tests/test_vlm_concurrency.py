# tests/test_vlm_concurrency.py
"""Единый лимит всех VLM запросов, освобождение slots и безопасные метрики ожидания/HTTP."""

import asyncio
import json

import httpx
import pytest

from app.domain.errors import IntakeError
from app.infrastructure.vision import SharedVision


def response():
    """Возвращает настоящий SSE с usage отдельно от chunks контента."""
    events = [
        {"choices": [{"delta": {"content": '{"ok":true}'}, "finish_reason": "stop"}]},
        {"choices": [], "usage": {"prompt_tokens": 123, "completion_tokens": 17}},
    ]
    return httpx.Response(
        200,
        text="".join("data: " + json.dumps(event) + "\n\n" for event in events)
        + "data: [DONE]\n\n",
    )


@pytest.mark.parametrize("limit", [1, 4])
async def test_one_vlm_limit_for_classification_comparison_extraction_and_retries(
    settings, caplog, limit
):
    """Все типы запросов используют общий adapter, очередь не включает дополнительный HTTP."""
    settings.vlm_request_concurrency = limit
    active, peak, calls = 0, 0, 0
    gate, reached = asyncio.Event(), asyncio.Event()
    metrics = []
    caplog.set_level("INFO", logger="intake")

    async def transport(request):
        """Считает фактические запросы, а не число задач, ожидающих semaphore."""
        nonlocal active, peak, calls
        assert json.loads(request.content)["stream_options"] == {"include_usage": True}
        active += 1
        calls += 1
        peak = max(peak, active)
        if active == limit:
            reached.set()
        try:
            await gate.wait()
            await asyncio.sleep(0.02)
            return response()
        finally:
            active -= 1

    async def emit(kind, data):
        """Сохраняет только агрегаты; raw неподтверждённый текст не становится метрикой."""
        if kind == "vlm_timing":
            metrics.append(data)

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        vision = SharedVision(settings, client)
        phases = ["classify_document", "compare_adjacent_pages", "extract_document"] * 3
        tasks = [
            asyncio.create_task(
                vision.request(
                    "PRIVATE_PROMPT",
                    (
                        {"left_page": {}, "right_page": {}, "private": "PRIVATE_DOC"}
                        if phase == "compare_adjacent_pages"
                        else {"task": phase, "private": "PRIVATE_DOC"}
                    ),
                    [],
                    {"type": "object"},
                    emit,
                )
            )
            for phase in phases
        ]
        try:
            await asyncio.wait_for(reached.wait(), 5)
            await asyncio.sleep(0.05)
            assert active == calls == limit
            gate.set()
            assert all(r == {"ok": True} for r in await asyncio.gather(*tasks))
            assert peak == limit and active == 0 and calls == 9
        finally:
            gate.set()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
    assert len(metrics) == 9
    assert {m["stage"] for m in metrics} == {"classification", "comparison", "extraction"}
    assert all(m["prompt_tokens"] == 123 and m["completion_tokens"] == 17 for m in metrics)
    assert any(m["wait_ms"] > 20 for m in metrics)
    assert all(m["http_ms"] >= 0 and m["wait_ms"] >= 0 for m in metrics)
    assert "PRIVATE_PROMPT" not in caplog.text and "PRIVATE_DOC" not in caplog.text


async def test_cancelled_vlm_waiter_makes_no_http_request_and_slot_is_reusable(settings):
    """Отмена в локальной очереди не отправляет запрос и не уменьшает ёмкость semaphore."""
    settings.vlm_request_concurrency = 1
    entered, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def transport(request):
        """Держит первый настоящий HTTP-вызов до явного разрешения теста."""
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        return response()

    async def emit(kind, data):
        """Не создаёт файлов и не логирует синтетический payload."""

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        vision = SharedVision(settings, client)
        first = asyncio.create_task(vision.request("x", {}, [], {}, emit))
        await entered.wait()
        waiting = asyncio.create_task(vision.request("x", {}, [], {}, emit))
        await asyncio.sleep(0.02)
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        assert calls == 1
        release.set()
        await first
        assert await vision.request("x", {}, [], {}, emit) == {"ok": True}
        assert calls == 2


async def test_http_failure_releases_slot_and_preserves_safe_error(settings):
    """Ошибка VLM не удерживает общий slot; следующий запрос проходит без пересоздания root."""
    settings.vlm_request_concurrency = 1
    calls = 0

    async def transport(request):
        """Один раз возвращает отказ без отражения его приватного тела."""
        nonlocal calls
        calls += 1
        return httpx.Response(503, text="PRIVATE_BACKEND") if calls == 1 else response()

    async def emit(kind, data):
        """Не выводит upstream payload в SSE или лог."""
        assert "PRIVATE_BACKEND" not in json.dumps(data)

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        vision = SharedVision(settings, client)
        with pytest.raises(IntakeError) as caught:
            await vision.request("x", {}, [], {}, emit)
        assert caught.value.code == "vlm_unavailable"
        assert await vision.request("x", {}, [], {}, emit) == {"ok": True}
