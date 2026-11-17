# app/domain/related_dates.py
"""Проверка визуальных свидетельств связанной даты; значение и роль определяет VLM."""

import re
import unicodedata
from datetime import date as Date
from typing import Literal

from pydantic import Field

from app.domain.errors import IntakeError
from app.domain.identities import identifier_key
from app.domain.schema import StrictModel
from app.domain.segmentation import PageRef


class RelatedDateReading(StrictModel):
    """Связь одного известного номера с датой и буквальным фрагментом источника."""

    index: int = Field(ge=0)
    reference_kind: Literal["court_act", "court_case", "enforcement_proceeding", "unknown"]
    date: Date | None
    readability: Literal["legible", "missing", "ambiguous"]
    date_role: Literal[
        "document_date",
        "case_date",
        "proceeding_started",
        "entry_into_force",
        "copy_issued",
        "hearing_date",
        "current_document_date",
        "other",
        "not_stated",
    ]
    date_literal: str | None = Field(max_length=100)
    source_quote: str | None = Field(max_length=1200)
    source_page: PageRef | None
    warnings: list[str]


class RelatedDatesReading(StrictModel):
    """Ограниченная порция свидетельств; общий список ссылок не ограничивается её размером."""

    readings: list[RelatedDateReading] = Field(max_length=12)


class RelatedDateSummary(StrictModel):
    """Уточнение только краткого содержания после изменения связанных дат."""

    summary: str | None = Field(max_length=4000)
    warnings: list[str]


def literal_date(value: str) -> Date | None:
    """Проверяет нормализацию переданной VLM даты, не выбирая её из OCR или текста."""
    text = unicodedata.normalize("NFKC", value).casefold().strip()
    text = re.sub(r"\s*(?:года|г\.)$", "", text).strip()
    iso = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", text)
    numeric = re.fullmatch(r"(\d{1,2})\s*[./-]\s*(\d{1,2})\s*[./-]\s*(\d{4})", text)
    months = (
        "января",
        "февраля",
        "марта",
        "апреля",
        "мая",
        "июня",
        "июля",
        "августа",
        "сентября",
        "октября",
        "ноября",
        "декабря",
    )
    words = re.fullmatch(r"(\d{1,2})\s+(" + "|".join(months) + r")\s+(\d{4})", text)
    try:
        if iso:
            return Date(*map(int, iso.groups()))
        if numeric:
            day, month, year = map(int, numeric.groups())
            return Date(year, month, day)
        if words:
            return Date(int(words[3]), months.index(words[2]) + 1, int(words[1]))
    except ValueError:
        return None
    return None


def contains_number(quote: str, number: str) -> bool:
    """Проверяет номер в цитате с пробелами/дефисами, без замены похожих букв и цифр."""
    # NFKC превращает № в латинское No; маркер заранее оставляем разделителем.
    text = unicodedata.normalize("NFKC", quote.replace("№", " ")).casefold()
    text = "".join("-" if c in "‐‑‒–—−" else c for c in text)
    pattern = r"(?<![\w/])" + r"\s*".join(map(re.escape, identifier_key(number))) + r"(?![\w/])"
    return bool(re.search(pattern, text))


def validate_related_dates(
    result: RelatedDatesReading, references: dict[int, str], pages: set[PageRef]
) -> None:
    """Проверяет покрытие, роль, адрес и согласованность даты с буквальной цитатой VLM."""
    indices = [entry.index for entry in result.readings]
    if len(indices) != len(references) or set(indices) != set(references):
        raise IntakeError(
            "schema_validation_failed", "Индексы ссылок потеряны, повторены или чужие"
        )
    roles = {
        "court_act": "document_date",
        "court_case": "case_date",
        "enforcement_proceeding": "proceeding_started",
    }
    for entry in result.readings:
        if entry.source_page is not None and entry.source_page not in pages:
            raise IntakeError("schema_validation_failed", "Цитата связанной даты с чужой страницы")
        if (entry.readability == "legible") != (entry.date is not None):
            raise IntakeError("schema_validation_failed", "Связанная дата противоречит читаемости")
        if entry.date is None:
            if entry.date_literal is not None or not entry.warnings:
                raise IntakeError(
                    "schema_validation_failed", "Для неизвестной даты нужны null literal и warning"
                )
            continue
        if entry.date_role != roles.get(entry.reference_kind):
            raise IntakeError(
                "schema_validation_failed",
                "Роль даты не соответствует ссылке. Дата вступления в силу, "
                "выдачи копии, заседания "
                "и текущего постановления не является собственной датой связанного акта. "
                "Перечитай номер и его дату; если она не указана — null с warning.",
            )
        if not entry.source_page or not entry.source_quote or not entry.date_literal:
            raise IntakeError(
                "schema_validation_failed", "Читаемая связанная дата требует цитаты и страницы"
            )
        path = f"readings[index={entry.index}]"
        if not contains_number(entry.source_quote, references[entry.index]):
            raise IntakeError(
                "schema_validation_failed",
                f"{path}.source_quote не содержит точный номер ссылки. "
                "Перечитай источник и приведи цитату с номером и его собственной датой.",
            )
        if identifier_key(entry.date_literal) not in identifier_key(entry.source_quote):
            raise IntakeError(
                "schema_validation_failed",
                f"{path}.date_literal отсутствует в source_quote. "
                "Скопируй буквальное написание даты из цитаты, сохрани цифры, слова "
                "и разделители источника; ISO-нормализация допустима только в date. "
                "Если дата напечатана в начале предложения, расширь source_quote "
                "до полного предложения с датой и номером. "
                "Если цитата не подтверждает собственную дату ссылки, верни date=null, "
                "date_literal=null, readability=ambiguous и warning.",
            )
        if literal_date(entry.date_literal) != entry.date:
            raise IntakeError(
                "schema_validation_failed",
                f"{path}.date не соответствует date_literal. "
                "Нормализуй только однозначно прочитанную буквальную дату; "
                "при неоднозначности верни null и warning.",
            )
