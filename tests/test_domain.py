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
from app.domain.segmentation import windows
from app.domain.taxonomy import LETTER_PATHS, taxonomy_json
from app.infrastructure.vision import strict_json
from tests.support import facts, fixed_group


def test_one_c_cannot_add_an_unapproved_template_leaf():
    """Справочные поля 1С не обходят закрытый каталог при неизвестном направлении."""
    value = facts()
    value["one_c"]["template_leaf"] = "Произвольный шаблон"
    group = fixed_group([{"file_id": "a", "page_number": 1}])
    with pytest.raises(IntakeError):
        validate_extraction(Extracted.model_validate(value), group, None)


def refs(count=6, file_id="source-a"):
    """Создаёт независимые адреса физических страниц для проверки плана."""
    return [{"file_id": file_id, "page_number": n} for n in range(1, count + 1)]


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


def test_unknown_direction_accepts_only_closed_partial_classification():
    """Неизвестное направление сохраняет null и пустой путь, разрешая виды/пары из каталога."""
    for path in LETTER_PATHS:
        for subkind in (None, path[3] if len(path) == 4 else None):
            classification = Classification(
                document_root=path[0],
                direction=None,
                is_letter=True,
                template_path=[],
                document_kind=path[2],
                document_subkind=subkind,
            )
            assert classification.direction is None and classification.template_path == []
            assert classification.document_kind == path[2]
    classification = Classification.model_validate(facts()["classification"])
    assert classification.document_kind is None and classification.document_subkind is None


@pytest.mark.parametrize(
    ("kind", "subkind"),
    [
        ("Новый вид", None),
        ("Претензия", None),
        ("", None),
        (None, "Новый подвид"),
        (None, "Исходящее письмо Взлет-МСК"),
        ("Переписка с контрагентом", "Новый подвид"),
        ("Переписка с судами", "Исходящее письмо Взлет-МСК"),
        ("Исходящее письмо Взлет-МСК", None),
    ],
)
def test_unknown_direction_keeps_factual_kind_outside_template_catalog(kind, subkind):
    """Фактический вид/подвид отделён от пути каталога; пустая строка не заменяет null."""
    value = facts()["classification"]
    value.update(document_kind=kind, document_subkind=subkind)
    if kind == "":
        with pytest.raises(ValidationError):
            Classification.model_validate(value)
    else:
        parsed = Classification.model_validate(value)
        assert parsed.document_kind == kind and parsed.document_subkind == subkind


def test_nonletter_still_accepts_preliminary_kind_outside_letter_catalog():
    """Каталог писем не ограничивает предварительные виды/подвиды договорных документов."""
    classification = Classification(
        document_root="Договоры",
        direction=None,
        is_letter=False,
        template_path=[],
        document_kind="Дополнительное соглашение",
        document_subkind="Предварительный подвид договора",
    )
    assert classification.document_kind == "Дополнительное соглашение"


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
    matrix = json.loads(Path("tests/fixtures/regression_matrix.json").read_text("utf-8"))
    fixtures = [Path("examples", case["file"]).with_suffix(".json") for case in matrix["cases"]]
    if not all(path.is_file() for path in fixtures):
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
                elif classification["is_letter"] and classification["direction"] is None:
                    # Legacy описательный вид не входит в каталог; здесь проверяется только форма.
                    classification["document_kind"] = None
                    classification["document_subkind"] = None
        parsed = BatchResult.model_validate(adapted)
        assert set(parsed.model_dump()) == {"schema_version", "batch_id", "files"}
