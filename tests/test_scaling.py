# tests/test_scaling.py
"""Синтетические пределы 400 МиБ/1000 страниц и ограниченное дерево VLM-запросов."""

import json
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.domain.boundaries import FixedGroup
from app.domain.compact_plan import CompactPlan
from app.domain.errors import IntakeError
from app.domain.jobs import Job, utcnow
from app.domain.limits import UploadBudget
from app.domain.segmentation import PageRef, validate_plan
from app.infrastructure.conversion import convert
from tests.support import facts, pdf_bytes, plan_for


def test_file_and_batch_bytes_are_checked_independently_without_large_allocation():
    """Точный предел 400 МиБ допустим, лишний байт и 1024 МиБ общего объёма отклоняются."""
    mib = 1024 * 1024
    budget = UploadBudget(400 * mib, 1024 * mib)
    budget.next_file()
    for _ in range(400):
        budget.consume(mib)
    with pytest.raises(IntakeError) as failure:
        budget.consume(1)
    assert failure.value.code == "upload_file_limit"
    budget.next_file()
    budget.consume(400 * mib)
    budget.next_file()
    budget.consume(224 * mib)
    with pytest.raises(IntakeError) as failure:
        budget.consume(1)
    assert failure.value.code == "upload_batch_limit"


def test_compact_plan_rejects_unbounded_repetitive_reason():
    """Live-регрессия: длинное зацикленное обоснование не поглощает весь ответ на стыке."""
    document = {
        "spans": [{"start": 1, "end": 2}],
        "main_spans": [{"start": 1, "end": 2}],
        "probable_kind": None,
        "attachments": [],
        "confidence": None,
        "reason": "я" * 501,
        "status": "verified",
    }
    with pytest.raises(ValidationError):
        CompactPlan.model_validate({"documents": [document], "warnings": []})


def test_compact_expansion_preserves_model_uncertainty():
    """Техническое разворачивание интервалов не присваивает VLM ложный verified."""
    document = {
        "spans": [{"start": 1, "end": 2}],
        "main_spans": [{"start": 1, "end": 2}],
        "probable_kind": None,
        "attachments": [],
        "confidence": None,
        "reason": "Граница не установлена",
        "status": "needs_review",
    }
    refs = [PageRef(file_id="synthetic", page_number=n) for n in (1, 2)]
    plan = CompactPlan.model_validate({"documents": [document], "warnings": []}).expand(refs)
    assert plan.documents[0].status == "needs_review"
    with pytest.raises(IntakeError):
        validate_plan(plan, refs)


def test_real_thousand_page_conversion_progresses_before_last_thumbnail(tmp_path):
    """Создаёт реальные 1000 PNG/thumbnail; первое событие доступно до конца конвертации."""
    source = tmp_path / "thousand.pdf"
    source.write_bytes(pdf_bytes(1000))
    target = tmp_path / "renders"
    events = []

    def progress(kind, data):
        """Проверяет готовый первый PNG в момент события, пока последний ещё не создан."""
        if kind == "page_ready" and data["page_number"] == 1:
            assert (target / "page-1.png").exists()
            assert not (target / "page-1000.png").exists()
        events.append((kind, data.get("page_number")))

    result = convert(
        source, target, {"max_pages": 1000, "max_pixels": 40_000_000, "render_side": 400}, progress
    )
    assert len(result["pages"]) == 1000
    assert len(list(target.glob("thumb-*.png"))) == 1000
    assert events[0][0] == "source_discovered" and events[-1] == ("page_ready", 1000)
    with pytest.raises(IntakeError):
        convert(
            source,
            tmp_path / "rejected",
            {"max_pages": 999, "max_pixels": 40_000_000, "render_side": 400},
        )


@pytest.mark.parametrize("count,independent", [(200, True), (1000, False)])
async def test_large_segmentation_uses_bounded_hierarchy(container, count, independent):
    """200 отдельных писем и длинное продолжение не требуют гигантского итогового ответа."""
    pages = [
        {"file_id": "synthetic", "page_number": i, "ocr": "Синтетический текст"}
        for i in range(1, count + 1)
    ]
    physical = [PageRef(file_id=p["file_id"], page_number=p["page_number"]) for p in pages]
    job = Job(
        id=str(uuid4()),
        owner="synthetic",
        status="running",
        created_at=utcnow(),
        expires_at=None,
        sources=[],
        prompts=container.prompts.base,
    )

    async def request(prompt, context, images, schema, emit):
        """Детерминированный double проверяет геометрию, не подтверждает точность реальной VLM."""
        assert len(images) <= container.settings.vlm_window_pages
        assert len(json.dumps(context, ensure_ascii=False)) < 16000
        container.vision.calls.append({"context": context, "schema": schema})
        if schema["title"] == "Plan":
            refs = [{"file_id": p["file_id"], "page_number": p["page_number"]} for p in images]
            if not independent:
                return plan_for(refs)
            documents = [plan_for([p])["documents"][0] for p in refs]
            return {
                "documents": [{**d, "document_index": i} for i, d in enumerate(documents, 1)],
                "unassigned_pages": [],
                "warnings": [],
            }
        proposals = [doc for plan in context["proposals"] for doc in plan["documents"]]
        if independent:
            unique = {doc["spans"][0]["start"]: doc for doc in proposals}
            documents = [unique[i] for i in sorted(unique)]
        else:
            begin = min(s["start"] for d in proposals for s in d["spans"])
            end = max(s["end"] for d in proposals for s in d["spans"])
            documents = [
                {
                    **proposals[0],
                    "spans": [{"start": begin, "end": end}],
                    "main_spans": [{"start": begin, "end": end}],
                }
            ]
        return {"documents": documents, "warnings": []}

    async def emit(kind, data):
        """Синтетический поток не публикуется в production-журнал."""

    container.analysis.vision.request = request
    result = await container.analysis._plan(job, pages, emit)
    validate_plan(result, physical)
    assert len(result.documents) == (count if independent else 1)
    assert result.documents[-1].pages[-1].page_number == count
    assert any(call["schema"]["title"] == "CompactPlan" for call in container.vision.calls)


async def test_large_extraction_reconciles_at_most_four_partial_answers(container):
    """Извлечение 200 страниц тоже согласуется деревом; схема JSON 1.0 остаётся прежней."""
    pages = [
        {"file_id": "synthetic", "page_number": i, "ocr": "Синтетические данные"}
        for i in range(1, 201)
    ]
    group = FixedGroup(
        document_index=1,
        pages=[PageRef(file_id=p["file_id"], page_number=p["page_number"]) for p in pages],
    )
    job = Job(
        id=str(uuid4()),
        owner="synthetic",
        status="running",
        created_at=utcnow(),
        expires_at=None,
        sources=[],
        prompts=container.prompts.base,
    )

    async def request(prompt, context, images, schema, emit):
        """Возвращает одинаковые синтетические факты, проверяя ограниченный fan-in запроса."""
        assert len(context.get("window_evidence", [])) <= 4
        assert len(images) <= 8
        assert "spans" in context["group"] and "pages" not in context["group"]
        return container.vision.readability(facts())

    async def emit(kind, data):
        """Принимает только тестовые события."""

    container.analysis.vision.request = request
    result = await container.analysis._extract(job, group, pages, emit)
    assert result.requisites.title == "Тестовое письмо"
