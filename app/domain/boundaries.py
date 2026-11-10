# app/domain/boundaries.py
"""Режимы и физические границы: ручной draft не является семантическим решением VLM."""

from collections import Counter
from typing import Literal

from pydantic import Field

from app.domain.errors import IntakeError
from app.domain.schema import StrictModel
from app.domain.segmentation import PageRef, Plan, PlannedAttachment

SegmentationSource = Literal[
    "source_files", "vlm_auto", "vlm_proposed", "vlm_confirmed", "manual", "manual_override"
]


def validate_mode(auto_split: bool, skip_segmentation: bool) -> None:
    """Отвергает противоречивый режим до чтения файлов или изменения задания."""
    if auto_split and skip_segmentation:
        raise IntakeError(
            "invalid_segmentation_mode", "Авторазделение несовместимо с пропуском границ", 422
        )


class ExcludedPage(PageRef):
    """Явное исключение из регистрационных PDF; исходная страница остаётся доступной."""

    reason: str | None = Field(None, max_length=500)


class ManualDocument(StrictModel):
    """Редактируемая регистрационная группа без выдуманной VLM-уверенности или приложений."""

    client_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$")
    pages: list[PageRef] = Field(min_length=1, max_length=10000)


class ManualPlan(StrictModel):
    """Черновик физических назначений, независимый от схемы экспортного JSON 1.0."""

    documents: list[ManualDocument] = Field(default_factory=list, max_length=10000)
    excluded_pages: list[ExcludedPage] = Field(default_factory=list, max_length=10000)


class ApprovedDocument(StrictModel):
    """Неизменяемые адреса одной подтверждённой регистрации."""

    model_config = {"frozen": True, "extra": "forbid"}
    client_id: str
    pages: tuple[PageRef, ...]


class ApprovedPlan(StrictModel):
    """Снимок подтверждения; неизменяемые tuples исключают изменение состава при extraction."""

    model_config = {"frozen": True, "extra": "forbid"}
    documents: tuple[ApprovedDocument, ...]
    excluded_pages: tuple[ExcludedPage, ...]

    @classmethod
    def from_draft(cls, draft: ManualPlan) -> "ApprovedPlan":
        """Снимает отдельную копию ссылок, не сохраняя изменяемые списки черновика."""
        return cls(
            documents=tuple(
                ApprovedDocument(client_id=d.client_id, pages=tuple(d.pages))
                for d in draft.documents
            ),
            excluded_pages=tuple(draft.excluded_pages),
        )


class FixedGroup(StrictModel):
    """Граница manual/source_files для extraction; внутренние приложения ещё не определены VLM."""

    document_index: int
    pages: list[PageRef]
    main_pages: list[PageRef] = Field(default_factory=list)
    attachments: list[PlannedAttachment] = Field(default_factory=list)
    probable_kind: str | None = None
    confidence: None = None
    reason: str = "Фиксированные регистрационные страницы"
    status: Literal["approved"] = "approved"


def validate_assignment(
    plan: ManualPlan | ApprovedPlan,
    physical_pages: list[PageRef],
    max_group_pages: int = 1000,
    *,
    complete: bool = True,
    require_documents: bool = False,
) -> None:
    """Проверяет только адреса, непересечение, исходный порядок и явный учёт исключений."""
    positions = {page: index for index, page in enumerate(physical_pages)}
    assigned = [page for document in plan.documents for page in document.pages]
    excluded = [PageRef(file_id=p.file_id, page_number=p.page_number) for p in plan.excluded_pages]
    if len({d.client_id for d in plan.documents}) != len(plan.documents):
        raise IntakeError("invalid_page_assignment", "Идентификаторы диапазонов повторяются")
    if require_documents and not plan.documents:
        raise IntakeError("invalid_segmentation", "Назначьте хотя бы один регистрационный диапазон")
    if any(not d.pages for d in plan.documents):
        raise IntakeError("invalid_page_assignment", "Диапазон не может быть пустым")
    if any(len(d.pages) > max_group_pages for d in plan.documents):
        raise IntakeError("page_limit", "Превышен лимит страниц одного регистрационного документа")
    if len(set(excluded)) != len(excluded) or any(p not in positions for p in excluded):
        raise IntakeError("invalid_exclusion", "Неизвестная или повторная исключённая страница")
    all_pages = assigned + excluded
    if len(set(all_pages)) != len(all_pages) or any(p not in positions for p in all_pages):
        raise IntakeError("invalid_page_assignment", "Страница неизвестна или назначена повторно")
    if complete and Counter(all_pages) != Counter(physical_pages):
        raise IntakeError(
            "invalid_segmentation", "Каждая страница должна быть назначена или исключена"
        )
    starts = []
    for document in plan.documents:
        indices = [positions[p] for p in document.pages]
        if indices != sorted(indices):
            raise IntakeError("invalid_page_assignment", "Нарушен исходный порядок страниц")
        starts.append(indices[0])
    if starts != sorted(starts):
        raise IntakeError("invalid_page_assignment", "Нарушен порядок регистрационных диапазонов")


def source_file_plan(pages: list[PageRef]) -> ManualPlan:
    """Создаёт техническую начальную разметку один-файл-одна-группа, без семантики VLM."""
    groups: dict[str, list[PageRef]] = {}
    for page in pages:
        groups.setdefault(page.file_id, []).append(page)
    return ManualPlan(
        documents=[
            ManualDocument(client_id=f"source-{index}", pages=group)
            for index, group in enumerate(groups.values(), 1)
        ]
    )


def draft_from_vlm(plan: Plan) -> ManualPlan:
    """Копирует предложенные регистрационные границы; приложения остаются в исходном VLM-плане."""
    return ManualPlan(
        documents=[
            ManualDocument(client_id=f"vlm-{d.document_index}", pages=list(d.pages))
            for d in plan.documents
        ]
    )
