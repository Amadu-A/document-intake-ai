# app/application/inference_schema.py
"""Сужает схему VLM-запроса по контексту, сохраняя публичный контракт и серверную проверку."""

from copy import deepcopy

from app.domain.document_kinds import categories
from app.domain.document_processing import DocumentClassification
from app.domain.extraction import ExtractionDraft
from app.domain.extractions import DetailedExtraction
from app.domain.schema import Classification, DocumentRoot
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


def _prune_definitions(schema: dict) -> None:
    """Не отправляет VLM неиспользуемые профили; достижимые определения не изменяет."""
    definitions = schema.get("$defs", {})
    used = set()

    def visit(value: object) -> None:
        """Обходит только достижимые локальные ссылки, включая рекурсивные."""
        if isinstance(value, dict):
            ref = value.get("$ref", "")
            if ref.startswith("#/$defs/"):
                key = ref.rsplit("/", 1)[-1]
                if key not in used:
                    used.add(key)
                    visit(definitions[key])
            for key, child in value.items():
                if key != "$defs":
                    visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(schema)
    schema["$defs"] = {key: value for key, value in definitions.items() if key in used}


def extraction_schema(
    organization_hint: str | None,
    selected: Classification | None = None,
    *,
    detailed: bool = False,
    profile: str | None = None,
) -> dict:
    """Ограничивает генерацию фактами контекста и каталогом без исправления ответа модели."""
    schema = (DetailedExtraction if detailed else ExtractionDraft).model_json_schema()
    definitions = schema["$defs"]
    definitions["Alternative"]["properties"]["template_path"]["enum"] = [
        [],
        *[list(path) for path in sorted(LETTER_PATHS)],
    ]
    classification = definitions["Classification"]
    one_c = definitions["OneC"]["properties"]
    # Модель извлекает факты, но не формирует карточку 1С: все поля проектирует backend.
    for field in one_c:
        one_c[field] = {"type": "null"}
    requisites = definitions["DetailedRequisites" if detailed else "Requisites"]
    requisites["properties"]["delivery_channel"] = {"enum": [None, "unknown"]}
    requisites["properties"]["response_required"]["description"] = (
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
    properties["template_path"]["enum"] = (
        [[]] if not organization_hint else [[], *[list(path) for path in sorted(LETTER_PATHS)]]
    )
    properties = other["properties"]
    properties["is_letter"] = {"type": "boolean", "const": False}
    properties["document_root"] = {
        "enum": [None, *(root.value for root in DocumentRoot if root != DocumentRoot.LETTER)]
    }
    if not organization_hint:
        properties["direction"] = {"type": "null"}
    properties["template_path"]["maxItems"] = 0
    properties["alternatives"]["maxItems"] = 0
    definitions["Classification"] = {"title": classification["title"], "anyOf": [letter, other]}
    if selected is not None:
        definitions["Classification"] = {
            "type": "object",
            "properties": {
                key: {"const": value} for key, value in selected.model_dump(mode="json").items()
            },
            "required": list(Classification.model_fields),
            "additionalProperties": False,
        }
    if detailed:
        definitions["Evidence"]["properties"]["confidence"] = {"type": "null"}
        primary = deepcopy(definitions["Evidence"])
        primary["properties"]["source"] = {"const": "image"}
        definitions["PrimaryEvidence"] = primary
        schema["properties"]["field_evidence"]["properties"] = {
            "requisites." + field: {
                "type": "array",
                "minItems": 1,
                "items": {"$ref": "#/$defs/PrimaryEvidence"},
            }
            for field in ("document_number", "document_date")
        }
        if selected is not None and selected.document_kind is not None:
            witness = deepcopy(definitions["Evidence"])
            witness["properties"]["status"] = {"const": "found"}
            witness["properties"]["raw"] = {"type": "string", "minLength": 1, "maxLength": 500}
            definitions["KindEvidence"] = witness
            schema["properties"]["field_evidence"]["properties"].update(
                {
                    "classification.document_kind": {
                        "type": "array",
                        "minItems": 1,
                        "items": {"$ref": "#/$defs/KindEvidence"},
                    }
                }
            )
        schema["properties"]["kind_code"] = {"const": profile}
        details = schema["properties"]["details"]
        if profile is None:
            schema["properties"]["details"] = {"type": "null"}
        else:
            reference = next(v for v in details["anyOf"] if "discriminator" in v)["discriminator"][
                "mapping"
            ][profile]
            schema["properties"]["details"] = {"$ref": reference}
            definitions[reference.split("/")[-1]]["properties"]["profile"] = {"const": profile}
        for definition in definitions.values():
            for name, prop in definition.get("properties", {}).items():
                if name in {"total_amount", "vat_amount", "quantity", "price", "amount", "value"}:
                    if "anyOf" in prop or name == "value":
                        definition["properties"][name] = {
                            "anyOf": [
                                {"type": "string", "pattern": r"^-?\d+(?:\.\d+)?$"},
                                *([{"type": "null"}] if name != "value" else []),
                            ]
                        }
    if detailed:
        _prune_definitions(schema)
    _require_properties(schema)
    return schema


def classification_schema(organization_hint: str | None) -> dict:
    """Использует тот же закрытый каталог в отдельной короткой схеме первого этапа."""
    schema = DocumentClassification.model_json_schema()
    definitions = extraction_schema(organization_hint)["$defs"]
    for key in schema.get("$defs", {}):
        if key in definitions:
            schema["$defs"][key] = deepcopy(definitions[key])
    if not organization_hint:
        schema["properties"]["warnings"]["minItems"] = 1
        schema["properties"]["kind_code"] = {
            "enum": [
                None,
                *[
                    code
                    for code in categories()
                    if code not in {"D01", "D04", "D05", "D06", "D07", "D08"}
                ],
            ]
        }
        for name in ("our_role", "role_evidence", "role_source_page"):
            schema["properties"][name] = {"type": "null"}
    _require_properties(schema)
    return schema
