# tests/test_scaling.py
"""Синтетические пределы 400 МиБ/1000 страниц и ограниченное дерево VLM-запросов."""

from uuid import uuid4

import pytest

from app.domain.boundaries import FixedGroup
from app.domain.errors import IntakeError
from app.domain.jobs import Job, utcnow
from app.domain.limits import UploadBudget
from app.domain.segmentation import PageRef
from app.infrastructure.conversion import convert
from tests.support import facts, pdf_bytes


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
        return container.vision.readability(container.vision.detailed(facts(), context))

    async def emit(kind, data):
        """Принимает только тестовые события."""

    container.analysis.vision.request = request
    result = await container.analysis._extract(job, group, pages, emit)
    assert result.requisites.title == "Тестовое письмо"
