# app/presentation/http/contracts.py
"""Публичные схемы статусов/редактора для Swagger; приватные пути и prompts в них отсутствуют."""

from datetime import datetime
from typing import Literal

from pydantic import Field

from app.domain.boundaries import ApprovedPlan, ExcludedPage, FixedGroup, ManualPlan
from app.domain.comparisons import Comparison
from app.domain.document_processing import DocumentProcessing
from app.domain.jobs import JobStatus, WorkProgress
from app.domain.schema import StrictModel
from app.domain.segmentation import PageRef


class ErrorDetail(StrictModel):
    """Безопасный код и сообщение без отражения payload/credentials."""

    code: str
    message: str


class ErrorResponse(StrictModel):
    """Единый envelope всех документированных HTTP-ошибок."""

    error: ErrorDetail


class PublicComparison(Comparison):
    """Состояние перехода без приватного отпечатка OCR/промпта."""

    fingerprint: str = Field("", exclude=True)


class ComparisonsView(StrictModel):
    """Фактические счётчики и решения соседних переходов."""

    total: int
    completed: int
    errors: int
    needs_review: bool
    transitions: list[PublicComparison]


class SourceView(StrictModel):
    """Сохранённый исходник без внутреннего filesystem path."""

    file_id: str
    file_name: str
    mime_type: str
    byte_size: int
    sha256: str
    page_count: int
    file_url: str


class ArtifactView(StrictModel):
    """Скачиваемый готовый PDF с проверкой целостности и статусом распознавания."""

    document_id: str
    file_name: str
    byte_size: int
    sha256: str
    file_url: str
    processing: DocumentProcessing | None


class PreviewView(PageRef):
    """Физический адрес готового рендера."""

    preview_url: str


class ProcessingPlan(StrictModel):
    """Готовый состав PDF и приложений с сохранёнными физическими адресами."""

    documents: list[FixedGroup]
    unassigned_pages: list[PageRef]
    warnings: list[str]


class ModeView(StrictModel):
    """Явный режим без серверных догадок по отсутствующим флагам."""

    auto_split: bool
    skip_segmentation: bool


class StatusView(StrictModel):
    """Полный атомарный статус без долгого HTTP ожидания OCR/VLM."""

    job_id: str
    status: JobStatus
    stage: str
    progress: int
    work_stage: str | None
    document_processing: dict[str, DocumentProcessing]
    work_progress: dict[str, WorkProgress]
    created_at: datetime
    completed_at: datetime | None
    expires_at: datetime | None
    cursor: int
    error: ErrorDetail | None
    organization_hint: str | None
    comparisons: ComparisonsView
    sources: list[SourceView]
    pages: list[PreviewView]
    artifacts: list[ArtifactView]
    plan: ProcessingPlan | ApprovedPlan | ManualPlan | None
    mode: ModeView
    segmentation_source: str
    plan_revision: int
    plan_locked: bool
    awaiting_confirmation: bool
    preview_complete: bool
    pages_total: int
    pages_ready: int
    deadlines: dict[str, datetime | None]
    counts: dict[str, int]
    links: dict[str, str]


class SegmentationView(StrictModel):
    """Читаемый/редактируемый план с optimistic revision и условиями confirm."""

    auto_split: bool
    skip_segmentation: bool
    segmentation_source: str
    plan_revision: int
    approved_revision: int | None
    locked: bool
    preview_complete: bool
    can_confirm: bool
    assigned_count: int
    excluded_count: int
    unassigned_count: int
    document_count: int
    proposed_plan: ManualPlan | None
    draft_plan: ManualPlan | None
    approved_plan: ApprovedPlan | None
    initial_plan: ManualPlan
    comparisons: ComparisonsView


class PageMetadata(PageRef):
    """Ленивая порция рендеров в исходном физическом порядке."""

    global_position: int
    source_order: int
    render_status: Literal["ready", "pending"]
    preview_url: str | None
    thumbnail_url: str | None


class PageSlice(StrictModel):
    """Метаданные порции без передачи всех изображений большой пачки."""

    pages: list[PageMetadata]
    offset: int
    limit: int
    total: int
    preview_complete: bool
    ready_count: int


class JobListItem(StrictModel):
    """Безопасная краткая история одного owner."""

    job_id: str
    status: JobStatus
    stage: str
    created_at: datetime
    completed_at: datetime | None
    expires_at: datetime | None
    plan_revision: int
    files: list[str]
    document_count: int


class JobList(StrictModel):
    """Ограниченная пагинация только своих действующих заданий."""

    jobs: list[JobListItem]
    offset: int
    limit: int
    has_more: bool


class PromptView(StrictModel):
    """Два owner-промпта и их базовые версии, без чужих overrides."""

    prompts: dict[str, str]
    base_prompts: dict[str, str]
    versions: dict[str, str]


class ArchiveView(StrictModel):
    """Готовый ZIP без внутреннего пути хранения."""

    file_name: str
    byte_size: int
    sha256: str
    file_url: str


class DocumentManifest(StrictModel):
    """Физический состав PDF-комплекта без внутренних путей и фактов OCR."""

    document_id: str
    pages: list[PageRef]
    group: FixedGroup
    sha256: str
    byte_size: int
    expires_at: datetime | None
    excluded_pages: list[ExcludedPage]


ERROR_RESPONSES = {
    code: {
        "model": ErrorResponse,
        "description": description,
        "content": {
            "application/json": {
                "example": {
                    "error": {
                        "code": {
                            401: "access_denied",
                            403: "access_denied",
                            404: "job_not_found",
                            408: "upload_timeout",
                            409: "plan_version_conflict",
                            410: "job_expired",
                            413: "upload_limit",
                            422: "invalid_request",
                            429: "rate_limit",
                            503: "service_unavailable",
                            507: "storage_insufficient",
                        }[code],
                        "message": description,
                    }
                }
            }
        },
    }
    for code, description in {
        401: "Требуется корректный X-API-Key либо web-сессия",
        403: "Не подтверждены Origin/CSRF или доступ к файлу",
        404: "Объект не найден либо принадлежит другому owner",
        409: "Конфликт revision, план locked либо результат ещё не готов",
        410: "Срок хранения истёк: ссылки и файлы недоступны",
        408: "Истёк ограниченный срок приёма запроса",
        413: "Превышен лимит загрузки",
        422: "Неверный формат запроса или нарушение контракта страниц",
        429: "Исчерпан бюджет запросов; учитывать Retry-After",
        503: "Зависимость/сервис временно недоступны",
        507: "Недостаточно свободного места для загрузки/сборки файла",
    }.items()
}


def file_responses(mime: str) -> dict:
    """Документирует потоковый файл и реальные заголовки целостности/TTL."""
    return {
        200: {
            "description": "Поток файла по owner либо временной подписанной ссылке",
            "content": {mime: {"schema": {"type": "string", "format": "binary"}}},
            "headers": {
                "Content-Disposition": {"schema": {"type": "string"}},
                "Content-Length": {"schema": {"type": "integer"}},
                "X-Content-SHA256": {"schema": {"type": "string", "pattern": "^[0-9a-f]{64}$"}},
                "X-Expires-At": {"schema": {"type": "string", "format": "date-time"}},
            },
        }
    }
