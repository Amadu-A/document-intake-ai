# app/domain/document_processing.py
"""Внутренние ответы классификации и честные статусы обработки отдельного PDF-комплекта."""

from typing import Literal

from pydantic import Field

from app.domain.boundaries import FixedGroup
from app.domain.schema import Classification, Extracted, StrictModel

MAX_VLM_PAGES = 8


class DocumentClassification(StrictModel):
    """Определяет тип всего утверждённого блока до выбора шаблона реквизитов."""

    classification: Classification
    continuation_summary: str | None = Field(max_length=2000)
    warnings: list[str]


class DocumentProcessing(StrictModel):
    """Публичная диагностика одного блока без OCR, реквизитов и внутренних ответов модели."""

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
    facts: Extracted
    group: FixedGroup
    processing: DocumentProcessing
