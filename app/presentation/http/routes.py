# app/presentation/http/routes.py
"""Независимый REST API для web/1С/Telegram; handlers вызывают общие use-cases."""

import asyncio
import hashlib
import json
from typing import Annotated, Literal
from urllib.parse import quote

from fastapi import APIRouter, Form, Header, Query, Request, UploadFile
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field
from starlette.formparsers import MultiPartException

from app.domain.boundaries import ExcludedPage, ManualDocument, ManualPlan
from app.domain.errors import IntakeError
from app.domain.jobs import TERMINAL, utcnow
from app.domain.schema import BatchResult
from app.presentation.http.dependencies import OptionalOwner, Owner, Services, authenticate
from app.presentation.http.multipart import BoundedMultipartParser


class LimitedMultipartRoute(APIRoute):
    """Авторизует загрузку до чтения файлов и задаёт parser limits из общих Settings."""

    def get_route_handler(self):
        """Оборачивает только multipart parsing; сценарии и REST-контракты не дублирует."""
        original = super().get_route_handler()

        async def handle(request: Request):
            """Ограничивает число файлов/полей до создания spool большого multipart."""
            if request.headers.get("content-type", "").startswith("multipart/form-data"):
                owner = await authenticate(request, request.headers.get("x-api-key"))
                if not owner:
                    raise IntakeError("access_denied", "Требуется API-ключ или web-сессия", 401)
                settings = request.app.state.container.settings
                try:
                    # Закреплённый Starlette сохраняет разобранную форму в этом Request cache.
                    storage = request.app.state.container.storage
                    request._form = await BoundedMultipartParser(
                        request.headers,
                        request.stream(),
                        settings,
                        storage.temporary_upload,
                        storage.reserve_upload,
                    ).parse()
                except MultiPartException as exc:
                    raise IntakeError("invalid_request", "Некорректная multipart-загрузка") from exc
            return await original(request)

        return handle


router = APIRouter(prefix="/api/v1", tags=["Документы"], route_class=LimitedMultipartRoute)


class PromptBody(BaseModel):
    """Два сессионных текста; технические контракты через UI не изменяются."""

    model_config = ConfigDict(extra="forbid")
    segmentation: str
    extraction: str


class ModeBody(BaseModel):
    """Явная смена режима с optimistic concurrency; owner никогда не принимается из body."""

    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=0)
    auto_split: bool
    skip_segmentation: bool


class DraftBody(BaseModel):
    """Физические страницы draft; action используется только для минимального аудита."""

    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=0)
    documents: list[ManualDocument] = Field(max_length=10000)
    excluded_pages: list[ExcludedPage] = Field(default_factory=list, max_length=10000)
    action: Literal[
        "edit", "split", "merge", "boundary_move", "exclude", "restore", "undo", "reset", "manual"
    ] = "edit"


class ConfirmBody(BaseModel):
    """Повтор одной подтверждённой ревизии не создаёт второе выполнение."""

    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=0)


async def upload_request(
    container,
    owner: str,
    files: list[UploadFile],
    organization: str | None,
    key: str | None,
    start: bool,
    auto_split: bool = True,
    skip_segmentation: bool = False,
) -> JSONResponse:
    """Передаёт transport streams общему сценарию и гарантированно закрывает multipart spools."""
    try:
        job = await container.intake.upload(
            owner, files, organization, key, start, auto_split, skip_segmentation
        )
        return JSONResponse(jsonable_encoder(container.exports.status(job)), status_code=202)
    finally:
        for upload in files:
            await upload.close()


@router.post("/jobs", status_code=202, summary="Загрузить файлы и запустить анализ")
async def submit(
    container: Services,
    owner: Owner,
    files: list[UploadFile],
    organization_hint: Annotated[str | None, Form()] = None,
    idempotency_key: Annotated[str | None, Header()] = None,
    auto_split: Annotated[bool, Form()] = True,
    skip_segmentation: Annotated[bool, Form()] = False,
):
    """Возвращает 202 сразу после загрузки; VLM работает в независимом worker."""
    return await upload_request(
        container,
        owner,
        files,
        organization_hint,
        idempotency_key,
        True,
        auto_split,
        skip_segmentation,
    )


@router.post("/uploads", status_code=202, summary="Сохранить пакет до запуска анализа")
async def upload(
    container: Services,
    owner: Owner,
    files: list[UploadFile],
    organization_hint: Annotated[str | None, Form()] = None,
    idempotency_key: Annotated[str | None, Header()] = None,
    auto_split: Annotated[bool, Form()] = True,
    skip_segmentation: Annotated[bool, Form()] = False,
):
    """Сохраняет оригиналы и ставит подготовку превью в очередь без вызова OCR/VLM."""
    return await upload_request(
        container,
        owner,
        files,
        organization_hint,
        idempotency_key,
        False,
        auto_split,
        skip_segmentation,
    )


