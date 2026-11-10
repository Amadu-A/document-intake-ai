# app/application/analysis.py
"""Полный двухстадийный VLM-конвейер; backend валидирует, но не угадывает границы."""

import asyncio
import json
from uuid import uuid4

from pydantic import ValidationError

from app.application.inference_schema import extraction_schema
from app.application.ports import OCR, AnalysisSettings, Emit, JobRepository, Storage, Vision
from app.application.reconciliation import reconcile
from app.core.observability import timed
from app.domain.boundaries import FixedGroup
from app.domain.compact_plan import page_spans
from app.domain.errors import IntakeError, LeaseLost
from app.domain.jobs import Job
from app.domain.schema import AttachmentFile, BatchResult, Document, Extracted, SourceFile
from app.domain.segmentation import PageRef, Plan, Segment, validate_plan, windows
from app.domain.taxonomy import taxonomy_json


def page_context(pages: list[dict]) -> list[dict]:
    """Передаёт модели глобальные адреса и OCR, исключая приватные filesystem paths."""
    return [
        {
            "file_id": p["file_id"],
            "page_number": p["page_number"],
            "ocr": p.get("ocr", ""),
            "ocr_truncated": p.get("ocr_truncated", False),
        }
        for p in pages
    ]


def validate_extraction(
    facts: Extracted, group: Segment | FixedGroup, organization: str | None
) -> None:
    """Проверяет контекст направления и сохранность приложений без подмены модельных фактов."""
    if not organization and facts.classification.direction is not None:
        raise IntakeError(
            "schema_validation_failed", "Без organization_hint направление должно быть null"
        )
    if (
        facts.classification.is_letter
        and facts.classification.direction is None
        and not facts.warnings
    ):
        raise IntakeError("schema_validation_failed", "Неизвестное направление требует warning")
    if not facts.classification.is_letter and not facts.warnings:
        raise IntakeError(
            "schema_validation_failed", "Предварительный вид неписьма требует warning"
        )
    if facts.one_c.form is not None or facts.one_c.received_via is not None:
        raise IntakeError(
            "schema_validation_failed", "Форма хранения и способ получения 1С не заданы контекстом"
        )
    if facts.requisites.delivery_channel not in {None, "unknown"}:
        raise IntakeError("schema_validation_failed", "Способ доставки не задан входным контекстом")
    if not organization and facts.one_c.organization is not None:
        raise IntakeError("schema_validation_failed", "Организация 1С неизвестна без контекста")
    path = facts.classification.template_path
    leaf = facts.one_c.template_leaf
    view = facts.one_c.view_of_document
    if (leaf is not None and (not path or leaf != path[-1])) or (
        view is not None and (not path or view != path[1])
    ):
        raise IntakeError("schema_validation_failed", "Поля шаблона 1С противоречат классификации")
    primary = group.pages[0].file_id
    if isinstance(group, FixedGroup):
        allowed = {p.page_number for p in group.pages if p.file_id == primary}
        used = set()
        for attachment in facts.requisites.attachments:
            span = attachment.page_range
            if not span:
                continue
            if len(span) != 2 or span[0] > span[1]:
                raise IntakeError("schema_validation_failed", "Некорректный диапазон приложения")
            numbers = set(range(span[0], span[1] + 1))
            if not numbers.issubset(allowed) or used.intersection(numbers):
                raise IntakeError(
                    "schema_validation_failed",
                    "Приложение выходит за утверждённые страницы или повторяется",
                )
            used.update(numbers)
        return
    expected = []
    for attachment in group.attachments:
        if attachment.pages and all(p.file_id == primary for p in attachment.pages):
            numbers = [p.page_number for p in attachment.pages]
            if numbers == list(range(min(numbers), max(numbers) + 1)):
                expected.append([min(numbers), max(numbers)])
    actual = [a.page_range for a in facts.requisites.attachments if a.page_range]
    if sorted(expected) != sorted(actual):
        raise IntakeError(
            "schema_validation_failed", "Приложения не соответствуют проверенному плану"
        )
    if len(facts.requisites.attachments) < len(group.attachments):
        raise IntakeError("schema_validation_failed", "Модель потеряла сведения о приложениях")


