# app/infrastructure/docling_tasks.py
"""Ожидание одной shared Docling task с приватным durable checkpoint, без повторной конвертации."""

import asyncio
import hashlib
import json
import logging
import re
from collections.abc import Callable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from time import monotonic

import httpx

from app.application.ports import Emit
from app.domain.errors import IntakeError


async def persist_checkpoint(write: Callable[..., None], *args) -> None:
    """Завершает начатую атомарную запись до отмены, не оставляя гонку с восстановлением."""
    saving = asyncio.create_task(asyncio.to_thread(write, *args))
    cancelled = False
    while True:
        try:
            await asyncio.shield(saving)
            break
        except asyncio.CancelledError:
            if saving.cancelled():
                raise
            # gather и внешний watchdog могут независимо отменить одну coroutine.
            # Каждую отмену запоминаем, не передавая её в незавершённую запись.
            cancelled = True
    if cancelled:
        raise asyncio.CancelledError


def retry_delay(response: httpx.Response, attempt: int) -> float:
    """Читает Retry-After seconds/date или выбирает конечную backoff-паузу."""
    value = response.headers.get("Retry-After", "")
    try:
        if value.isascii() and value.isdigit():
            return float(min(int(value), 86400))
        when = parsedate_to_datetime(value)
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        return max(0, (when - datetime.now(UTC)).total_seconds())
    except (ValueError, TypeError, OverflowError):
        return float(min(2 ** (attempt - 1), 30))


class DoclingTasks:
    """Сохраняет ссылку на свою задачу; не очищает shared storage и не меняет Docling."""

    def __init__(self, settings, client: httpx.AsyncClient) -> None:
        """Принимает Settings и HTTP-клиент адаптера без самостоятельных соединений/секретов."""
        self.settings, self.client = settings, client

    def path(self, pages: list[dict], fingerprint: list[dict]) -> Path:
        """Привязывает checkpoint к точному окну и OCR-конфигурации, а не одному имени PDF."""
        digest = hashlib.sha256(json.dumps(fingerprint, sort_keys=True).encode()).hexdigest()
        return Path(pages[0]["preview"]).parent / f".docling-{digest}.task.json"

    def load(self, path: Path, fingerprint: list[dict]) -> str | None:
        """Восстанавливает только собственный корректный task ID; повреждение не создаёт дубль."""
        try:
            data = json.loads(path.read_text("utf-8"))
            if data["fingerprint"] != fingerprint:
                raise ValueError("fingerprint")
            return self.task_id(data)
        except FileNotFoundError:
            return None
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise IntakeError(
                "docling_checkpoint_invalid", "Повреждён checkpoint OCR-задачи"
            ) from exc

    def task_id(self, data: dict) -> str:
        """Не позволяет подставить URL/путь/чужой endpoint через ответ upstream."""
        identifier = data.get("task_id")
        if not isinstance(identifier, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", identifier):
            raise IntakeError("docling_incomplete", "Docling не вернул корректный ID задачи")
        return identifier

    def save(self, path: Path, fingerprint: list[dict], identifier: str) -> None:
        """Атомарно фиксирует task ID до polling; содержимое не передаётся в события/логи."""
        temporary = path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps({"fingerprint": fingerprint, "task_id": identifier}), "utf-8"
        )
        temporary.replace(path)

    async def _get(self, path: str, emit: Emit) -> httpx.Response:
        """Повторяет только чтение той же task при перегрузке; новые POST здесь невозможны."""
        for attempt in range(1, self.settings.docling_max_attempts + 1):
            response = await self.client.get(
                self.settings.docling_base_url.rstrip("/") + path,
                headers={"X-Api-Key": self.settings.docling_api_key.get_secret_value()},
                timeout=self.settings.docling_timeout_seconds,
            )
            if response.status_code == 200:
                return response
            if response.status_code == 404:
                raise IntakeError(
                    "docling_task_result_unavailable",
                    "Задача или результат отсутствует в Docling",
                    503,
                )
            if response.status_code not in {429, 502, 503, 504}:
                raise IntakeError(
                    "docling_unavailable",
                    f"Docling отклонил чтение задачи: HTTP {response.status_code}",
                    503,
                )
            delay = retry_delay(response, attempt)
            if (
                attempt == self.settings.docling_max_attempts
                or delay > self.settings.docling_retry_max_seconds
            ):
                raise IntakeError("docling_busy", "Исчерпан срок ожидания OCR-задачи", 503)
            await emit(
                "ocr_retry",
                {
                    "attempt": attempt,
                    "http_status": response.status_code,
                    "retry_after_seconds": round(delay, 2),
                },
            )
            await asyncio.sleep(delay)
        raise AssertionError("Настройка DOCLING_MAX_ATTEMPTS должна быть положительной")

    async def wait(self, identifier: str, emit: Emit, *, resumed: bool = False) -> httpx.Response:
        """Ждёт ту же task; измеряет наблюдаемую очередь/обработку без приватного payload.

        Времена определяются по опросу, а не по недоступным серверным timestamps.
        Если started не наблюдался, отдельные интервалы неизвестны. Для восстановленной
        задачи измеряется только текущий сеанс ожидания. Timeout/отмена сохраняют task ID.
        """
        began, started, finished, outcome = monotonic(), None, None, "error"
        try:
            async with asyncio.timeout(self.settings.docling_task_timeout_seconds):
                while True:
                    response = await self._get(f"/v1/status/poll/{identifier}", emit)
                    data = response.json()
                    if self.task_id(data) != identifier:
                        raise IntakeError("docling_incomplete", "Docling вернул другую OCR-задачу")
                    status = data.get("task_status")
                    if status == "started" and started is None:
                        started = monotonic()
                    if status == "success":
                        finished = monotonic()
                        result = await self._get(f"/v1/result/{identifier}", emit)
                        outcome = "success"
                        return result
                    if status not in {"pending", "started"}:
                        raise IntakeError(
                            "docling_incomplete", "Docling не завершил OCR-задачу успешно"
                        )
                    await asyncio.sleep(self.settings.docling_poll_interval_seconds)
        except TimeoutError as exc:
            raise IntakeError(
                "docling_task_timeout", "Истёк срок ожидания OCR-задачи", 503
            ) from exc
        except asyncio.CancelledError:
            outcome = "cancelled"
            raise
        finally:
            ended = monotonic()
            logging.getLogger("intake").info(
                json.dumps(
                    {
                        "event": "ocr_task_timing",
                        "task_ref": hashlib.sha256(identifier.encode()).hexdigest()[:16],
                        "status": outcome,
                        "resumed": resumed,
                        "timing_source": "client_poll",
                        "wait_ms": round((ended - began) * 1000, 2),
                        "observed_queue_wait_ms": round((started - began) * 1000, 2)
                        if started is not None and not resumed
                        else None,
                        "observed_execution_ms": round(((finished or ended) - started) * 1000, 2)
                        if started is not None
                        else None,
                    }
                )
            )
