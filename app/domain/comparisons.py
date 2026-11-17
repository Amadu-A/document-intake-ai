# app/domain/comparisons.py
"""Независимые ответы о соседних страницах и их техническая сборка."""

from typing import Literal

from pydantic import Field, StrictBool, model_validator

from app.domain.errors import IntakeError
from app.domain.schema import StrictModel
from app.domain.segmentation import PageRef

ALGORITHM_VERSION = "adjacent-ocr-v1"


class PairAnswer(StrictModel):
    """Единственный разрешённый ответ текстового сравнения."""

    solution: StrictBool


class Comparison(StrictModel):
    """Сохраняемый переход; отсутствие решения никогда не означает false."""

    index: int = Field(ge=0)
    left_page: PageRef
    right_page: PageRef
    fingerprint: str = ""
    solution: StrictBool | None = None
    status: Literal["pending", "running", "completed", "unresolved"] = "pending"
    attempts: int = Field(default=0, ge=0)
    error_code: str | None = None
    duration_seconds: float = Field(default=0, ge=0)

    @model_validator(mode="after")
    def consistent(self) -> "Comparison":
        """Запрещает успешный статус без строгого логического ответа."""
        if (self.status == "completed") != (self.solution is not None):
            raise ValueError("Ответ допустим только у завершённого сравнения")
        return self


def adjacent_comparisons(pages: list[PageRef]) -> list[Comparison]:
    """Перечисляет N−1 переходов каждого файла, исключая межфайловые связи."""
    pairs = []
    for left, right in zip(pages, pages[1:], strict=False):
        if left.file_id != right.file_id:
            continue
        if right.page_number != left.page_number + 1:
            raise IntakeError("invalid_page_assignment", "Нарушена последовательность исходника")
        pairs.append(Comparison(index=len(pairs), left_page=left, right_page=right))
    return pairs


def sequential_ranges(
    pages: list[PageRef], comparisons: list[Comparison]
) -> tuple[list[list[PageRef]], list[Comparison]]:
    """Собирает известные true; неизвестные переходы остаются отдельными проблемами."""
    expected = adjacent_comparisons(pages)
    supplied = {pair.index: pair for pair in comparisons}
    if len(supplied) != len(comparisons) or set(supplied) - set(range(len(expected))):
        raise IntakeError("invalid_comparisons", "Повторное или неизвестное сравнение")
    resolved = {}
    unknown = []
    for target in expected:
        pair = supplied.get(target.index, target)
        if (pair.left_page, pair.right_page) != (target.left_page, target.right_page):
            raise IntakeError("invalid_comparisons", "Сравнение относится к другим страницам")
        resolved[pair.left_page, pair.right_page] = pair
        if pair.status != "completed":
            unknown.append(pair)
    groups: list[list[PageRef]] = []
    for page in pages:
        pair = resolved.get((groups[-1][-1], page)) if groups else None
        if pair and pair.status == "completed" and pair.solution is True:
            groups[-1].append(page)
        else:
            groups.append([page])
    return groups, unknown
