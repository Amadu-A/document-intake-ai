# tests/test_observability.py
"""Timing-декоратор измеряет monotonic duration, не пишет аргументы и сохраняет cancellation."""

import asyncio
import json
import logging

import pytest

from app.core.observability import report_failure, timed


def test_sync_timing_success_and_failure(caplog):
    """Одна запись на вызов содержит статус и duration, metadata исходной функции сохранена."""

    @timed("synthetic_sync")
    def operation(fail=False):
        """Синтетическая операция проверки success/error timing."""
        if fail:
            raise RuntimeError("synthetic")
        return 42

    caplog.set_level(logging.INFO, logger="intake")
    assert operation() == 42 and operation.__name__ == "operation"
    with pytest.raises(RuntimeError):
        operation(True)
    records = [json.loads(record.message) for record in caplog.records]
    assert [record["status"] for record in records] == ["success", "error"]
    assert all(record["duration_ms"] >= 0 for record in records)


async def test_async_timing_preserves_cancellation(caplog):
    """CancelledError распространяется, а timing отмечается ошибкой без traceback/payload."""

    @timed("synthetic_async")
    async def operation(mode):
        """Возвращает успех либо моделирует ошибку/отмену coroutine."""
        if mode == "cancel":
            raise asyncio.CancelledError
        if mode == "error":
            raise RuntimeError("synthetic")
        return 42

    caplog.set_level(logging.INFO, logger="intake")
    assert await operation("success") == 42
    with pytest.raises(RuntimeError):
        await operation("error")
    with pytest.raises(asyncio.CancelledError):
        await operation("cancel")
    assert [json.loads(r.message)["status"] for r in caplog.records] == [
        "success",
        "error",
        "error",
    ]


def test_processing_failure_diagnostic_omits_exception_message(caplog):
    """Причина сбоя остаётся технической: сообщение с документом/секретом не попадает в лог."""
    try:
        raise RuntimeError("SYNTHETIC_PRIVATE_OCR_AND_PASSWORD")
    except RuntimeError as exc:
        report_failure(exc)
    diagnostic = json.loads(caplog.records[-1].message)
    assert diagnostic["type"] == "RuntimeError" and diagnostic["frames"]
    assert set(diagnostic) == {"event", "type", "frames"}
    assert "SYNTHETIC_PRIVATE" not in caplog.text
