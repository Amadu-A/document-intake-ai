# tests/test_adjacent_pairs.py
"""Новый splitter: строгие ответы, N−1 переходов, checkpoint/retry и отмена."""

import asyncio
from collections import Counter
from datetime import timedelta

import pytest
from pydantic import ValidationError

from app.application.pairwise import Pairwise, usable_ocr
from app.domain.comparisons import PairAnswer, adjacent_comparisons, sequential_ranges
from app.domain.errors import IntakeError
from app.domain.jobs import utcnow
from app.domain.segmentation import PageRef
from app.infrastructure.database import JobRow
from app.worker import process
from tests.support import MemoryUpload, pdf_bytes


def refs(count, file_id="source"):
    """Генерирует только физические адреса, без семантических правил."""
    return [PageRef(file_id=file_id, page_number=i) for i in range(1, count + 1)]


@pytest.mark.parametrize("value", ["true", "false", 1, 0, None, [], {}])
def test_answer_requires_strict_boolean(value):
    """Строки, числа и null не становятся решением о границе."""
    with pytest.raises(ValidationError):
        PairAnswer.model_validate({"solution": value})


def test_answer_rejects_extra_or_missing_properties():
    """Даже корректный boolean с дополнительными полями отклоняется."""
    for value in ({}, {"solution": True, "reason": "synthetic"}):
        with pytest.raises(ValidationError):
            PairAnswer.model_validate(value)


@pytest.mark.parametrize("count", [1, 2, 8, 128, 173])
@pytest.mark.parametrize("solution", [True, False])
def test_n_minus_one_and_exact_coverage(count, solution):
    """Все true объединяют файл; все false дают N честных самостоятельных решений."""
    physical = refs(count)
    pairs = adjacent_comparisons(physical)
    assert len(pairs) == count - 1
    for pair in pairs:
        pair.solution, pair.status = solution, "completed"
    groups, unknown = sequential_ranges(physical, list(reversed(pairs)))
    assert not unknown
    assert groups == ([physical] if solution else [[p] for p in physical])
    assert [p for group in groups for p in group] == physical


def test_unknown_transition_is_not_false_and_files_are_never_compared():
    """Неизвестный переход хранится отдельно; межфайлового запроса нет."""
    physical = refs(3, "first") + refs(2, "second")
    pairs = adjacent_comparisons(physical)
    assert len(pairs) == 3
    pairs[0].solution, pairs[0].status = True, "completed"
    pairs[2].solution, pairs[2].status = True, "completed"
    groups, unknown = sequential_ranges(physical, pairs)
    assert groups == [physical[:2], physical[2:3], physical[3:]]
    assert len(unknown) == 1 and unknown[0].solution is None


@pytest.mark.parametrize("text", ["", " \n", "[]", '[{"kind":"texts","label":"text","text":""}]'])
def test_empty_ocr_metadata_is_not_text(text):
    """Служебные названия и координаты не маскируют пустую страницу."""
    assert not usable_ocr(text)


def test_ocr_table_cells_are_usable_without_metadata_becoming_text():
    """Содержимое таблицы считается OCR, одни координаты — нет."""
    assert usable_ocr('[{"kind":"tables","cells":[[0,1,0,1,"Синтетическая ячейка"]]}]')
    assert not usable_ocr('{"kind":"texts","text":"Служебный объект"}')


@pytest.mark.parametrize("problem", ["duplicate", "foreign_index", "foreign_page", "gap"])
def test_corrupted_transition_mapping_is_rejected(problem):
    """Чужой ответ не привязывается к другой границе, адреса не исправляются автоматически."""
    physical = refs(3)
    pairs = adjacent_comparisons(physical)
    if problem == "duplicate":
        pairs.append(pairs[0])
    elif problem == "foreign_index":
        pairs[0].index = 999
    elif problem == "foreign_page":
        pairs[0].left_page = PageRef(file_id="foreign", page_number=1)
    else:
        physical[1] = PageRef(file_id="source", page_number=9)
    with pytest.raises(IntakeError):
        sequential_ranges(physical, pairs)


async def prepared(container, count, files=1):
    """Готовит DB checkpoint для теста пар без конвертации сотни синтетических страниц."""
    job = await container.intake.upload(
        "integration:onec",
        [MemoryUpload(pdf_bytes(), f"source-{i}.pdf") for i in range(files)],
        None,
        None,
        True,
        auto_split=True,
    )
    job = await container.database.claim(job.id)
    physical = [p for source in job.sources for p in refs(count, source.file_id)]
    pages = [{**p.model_dump(), "ocr": f"SYNTHETIC OCR {p.page_number}"} for p in physical]
    job = await container.database.complete_preview(
        job.id,
        job.fence,
        {
            "pages": [p.model_dump() for p in physical],
            "source_counts": {source.file_id: count for source in job.sources},
        },
    )
    return job, pages