@router.post("/jobs/{job_id}/start", status_code=202)
async def start(job_id: str, container: Services, owner: Owner):
    """Запускает сохранённый пакет со снимками двух текущих промптов."""
    return container.exports.status(await container.intake.start(job_id, owner))


@router.get("/jobs/{job_id}")
@router.get("/jobs/{job_id}/snapshot")
async def status(job_id: str, container: Services, owner: Owner, include_pages: bool = True):
    """Отдаёт согласованный snapshot/cursor без OCR, prompts и локальных путей."""
    return container.exports.status(await container.intake.get(job_id, owner), include_pages)


@router.get("/jobs/{job_id}/pages")
async def pages(
    job_id: str,
    container: Services,
    owner: Owner,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=120)] = 60,
):
    """Отдаёт ограниченную страницу физического реестра без оригинальных PDF/base64."""
    return container.exports.page_slice(await container.intake.get(job_id, owner), offset, limit)


@router.get("/jobs/{job_id}/segmentation")
async def segmentation(job_id: str, container: Services, owner: Owner):
    """Публикует авторизованному owner черновик, предложение и подтверждённую ревизию."""
    return container.exports.segmentation(await container.intake.get(job_id, owner))


@router.patch("/jobs/{job_id}/mode")
async def change_mode(job_id: str, body: ModeBody, container: Services, owner: Owner):
    """Передаёт переключение режима общему сценарию, не создавая инфраструктуру в handler."""
    job = await container.boundaries.mode(
        job_id, owner, body.expected_revision, body.auto_split, body.skip_segmentation
    )
    return container.exports.segmentation(job)


@router.put("/jobs/{job_id}/segmentation")
async def save_segmentation(job_id: str, body: DraftBody, container: Services, owner: Owner):
    """Сохраняет разметку и manual-режим одной серверной операцией."""
    plan = ManualPlan(documents=body.documents, excluded_pages=body.excluded_pages)
    job = await container.boundaries.save(job_id, owner, body.expected_revision, plan, body.action)
    return container.exports.segmentation(job)


@router.post("/jobs/{job_id}/segmentation/confirm", status_code=202)
async def confirm_segmentation(job_id: str, body: ConfirmBody, container: Services, owner: Owner):
    """Подтверждает сохранённые границы, сохраняя идемпотентность по ревизии."""
    return container.exports.status(
        await container.boundaries.confirm(job_id, owner, body.expected_revision)
    )


@router.get("/jobs/{job_id}/result", response_model=BatchResult)
async def result(job_id: str, container: Services, owner: Owner):
    """Возвращает проверенный JSON 1.0 с настоящими временными download URLs."""
    return await container.exports.result(job_id, owner)


@router.post("/jobs/{job_id}/cancel")
async def cancel(job_id: str, container: Services, owner: Owner):
    """Идемпотентно завершает отмену и отзывает lease worker."""
    return container.exports.status(await container.intake.cancel(job_id, owner))


@router.get("/session/prompts", tags=["Промпты"])
async def prompts(container: Services, owner: Owner):
    """Позволяет прочитать оба промпта без загрузки документа и запуска анализа."""
    return await container.prompts.get(owner)


@router.put("/session/prompts", tags=["Промпты"])
async def save_prompts(body: PromptBody, container: Services, owner: Owner):
    """Сохраняет два независимых текста только в текущую сессию/интеграцию."""
    return await container.prompts.save(owner, body.model_dump())


@router.post("/session/prompts/{stage}/restore", tags=["Промпты"])
async def restore_prompts(stage: str, container: Services, owner: Owner):
    """Восстанавливает базовую версию одной вкладки."""
    return await container.prompts.restore(owner, stage)


