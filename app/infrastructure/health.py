# app/infrastructure/health.py
"""Readiness проверяет реальные зависимости и кэширует дорогие vision/OCR-пробы."""

import asyncio
import tempfile
from pathlib import Path
from time import monotonic

import pymupdf

from app.domain.errors import IntakeError


def raster_probe(directory: Path) -> list[dict]:
    """Создаёт PDF только с растровым маркером: PASS требует настоящего OCR, а не text parser."""
    pdf_path, preview = directory / "probe.pdf", directory / "probe.png"
    with pymupdf.open() as source:
        page = source.new_page(width=400, height=200)
        page.insert_text((30, 80), "INTAKE 7429", fontsize=24)
        image = page.get_pixmap(matrix=pymupdf.Matrix(2, 2)).tobytes("png")
    preview.write_bytes(image)
    with pymupdf.open() as raster:
        page = raster.new_page(width=400, height=200)
        page.insert_image(page.rect, stream=image)
        raster.save(pdf_path)
    return [
        {
            "file_id": "synthetic-health",
            "page_number": 1,
            "preview": str(preview),
            "pdf": str(pdf_path),
        }
    ]


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
        """Проверяет OCR растрового PDF и vision-маркер без встроенного текстового слоя."""
        checks = {}

        async def discard(_kind: str, _data: dict) -> None:
            """Не пишет synthetic inference в пользовательские SSE и обычные логи."""

        with tempfile.TemporaryDirectory(dir=self.settings.data_dir) as temporary:
            pages = await asyncio.to_thread(raster_probe, Path(temporary))
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
