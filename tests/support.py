# tests/support.py
"""Несекретные генераторы и явно тестовые doubles внешних OCR/VLM/AMQP."""

from collections import deque
from copy import deepcopy
from types import SimpleNamespace

import pymupdf

from app.domain.boundaries import AttachmentPages, FixedGroup
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


def fixed_group(refs: list[dict], main: int | None = None) -> FixedGroup:
    """Задаёт физический состав extraction в тесте без семантического Plan."""
    main = len(refs) if main is None else main
    return FixedGroup(
        document_index=1,
        pages=refs,
        main_pages=refs[:main],
        attachments=[
            AttachmentPages(kind="Приложение", pages=[ref], relation="listed_attachment")
            for ref in refs[main:]
        ],
    )


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
        if schema.get("title") == "DocumentClassification":
            if (
                self.answers
                and isinstance(self.answers[0], dict)
                and "continuation_summary" in self.answers[0]
            ):
                return self.answers.popleft()
            raw = next(
                (v for v in self.answers if isinstance(v, dict) and "classification" in v), facts()
            )
            return {
                "classification": raw["classification"],
                "continuation_summary": raw["requisites"]["summary"],
                "warnings": raw["warnings"],
                "kind_code": raw.get("kind_code"),
                "our_role": raw.get("our_role"),
                "role_evidence": raw.get("role_evidence"),
            }
        if schema.get("title") == "RelatedDatesReading" and (
            not self.answers
            or not isinstance(self.answers[0], dict)
            or "readings" not in self.answers[0]
        ):
            return {
                "readings": [
                    {
                        "index": ref["index"],
                        "reference_kind": "unknown",
                        "date": None,
                        "readability": "missing",
                        "date_role": "not_stated",
                        "date_literal": None,
                        "source_quote": None,
                        "source_page": None,
                        "warnings": ["Синтетическая связанная дата не установлена"],
                    }
                    for ref in context["references"]
                ]
            }
        if schema.get("title") == "RelatedDateSummary" and (
            not self.answers
            or not isinstance(self.answers[0], dict)
            or "summary" not in self.answers[0]
        ):
            return {"summary": context["source_summary"], "warnings": []}
        if schema.get("title") == "PrimaryDateReading" and (
            not self.answers
            or not isinstance(self.answers[0], dict)
            or "readability" not in self.answers[0]
        ):
            return {
                "document_date": None,
                "readability": "missing",
                "source_page": None,
                "warnings": [],
            }
        if schema.get("title") == "PairAnswer":
            if self.answers and (
                callable(self.answers[0])
                or isinstance(self.answers[0], Exception)
                or "solution" in self.answers[0]
            ):
                answer = self.answers.popleft()
                if isinstance(answer, Exception):
                    raise answer
                return answer(context) if callable(answer) else answer
            return {"solution": True}
        if self.answers:
            answer = self.answers.popleft()
            if isinstance(answer, Exception):
                raise answer
            value = answer(context) if callable(answer) else answer
            if "primary_readability" in schema.get("properties", {}) and isinstance(value, dict):
                value = self.readability(value)
            if (
                schema.get("title") == "DetailedExtraction"
                and isinstance(value, dict)
                and "requisites" in value
                and "classification" in value
            ):
                value = self.detailed(value, context)
            return value
        value = facts()
        if context.get("selected_classification"):
            value["classification"] = context["selected_classification"]
        if schema.get("title") == "DetailedExtraction":
            value = self.detailed(value, context)
        return (
            self.readability(value)
            if "primary_readability" in schema.get("properties", {})
            else value
        )

    @staticmethod
    def detailed(value, context):
        """Дополняет только synthetic ответы v2 по заданному профилю; явные ошибки не исправляет."""
        value = deepcopy(value)
        for amount in value["requisites"].get("amounts", []):
            if isinstance(amount.get("value"), (int, float)):
                amount["value"] = str(amount["value"])
        for item in value["requisites"].get("items", []):
            for key in ("quantity", "price", "amount"):
                if isinstance(item.get(key), (int, float)):
                    item[key] = str(item[key])
        value.setdefault("kind_code", context.get("selected_kind_code"))
        if "details" not in value:
            code = value["kind_code"]
            value["details"] = {"profile": code} if code else None
            if code == "D03":
                value["details"].update(
                    primary_document_type=value["classification"]["document_kind"],
                    primary_document_role="payment_request"
                    if value["classification"]["document_kind"] == "Счёт на оплату"
                    else "primary",
                )
        provided_evidence = "field_evidence" in value
        value.setdefault("field_evidence", {})
        refs = context.get("pages", [])
        if not refs:
            refs = [p for item in context.get("window_evidence", []) for p in item.get("pages", [])]
        if not refs:
            refs = [
                record
                for item in context.get("window_evidence", [])
                for records in item.get("facts", {}).get("field_evidence", {}).values()
                for record in records
            ]
        if refs and not provided_evidence:

            def prove(node, path):
                """Добавляет provenance только к synthetic v1-ответам тестового адаптера."""
                if isinstance(node, dict):
                    for key, child in node.items():
                        if key != "profile":
                            prove(child, path + "." + key)
                elif isinstance(node, list):
                    for index, child in enumerate(node):
                        prove(child, path + "." + str(index))
                elif node is not None and ".page_range." not in path:
                    value["field_evidence"].setdefault(
                        path,
                        [
                            {
                                "file_id": refs[0]["file_id"],
                                "page_number": refs[0]["page_number"],
                                "raw": str(node),
                                "status": "found",
                                "source": "image",
                                "confidence": None,
                            }
                        ],
                    )

            prove(value["requisites"], "requisites")
            if value.get("details"):
                prove(value["details"], "details")
            for path, fact in (
                ("requisites.document_number", value["requisites"].get("document_number")),
                ("requisites.document_date", value["requisites"].get("document_date")),
                ("classification.document_kind", value["classification"].get("document_kind")),
            ):
                if fact is not None:
                    value["field_evidence"].setdefault(
                        path,
                        [
                            {
                                "file_id": refs[0]["file_id"],
                                "page_number": refs[0]["page_number"],
                                "raw": str(fact),
                                "status": "found",
                                "source": "image",
                                "confidence": None,
                            }
                        ],
                    )
        return value

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
