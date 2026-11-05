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


def response_error(response: httpx.Response) -> IntakeError:
    """Описывает отказ Docling без копирования input, OCR, секретов или upstream-сообщений."""
    message = f"Docling отклонил OCR-запрос: HTTP {response.status_code}"
    try:
        payload = response.json() if len(response.content) <= 65536 else None
        detail = payload.get("detail") if isinstance(payload, dict) else None
    except ValueError:
        detail = None
    if response.status_code == 404 and detail == (
        "Task result not found. Please wait for a completion status."
    ):
        return IntakeError(
            "docling_task_result_unavailable",
            message + "; результат задания отсутствует в Docling. "
            "Проверьте состояние обработки и журнал существующего Docling",
            503,
        )
    if response.status_code == 422:
        fields = {
            "body",
            "files",
            "to_formats",
            "do_ocr",
            "force_ocr",
            "ocr_preset",
            "ocr_lang",
            "target_type",
            "do_table_structure",
            "table_mode",
            "image_export_mode",
            "include_images",
            "abort_on_error",
        }
        types = {
            "missing",
            "string_type",
            "bool_parsing",
            "enum",
            "list_type",
            "int_parsing",
            "greater_than",
            "less_than_equal",
            "value_error",
            "literal_error",
        }
        errors = []
        for error in detail[:8] if isinstance(detail, list) else []:
            if not isinstance(error, dict):
                continue
            location = error.get("loc")
            if (
                isinstance(location, (list, tuple))
                and location
                and len(location) <= 8
                and all(
                    isinstance(part, str)
                    and part in fields
                    or isinstance(part, int)
                    and not isinstance(part, bool)
                    and 0 <= part <= 1000
                    for part in location
                )
            ):
                path = ".".join("[]" if isinstance(part, int) else part for part in location)
            else:
                path = "request"
            kind = error.get("type")
            kind = kind if isinstance(kind, str) and kind in types else "validation_error"
            errors.append(f"{path}: {kind}")
        message += "; несовместимые параметры OCR. Проверьте DOCLING_OCR_PRESET, "
        message += "DOCLING_OCR_LANGUAGES, /v1/capabilities и /openapi.json"
        if errors:
            message += " (" + "; ".join(errors) + ")"
    return IntakeError("docling_unavailable", message, 503)


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


def vision_context(items: list[dict]) -> str:
    """Убирает повторные ссылки/сетки Docling, сохраняя текст, геометрию и координаты ячеек."""
    blocks = []
    cell_fields = (
        "start_row_offset_idx",
        "end_row_offset_idx",
        "start_col_offset_idx",
        "end_col_offset_idx",
        "text",
    )
    for item in items:
        block = {"kind": item["kind"], "label": item.get("label")}
        block["position"] = [
            {key: part[key] for key in ("page_no", "bbox") if key in part}
            for part in item.get("prov", [])
        ]
        if item["kind"] == "texts":
            block["text"] = item.get("text", item.get("orig", ""))
            if "orig" in item and item["orig"] != block["text"]:
                block["original_text"] = item["orig"]
        else:
            data = item.get("data", {})
            if isinstance(data.get("table_cells"), list) and data["table_cells"]:
                block.update(
                    rows=data.get("num_rows"),
                    columns=data.get("num_cols"),
                    cell_fields=list(cell_fields),
                    cells=[
                        [cell.get(field) for field in cell_fields] for cell in data["table_cells"]
                    ],
                )
            else:
                # Неизвестную структуру сохраняем; полный ответ также хранится отдельно.
                block["data"] = data
        blocks.append(block)
    return json.dumps(blocks, ensure_ascii=False, separators=(",", ":"))


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
            "ocr_preset": self.settings.docling_ocr_preset,
            "ocr_lang": list(self.settings.docling_ocr_languages),
            "target_type": "inbody",
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
                raise response_error(response)
            data = response.json()
            if not isinstance(data, dict):
                raise ValueError("response object required")
            if data.get("status") != "success":
                raise IntakeError("docling_incomplete", "Docling вернул неполное распознавание")
            document = data["document"]["json_content"]
            if isinstance(document, str):
                document = json.loads(document)
            if not isinstance(document, dict):
                raise ValueError("document object required")
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
                context = vision_context(items)
                if len(context) > 20000:
                    await emit(
                        "warning",
                        {
                            "code": "ocr_context_truncated",
                            "file_id": page["file_id"],
                            "page_number": page["page_number"],
                        },
                    )
                output.append(
                    {**page, "ocr": context[:20000], "ocr_truncated": len(context) > 20000}
                )
            return output
        except (httpx.HTTPError, TimeoutError) as exc:
            raise IntakeError(
                "docling_unavailable", "Docling недоступен или превысил timeout", 503
            ) from exc
        except (KeyError, ValueError, TypeError) as exc:
            raise IntakeError(
                "docling_incomplete", "Неподдерживаемый JSON-контракт Docling"
            ) from exc
