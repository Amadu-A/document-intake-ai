# app/application/registry.py
"""Реестр сохранённых v1-фактов; не вызывает OCR/VLM и не регистрирует документы в 1С."""

import asyncio
import hashlib
from datetime import date

from app.application.intake import Intake
from app.application.ports import RegistryWriter
from app.core.observability import timed
from app.domain.errors import IntakeError
from app.domain.jobs import Job
from app.domain.registry import (
    XLSX_MIME,
    XLSX_NAME,
    RegistryMappingView,
    RegistryResult,
    RegistryRow,
    registry_mapping,
)
from app.domain.schema import BatchResult, Document


def registry_number(number: str | None, document_date: date | None) -> str | None:
    """Форматирует собственные реквизиты: исходные нули/слеши, без искусственного №/б/н."""
    parts = [number] if number else []
    if document_date:
        parts.append(
            f"от {document_date.day:02d}.{document_date.month:02d}.{document_date.year:04d}"
        )
    return " ".join(parts) or None


def registry_delivery_method(value: str | None) -> str | None:
    """Допускает явное почта/курьер; инструкции и упоминания не интерпретирует."""
    return (
        {
            "почта": "Почта",
            "почтой": "Почта",
            "mail": "Почта",
            "post": "Почта",
            "postal_mail": "Почта",
            "курьер": "Курьер",
            "курьером": "Курьер",
            "courier": "Курьер",
        }.get(value.strip().casefold())
        if value
        else None
    )


def _trusted(document: Document, path: str, failed: bool):
    """Читает bounded dotted path; low confidence и непроверенные факты возвращает пустыми."""
    if failed and path.startswith("requisites."):
        return None
    aliases = (path, path.removeprefix("requisites."), path.rsplit(".", 1)[-1])
    if any(
        alias == field or alias.startswith(field + ".")
        for field in document.recognition.low_confidence_fields
        for alias in aliases
    ):
        return None
    current = document
    for part in path.split("."):
        if part.startswith("_"):
            raise ValueError("Недопустимый источник реестра")
        current = getattr(current, part, None)
        if current is None:
            break
    return current


def project_registry(job: Job) -> RegistryResult:
    """Упорядочивает все блоки, включая неписьма и ошибки, не меняя факты/план исходного job."""
    result = BatchResult.model_validate(job.result)
    mapping = registry_mapping()
    documents = sorted(
        (d for f in result.files for d in f.documents), key=lambda d: d.segment_index
    )
    rows = []
    for number, document in enumerate(documents, 1):
        state = job.document_processing.get(document.document_id)
        failed = state is not None and state.status in {
            "classification_failed",
            "extraction_failed",
            "unsupported_template",
        }
        values = {"document_id": document.document_id, "number": number}
        for key in mapping.column_order:
            source = mapping.columns[key][1]
            if source is False:
                continue
            if isinstance(source, list):
                values[key] = registry_number(*[_trusted(document, p, failed) for p in source])
            else:
                value = _trusted(document, source, failed)
                if key == "delivery_method":
                    value = registry_delivery_method(value)
                values[key] = value
        rows.append(RegistryRow.model_validate(values))
    return RegistryResult(job_id=job.id, items=rows)


class Registry:
    """Общий use-case JSON/XLSX для web/интеграций с owner/TTL и отдельным writer port."""

    def __init__(self, intake: Intake, writer: RegistryWriter) -> None:
        """Принимает сценарий доступа и XLSX adapter из composition root."""
        self.intake, self.writer = intake, writer

    async def schema(self) -> RegistryMappingView:
        """Возвращает единственный каталог колонок без документных данных."""
        return await asyncio.to_thread(registry_mapping)

    async def _ready(self, job_id: str, owner: str) -> Job:
        """Не выдаёт успешный пустой результат вместо отсутствующего распознавания."""
        job = await self.intake.get(job_id, owner)
        if job.status not in {"completed", "completed_with_warnings"} or job.result is None:
            raise IntakeError(
                "registry_not_ready", "Реестр доступен после извлечения реквизитов", 409
            )
        return job

    @timed("export_registry_json")
    async def result(self, job_id: str, owner: str) -> RegistryResult:
        """Проецирует v1-документы без обязательного v2 и повторного анализа."""
        job = await self._ready(job_id, owner)
        result = await asyncio.to_thread(project_registry, job)
        job.authorize(owner)
        return result

    @timed("export_registry_xlsx")
    async def xlsx(self, job_id: str, owner: str) -> tuple[Job, dict]:
        """Генерирует XLSX вне event loop; перепроверяет TTL и не меняет состояние job."""
        job = await self._ready(job_id, owner)
        result = await asyncio.to_thread(project_registry, job)
        try:
            payload = await asyncio.to_thread(self._xlsx_payload, result)
        except OSError as error:
            if error.errno == 28:
                raise IntakeError(
                    "storage_insufficient", "Недостаточно места для реестра", 507
                ) from error
            raise IntakeError(
                "registry_export_failed",
                "Не удалось сформировать реестр. Повторите скачивание",
                503,
            ) from error
        except Exception as error:
            raise IntakeError(
                "registry_export_failed",
                "Не удалось сформировать реестр. Повторите скачивание",
                503,
            ) from error
        job.authorize(owner)
        return job, payload

    def _xlsx_payload(self, result: RegistryResult) -> dict:
        """В том же потоке считает фактические длину/SHA-256 готовой книги, без файловых URL."""
        content = self.writer.render(result)
        return {
            "content": content,
            "byte_size": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
            "file_name": XLSX_NAME,
            "mime_type": XLSX_MIME,
        }
