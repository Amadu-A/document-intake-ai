# app/application/related_dates.py
"""Независимая VLM-проверка связанных судебных дат без OCR и привязки к старым значениям."""

from collections.abc import Callable
from typing import Protocol, TypeVar

from app.application.ports import Emit
from app.domain.errors import IntakeError
from app.domain.extraction import ExtractionDraft
from app.domain.related_dates import (
    RelatedDatesReading,
    RelatedDateSummary,
    validate_related_dates,
)
from app.domain.schema import StrictModel
from app.domain.segmentation import PageRef

T = TypeVar("T", bound=StrictModel)


class ValidatedRequest(Protocol):
    """Узкий callback существующей ограниченной проверки ответа VLM."""

    async def __call__(
        self,
        prompt: str,
        context: dict,
        pages: list[dict],
        model: type[T],
        validate: Callable[[T], None],
        emit: Emit,
    ) -> T:
        """Получает проверенный ответ, повторяя только в пределах существующего бюджета."""
        ...


async def review_court_dates(
    facts: ExtractionDraft,
    pages: list[dict],
    header_page: PageRef,
    prompt: str,
    request: ValidatedRequest,
    emit: Emit,
) -> ExtractionDraft:
    """Проецирует проверенное решение VLM в связанные даты; другие факты не переписывает."""
    references = facts.requisites.related_entities.court_cases
    known = [(i, ref) for i, ref in enumerate(references) if ref.number and ref.number.strip()]
    if not known:
        return facts
    allowed = {PageRef(file_id=p["file_id"], page_number=p["page_number"]) for p in pages}
    images = [
        {
            k: p[k]
            for k in ("file_id", "page_number", "preview", "header_preview", "footer_preview")
            if k in p
        }
        for p in pages
    ]
    result = facts.model_copy(deep=True)
    evidence = []
    changed = False
    for offset in range(0, len(known), 12):
        block = known[offset : offset + 12]
        numbers = {i: ref.number for i, ref in block}
        readings = await request(
            prompt,
            {
                "task": "read_related_court_dates",
                "footer_details": True,
                "header_detail_page": header_page.model_dump(),
                "references": [
                    {"index": i, "number": ref.number, "type": ref.type} for i, ref in block
                ],
                "instruction": "Независимо прочитай собственную дату КАЖДОЙ ссылки по номеру "
                "и типу, только по изображениям. Старые даты и OCR намеренно не переданы. "
                "Для судебного акта нужна дата самого акта (обычно рядом с «от»), "
                "для производства — дата возбуждения; для дела — только явно указанная "
                "собственная дата. Даты вступления в силу, выдачи копии, заседания и текущего "
                "постановления имеют ДРУГИЕ роли и не подставляются в date ссылки. "
                "Различай дату самого акта и отдельную дату выдачи КОПИИ. "
                "Судебный приказ/решение/постановление: reference_kind=court_act, "
                "date_role=document_date. Исполнительное производство: "
                "reference_kind=enforcement_proceeding, date_role=proceeding_started. "
                "Дело без ссылки на конкретный акт: court_case и case_date. "
                "Тип ссылки служит подсказкой; подтверди назначение по цитате. "
                "Не восстанавливай дату из цифр номера. Хронологическое "
                "противоречие не исправляй: сохрани напечатанные даты и warning. "
                "Для legible нужны date_literal (точное написание даты), короткая "
                "source_quote с номером и его датой, source_page. При отсутствии/"
                "неоднозначности — date=null, date_literal=null и warning. Верни все "
                "переданные индексы ровно один раз, только JSON текущей схемы.",
            },
            images,
            RelatedDatesReading,
            lambda value, expected=numbers: validate_related_dates(value, expected, allowed),
            emit,
        )
        for entry in readings.readings:
            target = result.requisites.related_entities.court_cases[entry.index]
            changed |= target.date != entry.date
            target.date = entry.date
            result.warnings.extend(entry.warnings)
            if entry.readability == "ambiguous":
                result.recognition.low_confidence_fields.append(
                    f"requisites.related_entities.court_cases.{entry.index}.date"
                )
        evidence.extend(entry.model_dump(mode="json") for entry in readings.readings)
    if changed and facts.requisites.summary:

        def validate_summary(value: RelatedDateSummary) -> None:
            """Не позволяет потерять существующее краткое содержание при уточнении даты."""
            if not value.summary or not value.summary.strip():
                raise IntakeError(
                    "schema_validation_failed", "Уточнение даты не должно удалять summary"
                )

        summary = await request(
            prompt,
            {
                "task": "reconcile_related_date_summary",
                "source_summary": facts.requisites.summary,
                "verified_references": [
                    r.model_dump(mode="json")
                    for r in result.requisites.related_entities.court_cases
                ],
                "date_evidence": evidence,
                "document_date": result.requisites.document_date.isoformat()
                if result.requisites.document_date
                else None,
                "instruction": "Исправь в summary только неверную привязку связанных дат "
                "по проверенным цитатам VLM. Сохрани остальные факты: суммы, процент "
                "удержания, исход дела, сроки, адреса и время явки. Не добавляй факты "
                "и не исправляй хронологию источника. Дата вступления в силу не равна "
                "дате самого акта. Верни только summary и warnings текущей схемы.",
            },
            [],
            RelatedDateSummary,
            validate_summary,
            emit,
        )
        result.requisites.summary = summary.summary
        result.warnings.extend(summary.warnings)
    result.warnings = list(dict.fromkeys(result.warnings))
    result.recognition.low_confidence_fields = list(
        dict.fromkeys(result.recognition.low_confidence_fields)
    )
    return result
