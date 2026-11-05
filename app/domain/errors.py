# app/domain/errors.py
"""Безопасные предметные ошибки; приватные payload и traceback наружу не передаются."""


class IntakeError(Exception):
    """Несёт стабильный код ошибки, безопасное сообщение и HTTP-смысл без зависимости от HTTP."""

    def __init__(self, code: str, message: str, status: int = 422) -> None:
        """Сохраняет только разрешённые для клиента диагностические сведения."""
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


class LeaseLost(IntakeError):
    """Прерывает устаревший worker, утративший право публиковать результат."""

    def __init__(self) -> None:
        """Обозначает отмену или смену поколения обработки без раскрытия внутренних данных."""
        super().__init__("lease_lost", "Право обработки задания завершено", 409)
