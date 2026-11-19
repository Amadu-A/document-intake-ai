# app/domain/document_kinds.py
"""Единственный версионированный реестр D01–D11; это кандидаты маршрутов, а не GUID 1С."""

import json
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path
from types import MappingProxyType


@lru_cache(maxsize=1)
def catalog_json() -> str:
    """Читает серверный каталог один раз; строка не позволяет вызывающему изменить источник."""
    return Path(__file__).with_name("taxonomy").joinpath("document_kinds.json").read_text("utf-8")


@lru_cache(maxsize=1)
def categories() -> Mapping[str, Mapping[str, str | tuple[str, ...]]]:
    """Возвращает immutable описание категорий без дублирования справочника в Python/JS."""
    return MappingProxyType(
        {
            item["kind_code"]: MappingProxyType(
                {
                    key: tuple(value) if isinstance(value, list) else value
                    for key, value in item.items()
                }
            )
            for item in json.loads(catalog_json())["categories"]
        }
    )


def kind_metadata(code: str | None) -> tuple[str | None, list[str]]:
    """Формирует имя/кандидат пути по проверенному коду; неизвестный код не подбирается."""
    item = categories().get(code)
    return (item["kind_name"], list(item["path_1c"])) if item else (None, [])
