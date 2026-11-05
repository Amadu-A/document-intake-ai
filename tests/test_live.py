# tests/test_live.py
"""Opt-in настоящие shared/HTTP проверки; отсутствие настроек при --live является FAIL."""

import asyncio
import hashlib
import json
import re
from decimal import Decimal
from pathlib import Path
from time import monotonic

import httpx
import pymupdf
import pytest

from app.core.settings import Settings
from app.operations import smoke

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


def field_value(value, path):
    """Читает путь критерия, поддерживая выбор суммы по её семантическому типу."""
    for part in path.split("."):
        match = re.fullmatch(r"(\w+)\[type=(\w+)\]", part)
        if match:
            value = next(item for item in value[match[1]] if item["type"] == match[2])
        elif part.isdigit():
            value = value[int(part)]
        else:
            value = value[part]
    return value


async def test_real_five_pairs_comparison_matrix():
    """Сравнивает реальные VLM-ответы с подтверждёнными фактами, отдельно от старых placeholders."""
    settings = Settings()
    settings.require_secrets()
    key = settings.api_keys.get("evaluation")
    assert key, "Для --live требуется отдельный evaluation API-ключ"
    headers = {"X-API-Key": key.get_secret_value()}
    matrix = json.loads(
        await asyncio.to_thread(Path("tests/fixtures/regression_matrix.json").read_text, "utf-8")
    )
    report = {"mode": "LIVE", "rows": [], "timings": []}
    base = settings.app_public_base_url.rstrip("/")
    directory = Path("reports/local")
    await asyncio.to_thread(directory.mkdir, parents=True, exist_ok=True)
    try:
        async with httpx.AsyncClient(timeout=60, trust_env=False) as client:
            for case in matrix["cases"]:
                path = Path("examples", case["file"])
                assert await asyncio.to_thread(path.is_file), "Нужны пять локальных examples"
                started = monotonic()
                payload = await asyncio.to_thread(path.read_bytes)
                response = await client.post(
                    base + "/api/v1/jobs",
                    headers=headers,
                    files={"files": (path.name, payload, "application/pdf")},
                )
                assert response.status_code == 202
                job_id = response.json()["job_id"]
                deadline = monotonic() + settings.job_timeout_seconds
                while monotonic() < deadline:
                    response = await client.get(base + f"/api/v1/jobs/{job_id}", headers=headers)
                    response.raise_for_status()
                    status = response.json()
                    if status["status"] in {
                        "completed",
                        "completed_with_warnings",
                        "failed",
                        "cancelled",
                    }:
                        break
                    await asyncio.sleep(2)
                assert status["status"] in {"completed", "completed_with_warnings"}, status.get(
                    "error"
                )
                response = await client.get(base + f"/api/v1/jobs/{job_id}/result", headers=headers)
                response.raise_for_status()
                result = response.json()
                documents = [doc for source in result["files"] for doc in source["documents"]]
                assert len(documents) == case["registrations"]
                document = documents[0]
                assert document["pages"] == list(range(1, case["pages"] + 1))
                assert (
                    sum(bool(a["page_range"]) for a in document["requisites"]["attachments"])
                    == case["physical_attachments"]
                )
                for criterion in case["checks"]:
                    try:
                        actual = field_value(document, criterion["path"])
                    except (KeyError, IndexError, StopIteration):
                        actual = "MISSING"
                    expected = criterion["expected"]
                    matches = (
                        Decimal(str(actual)) == Decimal(str(expected))
                        if isinstance(expected, (int, float))
                        and not isinstance(expected, bool)
                        and isinstance(actual, (int, float))
                        else actual == expected
                    )
                    report["rows"].append(
                        {
                            "file": case["file"],
                            "field_path": criterion["path"],
                            "source_page": criterion["page"],
                            "expected": expected,
                            "actual": actual,
                            "status": "match" if matches else "mismatch",
                        }
                    )
                download = await client.get(document["attachment_file"]["file_url"])
                assert download.status_code == 200
                with pymupdf.open(stream=download.content) as pdf:
                    assert len(pdf) == case["pages"]
                assert (
                    hashlib.sha256(download.content).hexdigest() == status["artifacts"][0]["sha256"]
                )
                report["timings"].append(
                    {"file": case["file"], "total_seconds": round(monotonic() - started, 2)}
                )
    finally:
        (directory / "live-comparison.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), "utf-8"
        )
    assert all(row["status"] == "match" for row in report["rows"]), (
        "Есть расхождения: reports/local/live-comparison.json"
    )
