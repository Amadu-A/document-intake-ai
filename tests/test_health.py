# tests/test_health.py
"""Readiness требует OCR растрового PDF и vision-маркер; doubles не доказывают shared runtime."""

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pymupdf
import pytest

from app.domain.errors import IntakeError
from app.infrastructure.health import Health, raster_probe


def test_ocr_probe_has_image_without_searchable_text(tmp_path):
    """Synthetic health PDF нельзя распознать простым извлечением встроенного текста."""
    pages = raster_probe(tmp_path)
    assert len(pages) == 1
    assert Path(pages[0]["preview"]).read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    with pymupdf.open(pages[0]["pdf"]) as document:
        assert len(document) == 1
        assert document[0].get_text() == ""
        assert len(document[0].get_images()) == 1


@pytest.mark.parametrize(
    ("vision_text", "ocr_text", "expected"),
    [
        ("INTAKE 7429", "INTAKE 7429", {"vlm_vision_json": "PASS", "docling_ocr": "PASS"}),
        ("", "INTAKE 7429", {"vlm_vision_json": "FAIL", "docling_ocr": "PASS"}),
        ("INTAKE 7429", "", {"vlm_vision_json": "PASS", "docling_ocr": "FAIL"}),
    ],
    ids=["both-markers", "vision-missing", "ocr-missing"],
)
async def test_health_requires_marker_from_both_adapters(
    settings, tmp_path, vision_text, ocr_text, expected
):
    """Readiness проверяет растровый PDF и не принимает ответ без прочитанного маркера."""
    settings.data_dir = tmp_path
    probe_paths = []

    async def vision_request(prompt, context, pages, schema, emit):
        """Проверяет физическое наличие vision input до synthetic ответа."""
        probe_paths.append(Path(pages[0]["pdf"]))
        assert await asyncio.to_thread(Path(pages[0]["preview"]).is_file)
        assert schema["required"] == ["text"]
        return {"text": vision_text}

    async def recognize(pages, emit):
        """Не подменяет OCR извлечением текста и возвращает явно synthetic результат."""
        with pymupdf.open(pages[0]["pdf"]) as document:
            assert document[0].get_text() == ""
            assert len(document[0].get_images()) == 1
        return [{**pages[0], "ocr": ocr_text}]

    health = Health(
        None,
        None,
        SimpleNamespace(request=vision_request),
        SimpleNamespace(recognize=recognize),
        settings,
    )
    assert await health._contracts() == expected
    assert probe_paths and all(not path.exists() for path in probe_paths)


async def test_health_preserves_safe_docling_failure_and_continues_checks(settings, tmp_path):
    """Readiness сохраняет диагностический код недоступного результата, не объявляя READY."""
    settings.data_dir = tmp_path
    database, rabbit = (
        SimpleNamespace(ping=AsyncMock()),
        SimpleNamespace(require_consumer=AsyncMock()),
    )
    vision = SimpleNamespace(request=AsyncMock(return_value={"text": "INTAKE 7429"}))
    ocr = SimpleNamespace(
        recognize=AsyncMock(
            side_effect=IntakeError(
                "docling_task_result_unavailable", "Docling: результат задания отсутствует", 503
            )
        )
    )
    health = Health(database, rabbit, vision, ocr, settings)
    result = await health.ready()
    assert result["status"] == "not_ready"
    assert result["checks"]["postgresql"] == result["checks"]["consumer"] == "PASS"
    assert "docling_task_result_unavailable" in result["checks"]["docling_ocr"]
    assert await health.ready() == result
    assert ocr.recognize.await_count == 1
    assert database.ping.await_count == rabbit.require_consumer.await_count == 2
