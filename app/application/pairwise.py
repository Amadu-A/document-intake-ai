# app/application/pairwise.py
"""Ограниченный, возобновляемый анализ соседних OCR без изображений."""

import asyncio
import hashlib
import json
from time import monotonic

from pydantic import ValidationError

from app.application.ports import AnalysisSettings, Emit, JobRepository, Vision
from app.domain.boundaries import ManualDocument, ManualPlan
from app.domain.comparisons import (
    ALGORITHM_VERSION,
    PairAnswer,
    adjacent_comparisons,
    sequential_ranges,
)
from app.domain.errors import IntakeError, LeaseLost
from app.domain.jobs import Job


def usable_ocr(text: str) -> bool:
    """Проверяет наличие текста, не принимая служебные ключи Docling за OCR."""
    if not isinstance(text, str) or not text.strip():
        return False
    try:
        blocks = json.loads(text)
    except ValueError:
        return any(char.isalnum() for char in text)
    if not isinstance(blocks, list):
        return False
    for block in blocks:
        if not isinstance(block, dict):
            continue
        values = [block.get("text", ""), block.get("original_text", "")]
        values.extend(
            cell[-1] for cell in block.get("cells", []) if isinstance(cell, list) and cell
        )
        if any(isinstance(v, str) and any(c.isalnum() for c in v) for v in values):
            return True
    return False


class Pairwise:
    """Хранит каждую попытку до HTTP и каждый ответ до начала следующей пары."""

    def __init__(self, repository: JobRepository, vision: Vision, settings: AnalysisSettings):
        """Общий семафор ограничивает пары всех заданий одного worker-процесса."""
        self.repository, self.vision, self.settings = repository, vision, settings
        self.slots = asyncio.Semaphore(settings.pair_concurrency)

    async def run(self, job: Job, pages: list[dict], emit: Emit) -> ManualPlan:
        """Возобновляет пары; при таймауте дожидается cleanup перед записью результатов."""
        pairs = adjacent_comparisons(job.page_refs())
        lookup = {
            (p["file_id"], p["page_number"]): p.get("ocr_full", p.get("ocr", "")) for p in pages
        }
        saved = {pair.index: pair for pair in job.comparisons}
        pending = asyncio.Queue()
        for pair in pairs:
            context = {
                side: {**ref.model_dump(), "ocr": lookup.get((ref.file_id, ref.page_number), "")}
                for side, ref in (("left_page", pair.left_page), ("right_page", pair.right_page))
            }
            pair.fingerprint = hashlib.sha256(
                json.dumps(
                    [ALGORITHM_VERSION, job.prompts["segmentation"], context],
                    ensure_ascii=False,
                    sort_keys=True,
                ).encode()
            ).hexdigest()
            previous = saved.get(pair.index)
            if previous and previous.fingerprint == pair.fingerprint:
                pair = previous.model_copy(deep=True)
                pairs[pair.index] = pair
            if pair.status != "completed":
                pending.put_nowait((pair, context))

        async def compare(pair, context) -> None:
            """Ошибка одной пары не отменяет независимые подтверждённые ответы."""
            if not all(usable_ocr(context[side]["ocr"]) for side in context):
                pair.status, pair.error_code = "unresolved", "ocr_unusable"
                await emit("pair_result", pair.model_dump(mode="json"))
                return
            while pair.attempts < self.settings.pair_max_attempts:
                async with self.slots:
                    pair.attempts += 1
                    pair.status, pair.solution = "running", None
                    await emit("pair_started", pair.model_dump(mode="json"))
                    started = monotonic()
                    try:
                        # Намеренно пустой images: изображения запрещены на этой стадии.
                        raw = await self.vision.request(
                            job.prompts["segmentation"],
                            context,
                            [],
                            PairAnswer.model_json_schema(),
                            lambda *_: asyncio.sleep(0),
                        )
                        answer = PairAnswer.model_validate(raw)
                        pair.solution, pair.status, pair.error_code = (
                            answer.solution,
                            "completed",
                            None,
                        )
                    except LeaseLost:
                        raise
                    except (IntakeError, ValidationError, TimeoutError) as exc:
                        pair.status = "unresolved"
                        pair.error_code = (
                            exc.code if isinstance(exc, IntakeError) else "pair_invalid_answer"
                        )
                    finally:
                        pair.duration_seconds += monotonic() - started
                    await emit("pair_result", pair.model_dump(mode="json"))
                if pair.status == "completed":
                    return
            if pair.status != "unresolved":
                pair.status = "unresolved"
                pair.error_code = pair.error_code or "pair_attempts_exhausted"
                await emit("pair_result", pair.model_dump(mode="json"))

        async def worker() -> None:
            """Не создаёт по HTTP-задаче на каждую страницу большого файла."""
            while not pending.empty():
                pair, context = pending.get_nowait()
                await compare(pair, context)

        tasks = [
            asyncio.create_task(worker())
            for _ in range(min(self.settings.pair_concurrency, pending.qsize()))
        ]
        timed_out = False
        try:
            async with asyncio.timeout(self.settings.vlm_segmentation_timeout_seconds):
                await asyncio.gather(*tasks)
        except TimeoutError:
            timed_out = True
        finally:
            for task in tasks:
                if not task.done() and not task.cancelling():
                    task.cancel()
            # Gather может завершиться после первого cancelled child. Остальные должны
            # закрыть транзакции до новых checkpoint, без повторной отмены их cleanup.
            await asyncio.gather(*tasks, return_exceptions=True)
        if timed_out:
            for pair in pairs:
                if pair.status != "completed":
                    pair.status, pair.solution, pair.error_code = (
                        "unresolved",
                        None,
                        "pair_stage_timeout",
                    )
                    await emit("pair_result", pair.model_dump(mode="json"))
        groups, _ = sequential_ranges(job.page_refs(), pairs)
        return ManualPlan(
            documents=[
                ManualDocument(client_id=f"pair-{i}", pages=group)
                for i, group in enumerate(groups, 1)
            ]
        )
