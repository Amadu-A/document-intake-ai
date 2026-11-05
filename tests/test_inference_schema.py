# tests/test_inference_schema.py
"""Схема генерации ограничивает VLM контекстом, не меняя JSON 1.0 или серверные проверки."""

from uuid import uuid4

import pytest

from app.application.inference_schema import extraction_schema
from app.domain.errors import IntakeError
from app.domain.jobs import Job, utcnow
from app.domain.schema import DocumentRoot, Extracted
from app.domain.segmentation import Plan, windows
from app.domain.taxonomy import LETTER_PATHS
from tests.support import facts, plan_for


def test_no_organization_schema_restricts_context_and_keeps_public_schema():
    """Без организации VLM не может вывести направление, 1С-организацию или способ получения."""
    original = Extracted.model_json_schema()
    schema = extraction_schema(None)
    letter, other = schema["$defs"]["Classification"]["anyOf"]
    for branch in (letter, other):
        assert branch["properties"]["direction"] == {"type": "null"}
        assert branch["properties"]["template_path"]["maxItems"] == 0
    one_c = schema["$defs"]["OneC"]["properties"]
    for field in ("organization", "form", "received_via", "template_leaf", "view_of_document"):
        assert one_c[field] == {"type": "null"}
    assert schema["$defs"]["Requisites"]["properties"]["delivery_channel"]["enum"] == [
        None,
        "unknown",
    ]
    assert schema["properties"]["warnings"]["minItems"] == 1
    assert Extracted.model_json_schema() == original


def test_letter_schema_uses_only_catalog_and_nonletter_keeps_preliminary_kind():
    """Каталожные enums относятся к письмам; предварительный вид неписьма остаётся свободным."""
    schema = extraction_schema("Тестовая организация")
    letter, other = schema["$defs"]["Classification"]["anyOf"]
    properties = letter["properties"]
    assert properties["is_letter"]["const"] is True
    assert properties["document_root"]["const"] == DocumentRoot.LETTER.value
    assert set(properties["document_kind"]["enum"]) == {None, *(p[2] for p in LETTER_PATHS)}
    assert set(properties["document_subkind"]["enum"]) == {
        None,
        *(p[3] for p in LETTER_PATHS if len(p) == 4),
    }
    assert "anyOf" in properties["direction"]
    assert "maxItems" not in properties["template_path"]
    assert other["properties"]["is_letter"]["const"] is False
    assert DocumentRoot.LETTER.value not in other["properties"]["document_root"]["enum"]
    assert "enum" not in other["properties"]["document_kind"]
    assert "anyOf" in schema["$defs"]["OneC"]["properties"]["organization"]


def test_inference_schema_requires_all_fields_without_changing_nullable_types():
    """Реквизиты требуют явного ответа null/[]; пропуск не скрывается значением по умолчанию."""
    schema = extraction_schema(None)
    pending, objects = [schema], 0
    while pending:
        value = pending.pop()
        if isinstance(value, dict):
            if "properties" in value:
                assert set(value["required"]) == set(value["properties"])
                objects += 1
            pending.extend(value.values())
        elif isinstance(value, list):
            pending.extend(value)
    assert objects >= 10
    requisites = schema["$defs"]["Requisites"]["properties"]
    assert {"type": "null"} in requisites["document_number"]["anyOf"]
    assert requisites["items"]["type"] == "array"
    assert "contracts" in schema["$defs"]["RelatedEntities"]["required"]
    assert "items" in schema["$defs"]["Requisites"]["required"]


@pytest.mark.parametrize("page_count", [1, 17], ids=["single-window", "windows-and-final"])
async def test_all_extraction_requests_use_context_schema(container, page_count):
    """Одинаковые ограничения передаются первичному, оконным и согласующему VLM-запросам."""
    pages = [{"file_id": "source-a", "page_number": n} for n in range(1, page_count + 1)]
    group = Plan.model_validate(plan_for(pages)).documents[0]
    chunk_count = len(windows(pages, 8, 2))
    request_count = 1 if chunk_count == 1 else chunk_count + 1
    container.vision.answers.extend(facts() for _ in range(request_count))
    job = Job(
        id=str(uuid4()),
        owner="synthetic",
        status="running",
        created_at=utcnow(),
        expires_at=None,
        sources=[],
        prompts=container.prompts.base,
    )

    async def emit(kind, data):
        """Не публикует synthetic ответы за пределами теста."""

    result = await container.analysis._extract(job, group, pages, emit)
    assert result.classification.direction is None
    assert len(container.vision.calls) == request_count
    assert all(call["schema"] == extraction_schema(None) for call in container.vision.calls)


async def test_schema_override_does_not_bypass_python_validation(container):
    """Если upstream игнорирует суженную схему, ложное направление отклоняется без исправления."""
    pages = [{"file_id": "source-a", "page_number": 1}]
    group = Plan.model_validate(plan_for(pages)).documents[0]
    invalid = facts()
    path = next(path for path in sorted(LETTER_PATHS) if path[1] == "Входящее письмо")
    invalid["classification"].update(
        direction="incoming",
        template_path=list(path),
        document_kind=path[2],
        document_subkind=path[3] if len(path) == 4 else None,
    )
    container.settings.model_attempts = 1
    container.vision.answers.append(invalid)
    job = Job(
        id=str(uuid4()),
        owner="synthetic",
        status="running",
        created_at=utcnow(),
        expires_at=None,
        sources=[],
        prompts=container.prompts.base,
    )
    events = []

    async def emit(kind, data):
        """Сохраняет только synthetic retry для проверки отказа вместо исправления фактов."""
        events.append((kind, data))

    with pytest.raises(IntakeError, match="ограниченных повторов"):
        await container.analysis._extract(job, group, pages, emit)
    assert invalid["classification"]["direction"] == "incoming"
    assert any(kind == "model_retry" for kind, _ in events)
