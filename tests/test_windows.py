# tests/test_windows.py
"""Большие пачки: VLM принимает итоговое решение на стыках, backend проверяет только целостность."""

from uuid import uuid4

from app.domain.jobs import Job, utcnow
from app.domain.segmentation import PageRef, Plan, windows
from tests.support import facts, plan_for


async def test_large_group_extraction_is_reconciled_by_vlm(container):
    """Извлечение всей большой группы согласует VLM, не локальный merge реквизитов."""
    pages = [
        {"file_id": "source-a", "page_number": n, "ocr": "Синтетические данные"}
        for n in range(1, 18)
    ]
    refs = [{"file_id": p["file_id"], "page_number": p["page_number"]} for p in pages]
    group = Plan.model_validate(plan_for(refs)).documents[0]
    chunks = windows(pages, 8, 2)
    container.vision.answers.extend([facts() for _ in range(len(chunks) + 1)])
    job = Job(
        id=str(uuid4()),
        owner="synthetic",
        status="running",
        created_at=utcnow(),
        expires_at=None,
        sources=[],
        prompts=container.prompts.base,
    )

    async def emit(kind, data):
        """Не публикует тестовые данные как факты настоящего VLM."""

    result = await container.analysis._extract(job, group, pages, emit)
    assert result.classification.direction is None
    final = container.vision.calls[-1]
    assert not final["pages"]
    assert len(final["context"]["window_evidence"]) == len(chunks)
    assert final["context"]["window_evidence"][-1]["pages"][-1]["page_number"] == 17


async def test_vlm_reconciles_more_than_fifty_pages_globally(container):
    """Девять перекрывающихся окон не превращаются в девять регистрационных документов."""
    pages = [
        {"file_id": "source-a", "page_number": n, "ocr": "Синтетические данные"}
        for n in range(1, 54)
    ]
    chunks = windows(pages, 8, 2)
    for chunk in chunks:
        refs = [{"file_id": p["file_id"], "page_number": p["page_number"]} for p in chunk]
        container.vision.answers.append(plan_for(refs))
    refs = [{"file_id": p["file_id"], "page_number": p["page_number"]} for p in pages]
    container.vision.answers.append(plan_for(refs, main=2))
    job = Job(
        id=str(uuid4()),
        owner="synthetic",
        status="running",
        created_at=utcnow(),
        expires_at=None,
        sources=[],
        prompts=container.prompts.base,
    )

    async def emit(kind, data):
        """Не выдаёт synthetic события за настоящий VLM-анализ."""

    plan = await container.analysis._plan(job, pages, emit)
    assert len(plan.documents) == 1
    assert len(plan.documents[0].pages) == 53
    assert len(plan.documents[0].attachments) == 51
    assert container.vision.calls[-1]["context"]["task"] == "global_reconciliation"
    assert len(container.vision.calls) == len(chunks) + 1
    assert plan.documents[0].pages[-1] == PageRef(file_id="source-a", page_number=53)
