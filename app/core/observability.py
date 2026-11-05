# app/core/observability.py
"""Единое timing-логирование; сообщения содержат только разрешённые технические поля."""

import inspect
import json
import logging
import traceback
from functools import wraps
from pathlib import Path
from time import perf_counter


def timed(operation: str):
    """Измеряет sync/async операцию monotonic-таймером без логирования аргументов."""

    def decorate(function):
        """Сохраняет метаданные и отмечает ошибку, включая asyncio cancellation."""

        def report(started: float, status: str) -> None:
            """Записывает одну техническую запись без exception payload."""
            logging.getLogger("intake").info(
                json.dumps(
                    {
                        "event": "operation_timing",
                        "operation": operation,
                        "duration_ms": round((perf_counter() - started) * 1000, 2),
                        "status": status,
                    }
                )
            )

        if inspect.iscoroutinefunction(function):

            @wraps(function)
            async def asynchronous(*args, **kwargs):
                """Измеряет async-вызов, сохраняя семантику исключений и отмены."""
                started, status = perf_counter(), "error"
                try:
                    result = await function(*args, **kwargs)
                    status = "success"
                    return result
                finally:
                    report(started, status)

            return asynchronous

        @wraps(function)
        def synchronous(*args, **kwargs):
            """Измеряет sync-вызов без копирования приватных данных в лог."""
            started, status = perf_counter(), "error"
            try:
                result = function(*args, **kwargs)
                status = "success"
                return result
            finally:
                report(started, status)

        return synchronous

    return decorate


class PrivateLogFilter(logging.Filter):
    """Разрешает технические logs и удаляет exception payload с документами/секретами."""

    def filter(self, record: logging.LogRecord) -> bool:
        """Сохраняет тип и координаты кадров, исключая args, SQL values и URLs."""
        if not record.name.startswith(("intake", "uvicorn.error", "asyncio")):
            return False
        if record.exc_info:
            kind, _, trace = record.exc_info
            record.msg = json.dumps(
                {
                    "event": "unhandled_error",
                    "type": kind.__name__,
                    "frames": [
                        f"{Path(frame.filename).name}:{frame.lineno}:{frame.name}"
                        for frame in traceback.extract_tb(trace)
                    ],
                }
            )
            record.args = ()
            record.exc_info = None
            record.exc_text = None
            record.stack_info = None
        return True


def configure_logging(level: str) -> None:
    """Отключает библиотечные HTTP/access-логи с URLs и настраивает собственный logger."""
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    for handler in logging.getLogger().handlers:
        handler.addFilter(PrivateLogFilter())
    logging.getLogger("intake").setLevel(level)
    for name in ("httpx", "httpcore", "uvicorn.access", "aio_pika", "aiormq"):
        logging.getLogger(name).disabled = True
