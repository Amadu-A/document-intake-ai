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
    return value.strip(" ._")[:limit].rstrip(" ._")


def document_filename(index: int, facts: Extracted) -> str:
    """Строит doc_N_вид[_номер][_дата].pdf только из собственных реквизитов документа."""
    classification = facts.classification
    if classification.is_letter:
        kind = {"incoming": "Входящее письмо", "outgoing": "Исходящее письмо"}.get(
            classification.direction, "Письмо"
        )
    else:
        kind = classification.document_kind or "Документ"
    parts = [f"doc_{index}", filename_part(kind, 48) or "Документ"]
    if facts.requisites.document_number:
        number = filename_part(facts.requisites.document_number, 32)
        if number:
            parts.append(number)
    if facts.requisites.document_date:
        parts.append(facts.requisites.document_date.isoformat())
    return "_".join(parts) + ".pdf"
