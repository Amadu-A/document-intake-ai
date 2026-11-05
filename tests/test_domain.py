# tests/test_domain.py
"""Контракты JSON, immutable taxonomy и техническая целостность VLM-плана."""

import copy
import json
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.application.analysis import validate_extraction
from app.domain.errors import IntakeError
from app.domain.schema import Amount, BatchResult, Classification, Extracted
from app.domain.segmentation import PageRef, Plan, validate_plan, windows
from app.domain.taxonomy import LETTER_PATHS, taxonomy_json
from app.infrastructure.vision import strict_json
from tests.support import facts, plan_for


def test_one_c_cannot_add_an_unapproved_template_leaf():
    """Справочные поля 1С не обходят закрытый каталог при неизвестном направлении."""
    value = facts()
    value["one_c"]["template_leaf"] = "Произвольный шаблон"
    group = Plan.model_validate(plan_for([{"file_id": "a", "page_number": 1}])).documents[0]
    with pytest.raises(IntakeError):
        validate_extraction(Extracted.model_validate(value), group, None)


def refs(count=6, file_id="source-a"):
    """Создаёт независимые адреса физических страниц для проверки плана."""
    return [{"file_id": file_id, "page_number": n} for n in range(1, count + 1)]


def test_claim_with_four_attachments_is_one_registration():
    """Шесть страниц претензии образуют один комплект и четыре внутренних приложения."""
    pages = refs()
    plan = Plan.model_validate(plan_for(pages, main=2))
    validate_plan(plan, [PageRef(**p) for p in pages])
    assert len(plan.documents) == 1
    assert len(plan.documents[0].attachments) == 4


@pytest.mark.parametrize(
    "mutation",
    ["missing", "duplicate", "unknown", "nested_duplicate", "order", "review", "unassigned"],
)
def test_plan_rejects_integrity_errors(mutation):
    """Невалидные модельные границы отклоняются без локального семантического исправления."""
    pages = refs()
    raw = plan_for(pages, main=2)
    group = raw["documents"][0]
    if mutation == "missing":
        group["pages"] = group["pages"][:-1]
    elif mutation == "duplicate":
        group["pages"] = group["pages"] + [pages[0]]
    elif mutation == "unknown":
        group["pages"] = group["pages"] + [{"file_id": "foreign", "page_number": 1}]
    elif mutation == "nested_duplicate":
        group["main_pages"] = group["main_pages"] + [pages[2]]
    elif mutation == "order":
        group["pages"] = list(reversed(group["pages"]))
    elif mutation == "review":
        group["status"] = "needs_review"
    elif mutation == "unassigned":
        raw["unassigned_pages"] = [pages[-1]]
    with pytest.raises(IntakeError, match="."):
        validate_plan(Plan.model_validate(raw), [PageRef(**p) for p in pages])


def test_two_independent_letters_keep_all_pages():
    """Два явных решения VLM не объединяются backend по совпадению контрагента."""
    pages = refs(4)
    first, second = plan_for(pages[:2]), plan_for(pages[2:])
    second["documents"][0]["document_index"] = 2
    first["documents"].extend(second["documents"])
    parsed = Plan.model_validate(first)
    validate_plan(parsed, [PageRef(**p) for p in pages])
    assert len(parsed.documents) == 2


def test_windows_cross_fifty_pages_without_loss():
    """Ограничения Docling/VLM задают окна, не предел сервиса или границы документов."""
    pages = list(range(1, 124))
    chunks = windows(pages, 8, 2)
    assert set(p for chunk in chunks for p in chunk) == set(pages)
    assert chunks[0][-2:] == chunks[1][:2]
    assert all(len(chunk) <= 8 for chunk in chunks)
    assert windows(pages, 50)[-1] == list(range(101, 124))


def test_catalog_has_exact_leaves_and_accepts_all():
    """Каждый из 38 утверждённых листов проходит один и тот же серверный валидатор."""
    assert len(LETTER_PATHS) == 38
    assert set(tuple(p) for p in json.loads(taxonomy_json())["paths"]) == LETTER_PATHS
    for path in LETTER_PATHS:
        Classification(
            document_root=path[0],
            direction="incoming" if path[1] == "Входящее письмо" else "outgoing",
            is_letter=True,
            template_path=list(path),
            document_kind=path[2],
            document_subkind=path[3] if len(path) == 4 else None,
        )


def test_catalog_rejects_invented_leaf():
    """VLM не может динамически создать дополнительный вид письма."""
    with pytest.raises(ValidationError):
        Classification(
            document_root="Входящая/Исходящая корреспонденция",
            direction="incoming",
            is_letter=True,
            template_path=["Входящая/Исходящая корреспонденция", "Входящее письмо", "Новый вид"],
            document_kind="Новый вид",
        )


def test_money_is_json_number_without_decimal_calculation_error():
    """Decimal используется до сериализации, исходный контракт требует числовое JSON-значение."""
    amount = Amount(type="penalty", value=Decimal("3309.81"), currency="RUB")
    assert json.loads(amount.model_dump_json())["value"] == 3309.81
    assert amount.value + Decimal("0.19") == Decimal("3310.00")


@pytest.mark.parametrize("value", ['{"x":1,"x":2}', '{"x":NaN}', "[]", "```json\n{}\n```"])
def test_strict_json_rejects_ambiguous_output(value):
    """Повторные ключи, NaN и Markdown не становятся подтверждёнными реквизитами."""
    with pytest.raises(IntakeError):
        strict_json(value)


@pytest.mark.private
def test_user_fixtures_keep_backward_compatible_shape():
    """Проверяет форму пяти JSON отдельно от исторической семантики значений."""
    fixtures = list(Path("examples").glob("*.json"))
    if len(fixtures) != 5:
        pytest.skip("NOT_RUN: приватные примеры недоступны")
    for path in fixtures:
        data = json.loads(path.read_text("utf-8"))
        # Четыре исторических JSON содержат описательные виды вместо узлов закрытого каталога.
        # Для проверки формы адаптируются только тестовые копии; originals не изменяются.
        adapted = copy.deepcopy(data)
        for source in adapted["files"]:
            for document in source["documents"]:
                classification = document["classification"]
                if classification["template_path"]:
                    classification["document_kind"] = classification["template_path"][2]
        parsed = BatchResult.model_validate(adapted)
        assert set(parsed.model_dump()) == {"schema_version", "batch_id", "files"}
