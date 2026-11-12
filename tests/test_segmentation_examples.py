# tests/test_segmentation_examples.py
"""Эталоны владельца остаются в examples; offline-проверки не выдаются за точность shared-VLM."""

from pathlib import Path

import pymupdf
import pytest

from app.domain.boundaries import single_page_plan, validate_assignment
from app.domain.compact_plan import CompactPlan, compact
from app.domain.segmentation import PageRef, Plan, validate_plan
from tests.segmentation_support import compare_groups, expected_groups
from tests.support import plan_for

CASES = [
    ("Полученные_письма_по_почте_рф_06_11_2026", 173, 110),
    ("суд_ув", 8, 4),
]


@pytest.mark.private
@pytest.mark.parametrize("name,pages,groups", CASES)
def test_private_segmentation_examples_cover_every_physical_page(name, pages, groups):
    """Проверяет размеры новых PDF и точное сохранение эталона, включая группу 101,103."""
    path = Path("examples") / (name + ".pdf")
    if not path.exists():
        pytest.skip("NOT_RUN: приватные примеры недоступны")
    with pymupdf.open(path) as document:
        assert len(document) == pages
    expected = expected_groups(path.with_suffix(".json"), pages)
    assert len(expected) == groups
    if pages == 173:
        assert [101, 103] in expected and [102] in expected
    physical = [PageRef(file_id="source", page_number=n) for n in range(1, pages + 1)]
    documents = []
    for index, group in enumerate(expected, 1):
        document = plan_for([physical[n - 1].model_dump() for n in group])["documents"][0]
        documents.append({**document, "document_index": index})
    plan = Plan.model_validate({"documents": documents, "unassigned_pages": [], "warnings": []})
    validate_plan(plan, physical)
    expanded = CompactPlan.model_validate(compact(plan, physical)).expand(physical)
    assert expanded == plan
    validate_assignment(single_page_plan(physical), physical)
    assert compare_groups(expected, [[p.page_number for p in d.pages] for d in plan.documents])[
        "match"
    ]


@pytest.mark.parametrize(
    "content",
    [
        '{"range": [(1, 3), 2,],}',
        '{"range": [[1, 3], 2]}',
    ],
)
def test_literal_and_json_ranges_preserve_noncontiguous_group(tmp_path, content):
    """Круглые скобки описывают явные страницы, а не интервал с включением чужого листа."""
    path = tmp_path / "ranges.json"
    path.write_text(content, "utf-8")
    assert expected_groups(path, 3) == [[1, 3], [2]]
    result = compare_groups([[1, 3], [2]], [[1], [2], [3]])
    assert result["coverage_match"] and not result["match"]


@pytest.mark.parametrize(
    "content",
    [
        '{"range": [(1, 2), 2]}',
        '{"range": [1]}',
        '{"range": [True, 2, 3]}',
        '{"range": [(3, 1), 2]}',
        '__import__("os").getcwd()',
    ],
)
def test_invalid_or_executable_golden_is_rejected(tmp_path, content):
    """Некорректные эталоны явно отклоняются, произвольный код никогда не выполняется."""
    path = tmp_path / "ranges.json"
    path.write_text(content, "utf-8")
    with pytest.raises((ValueError, SyntaxError)):
        expected_groups(path, 3)
