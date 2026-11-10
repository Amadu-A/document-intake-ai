# app/domain/compact_plan.py
"""Компактные физические интервалы для ограниченных запросов согласования VLM."""

from typing import Annotated, Literal

from pydantic import Field

from app.domain.errors import IntakeError
from app.domain.schema import Confidence, StrictModel
from app.domain.segmentation import PageRef, Plan, Segment


class Span(StrictModel):
    """Включительный интервал сквозных позиций исходной последовательности, начиная с 1."""

    start: int = Field(ge=1)
    end: int = Field(ge=1)


class CompactAttachment(StrictModel):
    """Приложение, чьи физические страницы записаны интервалами."""

    kind: str = Field(max_length=200)
    spans: list[Span]
    relation: str = Field(max_length=300)


class CompactDocument(StrictModel):
    """Регистрационная группа; VLM определяет все интервалы и внутренние связи."""

    spans: list[Span]
    main_spans: list[Span]
    probable_kind: str | None = Field(max_length=200)
    attachments: list[CompactAttachment]
    confidence: Confidence
    reason: str = Field(min_length=1, max_length=500)
    status: Literal["verified", "needs_review"]


class CompactPlan(StrictModel):
    """Ответ согласования без повторения тысячи объектов PageRef."""

    documents: list[CompactDocument]
    warnings: list[Annotated[str, Field(max_length=500)]] = Field(max_length=30)

    def expand(self, physical: list[PageRef]) -> Plan:
        """Технически разворачивает интервалы; недопустимые позиции не исправляет."""

        def refs(spans: list[Span]) -> list[PageRef]:
            """Разрешает только конечные интервалы внутри общей физической последовательности."""
            result = []
            for span in spans:
                if span.end < span.start or span.end > len(physical):
                    raise IntakeError("invalid_segmentation", "Интервал VLM вне исходных страниц")
                result.extend(physical[span.start - 1 : span.end])
            return result

        return Plan(
            documents=[
                Segment(
                    document_index=i,
                    pages=refs(doc.spans),
                    main_pages=refs(doc.main_spans),
                    probable_kind=doc.probable_kind,
                    attachments=[
                        {"kind": a.kind, "pages": refs(a.spans), "relation": a.relation}
                        for a in doc.attachments
                    ],
                    confidence=doc.confidence,
                    reason=doc.reason,
                    status=doc.status,
                )
                for i, doc in enumerate(self.documents, 1)
            ],
            unassigned_pages=[],
            warnings=self.warnings,
        )


def page_spans(refs: list[PageRef], physical: list[PageRef]) -> list[dict]:
    """Сжимает смежные позиции без принятия решений об их смысловой принадлежности."""
    positions = {p: i for i, p in enumerate(physical, 1)}
    output = []
    for ref in refs:
        position = positions[ref]
        if output and output[-1]["end"] + 1 == position:
            output[-1]["end"] = position
        else:
            output.append({"start": position, "end": position})
    return output


def compact(plan: Plan, physical: list[PageRef]) -> dict:
    """Сжимает готовое решение без изменения состава, границ и принадлежности приложений."""

    def spans(refs: list[PageRef]) -> list[dict]:
        """Объединяет только смежные физические позиции внутри уже принятого решения."""
        return page_spans(refs, physical)

    return {
        "documents": [
            {
                "spans": spans(d.pages),
                "main_spans": spans(d.main_pages),
                "probable_kind": d.probable_kind,
                "attachments": [
                    {"kind": a.kind, "spans": spans(a.pages), "relation": a.relation}
                    for a in d.attachments
                ],
                "confidence": d.confidence,
                "reason": d.reason,
                "status": d.status,
            }
            for d in plan.documents
        ],
        "warnings": plan.warnings,
    }
