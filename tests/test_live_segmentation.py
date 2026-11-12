# tests/test_live_segmentation.py
"""Реальные два эталона разделения: точное предложение либо честный постраничный fallback."""

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from app.core.settings import Settings
from tests.segmentation_support import run_segmentation_case

pytestmark = pytest.mark.live


async def test_real_two_new_segmentation_examples():
    """Проверяет новые PDF через развёрнутый API; расхождения не скрываются за успешным HTTP."""
    settings = Settings()
    settings.require_secrets()
    key = settings.api_keys.get("evaluation")
    assert key, "Для --live требуется отдельный evaluation API-ключ"
    directory = Path("reports/local")
    await asyncio.to_thread(directory.mkdir, parents=True, exist_ok=True)
    results = []
    async with httpx.AsyncClient(
        base_url=settings.app_public_base_url.rstrip("/"), timeout=600, trust_env=False
    ) as client:
        for name in ("суд_ув", "Полученные_письма_по_почте_рф_06_11_2026"):
            report = await run_segmentation_case(
                client,
                Path("examples") / (name + ".pdf"),
                headers={"X-API-Key": key.get_secret_value()},
                timeout_seconds=settings.job_timeout_seconds,
            )
            report["safe_fallback"] = (
                report["segmentation_source"] == "page_fallback"
                and report["status"] == "awaiting_confirmation"
                and report["actual"] == [[n] for n in range(1, report["pages"] + 1)]
            )
            results.append(report)
            await asyncio.to_thread(
                (directory / "live-segmentation.json").write_text,
                json.dumps(results, ensure_ascii=False, indent=2),
                "utf-8",
            )
    assert len(results) == 2
    assert all(case["match"] or case["safe_fallback"] for case in results), (
        "Ошибочные границы или неполное покрытие: reports/local/live-segmentation.json"
    )
