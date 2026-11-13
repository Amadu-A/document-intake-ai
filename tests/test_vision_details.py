# tests/test_vision_details.py
"""Дополнительный фрагмент улучшает видимость мелкого текста, сохраняя исходную страницу."""

import base64
import json
from io import BytesIO

import httpx
import pytest
from PIL import Image

from app.infrastructure.vision import SharedVision


@pytest.mark.parametrize("details", [False, True])
@pytest.mark.parametrize("stored_footer", [False, True])
@pytest.mark.parametrize("header_requested", [False, True])
async def test_footer_detail_keeps_pixels_and_page_identity(
    settings, tmp_path, details, stored_footer, header_requested
):
    """В запросе остаётся оригинал; footer не меняет пиксели и не получает отдельный PageRef."""
    preview = tmp_path / "synthetic.png"
    with Image.new("RGB", (90, 120), "white") as image:
        image.paste("blue", (0, 80, 90, 120))
        image.save(preview)
    page = {"file_id": "source", "page_number": 17, "preview": str(preview)}
    if stored_footer:
        footer_path = tmp_path / "footer.png"
        with Image.new("RGB", (180, 80), "blue") as image:
            image.save(footer_path)
        page["footer_preview"] = str(footer_path)
        header_path = tmp_path / "header.png"
        with Image.new("RGB", (180, 80), "lime") as image:
            image.save(header_path)
        page["header_preview"] = str(header_path)
    captured = []

    async def transport(request):
        """Фиксирует настоящий HTTP payload без подключения к внешнему VLM."""
        captured.append(json.loads(request.content))
        return httpx.Response(
            200,
            text=(
                'data: {"choices":[{"delta":{"content":"{}"},"finish_reason":"stop"}]}\n\n'
                "data: [DONE]\n\n"
            ),
        )

    async def emit(kind, data):
        """Не публикует синтетические ответы в production logs."""

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        await SharedVision(settings, client).request(
            "Синтетический запрос",
            {
                "footer_details": details,
                "header_detail_page": (
                    {"file_id": "source", "page_number": 17} if header_requested else None
                ),
            },
            [page],
            {"type": "object"},
            emit,
        )
    content = captured[0]["messages"][1]["content"]
    images = [c["image_url"]["url"] for c in content if c["type"] == "image_url"]
    assert len(images) == (2 if details else 1)
    assert base64.b64decode(images[0].split(",")[1]) == preview.read_bytes()
    if details:
        assert json.loads(content[-2]["text"]) == {
            "file_id": "source",
            "page_number": 17,
            "region": "top_third" if stored_footer and header_requested else "bottom_third",
            "same_physical_page": True,
        }
        with Image.open(BytesIO(base64.b64decode(images[-1].split(",")[1]))) as footer:
            assert footer.size == ((180, 80) if stored_footer else (90, 40))
            color = [0, 255, 0] if stored_footer and header_requested else [0, 0, 255]
            assert footer.tobytes() == bytes(color) * (footer.width * footer.height)
