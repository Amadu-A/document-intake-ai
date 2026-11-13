# tests/test_primary_date.py
"""Дата основной части читается VLM независимо от OCR и дат физических приложений."""

import copy

import pytest

from app.domain.jobs import Job, utcnow
from app.domain.segmentation import Plan
from tests.support import facts, plan_for


def packet():
    """Возвращает синтетическую основную страницу и приложение с посторонним OCR."""
    pages = [{"file_id": "source", "page_number": n, "ocr": "UNTRUSTED_OCR_DATE"} for n in (1, 2)]
    group = Plan.model_validate(
        plan_for([{"file_id": "source", "page_number": n} for n in (1, 2)], main=1)
    ).documents[0]
    value = facts([{"title": "Приложение", "page_range": [2, 2]}])
    return pages, group, value


def job(container):
    """Создаёт тестовый снимок без сети, файлов и приватных реквизитов."""
    return Job(
        id="synthetic",
        owner="synthetic",
        status="running",
        created_at=utcnow(),
        expires_at=None,
        sources=[],
        prompts=container.prompts.base,
    )


async def emit(kind, data):
    """Не публикует синтетический источник вне теста."""


def reading():
    """Имитирует независимое визуальное решение VLM по основной странице."""
    return {
        "document_date": "2031-01-12",
        "readability": "legible",
        "source_page": {"file_id": "source", "page_number": 1},
        "warnings": [],
    }


async def test_primary_date_ignores_attachment_ocr_and_is_revalidated_on_full_answer(container):
    """Неверная дата полного ответа отклоняется, правильную повторно возвращает сама VLM."""
    pages, group, raw = packet()
    wrong, corrected = copy.deepcopy(raw), copy.deepcopy(raw)
    wrong["requisites"]["document_date"] = "2031-02-13"
    corrected["requisites"]["document_date"] = "2031-01-12"
    container.vision.answers.extend([reading(), wrong, corrected])
    result = await container.analysis._extract(job(container), group, pages, emit)
    assert result.requisites.document_date.isoformat() == "2031-01-12"
    assert len(container.vision.calls) == 3
    primary, full, retry = container.vision.calls
    assert primary["schema"]["title"] == "PrimaryDateReading"
    assert [p["page_number"] for p in primary["pages"]] == [1]
    assert "UNTRUSTED_OCR_DATE" not in str(primary["context"])
    for call in (full, retry):
        assert (
            call["schema"]["$defs"]["Requisites"]["properties"]["document_date"]["const"]
            == "2031-01-12"
        )
    assert retry["context"]["validation_errors"]
    assert "primary_date_evidence" not in result.model_dump()


@pytest.mark.parametrize("status", ["ambiguous", "missing"])
async def test_unreadable_primary_date_stays_null_without_guessing(container, status):
    """Отсутствие/неоднозначность независимого чтения не заменяются датой приложения."""
    pages, group, value = packet()
    independent = reading()
    independent.update(document_date=None, readability=status)
    if status == "ambiguous":
        independent["warnings"] = ["Две возможные даты"]
        value["recognition"]["low_confidence_fields"] = ["requisites.document_date"]
    value["primary_readability"] = {
        "document_date": status,
        "document_number": "missing",
        "handwritten_fields": [],
    }
    container.vision.answers.extend([independent, value])
    result = await container.analysis._extract(job(container), group, pages, emit)
    assert result.requisites.document_date is None
    assert all(warning in result.warnings for warning in independent["warnings"])
    assert container.vision.calls[-1]["schema"]["$defs"]["Requisites"]["properties"][
        "document_date"
    ] == {"type": "null"}


@pytest.mark.parametrize(
    "problem", ["foreign_page", "no_page", "null_legible", "missing_value", "no_warning"]
)
async def test_invalid_independent_date_evidence_requires_vlm_retry(container, problem):
    """Чужая страница и противоречивый статус вызывают повтор, backend не чинит ответ."""
    pages, group, value = packet()
    bad = reading()
    if problem == "foreign_page":
        bad["source_page"]["page_number"] = 2
    elif problem == "no_page":
        bad["source_page"] = None
    elif problem == "null_legible":
        bad["document_date"] = None
    elif problem == "missing_value":
        bad["readability"] = "missing"
    else:
        bad.update(document_date=None, readability="ambiguous")
    value["requisites"]["document_date"] = "2031-01-12"
    container.vision.answers.extend([bad, reading(), value])
    result = await container.analysis._extract(job(container), group, pages, emit)
    assert result.requisites.document_date.isoformat() == "2031-01-12"
    assert [c["schema"]["title"] for c in container.vision.calls] == [
        "PrimaryDateReading",
        "PrimaryDateReading",
        "ExtractionDraft",
    ]
