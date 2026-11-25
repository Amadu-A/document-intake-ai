# app/domain/registry.py
"""Независимый контракт реестра и единый каталог колонок, без изменений JSON распознавания."""

import json
from datetime import date as Date
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from app.domain.schema import StrictModel

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
XLSX_NAME = "Реестр документов.xlsx"


class RegistryRow(StrictModel):
    """Одна регистрация; ручные поля неизвестны, document_id не является колонкой Excel."""

    document_id: str = Field(min_length=1)
    number: int = Field(ge=1, strict=True)
    date: Date | None = None
    delivery_method: str | None = None
    receipt_city: str | None = None
    recipient: str | None = None
    document_name: str | None = None
    sender: str | None = None
    document_number: str | None = None
    opening_date: Date | None = None
    scanning_date: Date | None = None
    registration_date: Date | None = None
    responsible: str | None = None
    notes: str | None = None
    document_link: str | None = None


class RegistryResult(StrictModel):
    """Версия относится к реестру; исходные схемы 1.0/2.0 сохраняются."""

    schema_version: Literal["1.0"] = "1.0"
    job_id: str
    items: list[RegistryRow]


class RegistryMappingView(StrictModel):
    """Публичный ordered mapping: заголовок и false/путь/список путей источников документа."""

    schema_version: Literal["1.0"]
    column_order: list[str]
    columns: dict[str, tuple[str, Literal[False] | str | list[str]]]

    @model_validator(mode="after")
    def fixed_columns(self) -> "RegistryMappingView":
        """Запрещает лишние колонки, document_id в Excel и повторяющийся порядок."""
        fields = set(RegistryRow.model_fields) - {"document_id"}
        if (
            len(self.column_order) != len(fields)
            or set(self.column_order) != fields
            or set(self.columns) != fields
        ):
            raise ValueError("Требуются ровно 14 утверждённых колонок реестра")
        return self


@lru_cache(maxsize=1)
def _mapping() -> RegistryMappingView:
    """Читает версионированный ресурс один раз, как существующие domain каталоги."""
    return RegistryMappingView.model_validate(
        json.loads(Path(__file__).with_name("registry_mapping.json").read_text("utf-8"))
    )


def registry_mapping() -> RegistryMappingView:
    """Возвращает копию: API/adapter не могут изменить общий source of truth."""
    return _mapping().model_copy(deep=True)
