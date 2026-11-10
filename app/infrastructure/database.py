# app/infrastructure/database.py
"""PostgreSQL: атомарные переходы, outbox, lease/fencing и монотонный SSE-журнал."""

from datetime import timedelta

from sqlalchemy import JSON, DateTime, Integer, String, UniqueConstraint, delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.domain.errors import IntakeError, LeaseLost
from app.domain.jobs import TERMINAL, Job, utcnow
from app.infrastructure.boundary_transactions import BoundaryTransactions


class Base(DeclarativeBase):
    """Общая метаинформация только собственных таблиц приложения."""


class JobRow(Base):
    """Агрегат задания; индексируемые поля обновляются вместе с JSON-снимком."""

    __tablename__ = "jobs"
    __table_args__ = (UniqueConstraint("owner", "idempotency_key"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner: Mapped[str] = mapped_column(String(160), index=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(128))
    digest: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), index=True)
    data: Mapped[dict] = mapped_column(JSON)
    lease_until: Mapped[object | None] = mapped_column(DateTime(timezone=True), nullable=True)


class EventRow(Base):
    """Событие с локальным монотонным номером внутри задания."""

    __tablename__ = "events"
    job_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    sequence: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(40))
    data: Mapped[dict] = mapped_column(JSON)


class SignalRow(Base):
    """Транзакционная очередь публикации; RabbitMQ не является источником состояния."""

    __tablename__ = "outbox"
    job_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    next_at: Mapped[object] = mapped_column(DateTime(timezone=True), index=True)


class SessionRow(Base):
    """Изолированная анонимная сессия и два редактируемых промпта."""

    __tablename__ = "sessions"
    owner: Mapped[str] = mapped_column(String(160), primary_key=True)
    prompts: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    expires_at: Mapped[object] = mapped_column(DateTime(timezone=True), index=True)


class LimitRow(Base):
    """Общий для всех API-процессов счётчик запросов в минутном окне."""

    __tablename__ = "rate_limits"
    owner: Mapped[str] = mapped_column(String(160), primary_key=True)
    window: Mapped[int] = mapped_column(Integer)
    count: Mapped[int] = mapped_column(Integer)


