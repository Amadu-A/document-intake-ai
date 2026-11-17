# tests/test_health.py
"""Readiness требует OCR растрового PDF и vision-маркер; doubles не доказывают shared runtime."""

import asyncio
import hashlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pymupdf
import pytest
from pydantic import SecretStr

from app.domain.errors import IntakeError
from app.infrastructure.health import Health, raster_probe
from app.operations import OperationError, smoke


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


@pytest.mark.parametrize(
    "processing",
    ["recognized", "unsupported_template", "classification_failed", "extraction_failed", None],
)
async def test_http_smoke_requires_recognized_letter_not_just_available_pdf(
    settings, monkeypatch, processing
):
    """Bootstrap не объявляет PASS для заглушки/отказа, даже если JSON и PDF доступны."""
    settings.api_keys["evaluation"] = SecretStr("e" * 48)
    content = b"%PDF-SYNTHETIC-DOWNLOAD"
    downloads = []

    def respond(request):
        """Возвращает явно синтетический HTTP-контракт без shared-сервисов и документов."""
        path = request.url.path
        if path == "/api/v1/jobs" and request.method == "POST":
            return httpx.Response(202, json={"job_id": "synthetic"})
        if path == "/api/v1/jobs/synthetic":
            return httpx.Response(
                200,
                json={
                    "status": "completed_with_warnings",
                    "sources": [],
                    "artifacts": [
                        {
                            "processing": {"status": processing} if processing else None,
                            "file_url": "http://testserver/download",
                            "sha256": hashlib.sha256(content).hexdigest(),
                            "byte_size": len(content),
                        }
                    ],
                },
            )
        if path.endswith("/result"):
            return httpx.Response(
                200, json={"schema_version": "1.0", "files": [{"documents": [{}]}]}
            )
        if path == "/download":
            downloads.append(path)
            return httpx.Response(200, content=content)
        assert path in {"/", "/docs", "/health/live", "/health/ready"}
        return httpx.Response(200, json={})

    original = httpx.AsyncClient

    def client(**kwargs):
        """Сохраняет production smoke, заменяя только сетевой транспорт тестовым."""
        return original(transport=httpx.MockTransport(respond), **kwargs)

    monkeypatch.setattr("app.operations.httpx.AsyncClient", client)
    if processing == "recognized":
        await smoke(settings)
        assert downloads == ["/download"]
    else:
        with pytest.raises(OperationError, match="реквизиты тестового письма не подтверждены"):
            await smoke(settings)
        assert not downloads
