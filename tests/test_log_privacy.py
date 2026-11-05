# tests/test_log_privacy.py
"""Проверяет защиту от утечек SQL/HTTP exception payload в runtime logs."""

import json
import logging
import sys

from app.core.observability import PrivateLogFilter


def test_exception_payload_is_not_logged():
    """SQL/HTTP исключение может содержать секрет; журнал сохраняет безопасные кадры."""
    try:
        raise RuntimeError("SYNTHETIC-SECRET-DOCUMENT")
    except RuntimeError:
        record = logging.LogRecord(
            "uvicorn.error",
            logging.ERROR,
            __file__,
            1,
            "unsafe exception %s",
            ("SYNTHETIC-SECRET-DOCUMENT",),
            sys.exc_info(),
        )
    assert PrivateLogFilter().filter(record)
    assert "SYNTHETIC-SECRET-DOCUMENT" not in record.getMessage()
    assert record.exc_info is None
    assert json.loads(record.getMessage())["type"] == "RuntimeError"
