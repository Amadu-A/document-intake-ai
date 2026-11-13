# tests/test_correspondence_semantics.py
"""Предметная матрица замечает неправильные события/даты, исторический штраф и место отправки."""

import copy

import pytest

from app.application.one_c_projection import project_one_c
from tests.correspondence_support import compare_correspondence, summary_dates, summary_times
from tests.test_correspondence_projection import ORGANIZATION, procedural


@pytest.mark.parametrize("text", ["12.01.2031", "2031-01-12", "12 января 2031 года"])
def test_summary_dates_accept_equivalent_factual_formats(text):
    """Формат записи не подменяет проверку календарной даты."""
    assert summary_dates(text) == {"2031-01-12"}


def test_hearing_time_does_not_match_calendar_date_or_law_part():
    """Календарное число/часть закона не являются временем заседания."""
    assert not summary_times("12.01.2031; по 12 части")
    assert summary_times("В 09:30 или в 9 часов 30 минут") == {"09:30"}


def hearing_document():
    """Создаёт только синтетический эталон повестки; реальных дат/реквизитов здесь нет."""
    facts = procedural()
    facts.requisites.summary = "Заседание 12 января 2031 года в 09:30."
    facts.requisites.sender.address = "г. Тестоград, ул. Синтетическая, д. 17"
    facts.one_c = project_one_c(facts, ORGANIZATION)
    value = facts.model_dump(mode="json")
    value["pages"] = [1, 2]
    value["requisites"]["related_entities"]["court_cases"] = [
        {"number": "SYNTHETIC-CASE", "date": None, "type": "Дело"}
    ]
    return value


def compare(expected, actual):
    """Передаёт матрице факты, не UUID/токены и не предположения по confidence."""
    actual["one_c"]["comment"] = " ".join(actual["requisites"]["summary"].split())
    return compare_correspondence(
        {"files": [{"documents": [expected]}]},
        {"files": [{"documents": [actual]}]},
        ORGANIZATION,
    )


@pytest.mark.parametrize(
    "problem,check",
    [
        ("date", "hearing_date_present"),
        ("time", "hearing_time_present"),
        ("address", "sender_street_from_envelope"),
        ("reply", "requisites.response_due_date"),
        ("case_date", "court_cases[0].date"),
    ],
)
def test_semantic_matrix_rejects_wrong_event_or_sender_location(problem, check):
    """Даты заседания/дела/ответа и адрес отправителя проверяются независимо."""
    wanted = hearing_document()
    actual = copy.deepcopy(wanted)
    assert all(row["match"] for row in compare(wanted, actual))
    if problem == "date":
        actual["requisites"]["summary"] = "Заседание 13.01.2031 в 09:30."
    elif problem == "time":
        actual["requisites"]["summary"] = "Заседание 12.01.2031 в 08:30."
    elif problem == "address":
        actual["requisites"]["sender"]["address"] = "г. Тестоград, ул. Иная, д. 17"
    elif problem == "reply":
        actual["requisites"]["response_due_date"] = "2031-01-12"
    else:
        actual["requisites"]["related_entities"]["court_cases"][0]["date"] = "2031-01-12"
    assert any(row["field"] == check and not row["match"] for row in compare(wanted, actual))


def test_historical_fine_cannot_be_a_generic_current_debt():
    """Совпавшая цифра не скрывает потерю контекста прекращённого производства."""
    wanted = hearing_document()
    wanted["classification"]["document_kind"] = "Решение по делу об административном правонарушении"
    wanted["requisites"]["summary"] = "Производство прекращено, постановление отменено."
    wanted["requisites"]["amounts"] = [
        {"type": "Штраф по прекращённому делу", "value": 500, "currency": "RUB"}
    ]
    actual = copy.deepcopy(wanted)
    actual["requisites"]["amounts"][0]["type"] = "debt"
    assert any(
        row["field"] == "historical_amount[0].role" and not row["match"]
        for row in compare(wanted, actual)
    )
