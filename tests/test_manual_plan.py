# tests/test_manual_plan.py
"""Режимы и ручные границы: полнота, исключения, порядок и неизменяемость снимка."""

import pytest
from pydantic import ValidationError

from app.domain.boundaries import (
    ApprovedPlan,
    ExcludedPage,
    ManualDocument,
    ManualPlan,
    source_file_plan,
    validate_assignment,
    validate_mode,
)
from app.domain.errors import IntakeError
from app.domain.jobs import Job, utcnow
from app.domain.segmentation import PageRef


def refs(count=4, file_id="source-a"):
    """Создаёт синтетические физические адреса без пользовательских документов."""
    return [PageRef(file_id=file_id, page_number=n) for n in range(1, count + 1)]


@pytest.mark.parametrize("auto,skip", [(True, False), (False, False), (False, True)])
def test_three_valid_modes_and_review_job_defaults(auto, skip):
    """Три режима допустимы; новый объект без параметров ожидает проверки состава."""
    validate_mode(auto, skip)
    job = Job(
        id="synthetic",
        owner="test",
        status="uploaded",
        created_at=utcnow(),
        expires_at=None,
        sources=[],
        prompts={},
    )
    assert not job.auto_split and not job.skip_segmentation and job.plan_revision == 0


def test_conflicting_flags_fail_before_work():
    """Противоречие имеет стабильный машинный код и HTTP 422."""
    with pytest.raises(IntakeError) as failure:
        validate_mode(True, True)
    assert failure.value.code == "invalid_segmentation_mode" and failure.value.status == 422


def test_cross_source_manual_group_exclusion_and_immutable_approval():
    """Равные физические номера разных источников различимы; excluded остаётся вне PDF."""
    physical = refs(2) + refs(2, "source-b")
    draft = ManualPlan(
        documents=[ManualDocument(client_id="group-a", pages=physical[:3])],
        excluded_pages=[ExcludedPage(**physical[-1].model_dump())],
    )
    validate_assignment(draft, physical)
    approved = ApprovedPlan.from_draft(draft)
    draft.documents[0].pages.pop()
    assert len(approved.documents[0].pages) == 3
    with pytest.raises(ValidationError):
        approved.documents[0].client_id = "changed"
    assert len(approved.excluded_pages) == 1


@pytest.mark.parametrize(
    "failure", ["duplicate", "unknown", "missing", "order", "overlap", "excluded"]
)
def test_invalid_assignments_are_rejected(failure):
    """Неизвестные адреса, дубли, потери и перестановки не исправляются backend автоматически."""
    physical = refs()
    plan = source_file_plan(physical)
    if failure == "duplicate":
        plan.documents[0].pages.append(physical[0])
    elif failure == "unknown":
        plan.documents[0].pages[0] = PageRef(file_id="foreign", page_number=1)
    elif failure == "missing":
        plan.documents[0].pages.pop()
    elif failure == "order":
        plan.documents[0].pages.reverse()
    elif failure == "overlap":
        plan.excluded_pages.append(ExcludedPage(**physical[0].model_dump()))
    else:
        plan.excluded_pages.append(ExcludedPage(file_id="foreign", page_number=1))
    with pytest.raises(IntakeError):
        validate_assignment(plan, physical)


def test_thousand_pages_group_is_allowed_but_next_page_is_rejected():
    """Лимит группы проверяется числом адресов, без выделения гигабайтов изображений."""
    validate_assignment(source_file_plan(refs(1000)), refs(1000))
    with pytest.raises(IntakeError) as failure:
        validate_assignment(source_file_plan(refs(1001)), refs(1001))
    assert failure.value.code == "page_limit"


def test_incomplete_preview_draft_cannot_be_approved():
    """Частичный черновик допустим при подготовке, но финальная полнота обязательна."""
    plan = source_file_plan(refs(2))
    validate_assignment(plan, refs(4), complete=False)
    with pytest.raises(IntakeError):
        validate_assignment(plan, refs(4), complete=True)
    with pytest.raises(IntakeError):
        validate_assignment(
            ManualPlan(excluded_pages=[ExcludedPage(**p.model_dump()) for p in refs()]),
            refs(),
            require_documents=True,
        )


def test_explicit_operator_order_preserves_provenance_and_immutable_snapshot():
    """Ручная перестановка между файлами разрешена только как полная явная последовательность."""
    physical = refs(4) + refs(2, "source-b")
    order = [physical[i] for i in [2, 5, 0, 1, 4, 3]]
    plan = ManualPlan(
        documents=[
            ManualDocument(client_id="one", pages=order[:3]),
            ManualDocument(client_id="two", pages=order[3:5]),
        ],
        excluded_pages=[ExcludedPage(**order[-1].model_dump())],
        page_order=order,
    )
    validate_assignment(plan, physical)
    approved = ApprovedPlan.from_draft(plan)
    plan.page_order.reverse()
    assert approved.page_order == tuple(order)
    validate_assignment(approved, physical)


@pytest.mark.parametrize("problem", ["duplicate", "missing", "unknown", "group_order"])
def test_invalid_operator_order_cannot_hide_or_duplicate_pages(problem):
    """Нельзя потерять лист, заменить адрес или игнорировать порядок внутри диапазона."""
    physical = refs()
    order = list(reversed(physical))
    plan = ManualPlan(documents=[ManualDocument(client_id="one", pages=order)], page_order=order)
    if problem == "duplicate":
        plan.page_order[0] = plan.page_order[1]
    elif problem == "missing":
        plan.page_order.pop()
    elif problem == "unknown":
        plan.page_order[0] = PageRef(file_id="foreign", page_number=1)
    else:
        plan.documents[0].pages.reverse()
    with pytest.raises(IntakeError):
        validate_assignment(plan, physical)
