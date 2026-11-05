# tests/test_adapters.py
"""HTTP-контракты адаптеров проверяются локальными ответами; это не реальные shared прогоны."""

import asyncio
import json
from email.parser import BytesParser
from email.policy import default
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


def multipart_fields(request: httpx.Request) -> dict[str, list[bytes]]:
    """Читает настоящий multipart клиента для проверки контрактных параметров и PDF-окна."""
    prefix = f"Content-Type: {request.headers['content-type']}\r\n\r\n".encode()
    message = BytesParser(policy=default).parsebytes(prefix + request.content)
    fields = {}
    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition")
        fields.setdefault(name, []).append(part.get_payload(decode=True))
    return fields


@pytest.mark.parametrize("thinking", [False, True], ids=["direct-json", "thinking-override"])
async def test_vision_sends_images_alias_schema_and_unvalidated_stream(
    settings, tmp_path, thinking
):
    """Проверяет vision/schema, явный режим thinking и маркировку непроверенного JSON stream."""
    settings.vlm_enable_thinking = thinking
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
    assert payload["chat_template_kwargs"] == {"enable_thinking": thinking}
    assert "authorization" not in captured[0].headers
    images = [p for p in payload["messages"][1]["content"] if p["type"] == "image_url"]
    assert images[0]["image_url"]["url"].startswith("data:image/png;base64,")
    assert events[-1][1]["validated"] is False


@pytest.mark.parametrize("string_content", [False, True], ids=["object-json", "string-json"])
async def test_docling_maps_local_page_number_back_to_global_identifier(
    settings, tmp_path, string_content
):
    """Docling-страница 1 окна возвращается как physical page 51 исходного файла."""
    page = page_fixture(tmp_path)
    await asyncio.to_thread(Path(page["pdf"]).write_bytes, pdf_bytes(53))
    page["page_number"] = 51
    captured = []
    settings.docling_api_key = SecretStr("synthetic-docling-key")

    async def transport(request):
        """Возвращает типовую структуру Docling с текстом и таблицей."""
        captured.append(request)
        document = {
            "pages": {"1": {}},
            "texts": [{"text": "SYNTHETIC OCR", "prov": [{"page_no": 1}]}],
            "tables": [{"data": {"table_cells": []}, "prov": [{"page_no": 1}]}],
        }
        return httpx.Response(
            200,
            json={
                "status": "success",
                "document": {"json_content": json.dumps(document) if string_content else document},
            },
        )

    async def emit(kind, data):
        """Не публикует synthetic OCR события в обычные логи."""

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        result = await DoclingOCR(settings, client).recognize([page], emit)
    assert captured[0].headers["x-api-key"] == "synthetic-docling-key"
    assert captured[0].url.path == "/v1/convert/file"
    fields = multipart_fields(captured[0])
    assert fields["ocr_preset"] == [b"tesseract"]
    assert fields["ocr_lang"] == [b"iso:ru", b"iso:en"]
    assert fields["target_type"] == [b"inbody"]
    assert fields["to_formats"] == [b"json"]
    assert fields["do_ocr"] == [b"true"]
    assert fields["force_ocr"] == [b"false"]
    assert fields["do_table_structure"] == [b"true"]
    assert fields["table_mode"] == [b"accurate"]
    assert "ocr_engine" not in fields
    with pymupdf.open(stream=fields["files"][0]) as window:
        assert len(window) == 1
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


async def test_docling_uses_configured_ocr_preset_and_languages(settings, tmp_path):
    """Частный override выбора OCR передаётся адаптеру без хардкода или изменения shared runtime."""
    settings.docling_api_key = SecretStr("synthetic-docling-key")
    settings.docling_ocr_preset = "rapidocr"
    settings.docling_ocr_languages = ("en",)

    async def transport(request):
        """Проверяет override до намеренного безопасного отказа тестового upstream."""
        fields = multipart_fields(request)
        assert fields["ocr_preset"] == [b"rapidocr"]
        assert fields["ocr_lang"] == [b"en"]
        return httpx.Response(200, json={"status": "failure"})

    async def emit(kind, data):
        """Не публикует synthetic payload за пределами теста."""

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        with pytest.raises(IntakeError, match="неполное распознавание"):
            await DoclingOCR(settings, client).recognize([page_fixture(tmp_path)], emit)


@pytest.mark.parametrize("page_numbers", [[], [2], [1, 2]], ids=["missing", "wrong", "extra"])
async def test_docling_refuses_missing_wrong_or_extra_pages(settings, tmp_path, page_numbers):
    """Статус success не разрешает потерять страницу или принять другой состав OCR-окна."""
    settings.docling_api_key = SecretStr("synthetic-docling-key")
    page = page_fixture(tmp_path)

    async def transport(request):
        """Имитирует success с неполным либо противоречивым документом."""
        return httpx.Response(
            200,
            json={
                "status": "success",
                "document": {"json_content": {"pages": {str(n): {} for n in page_numbers}}},
            },
        )

    async def emit(kind, data):
        """Не публикует synthetic payload за пределами теста."""

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        with pytest.raises(IntakeError) as error:
            await DoclingOCR(settings, client).recognize([page], emit)
    assert error.value.code == "docling_incomplete"
    assert not Path(page["preview"]).with_suffix(".ocr.json").exists()


@pytest.mark.parametrize(
    ("status", "detail", "code", "diagnostic"),
    [
        (
            422,
            [
                {
                    "loc": ["body", "ocr_lang", 0],
                    "type": "string_type",
                    "msg": "SYNTHETIC-PRIVATE",
                    "input": "SYNTHETIC-PRIVATE",
                }
            ],
            "docling_unavailable",
            "body.ocr_lang.[]: string_type",
        ),
        (
            422,
            [
                {
                    "loc": ["SYNTHETIC-PRIVATE"],
                    "type": "SYNTHETIC-PRIVATE",
                    "msg": "SYNTHETIC-PRIVATE",
                }
            ],
            "docling_unavailable",
            "request: validation_error",
        ),
        (
            422,
            "ocr_preset 'SYNTHETIC-PRIVATE' is not allowed.",
            "docling_unavailable",
            "/v1/capabilities",
        ),
        (
            404,
            "Task result not found. Please wait for a completion status.",
            "docling_task_result_unavailable",
            "результат задания отсутствует",
        ),
        (404, "SYNTHETIC-PRIVATE", "docling_unavailable", "HTTP 404"),
    ],
    ids=["validation", "unsafe-location", "policy", "missing-task-result", "other-not-found"],
)
async def test_docling_reports_safe_contract_errors(
    settings, tmp_path, status, detail, code, diagnostic
):
    """Отказы 422/404 имеют полезную диагностику без input, произвольного msg или секретов."""
    settings.docling_api_key = SecretStr("synthetic-docling-key")

    async def transport(request):
        """Возвращает контролируемый upstream отказ, который может содержать приватный input."""
        return httpx.Response(status, json={"detail": detail, "input": "SYNTHETIC-PRIVATE"})

    async def emit(kind, data):
        """Не публикует synthetic payload за пределами теста."""

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        with pytest.raises(IntakeError) as error:
            await DoclingOCR(settings, client).recognize([page_fixture(tmp_path)], emit)
    assert error.value.code == code
    assert error.value.status == 503
    assert diagnostic in error.value.message
    assert "SYNTHETIC-PRIVATE" not in error.value.message
