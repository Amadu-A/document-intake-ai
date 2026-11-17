# tests/test_related_dates.py
"""Роли связанных дат, буквальные цитаты и ограниченная независимая VLM-проверка."""

import copy
from datetime import date

import pytest

from app.application.related_dates import review_court_dates
from app.domain.errors import IntakeError
from app.domain.extraction import ExtractionDraft
from app.domain.related_dates import (
    RelatedDatesReading,
    contains_number,
    literal_date,
    validate_related_dates,
)
from app.domain.segmentation import PageRef
from tests.support import ScriptedVision, facts

PAGE = PageRef(file_id="source", page_number=1)


def evidence():
    """Создаёт синтетическую связь номера акта с собственной датой."""
    return {
        "index": 0,
        "reference_kind": "court_act",
        "date": "2031-01-12",
        "readability": "legible",
        "date_role": "document_date",
        "date_literal": "12.01.2031",
        "source_quote": "Судебный акт №ACT-1/2031 от 12.01.2031",
        "source_page": PAGE.model_dump(),
        "warnings": [],
    }


def checked(value):
    """Проверяет один источник, не выполняя OCR и не выбирая другую дату."""
    result = RelatedDatesReading.model_validate({"readings": [value]})
    validate_related_dates(result, {0: "ACT-1/2031"}, {PAGE})
    return result


@pytest.mark.parametrize(
    "text", ["12.01.2031", "12/01/2031", "12-01-2031", "2031-01-12", "12 января 2031 года"]
)
def test_literal_normalization_only_checks_the_date_selected_by_vlm(text):
    """Разные буквальные оформления проверяются без эвристического выбора роли даты."""
    assert literal_date(text) == date(2031, 1, 12)


@pytest.mark.parametrize("text", ["31.02.2031", "12.13.2031", "12.01.31", "неизвестно"])
def test_invalid_or_incomplete_literal_cannot_confirm_a_date(text):
    """Невалидная календарная дата и неподтверждённый век не становятся ISO-датой."""
    assert literal_date(text) is None


def test_number_marker_and_spacing_do_not_hide_or_replace_identifier():
    """Маркер № без пробела и типографский дефис допустимы, похожие идентификаторы — нет."""
    assert contains_number("№ACT-1/2031 от ...", "ACT-1/2031")
    assert contains_number("№ ACT — 1 / 2031 от ...", "ACT-1/2031")
    assert not contains_number("ACT-10/2031", "ACT-1/2031")
    assert not contains_number("ACT-O/2031", "ACT-0/2031")


@pytest.mark.parametrize(
    "role", ["entry_into_force", "copy_issued", "hearing_date", "current_document_date"]
)
def test_other_event_date_cannot_become_the_date_of_the_act(role):
    """Даже правильные номер/цитата не разрешают подменить собственную дату другим событием."""
    value = evidence()
    value.update(date="2030-01-01", date_literal="01.01.2030", date_role=role)
    value["source_quote"] = "Акт №ACT-1/2031 вступил в силу 01.01.2030"
    original = copy.deepcopy(value)
    with pytest.raises(IntakeError):
        checked(value)
    assert value == original


def test_chronological_contradiction_does_not_change_the_printed_date():
    """Дата вступления в силу раньше даты акта не даёт backend права исправлять источник."""
    value = evidence()
    value["source_quote"] += ", вступил в силу 01.01.2030"
    assert checked(value).readings[0].date == date(2031, 1, 12)


@pytest.mark.parametrize(
    "problem",
    [
        "foreign_page",
        "no_page",
        "wrong_number",
        "no_literal",
        "wrong_literal",
        "wrong_value",
        "ambiguous_value",
    ],
)
def test_inconsistent_evidence_is_rejected(problem):
    """Страница, номер, буквальная дата, значение и читаемость проверяются независимо."""
    value = evidence()
    if problem == "foreign_page":
        value["source_page"]["file_id"] = "foreign"
    elif problem == "no_page":
        value["source_page"] = None
    elif problem == "wrong_number":
        value["source_quote"] = "Акт №ACT-10/2031 от 12.01.2031"
    elif problem == "no_literal":
        value["date_literal"] = None
    elif problem == "wrong_literal":
        value["date_literal"] = "13.01.2031"
    elif problem == "wrong_value":
        value["date"] = "2031-01-13"
    else:
        value["readability"] = "ambiguous"
    with pytest.raises(IntakeError):
        checked(value)


