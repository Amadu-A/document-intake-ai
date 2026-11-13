# tests/test_distant_evidence.py
"""Далёкие совпадения расширяют vision-контекст, но никогда не объединяют группы без VLM."""

from uuid import uuid4

import pytest

from app.application.reconciliation import reconcile
from app.domain.jobs import Job, utcnow
from app.domain.segmentation import Plan
from tests.support import plan_for


@pytest.mark.parametrize("same_identifier", [True, False])
async def test_distant_carrier_is_shown_to_vlm_without_forced_merge(container, same_identifier):
    """Через 38 чужих листов доступны исходные свидетельства и картинки, лимит окна сохранён."""
    pages = [{"file_id": "source", "page_number": n} for n in range(1, 41)]
    documents = [plan_for(pages[:2])["documents"][0]]
    documents += [plan_for([p])["documents"][0] for p in pages[2:]]
    for index, document in enumerate(documents, 1):
        document["document_index"] = index

    def evidence(page, role, number):
        """Создаёт исключительно синтетическое наблюдение VLM."""
        return {
            "page": page,
            "role": role,
            "identifiers": [{"kind": "court_case", "value": number}],
            "sender": "Тестовый орган",
            "recipient": "Тестовая организация",
        }

    documents[0]["evidence"] = [
        evidence(pages[0], "carrier", "SYNTHETIC-CASE"),
        evidence(pages[1], "main", "SYNTHETIC-CASE"),
    ]
    documents[-1]["evidence"] = [
        evidence(pages[-1], "main", "SYNTHETIC-CASE" if same_identifier else "OTHER-CASE")
    ]
    plans = [
        Plan(documents=documents[:19], unassigned_pages=[], warnings=[]),
        Plan(documents=documents[19:], unassigned_pages=[], warnings=[]),
    ]
    for plan in plans:
        for index, document in enumerate(plan.documents, 1):
            document.document_index = index

    def answer(context):
        """VLM явно оставляет документы самостоятельными даже при совпадающем номере дела."""
        return {
            "documents": [d for proposal in context["proposals"] for d in proposal["documents"]],
            "warnings": [],
        }

    container.vision.answers.append(answer)
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
        """Не публикует тестовые свидетельства в обычные логи."""

    result = await reconcile(container.analysis, job, pages, plans, emit)
    assert len(result.documents) == 39
    assert [p.page_number for p in result.documents[0].pages] == [1, 2]
    assert [p.page_number for p in result.documents[-1].pages] == [40]
    call = container.vision.calls[0]
    shown = {p["page_number"] for p in call["pages"]}
    assert len(shown) <= container.settings.vlm_window_pages
    assert ({1, 40} <= shown) is same_identifier
    if same_identifier:
        assert call["context"]["allowed_spans"] == [{"start": 1, "end": 40}]
        assert call["context"]["proposals"][0]["documents"][0]["evidence"]
