# app/infrastructure/storage.py
"""Приватное файловое хранилище с ограниченной загрузкой и изолированной конвертацией."""

import asyncio
import hashlib
import json
import os
import shutil
import signal
import sys
from pathlib import Path
from uuid import UUID, uuid4

import pymupdf

from app.application.ports import Emit, Upload
from app.domain.errors import IntakeError
from app.domain.jobs import Job, Source
from app.domain.segmentation import PageRef
from app.infrastructure.conversion import validate_signature


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
        total, result = 0, []
        for upload in uploads:
            name = (upload.filename or "document").replace("\\", "/").split("/")[-1]
            if len(name) > 240 or any(ord(c) < 32 for c in name):
                raise IntakeError("invalid_filename", "Недопустимое имя файла")
            suffix = Path(name).suffix.lower()
            file_id = str(uuid4())
            path = directory / (file_id + suffix)
            digest, size = hashlib.sha256(), 0
            with path.open("xb") as stream:
                while chunk := await upload.read(1024 * 1024):
                    total += len(chunk)
                    size += len(chunk)
                    if total > self.settings.upload_max_mb * 1024 * 1024:
                        raise IntakeError("upload_limit", "Превышен общий размер загрузки", 413)
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

    async def remove(self, job_id: str) -> None:
        """Идемпотентно удаляет только собственный UUID-каталог без перехода по ссылкам."""
        directory = self.directory(job_id)
        if directory.exists():
            await asyncio.to_thread(shutil.rmtree, directory)

    async def normalize(self, job: Job, emit: Emit) -> tuple[list[dict], dict]:
        """Запускает конвертер отдельным процессом с deadline и независимым lease-каталогом."""
        directory = self.directory(job.id) / f"attempt-{job.fence}"
        directory.mkdir(mode=0o700, exist_ok=True)
        pages, counts = [], {}
        for source in job.sources:
            request = directory / f"{source.file_id}.request.json"
            data = {
                "path": source.path,
                "target": str(directory / source.file_id),
                "settings": {
                    "timeout": self.settings.conversion_timeout_seconds,
                    "max_pages": self.settings.upload_max_pages - len(pages),
                    "max_pixels": self.settings.image_max_pixels,
                    "render_side": self.settings.render_max_side,
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
                async with asyncio.timeout(self.settings.conversion_timeout_seconds + 5):
                    stdout, _ = await process.communicate()
            except BaseException:
                if process.returncode is None:
                    if sys.platform != "win32":
                        os.killpg(process.pid, signal.SIGKILL)
                    else:
                        process.kill()
                    await process.wait()
                raise
            if process.returncode:
                code = stdout.decode().strip()
                allowed = {
                    "password_protected",
                    "page_limit",
                    "file_conversion_failed",
                    "invalid_mime",
                    "upload_limit",
                    "unsupported_file",
                }
                raise IntakeError(
                    code if code in allowed else "corrupt_document",
                    "Исходный документ не удалось безопасно преобразовать",
                )
            converted = json.loads(request.with_suffix(".result.json").read_text("utf-8"))
            source.page_count = counts[source.file_id] = len(converted["pages"])
            for page in converted["pages"]:
                pages.append({**page, "file_id": source.file_id})
                await emit(
                    "page_started", {"file_id": source.file_id, "page_number": page["page_number"]}
                )
        manifest = {"pages": pages, "source_counts": counts, "documents": {}}
        await emit("normalized", manifest)
        return pages, manifest

    async def pdf(self, job: Job, document_id: str, pages: list[PageRef]) -> dict:
        """Материализует уже проверенную VLM-группу, включая все её приложения."""
        path = self.directory(job.id) / f"attempt-{job.fence}" / f"{document_id}.pdf"

        def assemble() -> dict:
            """Копирует страницы по глобальному manifest и проверяет размер/хеш файла."""
            output = pymupdf.open()
            readers = {}
            try:
                for page in pages:
                    if page.file_id not in readers:
                        normalized = path.parent / page.file_id / "normalized.pdf"
                        readers[page.file_id] = pymupdf.open(normalized)
                    output.insert_pdf(
                        readers[page.file_id],
                        from_page=page.page_number - 1,
                        to_page=page.page_number - 1,
                        links=False,
                        annots=False,
                    )
                output.save(path, garbage=4, deflate=True)
            finally:
                output.close()
                for reader in readers.values():
                    reader.close()
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

        return await asyncio.to_thread(assemble)