@pytest.mark.parametrize("indices", [[0, 0], [1], []])
def test_lost_duplicate_or_unknown_reference_index_is_rejected(indices):
    """Ни одна запрошенная ссылка не теряется и не заменяется соседней."""
    entries = [{**evidence(), "index": index} for index in indices]
    with pytest.raises(IntakeError):
        validate_related_dates(
            RelatedDatesReading.model_validate({"readings": entries}), {0: "ACT-1/2031"}, {PAGE}
        )


def draft(value=None):
    """Подготавливает только синтетические факты с ошибочно привязанной старой датой."""
    raw = facts()
    raw["requisites"]["related_entities"]["court_cases"] = [
        {"number": "ACT-1/2031", "date": value, "type": "Судебный акт"}
    ]
    return ExtractionDraft.model_validate(ScriptedVision.readability(raw))


async def test_verified_vlm_date_updates_summary_but_preserves_other_facts(container):
    """Дату и уточнение summary возвращает VLM; прочие реквизиты backend не регенерирует."""
    original = draft("2030-01-01")
    original.requisites.summary = "Акт от 01.01.2030; удержание 50%."
    before = original.model_dump(mode="json")
    container.vision.answers.extend(
        [
            {"readings": [evidence()]},
            {"summary": "Акт от 12.01.2031; удержание 50%.", "warnings": []},
        ]
    )

    async def emit(kind, data):
        """Не выводит тестовый источник в обычный журнал."""

    result = await review_court_dates(
        original,
        [{**PAGE.model_dump(), "ocr": "UNTRUSTED_DATE"}],
        PAGE,
        "Синтетический prompt",
        container.analysis._validated,
        emit,
    )
    assert result.requisites.related_entities.court_cases[0].date == date(2031, 1, 12)
    assert result.requisites.summary == "Акт от 12.01.2031; удержание 50%."
    assert result.requisites.document_date == original.requisites.document_date
    assert result.requisites.amounts == original.requisites.amounts
    assert original.model_dump(mode="json") == before
    first, summary = container.vision.calls
    assert "2030-01-01" not in str(first["context"])
    assert "UNTRUSTED_DATE" not in str(first["context"]) + str(first["pages"])
    assert summary["pages"] == []
    assert summary["schema"]["title"] == "RelatedDateSummary"
    assert "source_quote" not in result.facts().model_dump()


async def test_reference_batches_are_bounded_without_a_limit_on_the_total(container):
    """Двадцать пять ссылок проходят тремя порциями без потерь; unknown остаётся null."""
    original = draft()
    ref = original.requisites.related_entities.court_cases[0]
    original.requisites.related_entities.court_cases = [
        ref.model_copy(update={"number": f"SYNTHETIC-{i}"}) for i in range(25)
    ]

    async def emit(kind, data):
        """Не сохраняет синтетический поток."""

    result = await review_court_dates(
        original,
        [PAGE.model_dump()],
        PAGE,
        "Синтетический prompt",
        container.analysis._validated,
        emit,
    )
    assert len(result.requisites.related_entities.court_cases) == 25
    assert all(ref.date is None for ref in result.requisites.related_entities.court_cases)
    assert [len(call["context"]["references"]) for call in container.vision.calls] == [12, 12, 1]


@pytest.mark.parametrize("status", ["missing", "ambiguous"])
async def test_unknown_related_date_stays_null_with_vlm_warning(container, status):
    """Сомнительную дату не сохраняют из первого ответа; предупреждение и путь остаются честными."""
    original = draft("2030-01-01")
    value = evidence()
    value.update(
        date=None,
        readability=status,
        date_role="not_stated",
        date_literal=None,
        source_quote=None,
        source_page=None,
        warnings=["Дата не установлена по источнику"],
    )
    container.vision.answers.append({"readings": [value]})

    async def emit(kind, data):
        """Не выводит синтетические реквизиты."""

    result = await review_court_dates(
        original,
        [PAGE.model_dump()],
        PAGE,
        "Синтетический prompt",
        container.analysis._validated,
        emit,
    )
    assert result.requisites.related_entities.court_cases[0].date is None
    assert value["warnings"][0] in result.warnings
    path = "requisites.related_entities.court_cases.0.date"
    assert (path in result.recognition.low_confidence_fields) == (status == "ambiguous")


