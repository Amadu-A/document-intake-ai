# app/application/inference_schema.py
"""Сужает схему VLM-запроса по контексту, сохраняя публичный контракт и серверную проверку."""

from copy import deepcopy

from app.domain.extraction import ExtractionDraft
from app.domain.schema import DocumentRoot
from app.domain.taxonomy import LETTER_PATHS


def _require_properties(value: object) -> None:
    """Требует явно вернуть все реквизиты; существующие null/пустые массивы остаются допустимыми."""
    if isinstance(value, dict):
        properties = value.get("properties")
        if isinstance(properties, dict):
            value["required"] = list(properties)
        for child in value.values():
            _require_properties(child)
    elif isinstance(value, list):
        for child in value:
            _require_properties(child)


def segmentation_schema(model) -> dict:
    """Требует явного поля evidence в новом ответе VLM; старые сохранённые планы совместимы."""
    schema = model.model_json_schema()
    _require_properties(schema)
    return schema


def extraction_schema(organization_hint: str | None) -> dict:
    """Ограничивает генерацию фактами контекста и каталогом без исправления ответа модели."""
    schema = ExtractionDraft.model_json_schema()
    definitions = schema["$defs"]
    classification = definitions["Classification"]
    one_c = definitions["OneC"]["properties"]
    # Модель извлекает факты, но не формирует карточку 1С: все поля проектирует backend.
    for field in one_c:
        one_c[field] = {"type": "null"}
    definitions["Requisites"]["properties"]["delivery_channel"] = {"enum": [None, "unknown"]}
    definitions["Requisites"]["properties"]["response_required"]["description"] = (
        "Нужен именно письменный ответ: true при явном требовании; false для завершённого "
        "информационного уведомления или решения о прекращении производства без такого "
        "требования; null для повестки/постановления об удержании, когда это не установлено. "
        "Явка и исполнение не равны ответному письму. Это поле requisites, не one_c."
    )
    definitions["Attachment"]["properties"]["page_range"] = {
        "anyOf": [
            {"type": "array", "items": {"type": "integer"}, "maxItems": 0},
            {
                "type": "array",
                "items": {"type": "integer", "minimum": 1},
                "minItems": 2,
                "maxItems": 2,
            },
        ]
    }
    if not organization_hint:
        classification["properties"]["direction"] = {"type": "null"}
        classification["properties"]["template_path"]["maxItems"] = 0
        schema["properties"]["warnings"]["minItems"] = 1
    letter, other = deepcopy(classification), deepcopy(classification)
    properties = letter["properties"]
    properties["is_letter"] = {"type": "boolean", "const": True}
    properties["document_root"] = {"type": "string", "const": DocumentRoot.LETTER.value}
    properties["document_kind"] = {"enum": [None, *sorted({p[2] for p in LETTER_PATHS})]}
    properties["document_subkind"] = {
        "enum": [None, *sorted({p[3] for p in LETTER_PATHS if len(p) == 4})]
    }
    properties = other["properties"]
    properties["is_letter"] = {"type": "boolean", "const": False}
    properties["document_root"] = {
        "enum": [None, *(root.value for root in DocumentRoot if root != DocumentRoot.LETTER)]
    }
    if not organization_hint:
        properties["direction"] = {"type": "null"}
    properties["template_path"]["maxItems"] = 0
    definitions["Classification"] = {"title": classification["title"], "anyOf": [letter, other]}
    _require_properties(schema)
    return schema
