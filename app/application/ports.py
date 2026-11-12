# app/application/ports.py
"""Маленькие порты сценариев; в контрактах нет HTTP, SQL и конкретного VLM-клиента."""

from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Protocol

from app.domain.boundaries import ManualPlan
from app.domain.jobs import Job, Source
from app.domain.segmentation import PageRef, Plan

Emit = Callable[[str, dict], Awaitable[None]]


class AnalysisSettings(Protocol):
    """Только параметры VLM-оркестрации, необходимые application-слою."""

    model_attempts: int
    vlm_window_pages: int
    vlm_overlap_pages: int
    vlm_reconciliation_windows: int
    vlm_segmentation_timeout_seconds: int


class DownloadSigner(Protocol):
    """Подпись одного download capability без привязки application к HMAC-реализации."""

    def sign(self, path: str, owner: str, expires: datetime) -> str:
        """Создаёт ограниченную ссылку конкретного owner и артефакта."""
        ...

    def verify(
        self,
        path: str,
        owner: str,
        expires: datetime,
        token: str | None,
        allow_expired: bool = False,
    ) -> bool:
        """Проверяет scope подписи; expired нужен только для точной диагностики TTL."""
        ...


class Upload(Protocol):
    """Поток одного файла без привязки к FastAPI UploadFile."""

    filename: str | None
    content_type: str | None

    async def read(self, size: int = -1) -> bytes:
        """Читает ограниченную часть входного потока."""
        ...


class JobRepository(Protocol):
    """Хранит задания и атомарные переходы; транзакции не переживают VLM-вызовы."""

    async def create(self, job: Job, key: str | None, digest: str) -> tuple[Job, bool]:
        """Атомарно создаёт задание/outbox либо возвращает idempotency replay."""
        ...

    async def get(self, job_id: str) -> Job:
        """Возвращает приватный снимок либо предметную ошибку отсутствия."""
        ...

    async def start(self, job_id: str, owner: str, prompts: dict[str, str]) -> Job:
        """Снимает immutable snapshots и атомарно ставит загруженное задание в очередь."""
        ...

    async def cancel(self, job_id: str, owner: str) -> Job:
        """Идемпотентно отменяет задание и немедленно отзывает lease."""
        ...

    async def claim(self, job_id: str) -> Job | None:
        """Выдаёт новое fencing-поколение только доступному заданию."""
        ...

    async def heartbeat(self, job_id: str, fence: int) -> bool:
        """Продлевает действующий lease; false требует остановить обработку."""
        ...

    async def checkpoint(self, job_id: str, fence: int, kind: str, data: dict) -> None:
        """Сохраняет событие и прогресс при действующем fencing-токене."""
        ...

    async def finish(
        self,
        job_id: str,
        fence: int,
        result: dict | None,
        manifest: dict,
        error: dict | None,
        warnings: bool,
    ) -> None:
        """Атомарно публикует проверенный результат или терминальную ошибку и TTL."""
        ...

    async def events(self, job_id: str, after: int) -> list[dict]:
        """Читает монотонный журнал для SSE replay."""
        ...

    async def complete_preview(self, job_id: str, fence: int, manifest: dict) -> Job:
        """Публикует готовность preview и отделяет ожидание сотрудника от рабочего lease."""
        ...

    async def propose(self, job_id: str, fence: int, plan: Plan) -> Job:
        """Сохраняет предложение VLM, не перезаписывая ручное решение нового поколения."""
        ...

    async def fallback_pages(self, job_id: str, fence: int, reason: str) -> Job:
        """Предлагает технические одиночные страницы только для обязательной ручной проверки."""
        ...

    async def lock_plan(self, job_id: str, fence: int) -> Job:
        """Фиксирует неизменяемое множество страниц непосредственно перед извлечением."""
        ...


class BoundaryRepository(Protocol):
    """Короткие транзакционные команды ручной разметки и подтверждения."""

    async def change_mode(
        self, job_id: str, owner: str, revision: int, auto_split: bool, skip_segmentation: bool
    ) -> Job:
        """Атомарно меняет режим с owner/revision/fence проверкой."""
        ...

    async def save_draft(
        self, job_id: str, owner: str, revision: int, plan: ManualPlan, action: str
    ) -> Job:
        """Сохраняет черновик и manual source одной операцией."""
        ...

    async def confirm(self, job_id: str, owner: str, revision: int, prompts: dict[str, str]) -> Job:
        """Идемпотентно утверждает ревизию и создаёт outbox продолжения."""
        ...


class PromptRepository(Protocol):
    """Изолированные настройки двух промптов для одного owner."""

    async def get_prompts(self, owner: str) -> dict[str, str] | None:
        """Читает сохранённые сессионные тексты без глобального изменения базы."""
        ...

    async def save_prompts(self, owner: str, prompts: dict[str, str]) -> None:
        """Сохраняет оба текста атомарно."""
        ...


class Storage(Protocol):
    """Приватные оригиналы и артефакты с проверкой физической целостности."""

    async def ingest(self, job_id: str, uploads: list[Upload]) -> list[Source]:
        """Принимает ограниченные потоки, проверяет сигнатуры и вычисляет SHA-256."""
        ...

    async def remove(self, job_id: str) -> None:
        """Идемпотентно удаляет данные только указанного собственного задания."""
        ...

    async def normalize(self, job: Job, emit: Emit) -> tuple[list[dict], dict]:
        """Возвращает упорядоченные страницы, рендеры и технический manifest."""
        ...

    async def pdf(self, job: Job, document_id: str, pages: list[PageRef]) -> dict:
        """Создаёт целый PDF-комплект, возвращает размер, хеш и приватный путь."""
        ...


class Vision(Protocol):
    """JSON/vision вызов общего VLM; модельные решения всегда непроверенные."""

    async def request(
        self, prompt: str, context: dict, pages: list[dict], schema: dict, emit: Emit
    ) -> dict:
        """Передаёт изображения и контекст, транслирует непроверенные text_delta."""
        ...


class OCR(Protocol):
    """Внешний OCR/layout/table adapter, не определяющий границы документов."""

    async def recognize(self, pages: list[dict], emit: Emit) -> list[dict]:
        """Обогащает страницы Docling-структурой, сохраняя глобальные адреса."""
        ...
