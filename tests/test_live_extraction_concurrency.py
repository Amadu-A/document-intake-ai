# tests/test_live_extraction_concurrency.py
"""Opt-in: один частный пакет через настоящие Docling/VLM при лимитах документов 1/2/3."""

import asyncio
import hashlib
import json
from collections import Counter
from pathlib import Path
from time import perf_counter

import httpx
import pytest

from app.application.analysis import Analyze
from app.core.settings import Settings
from app.infrastructure.docling import DoclingOCR
from app.infrastructure.storage import FileStorage
from app.infrastructure.vision import SharedVision
from app.worker import process
from tests.test_boundary_api import KEY, confirm, segmentation

pytestmark = [pytest.mark.live, pytest.mark.private]


async def test_live_packet_limits(client, container, tmp_path_factory):
    """Записывает реальные длительности/ошибки; эталоны, prompts и общие сервисы не меняет."""
    source, cut = Path("examples/суд_ув.pdf"), Path("examples/суд_ув_cut.json")
    assert await asyncio.to_thread(source.is_file) and await asyncio.to_thread(cut.is_file), (
        "Нужны частные examples/суд_ув.pdf и суд_ув_cut.json"
    )
    runtime = Settings()
    assert runtime.docling_api_key.get_secret_value(), "Нужен разрешённый локальный DOCLING_API_KEY"
    data = await asyncio.to_thread(source.read_bytes)
    ranges = json.loads(await asyncio.to_thread(cut.read_text, "utf-8"))["range"]
    report = {"source": source.name, "sha256": hashlib.sha256(data).hexdigest(), "runs": []}
    target = Path("reports/local/extraction-concurrency-live.json")
    await asyncio.to_thread(target.parent.mkdir, parents=True, exist_ok=True)
    for limit in (1, 2, 3):
        container.settings.document_extraction_concurrency = limit
        configured = container.settings.model_copy(
            update={
                # Короткий путь внутри pytest basetemp обходит MAX_PATH Windows
                # без изменения production-путей или адаптера Docling.
                "data_dir": tmp_path_factory.getbasetemp() / "s",
                "document_extraction_concurrency": limit,
                "vlm_request_concurrency": 4,
                "docling_base_url": runtime.docling_base_url,
                "docling_api_key": runtime.docling_api_key,
                "docling_async": runtime.docling_async,
                "docling_ocr_preset": runtime.docling_ocr_preset,
                "docling_ocr_languages": runtime.docling_ocr_languages,
                "shared_vlm_base_url": runtime.shared_vlm_base_url,
                "shared_vlm_model": runtime.shared_vlm_model,
                "model_attempts": runtime.model_attempts,
            }
        )
        container.storage = FileStorage(configured)
        container.intake.storage = container.storage
        async with httpx.AsyncClient(
            trust_env=False,
            follow_redirects=False,
            limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
        ) as http:
            ocr = DoclingOCR(configured, http)
            vision = SharedVision(configured, http)
            analysis = Analyze(container.database, container.storage, vision, ocr, configured)
            container.analysis = analysis
            ocr_times, stage_times = [], []
            recognize, extract = ocr.recognize, analysis._extract_documents

            async def measured_ocr(pages, emit, impl=recognize, times=ocr_times):
                """Считает wall каждого OCR вызова, включая нормализацию поворотов."""
                started = perf_counter()
                try:
                    return await impl(pages, emit)
                finally:
                    times.append(perf_counter() - started)

            async def measured_extraction(*args, impl=extract, times=stage_times):
                """Измеряет общий wall пула отдельно от суммы перекрывающихся HTTP."""
                started = perf_counter()
                try:
                    return await impl(*args)
                finally:
                    times.append(perf_counter() - started)

            ocr.recognize, analysis._extract_documents = measured_ocr, measured_extraction
            started = perf_counter()
            response = await client.post(
                "/api/v1/uploads",
                headers=KEY,
                files={"files": (source.name, data, "application/pdf")},
            )
            assert response.status_code == 202
            identifier = response.json()["job_id"]
            await process(container, identifier)
            plan = await segmentation(client, identifier)
            file_id = (await container.database.get(identifier)).sources[0].file_id
            saved = await client.put(
                f"/api/v1/jobs/{identifier}/segmentation",
                headers=KEY,
                json={
                    "expected_revision": plan["plan_revision"],
                    "documents": [
                        {
                            "client_id": f"doc-{i}",
                            "pages": [{"file_id": file_id, "page_number": p} for p in pages],
                        }
                        for i, pages in enumerate(ranges)
                    ],
                },
            )
            assert saved.status_code == 200
            await confirm(client, identifier, saved.json()["plan_revision"])
            await process(container, identifier)
            total = perf_counter() - started
            job = await container.database.get(identifier)
            events, cursor = [], 0
            while page := await container.database.events(identifier, cursor):
                events.extend(page)
                cursor = page[-1]["id"]
            timings = [e["data"] for e in events if e["event"] == "vlm_timing"]
            phases = Counter()
            for metric in timings:
                phases[metric["stage"]] += (metric["http_ms"] or 0) / 1000
            record = {
                "limit": limit,
                "status": job.status,
                "total_s": round(total, 3),
                "ocr_initial_s": round(ocr_times[0], 3) if ocr_times else None,
                "ocr_calls_sum_s": round(sum(ocr_times), 3),
                "extraction_stage_s": round(sum(stage_times), 3),
                "vlm_http_sum_s": {k: round(v, 3) for k, v in phases.items()},
                "vlm_wait_sum_s": round(sum(m["wait_ms"] or 0 for m in timings) / 1000, 3),
                "vlm_requests": len(timings),
                "model_retries": sum(e["event"] == "model_retry" for e in events),
                "failed_documents": sum(
                    p.status != "recognized" for p in job.document_processing.values()
                ),
                "documents": len(job.document_results),
                "pages": len(job.page_refs()),
                "max_documents_active": max(
                    (
                        e["data"].get("active", 0)
                        for e in events
                        if e["event"] == "extraction_progress"
                    ),
                    default=0,
                ),
                "error_code": job.error["code"] if job.error else None,
            }
            for key in ("prompt_tokens", "completion_tokens"):
                known = [m[key] for m in timings if m.get(key) is not None]
                record[key] = sum(known) if known else None
                record[key + "_known_requests"] = len(known)
            report["runs"].append(record)
            await asyncio.to_thread(
                target.write_text, json.dumps(report, ensure_ascii=False, indent=2), "utf-8"
            )
            print("extraction_benchmark", json.dumps(record, ensure_ascii=True), flush=True)
            assert job.status in {"completed", "completed_with_warnings"}, job.error
            assert len(job.document_results) == len(ranges)
            assert (
                sorted(
                    (p for c in job.document_results.values() for p in c.group.pages),
                    key=lambda p: p.page_number,
                )
                == job.page_refs()
            )
            assert (
                await client.get(f"/api/v1/jobs/{identifier}/registry.xlsx", headers=KEY)
            ).status_code == 200
