# app/domain/document_names.py
"""Имена готовых PDF из проверенных фактов; классификацию и реквизиты не угадывает."""

import re
import unicodedata

from app.domain.schema import Extracted


def filename_part(value: str, limit: int) -> str:
    """Сохраняет кириллицу, удаляя пути, управляющие символы и Windows-reserved знаки."""
    value = unicodedata.normalize("NFC", value)
    value = "".join(c for c in value if not unicodedata.category(c).startswith("C"))
    value = re.sub(r'[<>:"/\\|?*\s]+', "_", value)
    value = value.strip(" ._")[:limit].rstrip(" ._")
    while len(value.encode("utf-8")) > limit * 2:
        value = value[:-1]
    return value.rstrip(" ._")


def document_basename(facts: Extracted) -> str:
    """Строит вид[_номер][_дата].pdf только из собственных реквизитов документа."""
    classification = facts.classification
    if classification.document_kind:
        kind = classification.document_kind
    elif classification.is_letter:
        kind = {"incoming": "Входящее письмо", "outgoing": "Исходящее письмо"}.get(
            classification.direction, "Письмо"
        )
    else:
        kind = classification.document_kind or "Документ"
    parts = [filename_part(kind, 48) or "Документ"]
    if facts.requisites.document_number:
        number = filename_part(facts.requisites.document_number, 32)
        if number:
            parts.append(number)
    if facts.requisites.document_date:
        parts.append(facts.requisites.document_date.isoformat())
    name = "_".join(parts)
    reserved = {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }
    if name.split(".", 1)[0].upper() in reserved:
        name = "Документ_" + name
    return name + ".pdf"


def document_filename(index: int, facts: Extracted) -> str:
    """Сохраняет уникальный номер пачки в имени отдельно скачиваемого PDF и JSON 1.0."""
    return f"doc_{index}_{document_basename(facts)}"


def document_archive_name(index: int, facts: Extracted) -> str:
    """Помещает PDF без префикса в папку doc_N; прямой слеш обязателен для ZIP на всех ОС."""
    return f"doc_{index}/{document_basename(facts)}"
