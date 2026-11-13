# app/application/analysis.py
"""Полный двухстадийный VLM-конвейер; backend валидирует, но не угадывает границы."""

import asyncio
import json
import re
from copy import deepcopy
from uuid import uuid4

from pydantic import ValidationError

from app.application.inference_schema import extraction_schema, segmentation_schema
from app.application.one_c_projection import (
    canonical_organization,
    project_one_c,
    supported_direction,
)
from app.application.ports import OCR, AnalysisSettings, Emit, JobRepository, Storage, Vision
from app.application.reconciliation import reconcile
from app.core.observability import timed
from app.domain.boundaries import FixedGroup
from app.domain.compact_plan import CompactPlan, page_spans
from app.domain.errors import IntakeError, LeaseLost
from app.domain.extraction import ExtractionDraft, PrimaryDateReading
from app.domain.jobs import Job
from app.domain.schema import (
    AttachmentFile,
    BatchResult,
    Document,
    DocumentRoot,
    Extracted,
    SourceFile,
)
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
    organization = canonical_organization(organization)
    if not organization and facts.classification.direction is not None:
        raise IntakeError(
            "schema_validation_failed", "Без organization_hint направление должно быть null"
        )
    if facts.classification.direction is None and not facts.warnings:
        raise IntakeError("schema_validation_failed", "Неизвестное направление требует warning")
    if not facts.classification.is_letter and not facts.warnings:
        raise IntakeError(
            "schema_validation_failed", "Предварительный вид неписьма требует warning"
        )
    if any(getattr(facts.one_c, field) is not None for field in type(facts.one_c).model_fields):
        raise IntakeError(
            "schema_validation_failed", "Поля one_c формирует backend; VLM должна вернуть все null"
        )
    if facts.classification.direction is not None and (
        facts.classification.direction != supported_direction(facts.requisites, organization)
    ):
        raise IntakeError(
            "schema_validation_failed",
            "Направление противоречит сторонам документа или не подтверждено обслуживаемой "
            "организацией. Уточни sender/recipient по изображению; при конфликте direction=null "
            "и предупреждение, не меняй юридический род документа.",
        )
    if facts.requisites.delivery_channel not in {None, "unknown"}:
        raise IntakeError("schema_validation_failed", "Способ доставки не задан входным контекстом")
    if facts.classification.document_root == DocumentRoot.PROCEDURAL and any(
        amount.type.casefold().replace("_", " ").strip()
        in {"fine", "штраф", "administrative fine", "административный штраф"}
        for amount in facts.requisites.amounts
    ):
        raise IntakeError(
            "schema_validation_failed",
            "В процессуальном документе тип fine/штраф без контекста недостаточен. "
            "Уточни по источнику, относится ли сумма к действующему постановлению или "
            "к прекращённому/отменённому делу, и явно отрази это в amounts[].type. "
            "Не определяй исход по умолчанию и не меняй сумму.",
        )
    if isinstance(facts, ExtractionDraft):
        for field in ("document_number", "document_date"):
            path = f"requisites.{field}"
            reading = getattr(facts.primary_readability, field)
            value = getattr(facts.requisites, field)
            uncertain = set(facts.recognition.low_confidence_fields)
            if (reading == "legible") != (value is not None) or (
                reading == "ambiguous"
                and (not uncertain.intersection({field, path}) or not facts.warnings)
            ):
                raise IntakeError(
                    "schema_validation_failed",
                    f"{path} противоречит primary_readability={reading}. "
                    "Перепроверь изображение: legible — однозначно читаемое значение; "
                    "missing — null; ambiguous — null, warning и путь в low_confidence_fields. "
                    "Низкая уверенность сама по себе не означает неоднозначность. "
                    "Не подставляй вероятный вариант из OCR.",
                )
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
        uncertain_fields = set()
        candidate = context.get("candidate")
        if model is ExtractionDraft and context.get("candidate"):
            uncertain_fields.update(
                ExtractionDraft.model_validate(context["candidate"]).uncertain_fields()
            )
        if schema is None and model in {Plan, CompactPlan}:
            schema = segmentation_schema(model)
        for attempt in range(1, self.settings.model_attempts + 1):
            try:
                request_schema = model.model_json_schema() if schema is None else schema
                if uncertain_fields:
                    request_schema = deepcopy(request_schema)
                    for field in uncertain_fields:
                        request_schema["$defs"]["Requisites"]["properties"][field] = {
                            "type": "null"
                        }
                        request_schema["$defs"]["PrimaryReadability"]["properties"][field] = {
                            "type": "string",
                            "enum": ["ambiguous"],
                        }
                request_context = {**context, "validation_errors": feedback}
                if candidate is not None:
                    request_context["candidate"] = candidate
                if attempt > 1 and model is ExtractionDraft and candidate is not None:
                    request_context["revision_instruction"] = (
                        "Исправь нарушения из validation_errors в предыдущем candidate. "
                        "Независимые однозначно прочитанные реквизиты сохрани; ошибка номера "
                        "не делает дату неоднозначной и наоборот. Не заменяй прочитанную дату "
                        "датой приложения. Пересматривай другой факт только при явном "
                        "противоречии изображению, отмечая причину в warnings."
                    )
                raw = await self.vision.request(
                    prompt,
                    request_context,
                    pages,
                    request_schema,
                    emit,
                )
                parsed = model.model_validate(raw)
                if isinstance(parsed, ExtractionDraft):
                    candidate = parsed.model_dump(mode="json")
                    uncertain_fields.update(parsed.uncertain_fields())
                    if any(
                        getattr(parsed.requisites, field) is not None
                        or getattr(parsed.primary_readability, field) != "ambiguous"
                        for field in uncertain_fields
                    ):
                        raise IntakeError(
                            "schema_validation_failed",
                            "VLM отметила неоднозначность реквизитов "
                            f"{['requisites.' + field for field in sorted(uncertain_fields)]}. "
                            "Верни для них null, ambiguous, "
                            "warning и путь в low_confidence_fields. Повтор без нового "
                            "источника не должен превращать уже отмеченное сомнение в факт.",
                        )
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
                    "Сейчас распределяй только страницы данного окна; итог решается отдельно. "
                    "Связанные листы могут быть разделены произвольным числом чужих страниц. "
                    "Состав одной регистрации может быть несмежным; чужие листы не включай в неё. "
                    "Одинаковая форма уведомлений сама по себе не объединяет разные уведомления. "
                    "reason — одно короткое предложение; сомнительные границы — needs_review.",
                },
                chunk,
                Plan,
                lambda p, refs=refs: validate_plan(p, refs, require_verified=False),
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
                "page_evidence": [
                    {
                        "file_id": p["file_id"],
                        "page_number": p["page_number"],
                        "ocr": p.get("ocr", "")[:500],
                    }
                    for p in pages
                ],
                "instruction": "Прими глобальное решение, согласуй пересечения и продолжения. "
                "Не повторяй страницы перекрытия. Только ты определяешь границы.",
            },
            [],
            Plan,
            lambda p: validate_plan(p, refs, require_verified=False),
            emit,
        )

    async def _extract(
        self, job: Job, group: Segment | FixedGroup, pages: list[dict], emit: Emit
    ) -> Extracted:
        """Извлекает факты всей группы; большие комплекты согласует повторным VLM-проходом."""
        organization = canonical_organization(job.organization_hint)
        schema = extraction_schema(organization)
        first_main = next(
            (e.page for e in getattr(group, "evidence", []) if e.role == "main"),
            next(iter(group.main_pages or group.pages)),
        )
        context = {
            "organization_hint": organization,
            "footer_details": True,
            "header_detail_page": first_main.model_dump() if len(group.pages) > 1 else None,
            "catalog": json.loads(taxonomy_json()),
            "group": group.model_dump(mode="json"),
            "rules": "attachment_file создаёт backend и в ответ его не включать. "
            "Только для писем вид/подвид при известном направлении равны узлам template_path. "
            "Без контекста организации direction=null, template_path=[], warning. "
            "Направление независимо от is_letter: у повестки, решения, постановления "
            "возможно incoming/outgoing при подтверждённой организации, но род остаётся "
            "юридическим, template_path=[]; one_c ВСЕ поля null, их формирует backend. "
            "При конфликте организации с sender/recipient — direction=null и warning. "
            "Неписьма имеют предварительный вид и warning. Неизвестный способ получения "
            "и форма хранения — null. Не вычисляй внутренние номера/статусы 1С. "
            "Неизвестное направление не меняет род письма: оно остаётся перепиской. "
            "Определи сам документ, а не род упомянутого в нём договора или приложения. "
            "Конверт/адресный оборот в main_pages — технический носитель, не юридический род; "
            "определи род по содержательному вложению (evidence.role=main/continuation). "
            "Номер и дата документа относятся к основной части group.main_pages; "
            "номера/даты связанных документов сохраняются в related_entities/attachments. "
            "При расхождении OCR с изображением проверяй оригинальное изображение. "
            "Нечитаемое значение оставь null с предупреждением. "
            "Проверь requisites.response_required отдельно от пустого one_c: для "
            "решения о прекращении производства без запроса письменного ответа — false; "
            "для повестки/постановления об удержании без установленного запроса — null; "
            "true только при явно требуемом письменном ответе.",
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
            date_reading = None
            if group.attachments and group.main_pages:
                main = set(group.main_pages)
                main_images = [
                    p
                    for p in pages
                    if PageRef(file_id=p["file_id"], page_number=p["page_number"]) in main
                ]

                def validate_date(reading: PrimaryDateReading) -> None:
                    """Проверяет читаемость и адрес основной страницы без угадывания даты."""
                    if (reading.readability == "legible") != (reading.document_date is not None):
                        raise IntakeError(
                            "schema_validation_failed", "Читаемость даты противоречит значению"
                        )
                    if reading.source_page is not None and reading.source_page not in main:
                        raise IntakeError(
                            "schema_validation_failed", "Дата ссылается не на основную часть"
                        )
                    if reading.readability == "legible" and reading.source_page is None:
                        raise IntakeError(
                            "schema_validation_failed", "Читаемая дата требует адрес страницы"
                        )
                    if reading.readability == "ambiguous" and not reading.warnings:
                        raise IntakeError(
                            "schema_validation_failed", "Неоднозначная дата требует warning"
                        )

                date_reading = await self._validated(
                    job.prompts["extraction"],
                    {
                        "task": "read_primary_document_date",
                        "footer_details": True,
                        "header_detail_page": first_main.model_dump(),
                        "main_pages": [p.model_dump() for p in group.main_pages],
                        "instruction": "Прочитай ТОЛЬКО собственную дату составления основного "
                        "документа по изображениям, независимо от OCR. Не используй даты "
                        "договоров, расчётов или приложений. День/месяц могут быть рукописными, "
                        "а год печатным: это одна дата. Сомнение в номере не относится к дате. "
                        "legible — однозначное чтение и source_page; missing/ambiguous — null; "
                        "при неоднозначности warning. Верни только поля переданной схемы; "
                        "полный Extracted сейчас не нужен.",
                    },
                    main_images,
                    PrimaryDateReading,
                    validate_date,
                    emit,
                )
                value = date_reading.document_date
                schema["$defs"]["Requisites"]["properties"]["document_date"] = (
                    {"type": "string", "format": "date", "const": value.isoformat()}
                    if value is not None
                    else {"type": "null"}
                )
                schema["$defs"]["PrimaryReadability"]["properties"]["document_date"] = {
                    "type": "string",
                    "enum": [date_reading.readability],
                }
                context["primary_date_evidence"] = date_reading.model_dump(mode="json")
                context["rules"] += (
                    " Собственная дата уже независимо прочитана VLM на основных страницах "
                    "без OCR: сохрани primary_date_evidence, не подменяй её датами приложений."
                )

            def validate_facts(value: ExtractionDraft) -> None:
                """Сверяет полный ответ с независимым визуальным чтением даты."""
                validate_extraction(value, group, job.organization_hint)
                if date_reading is not None and (
                    value.requisites.document_date != date_reading.document_date
                    or value.primary_readability.document_date != date_reading.readability
                ):
                    raise IntakeError(
                        "schema_validation_failed",
                        "Собственная дата противоречит независимому primary_date_evidence VLM; "
                        "сохрани его значение и читаемость, не подменяй датой приложения/OCR.",
                    )

            facts = await self._validated(
                job.prompts["extraction"],
                {**context, "pages": page_context(pages)},
                pages,
                ExtractionDraft,
                validate_facts,
                emit,
                schema=schema,
            )
            # Метки OCR лишь указывают, что нужно ещё раз посмотреть изображение;
            # значения не копируются из OCR и не вычисляются backend.
            ocr_text = " ".join(p.get("ocr", "") for p in pages).upper()
            missing = [
                f"requisites.sender.{field}"
                for field, label in (("inn", "ИНН"), ("kpp", "КПП"), ("ogrn", "ОГРН"))
                if facts.classification.is_letter
                and getattr(facts.requisites.sender, field) is None
                and label in ocr_text
            ]
            if (
                facts.classification.is_letter
                and not facts.requisites.related_entities.invoices
                and re.search(r"\bСЧ[ЕЁ]Т(?:А|У)?\s*№", ocr_text)
            ):
                missing.append("requisites.related_entities.invoices")
            if not facts.classification.is_letter and facts.requisites.related_entities.court_cases:
                missing.append("requisites.related_entities.court_cases[].date")
            if missing:
                facts = await self._validated(
                    job.prompts["extraction"],
                    {
                        **context,
                        "pages": page_context(pages),
                        "candidate": facts.model_dump(mode="json"),
                        "verification_fields": missing,
                        "instruction": "Одна дополнительная проверка полноты поисковых реквизитов. "
                        "Метки OCR не доказывают значение или принадлежность отправителю. "
                        "Перечитай бланк, фрагменты и ссылки по изображению; каждый реквизит "
                        "независимо. Отличай банковский счёт от счёта на оплату: банк не попадает "
                        "в related_entities.invoices. Верни полный JSON: "
                        "достоверные факты сохрани, "
                        "связанные судебные даты проверь по каждому номеру отдельно: "
                        "дату судебного приказа не теряй, а дату текущего постановления/"
                        "заседания не переноси в дату дела или исполнительного производства "
                        "без явного указания в источнике. "
                        "неоднозначные оставь null с warning/low_confidence_fields. "
                        "Не подставляй реквизиты получателя или другого документа.",
                    },
                    pages,
                    ExtractionDraft,
                    validate_facts,
                    emit,
                    schema=schema,
                )
            if date_reading is not None:
                facts.warnings = list(dict.fromkeys([*facts.warnings, *date_reading.warnings]))
            return facts.facts()
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
                ExtractionDraft,
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
                    ExtractionDraft,
                    lambda _: None,
                    emit,
                    schema=schema,
                )
                reduced.append({"facts": partial.model_dump(mode="json")})
            evidence = reduced
        result = await self._validated(
            job.prompts["extraction"],
            {
                **context,
                "window_evidence": evidence,
                "instruction": "Согласуй факты по всему комплекту. Удали дубли перекрытий, "
                "сохрани конфликты в warnings и все приложения проверенного плана.",
            },
            [],
            ExtractionDraft,
            lambda p: validate_extraction(p, group, job.organization_hint),
            emit,
            schema=schema,
        )
        return result.facts()

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
                    await emit("stage", {"stage": "segmentation_vlm", "progress": 35})
                    try:
                        async with asyncio.timeout(self.settings.vlm_segmentation_timeout_seconds):
                            # Границы определяются изображениями VLM. OCR нужен для реквизитов
                            # после фиксации состава; review не расходует OCR на всю пачку.
                            plan = await self._plan(job, pages, emit)
                        if job.auto_split:
                            validate_plan(plan, job.page_refs())
                    except TimeoutError:
                        await self.repository.fallback_pages(
                            job.id, job.fence, "segmentation_timeout"
                        )
                        return
                    except IntakeError as exc:
                        if exc.code not in {
                            "invalid_segmentation",
                            "schema_validation_failed",
                            "model_invalid_json",
                            "model_output_limit",
                        }:
                            raise
                        await self.repository.fallback_pages(job.id, job.fence, exc.code)
                        return
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
            if job.manifest.get("segmentation_fallback"):
                plan_warnings = [
                    *plan_warnings,
                    "Авторазделение не подтверждено; использована проверенная пользователем "
                    "постраничная разметка",
                ]
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
                facts.one_c = project_one_c(facts, job.organization_hint)
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