async def run(container, job, pages):
    """Прогоняет настоящий coordinator с транзакционными checkpoint."""

    async def emit(kind, data):
        """Не публикует OCR; сохраняет приватное состояние перехода."""
        await container.database.checkpoint(job.id, job.fence, kind, data)

    return await container.analysis.pairwise.run(job, pages, emit)


async def test_parallel_order_retry_only_failed_pair_and_restart(container):
    """Разный порядок ответов не меняет диапазоны; успешные пары не вызываются повторно."""
    job, pages = await prepared(container, 8)
    calls, completions = Counter(), []
    active = peak = 0
    parallel = asyncio.Event()

    async def request(prompt, context, images, schema, emit):
        """Задерживает ответы и ломает только первую попытку перехода 3–4."""
        nonlocal active, peak
        assert images == [] and set(context) == {"left_page", "right_page"}
        assert all(set(p) == {"file_id", "page_number", "ocr"} for p in context.values())
        assert schema["additionalProperties"] is False
        index = context["left_page"]["page_number"]
        calls[index] += 1
        active += 1
        peak = max(peak, active)
        try:
            if index <= 4 and calls[index] == 1:
                if active == 4:
                    parallel.set()
                await asyncio.wait_for(parallel.wait(), 3)
            await asyncio.sleep((8 - index) / 500)
            completions.append(index)
            return {"solution": "true" if index == 3 and calls[index] == 1 else index not in {3, 5}}
        finally:
            active -= 1

    container.analysis.pairwise.vision.request = request
    plan = await run(container, job, pages)
    assert [[p.page_number for p in d.pages] for d in plan.documents] == [
        [1, 2, 3],
        [4, 5],
        [6, 7, 8],
    ]
    assert 1 < peak <= 4 and completions != sorted(completions)
    assert calls == Counter({1: 1, 2: 1, 3: 2, 4: 1, 5: 1, 6: 1, 7: 1})
    previous = calls.copy()
    # Новый экземпляр и fencing-поколение имитируют настоящий restart worker.
    async with container.database.sessions.begin() as session:
        row = await session.get(JobRow, job.id)
        row.lease_until = utcnow() - timedelta(seconds=1)
    await container.database.recover()
    restarted = await container.database.claim(job.id)
    assert restarted.fence != job.fence
    container.analysis.pairwise = Pairwise(container.database, container.vision, container.settings)
    assert await run(container, restarted, pages) == plan
    assert calls == previous


async def test_missing_ocr_and_invalid_pair_require_confirmation(container):
    """Частичный план сохраняет true и неизвестные переходы без фиктивного false."""
    job, pages = await prepared(container, 4)
    pages[2]["ocr"] = "[]"
    plan = await run(container, job, pages)
    proposed = await container.database.propose(job.id, job.fence, plan)
    assert proposed.status == "awaiting_confirmation"
    assert proposed.segmentation_source == "vlm_partial" and proposed.result is None
    assert [p.solution for p in proposed.comparisons] == [True, None, None]
    assert len(container.vision.calls) == 1
    assert all(p.error_code == "ocr_unusable" for p in proposed.comparisons[1:])
    assert container.exports.comparisons(proposed)["errors"] == 2


async def test_switching_back_to_auto_cannot_silently_approve_unknown_transitions(container):
    """Переключатель auto не заменяет явное подтверждение проблемных стыков."""
    job, pages = await prepared(container, 3)
    pages[1]["ocr"] = "[]"
    plan = await run(container, job, pages)
    proposed = await container.database.propose(job.id, job.fence, plan)
    changed = await container.database.change_mode(
        job.id, job.owner, proposed.plan_revision, True, False
    )
    assert changed.segmentation_source == "vlm_partial"
    count = len(container.vision.calls)
    await container.intake.start(job.id, job.owner)
    await process(container, job.id)
    current = await container.database.get(job.id)
    assert current.status == "awaiting_confirmation"
    assert current.approved_plan is None and current.result is None
    assert len(container.vision.calls) == count


@pytest.mark.parametrize("count", [1, 2, 8, 128, 173])
async def test_coordinator_calls_exactly_each_logical_pair(container, count):
    """Одна страница не вызывает splitter, 173 страницы дают ровно 172 сравнения."""
    job, pages = await prepared(container, count)
    await run(container, job, pages)
    calls = [c for c in container.vision.calls if c["schema"]["title"] == "PairAnswer"]
    assert len(calls) == count - 1
    assert {
        (c["context"]["left_page"]["page_number"], c["context"]["right_page"]["page_number"])
        for c in calls
    } == {(n, n + 1) for n in range(1, count)}
    assert all(not c["pages"] for c in calls)


async def test_coordinator_never_compares_two_source_files(container):
    """Одинаковые OCR и смежные позиции разных источников не дают межфайлового запроса."""
    job, pages = await prepared(container, 2, files=2)
    plan = await run(container, job, pages)
    assert len(plan.documents) == 2 and len(container.vision.calls) == 2
    assert all(
        call["context"]["left_page"]["file_id"] == call["context"]["right_page"]["file_id"]
        for call in container.vision.calls
    )


