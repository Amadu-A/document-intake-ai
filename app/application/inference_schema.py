# app/application/inference_schema.py
"""Сужает схему VLM-запроса по контексту, сохраняя публичный контракт и серверную проверку."""

from copy import deepcopy

from app.domain.schema import DocumentRoot, Extracted
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


def extraction_schema(organization_hint: str | None) -> dict:
    """Ограничивает генерацию фактами контекста и каталогом без исправления ответа модели."""
    schema = Extracted.model_json_schema()
    definitions = schema["$defs"]
    classification = definitions["Classification"]
    one_c = definitions["OneC"]["properties"]
    for field in ("form", "received_via"):
        one_c[field] = {"type": "null"}
    definitions["Requisites"]["properties"]["delivery_channel"] = {"enum": [None, "unknown"]}
    if not organization_hint:
        classification["properties"]["direction"] = {"type": "null"}
        classification["properties"]["template_path"]["maxItems"] = 0
        for field in ("organization", "template_leaf", "view_of_document"):
            one_c[field] = {"type": "null"}
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
    properties["direction"] = {"type": "null"}
    properties["template_path"]["maxItems"] = 0
    definitions["Classification"] = {"title": classification["title"], "anyOf": [letter, other]}
    _require_properties(schema)
    return schema
