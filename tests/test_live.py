# tests/test_live.py
"""Opt-in настоящие shared/HTTP проверки; отсутствие настроек при --live является FAIL."""

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from app.core.settings import Settings
from app.operations import smoke
from tests.correspondence_support import run_correspondence
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


@pytest.mark.parametrize("with_organization", [True, False], ids=["organization", "no-context"])
async def test_real_court_correspondence_and_downloads(with_organization):
    """Частный эталон проверяет реквизиты, четыре регистрации и целостность восьми страниц."""
    settings = Settings()
    settings.require_secrets()
    key = settings.api_keys.get("evaluation")
    assert key, "Для --live требуется отдельный evaluation API-ключ"
    source = Path("examples/суд_ув.pdf")
    expected = Path("examples/суд_ув.expected.json")
    if not await asyncio.to_thread(expected.exists):
        expected = source.with_suffix(".json")
    assert all(
        await asyncio.gather(asyncio.to_thread(source.is_file), asyncio.to_thread(expected.is_file))
    ), "Нужны частные PDF и полный эталон JSON"
    golden = json.loads(await asyncio.to_thread(expected.read_text, "utf-8"))
    organization = (
        next(
            d["one_c"]["organization"]
            for f in golden["files"]
            for d in f["documents"]
            if d["one_c"]["organization"]
        )
        if with_organization
        else None
    )
    async with httpx.AsyncClient(
        base_url=settings.app_public_base_url.rstrip("/"), timeout=60, trust_env=False
    ) as client:
        report = await run_correspondence(
            client,
            source,
            expected,
            {"X-API-Key": key.get_secret_value()},
            organization,
            settings.job_timeout_seconds,
        )
    path = Path(f"reports/local/correspondence-{with_organization}.json")
    await asyncio.to_thread(path.parent.mkdir, parents=True, exist_ok=True)
    await asyncio.to_thread(
        path.write_text, json.dumps(report, ensure_ascii=False, indent=2), "utf-8"
    )
    assert report["status"] in {"completed", "completed_with_warnings"}, str(path)
    assert report["checks"] and all(row["match"] for row in report["checks"]), str(path)
