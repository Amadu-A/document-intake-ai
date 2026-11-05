# tests/test_adapters.py
"""HTTP-контракты адаптеров проверяются локальными ответами; это не реальные shared прогоны."""

import asyncio
import json
from pathlib import Path

import httpx
import pymupdf
import pytest
from pydantic import SecretStr

from app.domain.errors import IntakeError
from app.infrastructure.docling import DoclingOCR
from app.infrastructure.vision import SharedVision
from tests.support import pdf_bytes


async def collect_events(target, kind, data):
    """Сохраняет test events отдельно от production-журнала."""
    target.append((kind, data))


def page_fixture(tmp_path):
    """Готовит синтетическую физическую страницу для проверки реального vision payload."""
    pdf = tmp_path / "source.pdf"
    pdf.write_bytes(pdf_bytes())
    image = tmp_path / "page.png"
    with pymupdf.open(pdf) as document:
        document[0].get_pixmap().save(image)
    return {"file_id": "global-file", "page_number": 1, "pdf": str(pdf), "preview": str(image)}


async def test_vision_sends_images_alias_schema_and_unvalidated_stream(settings, tmp_path):
    """Проверяет изображения, logical alias, JSON Schema и маркировку сырого stream."""
    page = page_fixture(tmp_path)
    captured, events = [], []

    async def transport(request):
        """Эмулирует OpenAI-compatible SSE локально, без внешней модели."""
        captured.append(request)
        return httpx.Response(
            200,
            text='data: {"choices":[{"delta":{"content":"{\\"ok\\":true}"},'
            '"finish_reason":null}]}\n\n'
            'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
            "data: [DONE]\n\n",
        )

    async def emit(kind, data):
        """Собирает только synthetic stream для проверки validated=false."""
        await collect_events(events, kind, data)

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        result = await SharedVision(settings, client).request(
            "Тестовый промпт", {}, [page], {"type": "object"}, emit
        )
    payload = json.loads(captured[0].content)
    assert result == {"ok": True}
    assert payload["model"] == "shared-vlm"
    assert payload["response_format"]["type"] == "json_schema"
    assert "authorization" not in captured[0].headers
    images = [p for p in payload["messages"][1]["content"] if p["type"] == "image_url"]
    assert images[0]["image_url"]["url"].startswith("data:image/png;base64,")
    assert events[-1][1]["validated"] is False


async def test_docling_maps_local_page_number_back_to_global_identifier(settings, tmp_path):
    """Docling-страница 1 окна возвращается как physical page 51 исходного файла."""
    page = page_fixture(tmp_path)
    await asyncio.to_thread(Path(page["pdf"]).write_bytes, pdf_bytes(53))
    page["page_number"] = 51
    captured = []
    settings.docling_api_key = SecretStr("synthetic-docling-key")

    async def transport(request):
        """Возвращает типовую структуру Docling с текстом и таблицей."""
        captured.append(request)
        return httpx.Response(
            200,
            json={
                "status": "success",
                "document": {
                    "json_content": {
                        "pages": {"1": {}},
                        "texts": [{"text": "SYNTHETIC OCR", "prov": [{"page_no": 1}]}],
                        "tables": [{"data": {"table_cells": []}, "prov": [{"page_no": 1}]}],
                    }
                },
            },
        )

    async def emit(kind, data):
        """Не публикует synthetic OCR события в обычные логи."""

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        result = await DoclingOCR(settings, client).recognize([page], emit)
    assert captured[0].headers["x-api-key"] == "synthetic-docling-key"
    assert captured[0].url.path == "/v1/convert/file"
    assert result[0]["file_id"] == "global-file"
    assert result[0]["page_number"] == 51
    assert "SYNTHETIC OCR" in result[0]["ocr"] and "tables" in result[0]["ocr"]


async def test_docling_refuses_partial_success(settings, tmp_path):
    """Неполный OCR не маскируется под успешное распознавание."""
    settings.docling_api_key = SecretStr("synthetic-docling-key")

    async def transport(request):
        """Возвращает partial_success для проверки строгого отказа."""
        return httpx.Response(200, json={"status": "partial_success"})

    async def emit(kind, data):
        """Не пишет тестовый payload в production logs."""

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        with pytest.raises(IntakeError) as error:
            await DoclingOCR(settings, client).recognize([page_fixture(tmp_path)], emit)
    assert error.value.code == "docling_incomplete"
