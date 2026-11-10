# app/presentation/http/multipart.py
"""Потоковый multipart счётчик отклоняет большой источник до сохранения полного spool."""

import errno

from starlette.formparsers import MultiPartParser

from app.domain.errors import IntakeError
from app.domain.limits import UploadBudget


class BoundedMultipartParser(MultiPartParser):
    """Проверяет размеры в Starlette parser и закрывает частичные spooled-файлы."""

    def __init__(self, headers, stream, settings, temporary_upload, reserve_upload) -> None:
        """Использует общий каталог Settings, сохраняя стандартный ограниченный spool на диск."""

        async def checked_stream():
            """Не расходует дисковый резерв даже до передачи данных application ingest."""
            async for chunk in stream:
                await reserve_upload(len(chunk))
                yield chunk

        super().__init__(
            headers,
            checked_stream(),
            max_files=settings.upload_max_files,
            max_fields=4,
            max_part_size=64 * 1024,
        )
        self.budget = UploadBudget(
            settings.upload_max_file_mb * 1024 * 1024, settings.upload_max_mb * 1024 * 1024
        )
        self.header_bytes = 0
        self.temporary_upload = temporary_upload

    def on_headers_finished(self) -> None:
        """Сохраняет стандартный UploadFile, заменяя только пустой spool безопасной фабрикой."""
        super().on_headers_finished()
        upload = self._current_part.file
        if upload is not None:
            upload.file.close()
            upload.file = self.temporary_upload()
            self._files_to_close_on_error[-1] = upload.file

    def on_header_field(self, data: bytes, start: int, end: int) -> None:
        """Общий лимит включает имя заголовка, а не только его значение."""
        self.header_bytes += end - start
        if self.header_bytes > 64 * 1024:
            raise IntakeError("invalid_request", "Слишком большой заголовок части загрузки")
        super().on_header_field(data, start, end)

    def on_part_begin(self) -> None:
        """Сбрасывает счётчик нового источника и небольшой бюджет его MIME-заголовков."""
        super().on_part_begin()
        self.budget.next_file()
        self.header_bytes = 0

    def on_header_value(self, data: bytes, start: int, end: int) -> None:
        """Не позволяет гигантскому имени/заголовку накопиться в памяти multipart parser."""
        self.header_bytes += end - start
        if self.header_bytes > 64 * 1024:
            raise IntakeError("invalid_request", "Слишком большой заголовок части загрузки")
        super().on_header_value(data, start, end)

    def on_part_data(self, data: bytes, start: int, end: int) -> None:
        """Проверяет размер файлового chunk до передачи его стандартной очереди записи на диск."""
        if self._current_part.file is not None:
            self.budget.consume(end - start)
        super().on_part_data(data, start, end)

    async def parse(self):
        """Закрывает все временные файлы также при disconnect/cancel и собственном limit error."""
        try:
            return await super().parse()
        except OSError as exc:
            for stream in self._files_to_close_on_error:
                stream.close()
            if exc.errno == errno.ENOSPC:
                raise IntakeError(
                    "storage_insufficient", "Недостаточно места для загрузки", 507
                ) from exc
            raise
        except BaseException:
            for stream in self._files_to_close_on_error:
                stream.close()
            raise
