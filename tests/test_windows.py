# tests/test_windows.py
"""Большие пачки: VLM принимает итоговое решение на стыках, backend проверяет только целостность."""

from uuid import uuid4

import pytest

from app.application.reconciliation import reconcile
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


@pytest.mark.parametrize("failure", ["out_of_scope", "duplicate"])
async def test_intermediate_vlm_cannot_expand_to_total_printed_on_page(container, failure):
    """Регрессия live-200: промежуточный уровень 1–194 не разрешает VLM добавить 195–200."""
    pages = [
        {"file_id": "synthetic", "page_number": n, "ocr": f"Page {n} of 200"} for n in range(1, 201)
    ]
    refs = [{"file_id": p["file_id"], "page_number": p["page_number"]} for p in pages]
    plans = [
        Plan.model_validate(plan_for(refs[:98])),
        Plan.model_validate(plan_for(refs[96:194])),
        Plan.model_validate(plan_for(refs[192:])),
    ]
    job = Job(
        id=str(uuid4()),
        owner="synthetic",
        status="running",
        created_at=utcnow(),
        expires_at=None,
        sources=[],
        prompts=container.prompts.base,
    )

    def answer(context):
        """Возвращает решение ровно по явно разрешённым интервалам запроса."""
        return {
            "documents": [
                {
                    "spans": context["allowed_spans"],
                    "main_spans": context["allowed_spans"],
                    "probable_kind": "Отчёт",
                    "attachments": [],
                    "confidence": None,
                    "reason": "Синтетический ответ",
                    "status": "verified",
                }
            ],
            "warnings": [],
        }

    first = answer(
        {"allowed_spans": [{"start": 1, "end": 200 if failure == "out_of_scope" else 194}]}
    )
    if failure == "duplicate":
        first["documents"].append(first["documents"][0])
    container.vision.answers.extend([first, answer, answer])

    async def emit(kind, data):
        """Принимает synthetic retry без публикации документов."""

    result = await reconcile(container.analysis, job, pages, plans, emit)
    assert len(result.documents) == 1 and len(result.documents[0].pages) == 200
    assert len(container.vision.calls) == 3
    retry = container.vision.calls[1]["context"]
    assert retry["allowed_spans"] == [{"start": 1, "end": 194}]
    assert "194" in retry["validation_errors"][0]["message"]
    if failure == "duplicate":
        assert (
            "повторены в разных группах: [{'start': 1, 'end': 194}]"
            in retry["validation_errors"][0]["message"]
        )
