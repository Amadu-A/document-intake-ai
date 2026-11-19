# app/domain/plan_edits.py
"""Общие операции ручного редактора для web и headless клиентов; адреса страниц неизменны."""

from typing import Literal
from uuid import uuid4

from pydantic import ConfigDict, Field

from app.domain.boundaries import ExcludedPage, ManualDocument, ManualPlan
from app.domain.errors import IntakeError
from app.domain.schema import StrictModel
from app.domain.segmentation import PageRef


class PlanEdit(StrictModel):
    """Команда над сохранённой ревизией; owner и серверные секреты не принимаются."""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "expected_revision": 3,
                    "action": "move",
                    "pages": [{"file_id": "source-a", "page_number": 31}],
                    "target": {"file_id": "source-a", "page_number": 32},
                    "side": "after",
                },
                {
                    "expected_revision": 4,
                    "action": "split",
                    "pages": [{"file_id": "source-a", "page_number": 32}],
                },
            ]
        }
    )

    expected_revision: int = Field(ge=0)
    action: Literal["split", "merge", "create", "exclude", "restore", "move", "manual", "reset"]
    pages: list[PageRef] = Field(default_factory=list, max_length=10000)
    target: PageRef | None = None
    side: Literal["before", "after"] = "after"


def edit_plan(plan: ManualPlan, command: PlanEdit, physical: list[PageRef]) -> ManualPlan:
    """Применяет операторскую команду; DB затем проверяет покрытие, revision и lock."""
    result = plan.model_copy(deep=True)
    order = list(result.page_order or physical)
    positions = {page: index for index, page in enumerate(order)}
    chosen = set(command.pages)
    if len(chosen) != len(command.pages) or not chosen.issubset(positions):
        raise IntakeError(
            "invalid_page_assignment", "Выбранные страницы повторяются или не существуют"
        )

    def group_for(page: PageRef | None):
        """Находит точную регистрационную единицу по source/page."""
        return next((doc for doc in result.documents if page in doc.pages), None)

    page = command.target or next(iter(command.pages), None)
    group = group_for(page)
    if command.action in {"split", "merge"}:
        if group is None:
            raise IntakeError("invalid_page_assignment", "Выберите страницу диапазона")
        at = result.documents.index(group)
        if command.action == "split":
            index = group.pages.index(page) + 1
            if index == len(group.pages):
                raise IntakeError(
                    "invalid_page_assignment", "Диапазон уже заканчивается на выбранной странице"
                )
            result.documents.insert(
                at + 1, ManualDocument(client_id=str(uuid4()), pages=group.pages[index:])
            )
            group.pages = group.pages[:index]
        else:
            if at + 1 == len(result.documents):
                raise IntakeError("invalid_page_assignment", "Следующего диапазона нет")
            group.pages.extend(result.documents.pop(at + 1).pages)
    elif command.action in {"create", "restore", "exclude", "move"}:
        if not chosen:
            raise IntakeError("invalid_page_assignment", "Выберите страницы")
        if command.action == "move":
            if group is None or page in chosen:
                raise IntakeError(
                    "invalid_page_assignment", "Назначение должно быть другой страницей диапазона"
                )
            moving = [ref for ref in order if ref in chosen]
            rest = [ref for ref in order if ref not in chosen]
            at = rest.index(page) + (command.side == "after")
            order = rest[:at] + moving + rest[at:]
            result.page_order = order
            positions = {ref: index for index, ref in enumerate(order)}
        for document in result.documents:
            document.pages = [ref for ref in document.pages if ref not in chosen]
        result.excluded_pages = [
            ref
            for ref in result.excluded_pages
            if PageRef(file_id=ref.file_id, page_number=ref.page_number) not in chosen
        ]
        if command.action == "exclude":
            result.excluded_pages.extend(
                ExcludedPage(**ref.model_dump()) for ref in order if ref in chosen
            )
        elif command.action == "move":
            group.pages.extend(ref for ref in order if ref in chosen)
        else:
            result.documents.append(
                ManualDocument(
                    client_id=str(uuid4()), pages=[ref for ref in order if ref in chosen]
                )
            )
    result.documents = [doc for doc in result.documents if doc.pages]
    for document in result.documents:
        document.pages.sort(key=positions.__getitem__)
    result.documents.sort(key=lambda doc: positions[doc.pages[0]])
    return result
