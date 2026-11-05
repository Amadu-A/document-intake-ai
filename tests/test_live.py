# tests/test_live.py
"""Opt-in настоящие shared/HTTP проверки; отсутствие настроек при --live является FAIL."""

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from app.core.settings import Settings
from app.operations import smoke
from tests.live_support import run_comparison

pytestmark = pytest.mark.live


async def test_real_postgresql_amqp_docling_vlm_and_http():
    """Проверяет PostgreSQL, AMQP consumer, OCR и vision/JSON до headless HTTP smoke."""
    settings = Settings()
    settings.require_secrets()
    async with httpx.AsyncClient(timeout=600, trust_env=False) as client:
        response = await client.get(settings.app_public_base_url.rstrip("/") + "/health/ready")
        assert response.status_code == 200
        assert all(value == "PASS" for value in response.json()["checks"].values())
    await smoke(settings)


async def test_real_five_pairs_comparison_matrix():
    """Проверяет все пять файлов, сохраняя ошибки, структуру и скачивание в частную матрицу."""
    settings = Settings()
    settings.require_secrets()
    key = settings.api_keys.get("evaluation")
    assert key, "Для --live требуется отдельный evaluation API-ключ"
    matrix = json.loads(
        await asyncio.to_thread(Path("tests/fixtures/regression_matrix.json").read_text, "utf-8")
    )
    async with httpx.AsyncClient(
        base_url=settings.app_public_base_url.rstrip("/"), timeout=60, trust_env=False
    ) as client:
        report = await run_comparison(
            client,
            matrix["cases"],
            Path("examples"),
            Path("reports/local/live-comparison.json"),
            headers={"X-API-Key": key.get_secret_value()},
            job_timeout_seconds=settings.job_timeout_seconds,
        )
    assert len(report["cases"]) == len(matrix["cases"]) > 0
    assert all(case["status"] == "match" for case in report["cases"]), (
        "Есть ошибки или расхождения: reports/local/live-comparison.json"
    )
