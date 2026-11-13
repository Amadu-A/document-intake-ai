# app/domain/segmentation.py
"""Только техническая проверка VLM-плана; собственных семантических границ здесь нет."""

from collections import Counter
from typing import Literal

from pydantic import Field

from app.domain.errors import IntakeError
from app.domain.identities import identifier_key, name_key
from app.domain.schema import Confidence, StrictModel


class PageRef(StrictModel):
    """Глобальный адрес физической страницы в исходном файле."""

    model_config = {"frozen": True, "extra": "forbid"}
    file_id: str
    page_number: int = Field(ge=1)


class PlannedAttachment(StrictModel):
    """Отношение приложения к основной части, определённое только VLM."""

    kind: str
    pages: list[PageRef]
    relation: str


class PageIdentifier(StrictModel):
    """Номер, прочитанный VLM с конкретной страницы, с явно указанным назначением."""

    kind: Literal["court_case", "enforcement_proceeding", "document", "mailing"]
    value: str = Field(min_length=1, max_length=150)


class PageEvidence(StrictModel):
    """Короткие свидетельства связи; backend проверяет их согласованность, не выбирает границы."""

    page: PageRef
    role: Literal["main", "carrier", "attachment", "continuation", "unknown"]
    identifiers: list[PageIdentifier] = Field(default_factory=list, max_length=20)
    sender: str | None
    recipient: str | None


class Segment(StrictModel):
    """Регистрационная группа с полной последовательностью и внутренними ссылками."""

    document_index: int = Field(ge=1)
    pages: list[PageRef] = Field(min_length=1)
    main_pages: list[PageRef] = Field(min_length=1)
    probable_kind: str | None
    attachments: list[PlannedAttachment]
    confidence: Confidence
    reason: str
    status: Literal["verified", "needs_review"]
    evidence: list[PageEvidence] = Field(default_factory=list, max_length=64)


class Plan(StrictModel):
    """Неподтверждённый ответ первого прохода; проверяется по всему входному manifest."""

    documents: list[Segment]
    unassigned_pages: list[PageRef]
    warnings: list[str]


def validate_evidence(document: Segment) -> None:
    """Отвергает чужие ссылки и конфликт номеров носителя/вложения без перестановки страниц."""
    if len({e.page for e in document.evidence}) != len(document.evidence) or any(
        e.page not in document.pages for e in document.evidence
    ):
        raise IntakeError("invalid_segmentation", "Свидетельства содержат чужую/повторную страницу")
    main = [e for e in document.evidence if e.role in {"main", "continuation"}]
    for carrier in (e for e in document.evidence if e.role == "carrier"):
        strong = False
        for kind in ("court_case", "enforcement_proceeding", "document", "mailing"):
            source = {identifier_key(i.value) for i in carrier.identifiers if i.kind == kind}
            target = {
                identifier_key(i.value) for e in main for i in e.identifiers if i.kind == kind
            }
            if source and target and not source.intersection(target):
                raise IntakeError(
                    "invalid_segmentation",
                    f"Конфликт {kind} между носителем {carrier.page.page_number} и основной "
                    "частью: пересмотри связь VLM, не присоединяй конверт чужому документу",
                )
            strong |= bool(source.intersection(target))
        party_match = any(
            (carrier.sender and name_key(carrier.sender) == name_key(e.sender))
            or (carrier.recipient and name_key(carrier.recipient) == name_key(e.recipient))
            for e in main
        )
        if document.status == "verified" and not (strong and party_match):
            raise IntakeError(
                "invalid_segmentation",
                f"Носитель {carrier.page.model_dump()}: совпадение номера={strong}, "
                f"совпадение стороны={bool(party_match)}. Для verified нужны оба признака. "
                "Сверь печатные номера и названия сторон с основной частью; "
                "VLM должна уточнить принадлежность либо поставить needs_review",
            )


def validate_plan(plan: Plan, pages: list[PageRef], require_verified: bool = True) -> None:
    """Проверяет полноту, уникальность, порядок и принадлежность ссылок без исправлений."""
    errors = []
    positions = {p: i for i, p in enumerate(pages)}
    assigned = [p for doc in plan.documents for p in doc.pages]
    if Counter(assigned) != Counter(pages):
        errors.append("Страницы потеряны, повторены или отсутствуют в исходных файлах")
    if plan.unassigned_pages:
        errors.append("Есть нераспределённые страницы")
    if [d.document_index for d in plan.documents] != list(range(1, len(plan.documents) + 1)):
        errors.append("document_index должен идти от 1 по порядку")
    starts = []
    for doc in plan.documents:
        validate_evidence(doc)
        indices = [positions.get(p, -1) for p in doc.pages]
        starts.append(indices[0])
        if indices != sorted(indices):
            errors.append(f"Нарушен порядок страниц группы {doc.document_index}")
        nested = doc.main_pages + [p for a in doc.attachments for p in a.pages]
        if Counter(nested) != Counter(doc.pages):
            missing = [p.model_dump() for p in (Counter(doc.pages) - Counter(nested)).elements()]
            extra = [p.model_dump() for p in (Counter(nested) - Counter(doc.pages)).elements()]
            errors.append(
                f"main_pages и attachments не покрывают группу {doc.document_index}: "
                f"не включены {missing}; лишние/повторные {extra}. "
                "Каждая pages должна входить ровно в одну из main_pages/attachments.pages"
            )
        if require_verified and doc.status != "verified":
            errors.append(f"VLM не подтвердила границы группы {doc.document_index}")
    if starts != sorted(starts):
        errors.append("Нарушен порядок регистрационных групп")
    if errors:
        raise IntakeError("invalid_segmentation", "; ".join(errors))


def windows(pages: list, size: int, overlap: int = 0) -> list[list]:
    """Делит техническую последовательность на окна, не назначая регистрационные границы."""
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