async def test_unchanged_dates_do_not_regenerate_summary(container):
    """Проверка связки не переписывает уже согласованные даты и остальное краткое содержание."""
    original = draft("2031-01-12")
    original.requisites.summary = "Сохранить удержание 50% и остальные факты."
    container.vision.answers.append({"readings": [evidence()]})

    async def emit(kind, data):
        """Не публикует синтетический поток."""

    result = await review_court_dates(
        original,
        [PAGE.model_dump()],
        PAGE,
        "Синтетический prompt",
        container.analysis._validated,
        emit,
    )
    assert result.requisites.summary == original.requisites.summary
    assert len(container.vision.calls) == 1


async def test_reading_schema_only_allows_current_reference_indices_and_page_addresses(container):
    """Генерация выбирает существующий адрес/индекс; исходная DTO-схема и даты не подменяются."""
    original = draft()
    before_schema = RelatedDatesReading.model_json_schema()
    other = PageRef(file_id="second-source", page_number=9)

    async def emit(kind, data):
        """Не выводит синтетические реквизиты в журнал."""

    await review_court_dates(
        original,
        [PAGE.model_dump(), other.model_dump()],
        PAGE,
        "Синтетический prompt",
        container.analysis._validated,
        emit,
    )
    call = container.vision.calls[0]
    schema = call["schema"]
    assert schema["properties"]["readings"]["minItems"] == 1
    assert schema["properties"]["readings"]["maxItems"] == 1
    assert schema["$defs"]["RelatedDateReading"]["properties"]["index"]["enum"] == [0]
    allowed = {
        (branch["properties"]["file_id"]["const"], page)
        for branch in schema["$defs"]["PageRef"]["anyOf"]
        for page in branch["properties"]["page_number"]["enum"]
    }
    assert allowed == {(PAGE.file_id, 1), (other.file_id, 9)}
    assert {PageRef.model_validate(p) for p in call["context"]["allowed_pages"]} == {PAGE, other}
    assert RelatedDatesReading.model_json_schema() == before_schema


@pytest.mark.parametrize("problem", ["date_format", "truncated_quote"])
async def test_related_date_retry_receives_candidate_and_exact_conflicting_field(
    container, problem
):
    """Повтор исправляет формат или обрезанную цитату второй даты, сохраняя первую ссылку."""
    original = draft("2031-01-12")
    references = original.requisites.related_entities.court_cases
    references.append(references[0].model_copy(update={"number": "ACT-2/2031"}))
    first = evidence()
    second = {**evidence(), "index": 1}
    second["source_quote"] = "Акт №ACT-2/2031 от 12 января 2031 года"
    invalid = {"readings": [first, second]}
    corrected = copy.deepcopy(invalid)
    corrected["readings"][1]["date_literal"] = "12 января 2031 года"
    if problem == "truncated_quote":
        second["source_quote"] = "должностное лицо вынесло акт №ACT-2/2031"
        corrected["readings"][1]["source_quote"] = (
            "12.01.2031 должностное лицо вынесло акт №ACT-2/2031"
        )
        corrected["readings"][1]["date_literal"] = "12.01.2031"
    container.vision.answers.extend([invalid, corrected])
    before = original.model_dump(mode="json")

    async def emit(kind, data):
        """Не выводит частные цитаты в журнал."""

    result = await review_court_dates(
        original, [PAGE.model_dump()], PAGE, "Промпт", container.analysis._validated, emit
    )
    assert original.model_dump(mode="json") == before
    assert [ref.date for ref in result.requisites.related_entities.court_cases] == [
        date(2031, 1, 12),
        date(2031, 1, 12),
    ]
    initial, retry = container.vision.calls
    assert "candidate" not in initial["context"]
    assert retry["context"]["candidate"] == invalid
    assert "readings[index=1].date_literal" in str(retry["context"]["validation_errors"])
    assert retry["pages"] == initial["pages"]
    assert retry["schema"] == initial["schema"]


async def test_exhausted_related_date_retries_never_publish_unverified_date(container):
    """Повторяющееся несовпадение не ослабляет доказательство и не меняет исходные факты."""
    original = draft("2030-01-01")
    invalid = evidence()
    invalid["source_quote"] = "Акт №ACT-1/2031 от 13.01.2031"
    container.vision.answers.extend(
        [{"readings": [invalid]} for _ in range(container.settings.model_attempts)]
    )
    before = original.model_dump(mode="json")

    async def emit(kind, data):
        """Не выводит синтетическое содержимое в обычные логи."""

    with pytest.raises(IntakeError, match="ограниченных повторов"):
        await review_court_dates(
            original, [PAGE.model_dump()], PAGE, "Промпт", container.analysis._validated, emit
        )
    assert original.model_dump(mode="json") == before
    assert len(container.vision.calls) == container.settings.model_attempts
