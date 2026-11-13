# app/domain/identities.py
"""Техническое сравнение напечатанных имён и идентификаторов без fuzzy-поиска и догадок."""

import unicodedata


def name_key(value: str | None) -> str:
    """Убирает регистр/кавычки из полного имени; не подбирает другую организацию по сходству."""
    return "".join(c for c in unicodedata.normalize("NFKC", value or "").casefold() if c.isalnum())


def identifier_key(value: str) -> str:
    """Нормализует пробелы/типографские дефисы, сохраняя буквы, цифры и разделители номера."""
    text = unicodedata.normalize("NFKC", value).casefold()
    return "".join("-" if c in "‐‑‒–—−" else c for c in text if not c.isspace())
