# tests/test_interleaved_segmentation.py
"""Несмежные группы через 1–5 чужих листов остаются решениями VLM на стыках окон."""

from uuid import uuid4

import pytest

from app.domain.compact_plan import compact
from app.domain.jobs import Job, utcnow
from app.domain.segmentation import PageRef, Plan, validate_plan
from tests.support import plan_for


@pytest.mark.parametrize("gap", range(1, 6))
async def test_vlm_can_reunite_interleaved_pages_across_window_seam(container, gap):
    """Стык получает обе стороны продолжения и не поглощает чужие промежуточные листы."""
    pages = [
        {"file_id": "source", "page_number": n, "ocr": f"Текст страницы {n}"} for n in range(1, 121)
    ]
    physical = [PageRef(file_id="source", page_number=n) for n in range(1, 121)]
    pair = [6, 7 + gap]
    job = Job(
        id=str(uuid4()),
        owner="test",
        status="running",
        created_at=utcnow(),
        expires_at=None,
        sources=[],
        prompts=container.prompts.base,
    )
    seen = []

    async def request(prompt, context, images, schema, emit):
        """Double решает объединение только при совместном представлении двух листов."""
        if schema["title"] == "Plan":
            numbers = {p["page_number"] for p in images}
        else:
            numbers = {
                n
                for span in context["allowed_spans"]
                for n in range(span["start"], span["end"] + 1)
            }
            if set(pair).issubset(numbers):
                evidence = {p["page_number"] for p in context["page_evidence"]}
                proposed = [d for proposal in context["proposals"] for d in proposal["documents"]]
                already_paired = any(
                    set(pair).issubset(
                        {n for span in d["spans"] for n in range(span["start"], span["end"] + 1)}
                    )
                    for d in proposed
                )
                assert set(pair).issubset(evidence) or already_paired
                seen.append(context)
        groups = [[n] for n in sorted(numbers)]
        if set(pair).issubset(numbers):
            groups = [
                pair if group == [pair[0]] else group for group in groups if group != [pair[1]]
            ]
        documents = []
        for index, group in enumerate(groups, 1):
            doc = plan_for([physical[n - 1].model_dump() for n in group])["documents"][0]
            documents.append({**doc, "document_index": index})
        plan = Plan.model_validate({"documents": documents, "unassigned_pages": [], "warnings": []})
        return (
            plan.model_dump(mode="json") if schema["title"] == "Plan" else compact(plan, physical)
        )

    async def emit(kind, data):
        """Принимает только синтетические события."""

    container.vision.request = request
    result = await container.analysis._plan(job, pages, emit)
    validate_plan(result, physical)
    actual = [[p.page_number for p in d.pages] for d in result.documents]
    assert pair in actual and all([n] in actual for n in range(pair[0] + 1, pair[1]))
    assert len(actual) == 119 and seen
