# tests/test_document_processing.py
"""Классификация перед шаблоном, независимость блоков, продолжения и безопасные заглушки."""

import copy

import pytest

from app.domain.document_processing import DocumentClassification
from app.domain.errors import IntakeError, LeaseLost
from app.domain.jobs import Job, utcnow
from app.domain.schema import DocumentRoot
from tests.support import facts, fixed_group


def classification(is_letter=True):
    """Создаёт явно синтетическую классификацию, независимо от шаблона реквизитов."""
    value = copy.deepcopy(facts()["classification"])
    if not is_letter:
        value.update(
            document_root=DocumentRoot.PROCEDURAL.value,
            is_letter=False,
            document_kind="Постановление",
            document_subkind=None,
            template_path=[],
        )
    return {
        "classification": value,
        "continuation_summary": "Синтетическое описание блока",
        "warnings": ["Направление неизвестно"],
    }


def job(container):
    """Создаёт только локальный снимок с базовыми промптами, без сети и документов владельца."""
    return Job(
        id="synthetic",
        owner="test",
        status="running",
        created_at=utcnow(),
        expires_at=None,
        sources=[],
        prompts=container.prompts.base,
    )


def pages(count, offset=0):
    """Сохраняет физические номера, не перенумеровывая части блока с единицы."""
    return [
        {"file_id": "source", "page_number": offset + i, "ocr": "SYNTHETIC"}
        for i in range(1, count + 1)
    ]


async def emit(kind, data):
    """Не выводит синтетические факты в обычные логи."""


async def test_nonletter_has_classification_and_placeholder_without_extraction(container):
    """Постановление больше не проходит шаблон письма и дополнительную проверку судебных дат."""
    container.vision.answers.append(classification(False))
    physical = pages(1)
    group = fixed_group([{k: p[k] for k in ("file_id", "page_number")} for p in physical])
    result, status = await container.analysis.documents.run(
        job(container), group, physical, container.analysis._extract, emit
    )
    assert result.classification.document_root == DocumentRoot.PROCEDURAL
    assert status.status == "unsupported_template" and status.code == "template_not_supported"
    assert result.requisites.document_number is None
    assert result.requisites.related_entities.court_cases == []
    assert "template_not_supported" in str(result.warnings)
    assert [c["schema"]["title"] for c in container.vision.calls] == ["DocumentClassification"]


@pytest.mark.parametrize("count", [1, 8, 9, 17])
async def test_complete_block_is_classified_first_with_bounded_continuations(container, count):
    """Все страницы одного блока передаются по частям до шаблона; соседний блок не включается."""
    physical = pages(count, offset=100)
    group = fixed_group([{k: p[k] for k in ("file_id", "page_number")} for p in physical])
    result, status = await container.analysis.documents.run(
        job(container), group, physical, container.analysis._extract, emit
    )
    assert status.status == "recognized"
    calls = container.vision.calls
    classifiers = [c for c in calls if c["schema"]["title"] == "DocumentClassification"]
    assert len(classifiers) == (count + 7) // 8
    assert all(c["schema"]["title"] == "DocumentClassification" for c in calls[: len(classifiers)])
    assert [p["page_number"] for c in classifiers for p in c["pages"]] == list(
        range(101, 101 + count)
    )
    assert all(len(c["pages"]) <= 8 for c in calls)
    assert classifiers[0]["context"]["previous_part"] is None
    for index, c in enumerate(classifiers, 1):
        part = c["context"]["part"]
        assert part == {
            "block_id": "synthetic:1",
            "part_index": index,
            "parts_total": len(classifiers),
            "has_previous": index > 1,
            "has_next": index < len(classifiers),
            "same_document": True,
        }
        if index > 1:
            assert c["context"]["previous_part"]["classification"] == (
                DocumentClassification.model_validate(classification()).classification.model_dump(
                    mode="json"
                )
            )
    extraction = [c for c in calls if c["schema"]["title"] == "ExtractionDraft" and c["pages"]]
    assert all(
        c["context"]["selected_classification"] == result.classification.model_dump(mode="json")
        for c in extraction
    )
    assert all(c["context"]["part"]["same_document"] for c in extraction)
    assert extraction[-1]["pages"][-1]["page_number"] == 100 + count


async def test_failed_letter_is_explicitly_incomplete_and_lease_loss_is_not_swallowed(container):
    """Непроверенный ответ не становится реквизитами; отмена продолжает прерывать обработку."""
    physical = pages(1)
    group = fixed_group([{k: p[k] for k in ("file_id", "page_number")} for p in physical])

    async def invalid(*args):
        """Моделирует исчерпание реального ограниченного бюджета, не выдавая выдуманные факты."""
        raise IntakeError("schema_validation_failed", "Проверка не пройдена")

    result, status = await container.analysis.documents.run(
        job(container), group, physical, invalid, emit
    )
    assert status.status == "extraction_failed" and status.code == "schema_validation_failed"
    assert result.classification.is_letter and result.requisites.document_number is None
    assert result.recognition.low_confidence_fields == ["requisites"]

    async def revoked(*args):
        """Сигнализирует отмену или смену поколения worker."""
        raise LeaseLost()

    with pytest.raises(LeaseLost):
        await container.analysis.documents.run(job(container), group, physical, revoked, emit)


async def test_classification_schema_rejects_invented_letter_kind(container):
    """Отдельный классификатор использует тот же неизменяемый закрытый каталог."""
    from pydantic import ValidationError

    raw = classification()
    raw["classification"]["document_kind"] = "Выдуманный вид"
    with pytest.raises(ValidationError):
        DocumentClassification.model_validate(raw)
