# tests/test_ocr_orientation.py
"""Техническая ориентация не меняет исходник, физические адреса и checkpoints иных углов."""

import asyncio
import base64
import io
import json
from pathlib import Path

import httpx
import pymupdf
import pytest
from PIL import Image

from app.domain.document_processing import DocumentClassification
from app.infrastructure.docling import DoclingOCR, window_pdf
from app.infrastructure.vision import SharedVision, oriented_view
from tests.support import fixed_group
from tests.test_adapters import page_fixture
from tests.test_document_processing import classification, emit, job, pages
from tests.test_extractions_v2 import extracted, selected


@pytest.mark.parametrize("angle", [90, 180, 270])
async def test_rotation_isolated_window_cache_and_source_integrity(settings, tmp_path, angle):
    """Оба пути сборки окна учитывают угол; исходник и checkpoint нулевого угла неизменны."""
    page = page_fixture(tmp_path)
    before = await asyncio.to_thread(Path(page["pdf"]).read_bytes)
    rotated = {**page, "ocr_rotation": angle}
    async with httpx.AsyncClient() as client:
        adapter = DoclingOCR(settings, client)
        raw = json.dumps([{"kind": "texts", "text": "SYNTHETIC OCR"}])
        adapter._save_cache(page, raw)
        assert adapter._cached(page) is not None and adapter._cached(rotated) is None
        adapter._save_cache(rotated, raw)
        assert adapter._cached(rotated)["ocr_rotation"] == angle
        assert adapter._cached(page) is not None
        assert "rotation" not in adapter._cache_key(page)
        assert adapter._cache_key(rotated)["rotation"] == angle
        assembled = await adapter._isolated_pdf([rotated], 1_000_000)
    for content in (assembled, window_pdf([rotated])):
        with pymupdf.open(stream=content) as pdf:
            assert len(pdf) == 1 and pdf[0].rotation == angle
            assert "SYNTHETIC PAGE 1" in pdf[0].get_text()
    assert await asyncio.to_thread(Path(page["pdf"]).read_bytes) == before


@pytest.mark.parametrize("bad", ["duplicate", "foreign_page", "foreign_source"])
async def test_classifier_rotation_cannot_address_foreign_or_duplicate_pages(container, bad):
    """Ориентация не может затронуть соседний регистрационный блок или подменить источник."""
    value = classification()
    rotation = {"file_id": "source", "page_number": 1, "clockwise_degrees": 90}
    if bad == "foreign_page":
        rotation["page_number"] = 2
    elif bad == "foreign_source":
        rotation["file_id"] = "other"
    value["page_rotations"] = [rotation] * (2 if bad == "duplicate" else 1)
    container.vision.answers.extend([value] * container.settings.model_attempts)
    physical = pages(1)
    group = fixed_group([{"file_id": "source", "page_number": 1}])
    _, processing = await container.analysis.documents.run(
        job(container), group, physical, container.analysis._extract, emit
    )
    assert processing.status == "classification_failed"
    assert all(c["schema"]["title"] == "DocumentClassification" for c in container.vision.calls)


async def test_rotated_vision_uses_same_page_and_fresh_oriented_details(settings, tmp_path):
    """Полный рендер и его увеличенная область имеют одну ориентацию и физический адрес."""
    page = page_fixture(tmp_path)
    image = Image.new("RGB", (60, 100), "white")
    image.paste("red", (0, 0, 30, 40))
    image.save(page["preview"])
    page.update(ocr_rotation=90, header_preview=page["preview"], footer_preview=page["preview"])

    async def transport(request):
        """Проверяет байты реального vision payload, не доверяя только метаданным теста."""
        content = json.loads(request.content)["messages"][1]["content"]
        images = [
            base64.b64decode(p["image_url"]["url"].split(",")[1])
            for p in content
            if p["type"] == "image_url"
        ]
        with Image.open(io.BytesIO(images[0])) as full, Image.open(io.BytesIO(images[1])) as detail:
            assert full.size == (100, 60) and detail.size == (100, 24)
            assert full.getpixel((99, 0)) == (255, 0, 0)
            assert detail.getpixel((99, 0)) == (255, 0, 0)
        return httpx.Response(
            200,
            text='data: {"choices":[{"delta":{"content":"{}"},'
            '"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n',
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        await SharedVision(settings, client).request(
            "test",
            {
                "footer_details": True,
                "header_detail_page": {"file_id": page["file_id"], "page_number": 1},
            },
            [page],
            {"type": "object"},
            emit,
        )
    original = await asyncio.to_thread(Path(page["preview"]).read_bytes)
    assert original != oriented_view(original, 90)


@pytest.mark.parametrize("role", ["primary", "payment_request"])
def test_invoice_tax_role_is_not_confused_with_upd_or_payment_request(role):
    """Роль tax согласуется со счётом-фактурой; чужая финансовая роль требует повтора VLM."""
    from app.application.extraction_validation import validate_details
    from app.domain.errors import IntakeError
    from app.domain.extractions import DetailedExtraction

    selection = selected("Счёт-фактура")
    raw = extracted(
        selection,
        details={
            "profile": "D03",
            "primary_document_type": "Счёт-фактура",
            "primary_document_role": role,
        },
    )
    with pytest.raises(IntakeError, match="tax"):
        validate_details(DetailedExtraction.model_validate(raw), selection, pages(1))
    raw["details"]["primary_document_role"] = "tax"
    validate_details(DetailedExtraction.model_validate(raw), selection, pages(1))
    assert DocumentClassification.model_validate(selection).page_rotations == []
