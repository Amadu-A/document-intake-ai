# app/infrastructure/vision_stream.py
"""Защита JSON-потока от зацикливания форматирования без изменения строковых значений."""

from app.domain.errors import IntakeError


class JSONWhitespaceGuard:
    """Считает только непрерывные JSON-пробелы вне строк, сохраняя состояние между delta."""

    def __init__(self) -> None:
        """Ограничивает служебное форматирование, не длину реквизитов или содержимое строк."""
        self.inside_string = False
        self.escaped = False
        self.whitespace_run = 0

    def feed(self, text: str) -> None:
        """Прерывает поток после 512 пробелов/переводов строк подряд вне JSON-строки."""
        for character in text:
            if self.inside_string:
                if self.escaped:
                    self.escaped = False
                elif character == "\\":
                    self.escaped = True
                elif character == '"':
                    self.inside_string = False
            elif character == '"':
                self.inside_string = True
                self.whitespace_run = 0
            elif character in " \t\r\n":
                self.whitespace_run += 1
                if self.whitespace_run > 512:
                    raise IntakeError(
                        "model_output_limit",
                        "VLM зациклилась на пробельном форматировании JSON. "
                        "Верни один компактный JSON без отступов и пустых строк между токенами; "
                        "сохрани все поля и исходные пробелы внутри строковых значений.",
                    )
            else:
                self.whitespace_run = 0