class Analyze:
    """Оркестрирует OCR, два VLM-прохода, PDF и атомарную публикацию результата."""

    def __init__(
        self,
        repository: JobRepository,
        storage: Storage,
        vision: Vision,
        ocr: OCR,
        settings: AnalysisSettings,
    ) -> None:
        """Получает зависимости; транспорт и конкретные драйверы здесь не создаются."""
        self.repository, self.storage, self.vision = repository, storage, vision
        self.ocr, self.settings = ocr, settings

    async def _validated(
        self,
        prompt: str,
        context: dict,
        pages: list[dict],
        model,
        validate,
        emit: Emit,
        *,
        schema: dict | None = None,
    ):
        """Повторяет запрос с обратной связью; уточнённая схема не заменяет серверную проверку."""
        feedback = []
        last_code = "schema_validation_failed"
        for attempt in range(1, self.settings.model_attempts + 1):
            try:
                raw = await self.vision.request(
                    prompt,
                    {**context, "validation_errors": feedback},
                    pages,
                    model.model_json_schema() if schema is None else schema,
                    emit,
                )
                parsed = model.model_validate(raw)
                validate(parsed)
                return parsed
            except ValidationError as exc:
                feedback = [
                    {"path": list(e["loc"]), "type": e["type"], "message": e["msg"]}
                    for e in exc.errors(include_input=False, include_url=False)
                ]
                last_code = "schema_validation_failed"
            except IntakeError as exc:
                if exc.code == "lease_lost":
                    raise
                last_code = exc.code
                feedback = [{"code": exc.code, "message": exc.message}]
            await emit("model_retry", {"attempt": attempt, "code": last_code})
            if attempt < self.settings.model_attempts:
                await asyncio.sleep(min(2 ** (attempt - 1), 4))
        raise IntakeError(
            last_code, "Модель не вернула проверенный результат после ограниченных повторов"
        )

    async def _plan(self, job: Job, pages: list[dict], emit: Emit) -> Plan:
        """VLM анализирует окна с перекрытием и сама согласует окончательный глобальный план."""
        chunks = windows(pages, self.settings.vlm_window_pages, self.settings.vlm_overlap_pages)
        proposals = []
        for index, chunk in enumerate(chunks):
            refs = [PageRef(file_id=p["file_id"], page_number=p["page_number"]) for p in chunk]
            plan = await self._validated(
                job.prompts["segmentation"],
                {
                    "task": "window_segmentation",
                    "pages": page_context(chunk),
                    "window_index": index,
                    "window_count": len(chunks),
                    "instruction": "Границы окна могут продолжаться за его пределами. "
                    "Сейчас распределяй только страницы данного окна; итог решается отдельно.",
                },
                chunk,
                Plan,
                lambda p, refs=refs: validate_plan(p, refs, require_verified=len(chunks) == 1),
                emit,
            )
            proposals.append(plan.model_dump(mode="json"))
        if len(chunks) == 1:
            return plan
        if len(pages) > max(
            64, self.settings.vlm_window_pages * self.settings.vlm_reconciliation_windows
        ):
            return await reconcile(
                self, job, pages, [Plan.model_validate(p) for p in proposals], emit
            )
        refs = [PageRef(file_id=p["file_id"], page_number=p["page_number"]) for p in pages]
        return await self._validated(
            job.prompts["segmentation"],
            {
                "task": "global_reconciliation",
                "pages": [p.model_dump() for p in refs],
                "window_proposals": proposals,
                "instruction": "Прими глобальное решение, согласуй пересечения и продолжения. "
                "Не повторяй страницы перекрытия. Только ты определяешь границы.",
            },
            [],
            Plan,
            lambda p: validate_plan(p, refs),
            emit,
        )

    async def _extract(
        self, job: Job, group: Segment | FixedGroup, pages: list[dict], emit: Emit
    ) -> Extracted:
        """Извлекает факты всей группы; большие комплекты согласует повторным VLM-проходом."""
        schema = extraction_schema(job.organization_hint)
        context = {
            "organization_hint": job.organization_hint,
            "catalog": json.loads(taxonomy_json()),
            "group": group.model_dump(mode="json"),
            "rules": "attachment_file создаёт backend и в ответ его не включать. "
            "Вид/подвид при известном направлении равны узлам template_path. "
            "Без контекста организации direction=null, template_path=[], warning. "
            "Неписьма имеют предварительный вид и warning. Неизвестный способ получения "
            "и форма хранения — null. Не вычисляй внутренние номера/статусы 1С. "
            "Неизвестное направление не меняет род письма: оно остаётся перепиской. "
            "Определи сам документ, а не род упомянутого в нём договора или приложения. "
            "Номер и дата документа относятся к основной части group.main_pages; "
            "номера/даты связанных документов сохраняются в related_entities/attachments. "
            "При расхождении OCR с изображением проверяй оригинальное изображение. "
            "Нечитаемое значение оставь null с предупреждением.",
        }
        if isinstance(group, FixedGroup):
            context["rules"] += (
                " Страницы группы утверждены и неизменяемы. Определи основную часть и "
                "фактически присутствующие приложения только внутри group.pages. "
                "Пустые main_pages/attachments означают, что внутренний состав ещё не распознан; "
                "это не запрет находить приложения. Не переноси страницы между регистрациями. "
                "page_range — физические номера первого источника группы; для сложного "
                "межфайлового приложения без однозначного диапазона оставь [] и warning."
            )
        if len(group.pages) > 64:
            physical = job.page_refs() or group.pages
            context["group"] = {
                "document_index": group.document_index,
                "spans": page_spans(group.pages, physical),
                "main_spans": page_spans(group.main_pages, physical),
                "attachments": [
                    {"kind": a.kind, "spans": page_spans(a.pages, physical), "relation": a.relation}
                    for a in group.attachments
                ],
                "status": group.status,
            }
            context["position_map"] = [
                {"file_id": s.file_id, "page_count": s.page_count} for s in job.sources
            ] or [{"file_id": physical[0].file_id, "page_count": len(physical)}]
            context["rules"] += (
                " spans/main_spans — включительные сквозные позиции от 1; "
                "преобразуй page_range приложений обратно в физические номера первого источника."
            )
        chunks = windows(pages, self.settings.vlm_window_pages, self.settings.vlm_overlap_pages)
        if len(chunks) == 1:
            return await self._validated(
                job.prompts["extraction"],
                {**context, "pages": page_context(pages)},
                pages,
                Extracted,
                lambda p: validate_extraction(p, group, job.organization_hint),
                emit,
                schema=schema,
            )
        evidence = []
        for chunk in chunks:
            partial = await self._validated(
                job.prompts["extraction"],
                {
                    **context,
                    "pages": page_context(chunk),
                    "instruction": "Собери факты окна группы с физическими page_range, "
                    "не выдавай недоступные факты. Итог будет согласован отдельно.",
                },
                chunk,
                Extracted,
                lambda _: None,
                emit,
                schema=schema,
            )
            evidence.append(
                {
                    "pages": [
                        {"file_id": p["file_id"], "page_number": p["page_number"]} for p in chunk
                    ],
                    "facts": partial.model_dump(mode="json"),
                }
            )
        fan_in = self.settings.vlm_reconciliation_windows
        while len(evidence) > fan_in:
            reduced = []
            for offset in range(0, len(evidence), fan_in):
                block = evidence[offset : offset + fan_in]
                partial = await self._validated(
                    job.prompts["extraction"],
                    {
                        **context,
                        "window_evidence": block,
                        "instruction": "Промежуточный уровень согласования фактов. "
                        "Удали дубли перекрытий, сохрани все факты и противоречия в warnings. "
                        "Не меняй утверждённые регистрационные границы.",
                    },
                    [],
                    Extracted,
                    lambda _: None,
                    emit,
                    schema=schema,
                )
                reduced.append({"facts": partial.model_dump(mode="json")})
            evidence = reduced
        return await self._validated(
            job.prompts["extraction"],
            {
                **context,
                "window_evidence": evidence,
                "instruction": "Согласуй факты по всему комплекту. Удали дубли перекрытий, "
                "сохрани конфликты в warnings и все приложения проверенного плана.",
            },
            [],
            Extracted,
            lambda p: validate_extraction(p, group, job.organization_hint),
            emit,
            schema=schema,
        )

    async def _prepared(self, job: Job, emit: Emit) -> Job:
        """Продолжает с durable preview checkpoint и не конвертирует готовый пакет повторно."""
        if job.preview_complete:
            return job
        if job.manifest.get("preview_complete"):
            return await self.repository.complete_preview(job.id, job.fence, job.manifest)
        await emit("stage", {"stage": "preparing_preview", "progress": 5})
        _, manifest = await self.storage.normalize(job, emit)
        return await self.repository.complete_preview(job.id, job.fence, manifest)

    @timed("prepare_preview")
    async def prepare(self, job: Job) -> None:
        """Готовит только рендеры; OCR/VLM не вызываются, ожидание пользователя не держит lease."""

        async def emit(kind: str, data: dict) -> None:
            """Защищает каждый progressive checkpoint fencing-поколением подготовки."""
            await self.repository.checkpoint(job.id, job.fence, kind, data)

        try:
            await self._prepared(job, emit)
        except LeaseLost:
            raise
        except IntakeError as exc:
            await self.repository.finish(
                job.id, job.fence, None, {}, {"code": exc.code, "message": exc.message}, False
            )
        except Exception:
            await self.repository.finish(
                job.id,
                job.fence,
                None,
                {},
                {"code": "preview_failed", "message": "Не удалось подготовить страницы"},
                False,
            )

    @timed("analyze_documents")
    async def execute(self, job: Job) -> None:
        """Выполняет выбранный режим и публикует только validated JSON по заблокированному плану."""
        manifest = job.manifest

        async def emit(kind: str, data: dict) -> None:
            """Каждая запись проверяет fence; устаревший worker не публикует свои данные."""
            await self.repository.checkpoint(job.id, job.fence, kind, data)

        try:
            job = await self._prepared(job, emit)
            if job.status != "running":
                return
            manifest = job.manifest
            pages = manifest["pages"]
            recognized = None
            if not job.skip_segmentation and job.approved_plan is None:
                if job.proposed_plan is None:
                    await emit("stage", {"stage": "docling", "progress": 20})
                    recognized = await self.ocr.recognize(pages, emit)
                    await emit("stage", {"stage": "segmentation_vlm", "progress": 35})
                    plan = await self._plan(job, recognized, emit)
                    job = await self.repository.propose(job.id, job.fence, plan)
                elif not job.auto_split:
                    job = await self.repository.propose(job.id, job.fence, job.proposed_plan)
                if job.status == "awaiting_confirmation":
                    return
            job = await self.repository.lock_plan(job.id, job.fence)
            manifest = {**job.manifest, "documents": {}}
            if job.segmentation_source in {"vlm_auto", "vlm_confirmed"}:
                groups = list(job.proposed_plan.documents)
            else:
                groups = [
                    FixedGroup(document_index=i, pages=list(d.pages))
                    for i, d in enumerate(job.approved_plan.documents, 1)
                ]
                manifest["plan"] = {
                    "documents": [g.model_dump(mode="json") for g in groups],
                    "unassigned_pages": [],
                    "warnings": [],
                }
            manifest["excluded_pages"] = [
                p.model_dump(mode="json") for p in job.approved_plan.excluded_pages
            ]
            # Проверка фактического размера PDF делается до OCR/extraction ручного режима.
            artifacts = []
            for group in groups:
                document_id = str(uuid4())
                artifact = await self.storage.pdf(job, document_id, group.pages)
                manifest["documents"][document_id] = {
                    **artifact,
                    "group": group.model_dump(mode="json"),
                }
                artifacts.append((document_id, artifact))
            needed = {p for group in groups for p in group.pages}
            if recognized is None:
                selected_pages = [
                    p
                    for p in pages
                    if PageRef(file_id=p["file_id"], page_number=p["page_number"]) in needed
                ]
                await emit("stage", {"stage": "docling", "progress": 40})
                recognized = await self.ocr.recognize(selected_pages, emit)
            files = {
                s.file_id: SourceFile(
                    file_id=s.file_id,
                    file_name=s.file_name,
                    file_url="",
                    mime_type=s.mime_type,
                    documents=[],
                )
                for s in job.sources
            }
            plan_warnings = job.proposed_plan.warnings if job.proposed_plan else []
            has_warnings = bool(plan_warnings or manifest["excluded_pages"]) or any(
                p.get("ocr_truncated") for p in recognized
            )
            lookup = {(p["file_id"], p["page_number"]): p for p in recognized}
            for index, group in enumerate(groups, 1):
                await emit(
                    "stage",
                    {
                        "stage": "extraction_vlm",
                        "progress": 50 + int(40 * index / len(groups)),
                    },
                )
                selected = [lookup[(p.file_id, p.page_number)] for p in group.pages]
                facts = await self._extract(job, group, selected, emit)
                facts.recognition.ocr_engine = "docling"
                facts.warnings.extend(plan_warnings)
                if manifest["excluded_pages"]:
                    facts.warnings.append(
                        "В пакете есть явно исключённые страницы; оригиналы сохранены"
                    )
                if facts.recognition.low_confidence_fields:
                    facts.warnings.append("Некоторые реквизиты распознаны с низкой уверенностью")
                confidence = facts.classification.confidence
                if confidence is not None and confidence < 0.7:
                    facts.warnings.append("Низкая уверенность VLM в классификации")
                if any(p.get("ocr_truncated") for p in selected):
                    facts.warnings.append(
                        "OCR-контекст части страниц ограничен; VLM получила изображения"
                    )
                if group.confidence is not None and group.confidence < 0.7:
                    facts.warnings.append("Низкая уверенность VLM в группировке страниц")
                primary = group.pages[0].file_id
                if any(p.file_id != primary for p in group.pages):
                    facts.warnings.append(
                        "PDF-комплект объединяет несколько исходных файлов; см. manifest"
                    )
                for attachment in group.attachments:
                    numbers = [p.page_number for p in attachment.pages]
                    if numbers and (
                        any(p.file_id != primary for p in attachment.pages)
                        or numbers != list(range(min(numbers), max(numbers) + 1))
                    ):
                        facts.warnings.append(
                            "Сложный состав приложения: физические страницы указаны в manifest"
                        )
                document_id, artifact = artifacts[index - 1]
                files[primary].documents.append(
                    Document(
                        **facts.model_dump(),
                        document_id=document_id,
                        segment_index=index,
                        pages=[p.page_number for p in group.pages if p.file_id == primary],
                        split_confidence=group.confidence,
                        attachment_file=AttachmentFile(
                            file_name=artifact["file_name"], file_url=""
                        ),
                    )
                )
                has_warnings |= bool(facts.warnings)
            result = BatchResult(batch_id=job.id, files=list(files.values())).model_dump(
                mode="json"
            )
            # URLs привязываются к окончательному TTL при выдаче через API, а не придумываются VLM.
            await self.repository.finish(job.id, job.fence, result, manifest, None, has_warnings)
        except LeaseLost:
            raise
        except IntakeError as exc:
            await self.repository.finish(
                job.id, job.fence, None, manifest, {"code": exc.code, "message": exc.message}, False
            )
        except Exception:
            await self.repository.finish(
                job.id,
                job.fence,
                None,
                manifest,
                {"code": "processing_failed", "message": "Внутренняя ошибка обработки"},
                False,
            )
