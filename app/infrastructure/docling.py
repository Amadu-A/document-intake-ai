# app/infrastructure/docling.py
"""Аутентифицированный Docling OCR/layout/table с техническими окнами до 50 страниц."""

import asyncio
import json
from pathlib import Path

import httpx
import pymupdf

from app.application.ports import Emit
from app.core.observability import timed
from app.domain.errors import IntakeError
from app.domain.segmentation import windows


def window_pdf(pages: list[dict]) -> bytes:
    """Собирает ограниченное техническое OCR-окно без семантической нарезки."""
    result = pymupdf.open()
    try:
        for page in pages:
            with pymupdf.open(page["pdf"]) as source:
                result.insert_pdf(
                    source,
                    from_page=page["page_number"] - 1,
                    to_page=page["page_number"] - 1,
                    links=False,
                    annots=False,
                )
        return result.tobytes(garbage=4, deflate=True)
    finally:
        result.close()


class DoclingOCR:
    """Получает структуру только из существующего shared Docling; границы не определяет."""

    def __init__(self, settings, client: httpx.AsyncClient) -> None:
        """Принимает проверенную конфигурацию и HTTP-клиент без чтения чужих секретов."""
        self.settings, self.client = settings, client

    @timed("document_ocr")
    async def recognize(self, pages: list[dict], emit: Emit) -> list[dict]:
        """Разбивает большой вход на OCR-окна и восстанавливает глобальные page identifiers."""
        if not self.settings.docling_api_key.get_secret_value():
            raise IntakeError("docling_unavailable", "Не настроен DOCLING_API_KEY проекта", 503)
        result = []
        for chunk in windows(pages, self.settings.docling_window_pages):
            result.extend(await self._window(chunk, emit))
        return result

    async def _window(self, pages: list[dict], emit: Emit) -> list[dict]:
        """Уменьшает только технический размер OCR-запроса при превышении byte-лимита."""
        content = await asyncio.to_thread(window_pdf, pages)
        if len(content) > self.settings.docling_max_request_mb * 1024 * 1024:
            if len(pages) == 1:
                raise IntakeError(
                    "docling_request_limit", "Страница превышает лимит запроса Docling"
                )
            middle = len(pages) // 2
            return await self._window(pages[:middle], emit) + await self._window(
                pages[middle:], emit
            )
        parameters = {
            "to_formats": "json",
            "do_ocr": "true",
            "force_ocr": "false",
            "ocr_engine": "tesseract_cli",
            "ocr_lang": ["ru", "en"],
            "do_table_structure": "true",
            "table_mode": "accurate",
            "image_export_mode": "placeholder",
            "include_images": "false",
            "abort_on_error": "true",
        }
        try:
            response = await self.client.post(
                self.settings.docling_base_url.rstrip("/") + "/v1/convert/file",
                headers={"X-Api-Key": self.settings.docling_api_key.get_secret_value()},
                files={"files": ("window.pdf", content, "application/pdf")},
                data=parameters,
                timeout=self.settings.docling_timeout_seconds,
            )
            if response.status_code != 200:
                raise IntakeError(
                    "docling_unavailable",
                    f"Docling отклонил OCR-запрос: HTTP {response.status_code}",
                    503,
                )
            data = response.json()
            if data.get("status") != "success":
                raise IntakeError("docling_incomplete", "Docling вернул неполное распознавание")
            document = data["document"]["json_content"]
            if isinstance(document, str):
                document = json.loads(document)
            known_pages = {int(p) for p in document["pages"]}
            if known_pages != set(range(1, len(pages) + 1)):
                raise IntakeError("docling_incomplete", "Docling не подтвердил все страницы окна")
            output = []
            for number, page in enumerate(pages, 1):
                items = []
                for kind in ("texts", "tables"):
                    for item in document.get(kind, []):
                        if any(p.get("page_no") == number for p in item.get("prov", [])):
                            items.append({"kind": kind, **item})
                raw = json.dumps(items, ensure_ascii=False)
                target = Path(page["preview"]).with_suffix(".ocr.json")
                await asyncio.to_thread(target.write_text, raw, "utf-8")
                if len(raw) > 20000:
                    await emit(
                        "warning",
                        {
                            "code": "ocr_context_truncated",
                            "file_id": page["file_id"],
                            "page_number": page["page_number"],
                        },
                    )
                output.append({**page, "ocr": raw[:20000], "ocr_truncated": len(raw) > 20000})
            return output
        except (httpx.HTTPError, TimeoutError) as exc:
            raise IntakeError(
                "docling_unavailable", "Docling недоступен или превысил timeout", 503
            ) from exc
        except (KeyError, ValueError, TypeError) as exc:
            raise IntakeError(
                "docling_incomplete", "Неподдерживаемый JSON-контракт Docling"
            ) from exc
