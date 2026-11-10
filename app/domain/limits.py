# app/domain/limits.py
"""Независимые byte-бюджеты источника и пачки; счётчик не хранит содержимое файлов."""

from app.domain.errors import IntakeError


class UploadBudget:
    """Применяет одинаковые границы к multipart parser и инфраструктурному ingest."""

    def __init__(self, file_bytes: int, batch_bytes: int) -> None:
        """Получает пределы в байтах; лимит источника не зависит от общего бюджета."""
        self.file_limit, self.batch_limit = file_bytes, batch_bytes
        self.current = self.total = 0

    def next_file(self) -> None:
        """Начинает отдельный источник, сохраняя суммарный расход всей загрузки."""
        self.current = 0

    def consume(self, count: int) -> None:
        """Проверяет границы до записи очередного chunk; превышение немедленно даёт HTTP 413."""
        if count < 0:
            raise ValueError("Размер блока не может быть отрицательным")
        if self.current + count > self.file_limit:
            raise IntakeError("upload_file_limit", "Превышен лимит размера одного файла", 413)
        if self.total + count > self.batch_limit:
            raise IntakeError("upload_batch_limit", "Превышен общий размер пакета", 413)
        self.current += count
        self.total += count
