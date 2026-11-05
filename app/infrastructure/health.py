# app/infrastructure/health.py
"""Readiness проверяет реальные зависимости и кэширует дорогие vision/OCR-пробы."""

import asyncio
import tempfile
from pathlib import Path
from time import monotonic

import pymupdf

from app.domain.errors import IntakeError


class Health:
    """Не объявляет READY при недоступном OCR, VLM JSON/vision, PostgreSQL или consumer."""

    def __init__(self, database, rabbit, vision, ocr, settings) -> None:
        """Подключает настоящие адаптеры; тесты могут подставить контрактные doubles."""
        self.database, self.rabbit, self.vision, self.ocr = database, rabbit, vision, ocr
        self.settings = settings
        self.cached = None
        self.checked_at = 0.0
        self.lock = asyncio.Lock()

    async def ready(self, force: bool = False) -> dict:
        """Всегда проверяет DB/AMQP; дорогие synthetic OCR/vision — по ограниченному кэшу."""
        checks = {}
        for name, check in (
            ("postgresql", self.database.ping),
            ("consumer", self.rabbit.require_consumer),
        ):
            try:
                await asyncio.wait_for(check(), timeout=10)
                checks[name] = "PASS"
            except Exception as exc:
                checks[name] = "FAIL: " + type(exc).__name__
        async with self.lock:
            if (
                force
                or self.cached is None
                or monotonic() - self.checked_at > self.settings.health_cache_seconds
            ):
                self.cached = await self._contracts()
                self.checked_at = monotonic()
            checks.update(self.cached)
        return {
            "status": "ready" if all(v == "PASS" for v in checks.values()) else "not_ready",
            "checks": checks,
        }

    async def _contracts(self) -> dict:
        """Использует собственный несекретный PDF, проверяя реальный текст OCR и vision-ответ."""
        checks = {}

        async def discard(_kind: str, _data: dict) -> None:
            """Не пишет synthetic inference в пользовательские SSE и обычные логи."""

        with tempfile.TemporaryDirectory(dir=self.settings.data_dir) as temporary:
            directory = Path(temporary)
            pdf_path, preview = directory / "probe.pdf", directory / "probe.png"
            document = pymupdf.open()
            page = document.new_page(width=400, height=200)
            page.insert_text((30, 80), "INTAKE 7429", fontsize=24)
            page.get_pixmap().save(preview)
            document.save(pdf_path)
            document.close()
            pages = [
                {
                    "file_id": "synthetic-health",
                    "page_number": 1,
                    "preview": str(preview),
                    "pdf": str(pdf_path),
                }
            ]
            try:
                result = await self.vision.request(
                    'Прочитай изображение и верни JSON {"text": "прочитанный текст"}.',
                    {},
                    pages,
                    {
                        "type": "object",
                        "properties": {"text": {"type": "string"}},
                        "required": ["text"],
                        "additionalProperties": False,
                    },
                    discard,
                )
                checks["vlm_vision_json"] = "PASS" if "7429" in result.get("text", "") else "FAIL"
            except IntakeError as exc:
                checks["vlm_vision_json"] = "FAIL: " + exc.code + ": " + exc.message
            except Exception as exc:
                checks["vlm_vision_json"] = "FAIL: " + type(exc).__name__
            try:
                result = await self.ocr.recognize(pages, discard)
                checks["docling_ocr"] = "PASS" if "7429" in result[0]["ocr"] else "FAIL"
            except IntakeError as exc:
                checks["docling_ocr"] = "FAIL: " + exc.code + ": " + exc.message
            except Exception as exc:
                checks["docling_ocr"] = "FAIL: " + type(exc).__name__
        return checks