async def test_pair_uses_complete_ocr_without_changing_extraction_context_limit(container):
    """Граница получает весь OCR страницы, vision-извлечение сохраняет прежнее ограничение."""
    job, pages = await prepared(container, 2)
    pages[0].update(ocr="SHORT", ocr_full="SYNTHETIC " * 3000 + " END_OF_PAGE", ocr_truncated=True)
    await run(container, job, pages)
    assert container.vision.calls[0]["context"]["left_page"]["ocr"].endswith("END_OF_PAGE")
    assert container.vision.calls[0]["pages"] == []


async def test_pair_stage_timeout_preserves_unknowns_and_stops_pending_calls(container):
    """Конечный бюджет не заменяет неизвестные переходы ложными разделениями."""
    job, pages = await prepared(container, 8)
    container.settings.vlm_segmentation_timeout_seconds = 0.03

    async def slow(*args):
        """Ждёт отмены активного HTTP, не выдавая готового ответа."""
        await asyncio.Event().wait()

    container.vision.request = slow
    plan = await asyncio.wait_for(run(container, job, pages), 3)
    proposed = await container.database.propose(job.id, job.fence, plan)
    assert proposed.status == "awaiting_confirmation" and len(proposed.comparisons) == 7
    assert all(
        p.solution is None and p.error_code == "pair_stage_timeout" for p in proposed.comparisons
    )


async def test_timeout_drains_cancelled_workers_before_writing_unknown_results(container):
    """Checkpoint таймаута ждёт закрытия всех sibling-запросов и их транзакций."""
    job, pages = await prepared(container, 3)
    container.settings.vlm_segmentation_timeout_seconds = 0.03
    active = 0
    results = []

    async def request(prompt, context, *args):
        """Второй запрос освобождает ресурсы позже первого при одновременной отмене."""
        nonlocal active
        active += 1
        try:
            await asyncio.Event().wait()
        finally:
            if context["left_page"]["page_number"] == 2:
                await asyncio.sleep(0.1)
            active -= 1

    async def emit(kind, data):
        """Не разрешает запись результатов до завершения cleanup отменённых siblings."""
        if kind == "pair_result":
            assert active == 0
            results.append(data)

    container.vision.request = request
    plan = await asyncio.wait_for(container.analysis.pairwise.run(job, pages, emit), 3)
    assert active == 0 and len(results) == 2 and len(plan.documents) == 3
    assert all(result["error_code"] == "pair_stage_timeout" for result in results)


async def test_restart_does_not_reset_exhausted_attempt_budget(container):
    """Обрыв последней попытки не разрешает бесконечно повторять её при каждом restart."""
    job, pages = await prepared(container, 3)
    await run(container, job, pages)
    saved = await container.database.get(job.id)
    pair = saved.comparisons[0]
    pair.status, pair.solution, pair.attempts = (
        "running",
        None,
        container.settings.pair_max_attempts,
    )
    await container.database.checkpoint(
        job.id, job.fence, "pair_started", pair.model_dump(mode="json")
    )
    count = len(container.vision.calls)
    plan = await run(container, await container.database.get(job.id), pages)
    proposed = await container.database.propose(job.id, job.fence, plan)
    assert len(container.vision.calls) == count
    assert proposed.comparisons[0].error_code == "pair_attempts_exhausted"
    assert proposed.comparisons[1].status == "completed"


@pytest.mark.parametrize("stage", ["ocr", "pairs", "pdf", "extraction"])
async def test_cancel_interrupts_active_stage_and_accepts_next_job(container, stage):
    """Отмена отзывает текущий HTTP/конвертер и не запускает следующие пары/публикацию."""
    entered, closed = asyncio.Event(), asyncio.Event()

    async def wait(*args, **kwargs):
        """Моделирует отменяемый внешний I/O без остановки общего сервиса."""
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            closed.set()

    if stage == "ocr":
        container.analysis.ocr.recognize = wait
    elif stage == "pdf":
        container.storage.pdf = wait
    elif stage == "pairs":
        container.vision.request = wait
    else:
        original = container.vision.request

        async def request(prompt, context, images, schema, emit):
            """Пропускает сравнения и блокирует только извлечение."""
            if schema["title"] == "PairAnswer":
                return await original(prompt, context, images, schema, emit)
            return await wait()

        container.vision.request = request
    job = await container.intake.upload(
        "integration:onec", [MemoryUpload(pdf_bytes(8))], None, None, True, auto_split=True
    )
    task = asyncio.create_task(process(container, job.id))
    await asyncio.wait_for(entered.wait(), 20)
    await container.database.cancel(job.id, job.owner)
    await asyncio.wait_for(task, 2)
    assert closed.is_set()
    cancelled = await container.database.get(job.id)
    assert cancelled.status == "cancelled" and cancelled.result is None
    next_job = await container.intake.upload(
        job.owner, [MemoryUpload(pdf_bytes())], None, None, False
    )
    assert next_job.id != job.id and next_job.status == "uploaded"
