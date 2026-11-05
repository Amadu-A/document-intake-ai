# app/domain/taxonomy.py
"""Единственная загрузка версионированного immutable каталога шаблонов писем."""

import json
from pathlib import Path

_CATALOG = json.loads(
    Path(__file__).with_name("taxonomy").joinpath("letters.json").read_text("utf-8")
)
CATALOG_VERSION: str = _CATALOG["version"]
LETTER_PATHS: frozenset[tuple[str, ...]] = frozenset(tuple(p) for p in _CATALOG["paths"])


def taxonomy_json() -> str:
    """Отдаёт модели тот же каталог, который использует серверный валидатор."""
    return json.dumps(
        {"version": CATALOG_VERSION, "paths": sorted(LETTER_PATHS)}, ensure_ascii=False
    )