class Database(BoundaryTransactions):
    """Реализует порты заданий/промптов и технические операции фоновых процессов."""

    def __init__(self, engine: AsyncEngine, settings) -> None:
        """Подключает собственный engine и лимиты; соединения создаются по операции."""
        self.engine = engine
        self.sessions = async_sessionmaker(engine, expire_on_commit=False)
        self.settings = settings

    async def _serialize_admission(self, session) -> None:
        """Сериализует admission/idempotency между API-процессами PostgreSQL."""
        if self.engine.dialect.name == "postgresql":
            await session.execute(text("SELECT pg_advisory_xact_lock(842693015)"))
        elif self.engine.dialect.name == "sqlite" and not session.info.get("writer_lock"):
            # SQLite не имеет SELECT FOR UPDATE; короткая write-транзакция нужна dev/tests.
            await session.execute(text("BEGIN IMMEDIATE"))
            session.info["writer_lock"] = True

    async def _locked(self, session, job_id: str) -> tuple[JobRow, Job]:
        """Берёт короткую блокировку агрегата; VLM/файловое I/O под ней запрещено."""
        if self.engine.dialect.name == "sqlite":
            await self._serialize_admission(session)
        row = await session.scalar(select(JobRow).where(JobRow.id == job_id).with_for_update())
        if row is None:
            raise IntakeError("job_not_found", "Задание не найдено", 404)
        return row, Job.model_validate(row.data)

    def _write(self, session, row: JobRow, job: Job, kind: str, data: dict) -> None:
        """Публикует снимок и событие одной транзакцией с новым cursor."""
        job.cursor += 1
        row.status = job.status
        row.data = job.model_dump(mode="json")
        session.add(EventRow(job_id=job.id, sequence=job.cursor, kind=kind, data=data))

    def _terminal(self, session, row: JobRow, job: Job, status: str, error: dict | None) -> None:
        """Завершает задание и начинает единый часовой TTL для всех данных."""
        now = utcnow()
        job.status = status
        job.stage = status
        job.completed_at = now
        job.expires_at = now + timedelta(seconds=self.settings.retention_seconds)
        job.error = error
        row.lease_until = None
        self._write(session, row, job, "terminal", {"status": status, "error": error})

    def _assert_worker(self, row: JobRow, job: Job, fence: int) -> None:
        """Проверяет активное поколение как анализа, так и отдельной подготовки страниц."""
        active = job.status == "running" or (
            job.status == "uploaded" and job.work_kind == "prepare"
        )
        if not active or job.fence != fence or not self._lease_alive(row):
            raise LeaseLost()
        if job.deadline_at and job.deadline_at <= utcnow():
            raise LeaseLost()

    async def _drop_signal(self, session, job_id: str) -> None:
        """Удаляет только транзакционный сигнал текущего задания."""
        await session.execute(delete(SignalRow).where(SignalRow.job_id == job_id))

    async def _queue_work(self, session, row: JobRow, job: Job) -> None:
        """Планирует новый этап с отдельным бюджетом очереди, не создавая дубликат outbox."""
        queued = await session.scalar(
            select(func.count())
            .select_from(JobRow)
            .where(JobRow.status.in_(("queued", "running")), JobRow.id != job.id)
        )
        if queued >= self.settings.queue_max_depth:
            raise IntakeError("queue_full", "Очередь заполнена", 429)
        job.status, job.work_kind, job.stage = "queued", "analyze", "queued"
        job.attempt = 0
        job.expires_at = None
        job.queue_deadline_at = utcnow() + timedelta(seconds=self.settings.queue_timeout_seconds)
        job.deadline_at = job.queue_deadline_at
        signal = await session.get(SignalRow, job.id)
        if signal:
            signal.next_at = utcnow()
        else:
            session.add(SignalRow(job_id=job.id, next_at=utcnow()))

    async def _admit(self, session, owner: str) -> None:
        """Проверяет глобальную глубину и число активных заданий одного owner."""
        active = (
            select(func.count())
            .select_from(JobRow)
            .where(JobRow.status.in_(("uploaded", "queued", "running", "awaiting_confirmation")))
        )
        if await session.scalar(active) >= self.settings.queue_max_depth:
            raise IntakeError("queue_full", "Очередь заполнена; повторите позже", 429)
        owned = (
            select(func.count())
            .select_from(JobRow)
            .where(
                JobRow.owner == owner,
                JobRow.status.in_(("uploaded", "queued", "running", "awaiting_confirmation")),
            )
        )
        if await session.scalar(owned) >= self.settings.max_owner_active_jobs:
            raise IntakeError("queue_full", "Достигнут лимит активных заданий сессии", 429)

    async def create(self, job: Job, key: str | None, digest: str) -> tuple[Job, bool]:
        """Атомарно связывает owner/key/hash и создаёт запись outbox при запуске."""
        async with self.sessions.begin() as session:
            await self._serialize_admission(session)
            if key:
                row = await session.scalar(
                    select(JobRow).where(JobRow.owner == job.owner, JobRow.idempotency_key == key)
                )
                if row:
                    existing = Job.model_validate(row.data)
                    existing.authorize(job.owner)
                    if row.digest != digest:
                        raise IntakeError(
                            "idempotency_conflict", "Ключ уже использован для другого запроса", 409
                        )
                    return existing, False
            await self._admit(session, job.owner)
            if job.status == "queued" or job.work_kind == "prepare":
                job.queue_deadline_at = utcnow() + timedelta(
                    seconds=self.settings.queue_timeout_seconds
                )
                job.deadline_at = job.queue_deadline_at
            row = JobRow(
                id=job.id,
                owner=job.owner,
                digest=digest,
                idempotency_key=key,
                status=job.status,
                data={},
            )
            session.add(row)
            self._write(session, row, job, "created", {"status": job.status})
            if job.status == "queued" or job.work_kind == "prepare":
                session.add(SignalRow(job_id=job.id, next_at=utcnow()))
            return job, True

    async def get(self, job_id: str) -> Job:
        """Читает согласованный JSON-снимок, включая cursor и результат."""
        async with self.sessions() as session:
            row = await session.get(JobRow, job_id)
            if not row:
                raise IntakeError("job_not_found", "Задание не найдено", 404)
            return Job.model_validate(row.data)

    async def start(self, job_id: str, owner: str, prompts: dict[str, str]) -> Job:
        """Один раз фиксирует промпты и ставит upload в очередь транзакционно."""
        async with self.sessions.begin() as session:
            await self._serialize_admission(session)
            row, job = await self._locked(session, job_id)
            job.authorize(owner)
            if (
                job.segmentation_source in {"manual", "manual_override"}
                and job.approved_plan is None
                and job.status in {"uploaded", "awaiting_confirmation"}
            ):
                raise IntakeError(
                    "segmentation_confirmation_required", "Подтвердите сохранённую разметку", 409
                )
            if job.status != "uploaded":
                if job.status == "awaiting_confirmation":
                    raise IntakeError(
                        "segmentation_confirmation_required",
                        "Подтвердите текущую ревизию плана",
                        409,
                    )
                return job
            if not job.prompts:
                job.prompts = dict(prompts)
            job.processing_requested = True
            if job.work_kind == "prepare" and self._lease_alive(row):
                self._write(session, row, job, "analysis_requested", {"status": job.status})
                return job
            await self._queue_work(session, row, job)
            self._write(session, row, job, "queued", {"status": "queued"})
            return job

    async def cancel(self, job_id: str, owner: str) -> Job:
        """Отзывает fencing и завершает отмену без ожидания пользовательского решения."""
        async with self.sessions.begin() as session:
            row, job = await self._locked(session, job_id)
            job.authorize(owner)
            if job.status not in TERMINAL:
                job.fence += 1
                self._terminal(session, row, job, "cancelled", None)
                await session.execute(delete(SignalRow).where(SignalRow.job_id == job_id))
            return job

    async def claim(self, job_id: str) -> Job | None:
        """Выдаёт новый lease только queued; дубликаты доставки не запускают второй worker."""
        async with self.sessions.begin() as session:
            try:
                row, job = await self._locked(session, job_id)
            except IntakeError:
                return None
            if job.status != "queued" and not (
                job.status == "uploaded" and job.work_kind == "prepare" and not job.preview_complete
            ):
                return None
            if self._lease_alive(row):
                return None
            if job.deadline_at and job.deadline_at <= utcnow():
                self._terminal(
                    session,
                    row,
                    job,
                    "failed",
                    {"code": "job_timeout", "message": "Превышено время ожидания/обработки"},
                )
                return None
            job.fence += 1
            job.attempt += 1
            if job.work_kind != "prepare":
                job.status = "running"
            job.queue_deadline_at = None
            if not job.preview_complete:
                job.preview_deadline_at = job.preview_deadline_at or utcnow() + timedelta(
                    seconds=self.settings.preview_timeout_seconds
                )
                job.deadline_at = job.preview_deadline_at
            else:
                job.processing_deadline_at = job.processing_deadline_at or utcnow() + timedelta(
                    seconds=self.settings.job_timeout_seconds
                )
                job.deadline_at = job.processing_deadline_at
            row.lease_until = utcnow() + timedelta(seconds=self.settings.worker_lease_seconds)
            self._write(session, row, job, "started", {"attempt": job.attempt, "status": "running"})
            await session.execute(delete(SignalRow).where(SignalRow.job_id == job_id))
            return job

    async def heartbeat(self, job_id: str, fence: int) -> bool:
        """Продлевает lease только живого поколения в пределах общего deadline."""
        async with self.sessions.begin() as session:
            row, job = await self._locked(session, job_id)
            try:
                self._assert_worker(row, job, fence)
            except LeaseLost:
                return False
            row.lease_until = utcnow() + timedelta(seconds=self.settings.worker_lease_seconds)
            return True

    async def checkpoint(self, job_id: str, fence: int, kind: str, data: dict) -> None:
        """Сохраняет этап/рендеры/событие; устаревшему worker запись запрещена."""
        async with self.sessions.begin() as session:
            row, job = await self._locked(session, job_id)
            self._assert_worker(row, job, fence)
            if kind == "stage":
                job.stage, job.progress = data["stage"], data["progress"]
            elif kind == "normalized":
                job.manifest = data
                for source in job.sources:
                    source.page_count = data["source_counts"][source.file_id]
            elif kind == "source_discovered":
                counts = {
                    **job.manifest.get("source_counts", {}),
                    data["file_id"]: data["page_count"],
                }
                job.manifest = {**job.manifest, "source_counts": counts}
                for source in job.sources:
                    source.page_count = counts.get(source.file_id, source.page_count)
                job.plan_revision += 1
                if job.draft_plan is None or job.segmentation_source == "source_files":
                    from app.domain.boundaries import source_file_plan

                    job.draft_plan = source_file_plan(job.page_refs())
            elif kind == "page_ready":
                pages = job.manifest.get("pages", [])
                indexed = {(p["file_id"], p["page_number"]): p for p in pages}
                indexed.update({(p["file_id"], p["page_number"]): p for p in data["pages"]})
                job.manifest = {**job.manifest, "pages": list(indexed.values())}
            elif kind == "segmentation":
                job.manifest = {**job.manifest, "plan": data}
            self._write(session, row, job, kind, data)

    async def finish(
        self,
        job_id: str,
        fence: int,
        result: dict | None,
        manifest: dict,
        error: dict | None,
        warnings: bool,
    ) -> None:
        """Одной транзакцией публикует проверенный результат и терминальное событие."""
        async with self.sessions.begin() as session:
            row, job = await self._locked(session, job_id)
            self._assert_worker(row, job, fence)
            job.result = result
            job.manifest = manifest or job.manifest
            job.progress = 100 if result is not None else job.progress
            status = "failed" if error else ("completed_with_warnings" if warnings else "completed")
            self._terminal(session, row, job, status, error)

    def _lease_alive(self, row: JobRow) -> bool:
        """Истёкший lease нельзя оживить heartbeat или использовать до запуска recovery."""
        lease = row.lease_until
        if lease is None:
            return False
        if lease.tzinfo is None:
            lease = lease.replace(tzinfo=utcnow().tzinfo)
        return lease > utcnow()

    async def ensure_session(self, owner: str) -> None:
        """Регистрирует анонимную сессию в PostgreSQL без хранения cookie в открытом виде."""
        async with self.sessions.begin() as session:
            await self._serialize_admission(session)
            if not await session.get(SessionRow, owner):
                session.add(
                    SessionRow(
                        owner=owner,
                        prompts=None,
                        expires_at=utcnow() + timedelta(seconds=self.settings.session_ttl_seconds),
                    )
                )

    async def session_active(self, owner: str) -> bool:
        """Проверяет существование и TTL серверной части анонимной сессии."""
        async with self.sessions() as session:
            row = await session.get(SessionRow, owner)
            if row is None:
                return False
            expires = row.expires_at
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=utcnow().tzinfo)
            return expires > utcnow()

    async def events(self, job_id: str, after: int) -> list[dict]:
        """Читает ограниченную страницу журнала, поддерживая Last-Event-ID replay."""
        async with self.sessions() as session:
            rows = await session.scalars(
                select(EventRow)
                .where(EventRow.job_id == job_id, EventRow.sequence > after)
                .order_by(EventRow.sequence)
                .limit(100)
            )
            return [{"id": r.sequence, "event": r.kind, "data": r.data} for r in rows]

    async def pending(self) -> list[str]:
        """Читает outbox; повторная доставка разрешена и безопасна благодаря claim."""
        async with self.sessions() as session:
            return list(
                await session.scalars(
                    select(SignalRow.job_id)
                    .where(SignalRow.next_at <= utcnow())
                    .order_by(SignalRow.next_at)
                    .limit(20)
                )
            )

    async def published(self, job_id: str) -> None:
        """Оставляет durable сигнал до claim; подтверждённая публикация отложена на 30 секунд."""
        async with self.sessions.begin() as session:
            row = await session.get(SignalRow, job_id, with_for_update=True)
            if row:
                row.next_at = utcnow() + timedelta(seconds=30)

    async def recover(self) -> list[str]:
        """Восстанавливает зависшие lease, ограничивает retry и возвращает истёкшие job ids."""
        expired = []
        async with self.sessions.begin() as session:
            if self.engine.dialect.name == "sqlite":
                await self._serialize_admission(session)
            rows = await session.scalars(
                select(JobRow)
                .where(
                    JobRow.status.in_(
                        ("running", "queued", "uploaded", "awaiting_confirmation", *TERMINAL)
                    )
                )
                .with_for_update(skip_locked=True)
            )
            now = utcnow()
            for row in rows:
                job = Job.model_validate(row.data)
                preparing = (
                    job.status == "uploaded"
                    and job.work_kind == "prepare"
                    and not job.preview_complete
                )
                if job.status in {"running", "queued"} or preparing:
                    deadline = job.deadline_at and job.deadline_at <= now
                    lease = row.lease_until
                    if lease and lease.tzinfo is None:
                        lease = lease.replace(tzinfo=now.tzinfo)
                    stale = (job.status == "running" and (lease is None or lease <= now)) or (
                        preparing and lease is not None and lease <= now
                    )
                    if deadline or (stale and job.attempt >= self.settings.worker_max_attempts):
                        job.fence += 1
                        self._terminal(
                            session,
                            row,
                            job,
                            "failed",
                            {
                                "code": "job_timeout" if deadline else "worker_attempts_exhausted",
                                "message": "Исчерпан лимит времени или восстановлений worker",
                            },
                        )
                        await session.execute(delete(SignalRow).where(SignalRow.job_id == job.id))
                    elif stale:
                        job.fence += 1
                        job.status = "uploaded" if job.work_kind == "prepare" else "queued"
                        row.lease_until = None
                        self._write(session, row, job, "retry", {"attempt": job.attempt})
                        if not await session.get(SignalRow, job.id):
                            session.add(SignalRow(job_id=job.id, next_at=now))
                elif job.expires_at and job.expires_at <= now:
                    expired.append(job.id)
            await session.execute(delete(SessionRow).where(SessionRow.expires_at <= now))
            await session.execute(
                delete(LimitRow).where(LimitRow.window < int(now.timestamp()) // 60 - 2)
            )
        return expired

    async def purge(self, job_id: str) -> None:
        """Удаляет DB payload только после подтверждённого TTL и физической очистки."""
        async with self.sessions.begin() as session:
            row, job = await self._locked(session, job_id)
            if (
                not job.expires_at
                or job.expires_at > utcnow()
                or job.status in {"running", "queued"}
                or (
                    job.status == "uploaded"
                    and job.work_kind == "prepare"
                    and not job.preview_complete
                )
            ):
                return
            for table in (EventRow, SignalRow):
                await session.execute(delete(table).where(table.job_id == job_id))
            await session.delete(row)

    async def get_prompts(self, owner: str) -> dict[str, str] | None:
        """Читает только текущую сессию; истёкшие overrides игнорируются."""
        async with self.sessions() as session:
            row = await session.get(SessionRow, owner)
            if row:
                expires = row.expires_at
                if expires.tzinfo is None:
                    expires = expires.replace(tzinfo=utcnow().tzinfo)
                return row.prompts if expires > utcnow() else None
            return None

    async def save_prompts(self, owner: str, prompts: dict[str, str]) -> None:
        """Сохраняет оба текста с ограниченным сроком жизни сессии."""
        async with self.sessions.begin() as session:
            await self._serialize_admission(session)
            row = await session.get(SessionRow, owner, with_for_update=True)
            if not row:
                row = SessionRow(owner=owner)
                session.add(row)
            row.prompts = prompts
            row.expires_at = utcnow() + timedelta(seconds=self.settings.session_ttl_seconds)

    async def throttle(self, owner: str) -> None:
        """Применяет общий минутный лимит без локального бесконтрольного словаря."""
        async with self.sessions.begin() as session:
            await self._serialize_admission(session)
            row = await session.get(LimitRow, owner, with_for_update=True)
            window = int(utcnow().timestamp()) // 60
            if not row:
                row = LimitRow(owner=owner, window=window, count=0)
                session.add(row)
            if row.window != window:
                row.window, row.count = window, 0
            if row.count >= self.settings.rate_limit_per_minute:
                raise IntakeError("rate_limit", "Слишком много запросов", 429)
            row.count += 1

    async def ping(self) -> None:
        """Проверяет настоящий доступ к собственной БД без изменения данных."""
        async with self.sessions() as session:
            await session.execute(text("SELECT 1"))
