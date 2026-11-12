# tests/segmentation_support.py
"""Частные эталоны диапазонов и REST-сравнение авторазделения без извлечения реквизитов."""

import ast
import asyncio
import json
from collections import Counter
from pathlib import Path
from time import monotonic

import httpx


def expected_groups(path: Path, page_count: int) -> list[list[int]]:
    """Читает JSON или literal-нотацию с кортежами, проверяя полное покрытие страниц."""
    if path.stat().st_size > 1_048_576:
        raise ValueError("Слишком большой эталон диапазонов")
    text = path.read_text("utf-8-sig")
    try:
        data = json.loads(text)
    except ValueError:
        data = ast.literal_eval(text)
    if not isinstance(data, dict) or set(data) != {"range"} or not isinstance(data["range"], list):
        raise ValueError("Ожидается объект range со списком групп")
    groups = [list(item) if isinstance(item, (tuple, list)) else [item] for item in data["range"]]
    if not groups or any(
        not group or any(type(page) is not int for page in group) or group != sorted(group)
        for group in groups
    ):
        raise ValueError("Неверные номера или порядок страниц эталона")
    if Counter(page for group in groups for page in group) != Counter(range(1, page_count + 1)):
        raise ValueError("Эталон теряет, повторяет или добавляет страницы")
    if [group[0] for group in groups] != sorted(group[0] for group in groups):
        raise ValueError("Нарушен порядок групп эталона")
    return groups


def compare_groups(expected: list[list[int]], actual: list[list[int]]) -> dict:
    """Сравнивает точный состав регистраций, включая несмежные листы, без подгонки эталонов."""
    wanted, received = Counter(map(tuple, expected)), Counter(map(tuple, actual))
    expected_pages = Counter(page for group in expected for page in group)
    actual_pages = Counter(page for group in actual for page in group)
    return {
        "match": wanted == received and expected == actual,
        "coverage_match": expected_pages == actual_pages,
        "expected_count": len(expected),
        "actual_count": len(actual),
        "exact_groups": sum((wanted & received).values()),
        "missing_groups": [list(group) for group in (wanted - received).elements()],
        "unexpected_groups": [list(group) for group in (received - wanted).elements()],
    }


async def run_segmentation_case(
    client: httpx.AsyncClient, pdf: Path, *, headers: dict, timeout_seconds: int
) -> dict:
    """Сравнивает review-job через API, сохраняя только частную матрицу номеров страниц."""
    import pymupdf

    with pymupdf.open(pdf) as document:
        count = len(document)
    expected = expected_groups(pdf.with_suffix(".json"), count)
    began = monotonic()
    with pdf.open("rb") as stream:
        response = await client.post(
            "/api/v1/jobs",
            headers=headers,
            files={"files": (pdf.name, stream, "application/pdf")},
            data={"auto_split": "false", "skip_segmentation": "false"},
        )
    response.raise_for_status()
    identifier = response.json()["job_id"]
    while monotonic() - began < timeout_seconds:
        response = await client.get(
            f"/api/v1/jobs/{identifier}/snapshot?include_pages=false", headers=headers
        )
        response.raise_for_status()
        snapshot = response.json()
        if snapshot["status"] in {"awaiting_confirmation", "failed", "cancelled"}:
            response = await client.get(f"/api/v1/jobs/{identifier}/segmentation", headers=headers)
            response.raise_for_status()
            segmentation = response.json()
            actual = [
                [p["page_number"] for p in group["pages"]]
                for group in (segmentation["draft_plan"] or {}).get("documents", [])
            ]
            return {
                "file": pdf.name,
                "pages": count,
                "status": snapshot["status"],
                "segmentation_source": segmentation["segmentation_source"],
                "fallback_code": (segmentation.get("fallback") or {}).get("code"),
                "error_code": (snapshot.get("error") or {}).get("code"),
                "seconds": round(monotonic() - began, 2),
                "expected": expected,
                "actual": actual,
                **compare_groups(expected, actual),
            }
        await asyncio.sleep(0.25)
    raise TimeoutError("Превышено время проверки авторазделения")
