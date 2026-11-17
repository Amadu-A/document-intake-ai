# tests/test_segmentation_examples.py
"""Эталоны владельца остаются в examples; offline-проверки не выдаются за точность shared-VLM."""

from pathlib import Path

import pymupdf
import pytest

from app.domain.boundaries import ManualPlan, validate_assignment
from app.domain.segmentation import PageRef
from tests.segmentation_support import (
    ReferenceUnavailable,
    compare_groups,
    expected_groups,
    sequential_expected,
    sequential_reference,
)

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
    try:
        expected = sequential_expected(path, pages)
    except ReferenceUnavailable as exc:
        pytest.skip(f"NOT_RUN: {exc}")
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


@pytest.mark.parametrize("suffix", ["_cut.json", ".json"])
def test_confirmed_historical_ranges_have_separate_contiguous_projection(tmp_path, suffix):
    """Исторический [1,3] сохраняется; ожидание соседних границ учитывает чужой лист 2."""
    pdf = tmp_path / "synthetic.pdf"
    old = tmp_path / ("synthetic" + suffix)
    old.write_text('{"range":[[1,3],2]}', "utf-8")
    groups, provenance = sequential_reference(pdf, 3)
    assert groups == [[1], [2], [3]]
    assert provenance["mode"] == "historical_contiguous_projection"
    assert provenance["source_file"] == old.name
    assert expected_groups(old, 3) == [[1, 3], [2]]
    assert old.read_text("utf-8") == '{"range":[[1,3],2]}'


def test_historical_projection_preserves_runs_and_independent_adjacent_documents(tmp_path):
    """Разрывает только пробелы; разные соседние группы не сливаются."""
    pdf = tmp_path / "synthetic.pdf"
    source = tmp_path / "synthetic.json"
    source.write_text('{"range":[[1,2,8],[3,4],[5,6],7]}', "utf-8")
    assert sequential_expected(pdf, 8) == [[1, 2], [3, 4], [5, 6], [7], [8]]


def test_explicit_sequential_reference_wins_over_historical_source(tmp_path):
    """Новый подтверждённый файл имеет приоритет над прежними группами."""
    pdf = tmp_path / "synthetic.pdf"
    (tmp_path / "synthetic_cut.json").write_text('{"range":[[1,3],2]}', "utf-8")
    explicit = tmp_path / "synthetic.adjacent.expected.json"
    explicit.write_text('{"range":[[1,2],3]}', "utf-8")
    groups, provenance = sequential_reference(pdf, 3)
    assert groups == [[1, 2], [3]] and provenance["mode"] == "sequential"
    assert provenance["source_file"] == explicit.name


@pytest.mark.parametrize("content", ['{"range":[[1,3],2]}', '{"range":[1]}'])
def test_invalid_explicit_reference_is_never_replaced_by_legacy_source(tmp_path, content):
    """Ошибка нового эталона остаётся ошибкой даже при наличии корректного старого."""
    pdf = tmp_path / "synthetic.pdf"
    (tmp_path / "synthetic.adjacent.expected.json").write_text(content, "utf-8")
    (tmp_path / "synthetic_cut.json").write_text('{"range":[1,2,3]}', "utf-8")
    with pytest.raises(ValueError):
        sequential_expected(pdf, 3)


def test_missing_private_reference_is_reported_as_not_run(tmp_path, monkeypatch):
    """Неполный необязательный набор приватных файлов не ломает общий quality gate."""
    from tests.support import pdf_bytes

    monkeypatch.chdir(tmp_path)
    directory = tmp_path / "examples"
    directory.mkdir()
    (directory / "synthetic.pdf").write_bytes(pdf_bytes(3))
    with pytest.raises(pytest.skip.Exception, match="NOT_RUN"):
        test_private_segmentation_examples_cover_every_physical_page("synthetic", 3, 1)
    with pytest.raises(ReferenceUnavailable):
        sequential_expected(directory / "synthetic.pdf", 3)


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
