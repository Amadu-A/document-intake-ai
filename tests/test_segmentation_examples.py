# tests/test_segmentation_examples.py
"""Эталоны владельца остаются в examples; offline-проверки не выдаются за точность shared-VLM."""

from pathlib import Path

import pymupdf
import pytest

from app.domain.boundaries import ManualPlan, validate_assignment
from app.domain.segmentation import PageRef
from tests.segmentation_support import compare_groups, expected_groups, sequential_expected

CASES = [
    ("Полученные_письма_по_почте_рф_06_11_2026", 173, 111),
    ("суд_ув", 8, 5),
]


@pytest.mark.private
@pytest.mark.parametrize("name,pages,groups", CASES)
def test_private_segmentation_examples_cover_every_physical_page(name, pages, groups):
    """Проверяет подтверждённые последовательные диапазоны отдельно от старых полных JSON."""
    path = Path("examples") / (name + ".pdf")
    if not path.exists():
        pytest.skip("NOT_RUN: приватные примеры недоступны")
    with pymupdf.open(path) as document:
        assert len(document) == pages
    expected = sequential_expected(path, pages)
    assert len(expected) == groups
    if pages == 173:
        assert [101, 103] not in expected and [101] in expected and [102] in expected
    else:
        assert expected == [[1], [2, 3, 4], [5, 6], [7], [8]]
    physical = [PageRef(file_id="source", page_number=n) for n in range(1, pages + 1)]
    plan = ManualPlan(
        documents=[
            {"client_id": f"manual-{i}", "pages": [physical[n - 1] for n in group]}
            for i, group in enumerate(expected, 1)
        ]
    )
    validate_assignment(plan, physical)
    assert compare_groups(expected, [[p.page_number for p in d.pages] for d in plan.documents])[
        "match"
    ]


def test_new_ground_truth_cannot_be_derived_from_noncontiguous_old_ranges(tmp_path):
    """Исторический [1,3] не исправляется и не используется как новое модельное ожидание."""
    pdf = tmp_path / "synthetic.pdf"
    old = tmp_path / "synthetic_cut.json"
    old.write_text('{"range":[[1,3],2]}', "utf-8")
    with pytest.raises(ValueError, match="Несмежный"):
        sequential_expected(pdf, 3)
    assert old.read_text("utf-8") == '{"range":[[1,3],2]}'


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
