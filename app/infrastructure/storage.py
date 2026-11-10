# app/infrastructure/storage.py
"""Приватное файловое хранилище с ограниченной загрузкой и изолированной конвертацией."""

import asyncio
import hashlib
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from uuid import UUID, uuid4

from app.application.ports import Emit, Upload
from app.domain.errors import IntakeError
from app.domain.jobs import Job, Source
from app.domain.limits import UploadBudget
from app.domain.segmentation import PageRef
from app.infrastructure.conversion import stop_process, validate_signature


class FileStorage:
    """Каждое задание имеет отдельный каталог, каждый lease — отдельное поколение файлов."""

    def __init__(self, settings) -> None:
        """Фиксирует единственный разрешённый корень; не принимает пути из HTTP/VLM."""
        self.settings = settings
        self.root = settings.data_dir.resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def directory(self, job_id: str) -> Path:
        """Разрешает только UUID задания и гарантирует принадлежность пути хранилищу."""
        UUID(job_id)
        path = (self.root / job_id).resolve()
        if path.parent != self.root:
            raise IntakeError("access_denied", "Недопустимый путь", 403)
        return path

    def resolve(self, path: str) -> Path:
        """Проверяет приватный путь из DB manifest перед чтением файла."""
        result = Path(path).resolve()
        if not result.is_relative_to(self.root):
            raise IntakeError("access_denied", "Артефакт вне хранилища", 403)
        return result

    async def ingest(self, job_id: str, uploads: list[Upload]) -> list[Source]:
        """Потоково сохраняет оригиналы, считая размер и хеш без загрузки пачки в память."""
        if not 1 <= len(uploads) <= self.settings.upload_max_files:
            raise IntakeError("upload_limit", "Недопустимое число файлов")
        directory = self.directory(job_id)
        directory.mkdir(mode=0o700)
        budget = UploadBudget(
            self.settings.upload_max_file_mb * 1024 * 1024,
            self.settings.upload_max_mb * 1024 * 1024,
        )
        result = []
        for upload in uploads:
            budget.next_file()
            name = (upload.filename or "document").replace("\\", "/").split("/")[-1]
            if len(name) > 240 or any(ord(c) < 32 for c in name):
                raise IntakeError("invalid_filename", "Недопустимое имя файла")
            suffix = Path(name).suffix.lower()
            file_id = str(uuid4())
            path = directory / (file_id + suffix)
            digest, size = hashlib.sha256(), 0
            with path.open("xb") as stream:
                while chunk := await upload.read(1024 * 1024):
                    budget.consume(len(chunk))
                    size += len(chunk)
                    await asyncio.to_thread(self._disk_space, len(chunk))
                    digest.update(chunk)
                    await asyncio.to_thread(stream.write, chunk)
            mime = await asyncio.to_thread(validate_signature, path, suffix)
            supplied = upload.content_type
            if supplied and supplied not in {mime, "application/octet-stream"}:
                raise IntakeError("invalid_mime", "MIME не соответствует содержимому файла")
            result.append(
                Source(
                    file_id=file_id,
                    file_name=name,
                    mime_type=mime,
                    path=str(path),
                    byte_size=size,
                    sha256=digest.hexdigest(),
                )
            )
        return result

    def _disk_space(self, additional: int = 0) -> None:
        """Не расходует резерв файловой системы даже при корректном размере входного документа."""
        reserve = self.settings.storage_min_free_mb * 1024 * 1024
        if shutil.disk_usage(self.root).free < reserve + additional:
            raise IntakeError(
                "storage_insufficient", "Недостаточно свободного места в хранилище", 507
            )

    def temporary_upload(self):
        """Spool больших multipart идёт в private volume, а не в ограниченный RAM /tmp."""
        return tempfile.SpooledTemporaryFile(max_size=1024 * 1024, dir=self.root)

    async def reserve_upload(self, count: int) -> None:
        """Проверяет дисковый резерв до записи очередного multipart chunk."""
        await asyncio.to_thread(self._disk_space, count)

    async def remove(self, job_id: str) -> None:
        """Идемпотентно удаляет только собственный UUID-каталог без перехода по ссылкам."""
        directory = self.directory(job_id)
        if directory.exists():
            await asyncio.to_thread(shutil.rmtree, directory)

    async def _stop_converter(self, process) -> None:
        """После kill дренирует stdout: wait без чтения зависает при заполненном pipe."""
        await stop_process(process)

    async def normalize(self, job: Job, emit: Emit) -> tuple[list[dict], dict]:
        """Запускает конвертер отдельным процессом с deadline и независимым lease-каталогом."""
        directory = self.directory(job.id) / f"attempt-{job.fence}"
        directory.mkdir(mode=0o700, exist_ok=True)
        pages, counts = [], {}
        await asyncio.to_thread(self._disk_space)
        for source_order, source in enumerate(job.sources):
            request = directory / f"{source.file_id}.request.json"
            data = {
                "path": source.path,
                "target": str(directory / source.file_id),
                "settings": {
                    "timeout": self.settings.conversion_timeout_seconds,
                    "max_pages": min(
                        self.settings.upload_max_file_pages,
                        self.settings.upload_max_pages - len(pages),
                    ),
                    "max_pixels": self.settings.image_max_pixels,
                    "render_side": self.settings.render_max_side,
                    "min_free_bytes": self.settings.storage_min_free_mb * 1024 * 1024,
                },
            }
            request.write_text(json.dumps(data), "utf-8")
            process_environment = {
                key: os.environ[key]
                for key in ("PATH", "SystemRoot", "WINDIR")
                if key in os.environ
            }
            process_environment.update({"PYTHONDONTWRITEBYTECODE": "1", "LANG": "C.UTF-8"})
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "app.infrastructure.conversion",
                str(request),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                start_new_session=sys.platform != "win32",
                env=process_environment,
            )
            try:
                pending = []
                error_code = "corrupt_document"
                async with asyncio.timeout(self.settings.conversion_timeout_seconds + 5):
                    while line := await process.stdout.readline():
                        try:
                            event = json.loads(line)
                        except ValueError:
                            error_code = line.decode(errors="replace").strip()
                            continue
                        if event.get("event") == "source_discovered":
                            count = event["data"]["page_count"]
                            counts[source.file_id] = source.page_count = count
                            await emit(
                                "source_discovered",
                                {"file_id": source.file_id, "page_count": count},
                            )
                        elif event.get("event") == "page_ready":
                            item = {
                                **event["data"],
                                "file_id": source.file_id,
                                "source_order": source_order,
                                "global_position": len(pages) + 1,
                                "source_sha256": source.sha256,
                            }
                            pages.append(item)
                            pending.append(item)
                            if (
                                len(pending) >= 8
                                or len(pages) == 1
                                or item["page_number"] == counts[source.file_id]
                            ):
                                await emit("page_ready", {"pages": pending})
                                pending = []
                    await process.wait()
            except TimeoutError as exc:
                await self._stop_converter(process)
                raise IntakeError(
                    "preview_timeout", "Превышен срок подготовки исходного файла", 503
                ) from exc
            except BaseException:
                await self._stop_converter(process)
                raise
            if process.returncode:
                code = error_code
                allowed = {
                    "password_protected",
                    "page_limit",
                    "file_conversion_failed",
                    "invalid_mime",
                    "upload_limit",
                    "unsupported_file",
                    "storage_insufficient",
                }
                raise IntakeError(
                    code if code in allowed else "corrupt_document",
                    "Исходный документ не удалось безопасно преобразовать",
                )
            if source.file_id not in counts:
                raise IntakeError("corrupt_document", "Конвертер не сообщил реестр страниц")
        manifest = {
            "pages": pages,
            "source_counts": counts,
            "documents": {},
            "preview_complete": True,
        }
        await emit("normalized", manifest)
        return pages, manifest

    async def pdf(self, job: Job, document_id: str, pages: list[PageRef]) -> dict:
        """Материализует заблокированную группу, включая все её утверждённые страницы."""
        path = self.directory(job.id) / f"attempt-{job.fence}" / f"{document_id}.pdf"
        await asyncio.to_thread(path.parent.mkdir, mode=0o700, exist_ok=True)
        lookup = {(p["file_id"], p["page_number"]): p["pdf"] for p in job.manifest["pages"]}
        await self.reserve_upload(0)
        request = path.with_suffix(".request.json")
        payload = {
            "operation": "pdf_pages",
            "pages": [
                {
                    "pdf": str(self.resolve(lookup[(page.file_id, page.page_number)])),
                    "page_number": page.page_number,
                }
                for page in pages
            ],
            "target": str(path),
            "settings": {"timeout": self.settings.conversion_timeout_seconds},
        }
        environment = {
            key: os.environ[key] for key in ("PATH", "SystemRoot", "WINDIR") if key in os.environ
        }
        environment.update({"PYTHONDONTWRITEBYTECODE": "1", "LANG": "C.UTF-8"})
        process = None
        try:
            await asyncio.to_thread(request.write_text, json.dumps(payload), "utf-8")
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "app.infrastructure.conversion",
                str(request),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                start_new_session=sys.platform != "win32",
                env=environment,
            )
            try:
                async with asyncio.timeout(self.settings.conversion_timeout_seconds):
                    await process.communicate()
            except TimeoutError as exc:
                await self._stop_converter(process)
                raise IntakeError(
                    "file_conversion_failed", "Истёк срок сборки PDF-комплекта"
                ) from exc
            if process.returncode:
                raise IntakeError("file_conversion_failed", "Не удалось собрать PDF-комплект")
        except BaseException:
            if process is not None and process.returncode is None:
                await self._stop_converter(process)
            raise
        finally:
            await asyncio.to_thread(request.unlink, missing_ok=True)

        def metadata() -> dict:
            """Проверяет реальный размер и потоковый SHA-256, не загружая весь PDF в worker."""
            if path.stat().st_size > self.settings.document_max_mb * 1024 * 1024:
                path.unlink()
                raise IntakeError(
                    "upload_file_limit", "PDF-комплект превышает допустимый размер", 413
                )
            with path.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            return {
                "path": str(path),
                "file_name": f"{document_id}.pdf",
                "mime_type": "application/pdf",
                "byte_size": path.stat().st_size,
                "sha256": digest,
                "pages": [p.model_dump() for p in pages],
            }

        return await asyncio.to_thread(metadata)
