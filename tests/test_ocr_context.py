# tests/test_ocr_context.py
"""Контекст VLM сохраняет OCR-факты, не расходуя лимит на дублирующиеся метаданные Docling."""

import json
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from app.infrastructure.docling import DoclingOCR, vision_context
from tests.support import pdf_bytes


def test_compact_context_keeps_text_layout_and_spanning_table_cells():
    """Текст, отличие original и координаты объединённой ячейки сохраняются без duplicate grid."""
    position = {"page_no": 1, "bbox": {"l": 1, "t": 2, "r": 3, "b": 4}}
    items = [
        {"kind": "texts", "text": "Дата 02.09.2026", "orig": "Дата 02.09.202б", "prov": [position]},
        {
            "kind": "tables",
            "prov": [position],
            "data": {
                "num_rows": 2,
                "num_cols": 3,
                "grid": ["duplicate-metadata" * 5000],
                "table_cells": [
                    {
                        "start_row_offset_idx": 0,
                        "end_row_offset_idx": 2,
                        "start_col_offset_idx": 1,
                        "end_col_offset_idx": 3,
                        "text": "3 штуки",
                    },
                ],
            },
        },
    ]
    compact = json.loads(vision_context(items))
    assert compact[0]["text"] == items[0]["text"]
    assert compact[0]["original_text"] == items[0]["orig"]
    assert compact[0]["position"] == [position]
    assert compact[1]["cells"] == [[0, 2, 1, 3, "3 штуки"]]
    assert compact[1]["rows"] == 2 and compact[1]["columns"] == 3
    assert "duplicate-metadata" not in vision_context(items)
    unknown = {"kind": "tables", "data": {"future_layout": "Содержимое"}}
    assert json.loads(vision_context([unknown]))[0]["data"] == unknown["data"]


@pytest.mark.parametrize("text_size", [100, 30000], ids=["metadata-only-large", "text-limit"])
async def test_context_limit_counts_useful_content_and_preserves_full_ocr(
    settings, tmp_path, text_size
):
    """Большие служебные поля не отрезают текст; настоящий текстовый лимит честно помечается."""
    pdf = tmp_path / "source.pdf"
    pdf.write_bytes(pdf_bytes())
    page = {
        "file_id": "source-a",
        "page_number": 1,
        "pdf": str(pdf),
        "preview": str(tmp_path / "page.png"),
    }
    item = {"text": "A" * text_size, "prov": [{"page_no": 1}], "children": ["metadata" * 10000]}
    settings.docling_api_key = SecretStr("synthetic-docling-key")
    events = []

    async def emit(kind, data):
        """Записывает только synthetic warnings, без настоящего документа."""
        events.append((kind, data))

    async def transport(request):
        """Возвращает синтетическую страницу с заведомо большим служебным полем."""
        return httpx.Response(
            200,
            json={
                "status": "success",
                "document": {"json_content": {"pages": {"1": {}}, "texts": [item]}},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        result = (await DoclingOCR(settings, client).recognize([page], emit))[0]
    stored = json.loads(Path(page["preview"]).with_suffix(".ocr.json").read_text("utf-8"))
    assert stored[0]["text"] == item["text"] and stored[0]["children"] == item["children"]
    assert result["ocr_truncated"] is (text_size > 20000)
    assert any(kind == "warning" for kind, _ in events) is (text_size > 20000)
    assert "metadata" not in result["ocr"]
    if text_size < 20000:
        assert json.loads(result["ocr"])[0]["text"] == item["text"]
