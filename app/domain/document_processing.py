# app/domain/document_processing.py
"""Внутренние ответы классификации и честные статусы обработки отдельного PDF-комплекта."""

from typing import Literal

from pydantic import Field

from app.domain.boundaries import FixedGroup
from app.domain.document_kinds import categories
from app.domain.extractions import DetailedFacts
from app.domain.schema import Classification, Extracted, StrictModel
from app.domain.segmentation import PageRef

MAX_VLM_PAGES = 8


class PageRotation(PageRef):
    """Технический поворот для чтения по решению VLM; исходный PDF и физический адрес неизменны."""

    clockwise_degrees: Literal[0, 90, 180, 270]


class DocumentClassification(StrictModel):
    """Определяет тип всего утверждённого блока до выбора шаблона реквизитов."""

    classification: Classification
    continuation_summary: str | None = Field(max_length=2000)
    warnings: list[str]
    kind_code: str | None = Field(None, json_schema_extra={"enum": [None, *categories()]})
    our_role: Literal["supplier", "customer", "service_provider"] | None = None
    role_evidence: str | None = Field(None, max_length=500)
    role_source_page: PageRef | None = None
    page_rotations: list[PageRotation] = Field(
        default_factory=list,
        max_length=1000,
        description="Для каждой страницы части укажи корректирующий поворот ПО ЧАСОВОЙ "
        "стрелке до читаемого положения: 0 если уже прямо, иначе 90/180/270. "
        "Не текущий наклон: текст, уже повёрнутый вправо на 90°, исправляется на 270°. "
        "Это не поворот оригинала и не изменение его состава.",
    )


class DocumentProcessing(StrictModel):
    """Диагностика блока; unsupported_template читается только из исторических заданий."""

    segment_index: int = Field(ge=1)
    status: Literal[
        "processing",
        "recognized",
        "unsupported_template",
        "classification_failed",
        "extraction_failed",
    ]
    stage: Literal["classification", "extraction"]
    code: str | None = None
    parts_completed: int = Field(0, ge=0)
    parts_total: int = Field(1, ge=1)


class DocumentCheckpoint(StrictModel):
    """Приватный проверенный результат блока для восстановления worker без повторной VLM."""

    fingerprint: str
    facts: DetailedFacts | Extracted
    group: FixedGroup
    processing: DocumentProcessing
    extraction_schema_version: str = "1.0"