async def download(
    container,
    job_id: str,
    kind: str,
    item_id: str,
    owner: str | None,
    token: str | None,
    page: int | None = None,
) -> StreamingResponse:
    """Проверяет capability/owner/TTL и отдаёт целый файл с checksum и byte length."""
    job, artifact = await container.exports.artifact(job_id, kind, item_id, owner, token, page)
    path = container.storage.resolve(artifact["path"])
    if not path.is_file():
        raise IntakeError("artifact_not_found", "Файл недоступен", 404)
    size = artifact.get("byte_size", path.stat().st_size)
    digest = artifact.get("sha256")
    if not digest:

        def checksum() -> str:
            """Вычисляет хеш рендера без блокировки event loop."""
            with path.open("rb") as stream:
                return hashlib.file_digest(stream, "sha256").hexdigest()

        digest = await asyncio.to_thread(checksum)

    async def chunks():
        """Проверяет TTL на каждом блоке и закрывает descriptor при disconnect/очистке."""
        with path.open("rb") as stream:
            while not job.expires_at or utcnow() < job.expires_at:
                chunk = await asyncio.to_thread(stream.read, 256 * 1024)
                if not chunk:
                    break
                yield chunk

    disposition = "inline" if kind == "preview" else "attachment"
    headers = {
        "Content-Length": str(size),
        "X-Content-SHA256": digest,
        "ETag": f'"{digest}"',
        "Content-Disposition": f"{disposition}; filename*=UTF-8''{quote(artifact['file_name'])}",
        "Cache-Control": "private, no-store",
        "Referrer-Policy": "no-referrer",
    }
    if job.expires_at:
        headers["X-Expires-At"] = job.expires_at.isoformat()
    return StreamingResponse(chunks(), media_type=artifact["mime_type"], headers=headers)


@router.get("/jobs/{job_id}/source-files/{file_id}")
async def source_file(
    job_id: str, file_id: str, container: Services, owner: OptionalOwner, token: str | None = None
):
    """Отдаёт сохранённый неизменённый оригинал по owner либо подписи этого файла."""
    return await download(container, job_id, "source", file_id, owner, token)


@router.get("/jobs/{job_id}/documents/{document_id}/file")
async def document_file(
    job_id: str,
    document_id: str,
    container: Services,
    owner: OptionalOwner,
    token: str | None = None,
):
    """Отдаёт один готовый PDF со всей основной частью и приложениями."""
    return await download(container, job_id, "document", document_id, owner, token)


@router.get("/jobs/{job_id}/pages/{file_id}/{page}/preview")
async def preview(
    job_id: str,
    file_id: str,
    page: int,
    container: Services,
    owner: OptionalOwner,
    token: str | None = None,
):
    """Отдаёт безопасный PNG-рендер ограниченной геометрии."""
    return await download(container, job_id, "preview", file_id, owner, token, page)


@router.get("/jobs/{job_id}/pages/{file_id}/{page}/thumbnail")
async def thumbnail(
    job_id: str,
    file_id: str,
    page: int,
    container: Services,
    owner: OptionalOwner,
    token: str | None = None,
):
    """Отдаёт небольшой PNG ленты; подпись отдельна от ссылки основного просмотра."""
    return await download(container, job_id, "thumbnail", file_id, owner, token, page)


@router.get("/jobs/{job_id}/documents/{document_id}/manifest")
async def document_manifest(job_id: str, document_id: str, container: Services, owner: Owner):
    """Отдаёт provenance сквозного комплекта без расширения JSON 1.0."""
    job = await container.intake.get(job_id, owner)
    document = job.manifest.get("documents", {}).get(document_id)
    if not document:
        raise IntakeError("artifact_not_found", "Комплект не найден", 404)
    return {
        "document_id": document_id,
        "pages": document["pages"],
        "group": document["group"],
        "sha256": document["sha256"],
        "byte_size": document["byte_size"],
        "expires_at": job.expires_at,
        "excluded_pages": [p.model_dump(mode="json") for p in job.approved_plan.excluded_pages]
        if job.approved_plan
        else [],
    }


@router.get("/jobs/{job_id}/events")
async def events(
    job_id: str,
    request: Request,
    container: Services,
    owner: Owner,
    after: Annotated[int, Query(ge=0)] = 0,
    last_event_id: Annotated[int | None, Header(ge=0)] = None,
    include_pages: bool = True,
):
    """SSE с replay; raw text_delta явно неподтверждён и не заменяет /result."""
    await container.intake.get(job_id, owner)

    async def stream():
        """Сохраняет cursor и повторно проверяет owner/TTL на протяжении соединения."""
        cursor = max(after, last_event_id or 0)
        while not await request.is_disconnected():
            try:
                job = await container.intake.get(job_id, owner)
            except IntakeError:
                yield 'event: expired\ndata: {"status":"expired"}\n\n'
                return
            batch = await container.database.events(job_id, cursor)
            for item in batch:
                cursor = item["id"]
                data = container.exports.event_data(job, item["event"], item["data"], include_pages)
                payload = json.dumps(data, ensure_ascii=False)
                yield f"id: {cursor}\nevent: {item['event']}\ndata: {payload}\n\n"
            if job.status in TERMINAL and cursor >= job.cursor:
                return
            if not batch:
                yield ": heartbeat\n\n"
                await asyncio.sleep(2)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )
