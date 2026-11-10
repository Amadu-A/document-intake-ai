# app/application/reconciliation.py
"""Иерархическое VLM-согласование стыков, без эвристического исправления границ."""

from collections import Counter

from app.domain.compact_plan import CompactPlan, compact, page_spans
from app.domain.errors import IntakeError
from app.domain.segmentation import PageRef, Plan, validate_plan


async def reconcile(analysis, job, pages: list[dict], plans: list[Plan], emit) -> Plan:
    """Сводит соседние планы деревом; неизменённые группы остаются решениями предыдущей VLM."""
    physical = [PageRef(file_id=p["file_id"], page_number=p["page_number"]) for p in pages]
    positions = {p: i for i, p in enumerate(physical)}

    async def pair(left: Plan, right: Plan) -> Plan:
        """Пересогласует все группы перекрытия и ближайшие группы обеих сторон."""
        left_refs = {p for d in left.documents for p in d.pages}
        right_refs = {p for d in right.documents for p in d.pages}
        overlap = left_refs & right_refs
        # Выбираются только физически соприкасающиеся группы; их семантику решает модель.
        left_start = next(
            (i for i, d in enumerate(left.documents) if set(d.pages) & overlap),
            len(left.documents) - 1,
        )
        right_end = max(
            (i for i, d in enumerate(right.documents) if set(d.pages) & overlap), default=0
        )
        left_start = min(
            left_start,
            next((i for i, d in enumerate(left.documents) if d.status != "verified"), left_start),
        )
        right_end = max(
            right_end,
            max(
                (i for i, d in enumerate(right.documents) if d.status != "verified"),
                default=right_end,
            ),
        )
        affected = left.documents[left_start:] + right.documents[: right_end + 1]
        refs = sorted({p for d in affected for p in d.pages}, key=positions.__getitem__)
        allowed_refs = set(refs)
        seam = sorted(overlap, key=positions.__getitem__)
        center = positions[seam[0]] if seam else positions[right.documents[0].pages[0]]
        radius = max(1, analysis.settings.vlm_window_pages // 2)
        images = pages[max(0, center - radius) : center + radius]
        proposals = [
            compact(
                Plan(documents=left.documents[left_start:], unassigned_pages=[], warnings=[]),
                physical,
            ),
            compact(
                Plan(documents=right.documents[: right_end + 1], unassigned_pages=[], warnings=[]),
                physical,
            ),
        ]

        def validate(candidate: CompactPlan) -> None:
            """Ни одна страница затронутых групп не должна потеряться или продублироваться."""
            expanded = candidate.expand(physical)
            try:
                validate_plan(expanded, refs)
            except IntakeError as exc:
                counts = Counter(p for doc in expanded.documents for p in doc.pages)
                duplicate = sorted(
                    (p for p, n in counts.items() if n > 1), key=positions.__getitem__
                )
                missing = [p for p in refs if p not in counts]
                extra = sorted(
                    (p for p in counts if p not in allowed_refs), key=positions.__getitem__
                )
                raise IntakeError(
                    "invalid_segmentation",
                    f"{exc.message}. Разрешены ТОЛЬКО интервалы {page_spans(refs, physical)}; "
                    f"повторены в разных группах: {page_spans(duplicate, physical)}; "
                    f"потеряны: {page_spans(missing, physical)}; "
                    f"лишние: {page_spans(extra, physical)}. "
                    "Каждая позиция должна встречаться ровно один раз среди всех documents. "
                    "Другие страницы файла ещё не входят в этот уровень согласования.",
                ) from exc

        answer = await analysis._validated(
            job.prompts["segmentation"],
            {
                "task": "hierarchical_reconciliation",
                "position_map": [
                    {"file_id": s.file_id, "page_count": s.page_count} for s in job.sources
                ]
                or [{"file_id": physical[0].file_id, "page_count": len(physical)}],
                "proposals": proposals,
                "allowed_spans": page_spans(refs, physical),
                "instruction": "Ты принимаешь итоговое решение на этом уровне дерева. "
                "Согласуй все предложенные группы, продолжения и приложения. "
                "spans — включительные сквозные позиции от 1 в исходной последовательности. "
                "Верни ровно allowed_spans, без добавления остальных страниц исходного файла. "
                "Номер общего количества страниц на изображении не расширяет allowed_spans. "
                "Не теряй страницы и не повторяй перекрытие. "
                "proposals — конкурирующие предложения, а не обязательные выходные группы. "
                "Число documents не обязано совпадать с числом предложений; "
                "не повторяй объединённую группу дважды. "
                "reason — одно короткое предложение, желательно до 200 символов. "
                "status=verified только при подтверждённых тобой границах; иначе needs_review. "
                "Верни CompactPlan. Другие уже согласованные группы не входят в этот запрос.",
            },
            images,
            CompactPlan,
            validate,
            emit,
        )
        merged = answer.expand(physical)
        documents = (
            left.documents[:left_start] + merged.documents + right.documents[right_end + 1 :]
        )
        result = Plan(
            documents=[
                d.model_copy(update={"document_index": i}) for i, d in enumerate(documents, 1)
            ],
            unassigned_pages=[],
            warnings=list(dict.fromkeys(left.warnings + right.warnings + merged.warnings)),
        )
        validate_plan(result, sorted(left_refs | right_refs, key=positions.__getitem__))
        return result

    while len(plans) > 1:
        next_level = []
        for offset in range(0, len(plans), 2):
            next_level.append(
                await pair(plans[offset], plans[offset + 1])
                if offset + 1 < len(plans)
                else plans[offset]
            )
        plans = next_level
    validate_plan(plans[0], physical)
    return plans[0]
