# app/domain/segmentation.py
"""Физические адреса и технические окна; семантических эвристик здесь нет."""

from pydantic import Field

from app.domain.schema import StrictModel


class PageRef(StrictModel):
    """Неизменяемый адрес страницы исходного файла."""

    model_config = {"frozen": True, "extra": "forbid"}
    file_id: str
    page_number: int = Field(ge=1)


def windows(pages: list, size: int, overlap: int = 0) -> list[list]:
    """Делит техническую последовательность для OCR и извлечения реквизитов."""
    if size <= 0 or not 0 <= overlap < size:
        raise ValueError("Некорректная геометрия окна")
    result = []
    start = 0
    while start < len(pages):
        result.append(pages[start : start + size])
        if start + size >= len(pages):
            break
        start += size - overlap
    return result


def page_spans(refs: list[PageRef], physical: list[PageRef]) -> list[dict]:
    """Сжимает уже назначенные страницы без изменения состава документа."""
    positions = {p: i for i, p in enumerate(physical, 1)}
    output = []
    for ref in refs:
        position = positions[ref]
        if output and output[-1]["end"] + 1 == position:
            output[-1]["end"] = position
        else:
            output.append({"start": position, "end": position})
    return output
