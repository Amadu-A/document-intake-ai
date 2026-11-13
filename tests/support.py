# tests/support.py
"""Несекретные генераторы и явно тестовые doubles внешних OCR/VLM/AMQP."""

from collections import deque
from types import SimpleNamespace

import pymupdf

from app.domain.schema import Classification, Extracted, OneC, Recognition, Requisites


def pdf_bytes(count: int = 1) -> bytes:
    """Создаёт синтетический PDF с отличающимися маркерами физических страниц."""
    document = pymupdf.open()
    for number in range(1, count + 1):
        page = document.new_page()
        page.insert_text((40, 70), f"SYNTHETIC PAGE {number}")
    value = document.tobytes()
    document.close()
    return value


def facts(attachments: list | None = None) -> dict:
    """Готовит закрытый ответ для тестового VLM с честным неизвестным направлением."""
    value = Extracted(
        classification=Classification(
            document_root="Входящая/Исходящая корреспонденция",
            direction=None,
            is_letter=True,
            template_path=[],
            document_kind=None,
        ),
        requisites=Requisites(title="Тестовое письмо", attachments=attachments or []),
        one_c=OneC(),
        recognition=Recognition(),
        warnings=["Организация не установлена"],
    )
    return value.model_dump(mode="json")


def plan_for(refs: list[dict], main: int | None = None) -> dict:
    """Создаёт явный ожидаемый план одного комплекта; не используется production-кодом."""
    main = len(refs) if main is None else main
    return {
        "documents": [
            {
                "document_index": 1,
                "pages": refs,
                "main_pages": refs[:main],
                "probable_kind": "Письмо",
                "attachments": [
                    {"kind": "Приложение", "pages": [ref], "relation": "listed_attachment"}
                    for ref in refs[main:]
                ],
                "confidence": None,
                "reason": "Тестовый ответ VLM",
                "status": "verified",
            }
        ],
        "unassigned_pages": [],
        "warnings": [],
    }


class MemoryUpload:
    """Ограниченный тестовый поток загрузки для application-тестов."""

    def __init__(
        self, data: bytes, filename: str = "synthetic.pdf", content_type: str = "application/pdf"
    ) -> None:
        """Сохраняет bytes только синтетического документа."""
        self.value, self.filename, self.content_type = data, filename, content_type
        self.offset = 0

    async def read(self, size: int = -1) -> bytes:
        """Эмулирует поток, возвращая данные не более запрошенного размера."""
        size = len(self.value) if size < 0 else size
        chunk = self.value[self.offset : self.offset + size]
        self.offset += len(chunk)
        return chunk


class ScriptedVision:
    """Явный double VLM; результаты таких тестов никогда не обозначаются live PASS."""

    def __init__(self) -> None:
        """Инициализирует очередь ответов и журнал переданных изображений/контекста."""
        self.answers = deque()
        self.calls = []

    async def request(self, prompt, context, pages, schema, emit):
        """Выдаёт заготовленный ответ либо минимальную корректную синтетическую группу."""
        self.calls.append({"prompt": prompt, "context": context, "pages": pages, "schema": schema})
        await emit("text_delta", {"text": "{synthetic stream}", "validated": False})
        if self.answers:
            answer = self.answers.popleft()
            if isinstance(answer, Exception):
                raise answer
            value = answer(context) if callable(answer) else answer
            if "primary_readability" in schema.get("properties", {}) and isinstance(value, dict):
                value = self.readability(value)
            return value
        if schema.get("title") == "Plan":
            refs = [
                {"file_id": p["file_id"], "page_number": p["page_number"]} for p in context["pages"]
            ]
            return plan_for(refs)
        value = facts()
        return (
            self.readability(value)
            if "primary_readability" in schema.get("properties", {})
            else value
        )

    @staticmethod
    def readability(value):
        """Дополняет synthetic fixture служебным свидетельством, не исправляя её реквизиты."""
        if "primary_readability" not in value and isinstance(value.get("requisites"), dict):
            return {
                **value,
                "primary_readability": {
                    field: "missing" if value["requisites"].get(field) is None else "legible"
                    for field in ("document_number", "document_date")
                }
                | {"handwritten_fields": []},
            }
        return value


class SyntheticOCR:
    """Double OCR, сохраняющий физические адреса без обращения к Docling."""

    async def recognize(self, pages, emit):
        """Возвращает заведомо тестовый OCR для проверки оркестрации."""
        return [{**p, "ocr": "SYNTHETIC OCR"} for p in pages]


class SyntheticHealth:
    """Тестовая readiness без claims о реальных зависимостях."""

    async def ready(self, force=False):
        """Возвращает помеченную синтетическую готовность только внутри transport tests."""
        return {"status": "ready", "checks": {"synthetic": "PASS"}}


def simple_container(**values):
    """Собирает только test container, production сборка находится в app/core/container.py."""
    return SimpleNamespace(**values)
