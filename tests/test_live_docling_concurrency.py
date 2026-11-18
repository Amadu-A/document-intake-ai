# tests/test_live_docling_concurrency.py
"""Opt-in замер настоящего Docling: пустые кеши, 173 страницы при 1/2 окнах и два PDF."""

import asyncio
import hashlib
import json
import logging
from pathlib import Path
from time import perf_counter

import httpx
import pymupdf
import pytest

from app.core.settings import Settings
from app.infrastructure.docling import DoclingOCR

pytestmark = [pytest.mark.live, pytest.mark.private]


def content_hash(context: str) -> str:
    """Сравнивает весь текст/табличные факты, исключая лишь геометрию блока Docling."""
    blocks = json.loads(context)
    content = [{k: v for k, v in block.items() if k != "position"} for block in blocks]
    return hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()


def sources() -> list[Path]:
    """Выбирает только разрешённые локальные примеры по размеру, не публикуя их содержимое."""
    found = {}
    for path in Path("examples").glob("*.pdf"):
        with pymupdf.open(path) as pdf:
            found.setdefault(len(pdf), path.resolve())
    assert {173, 6, 8} <= set(found), "Для live OCR нужны локальные PDF на 173, 6 и 8 страниц"
    return [found[n] for n in (173, 6, 8)]


def input_pages(source: Path, folder: Path, index: int) -> list[dict]:
    """Задаёт оригинальные physical identifiers и новые cache paths без изменения PDF."""
    folder.mkdir()
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    with pymupdf.open(source) as pdf:
        return [
            {
                "file_id": f"source-{index}",
                "page_number": n,
                "pdf": str(source),
                "preview": str(folder / f"page-{n}.png"),
                "source_sha256": digest,
            }
            for n in range(1, len(pdf) + 1)
        ]


async def measure(settings, paths: list[Path], folder: Path, name: str, caplog) -> dict:
    """Измеряет новый адаптер, реальные started task и порядок всех OCR-результатов."""
    await asyncio.to_thread(folder.mkdir)
    inputs = [
        await asyncio.to_thread(input_pages, source, folder / str(i), i)
        for i, source in enumerate(paths)
    ]
    began, offset = perf_counter(), len(caplog.records)
    row = {"case": name, "concurrency": settings.docling_window_concurrency, "progress": []}
    states = {}
    counters = {"accepted_tasks": 0, "post_attempts": 0, "max_started": 0, "max_inflight": 0}

    async def response_hook(response):
        """Учитывает лишь статусы собственных task; ID, ключи и payload в отчёт не попадают."""
        path = response.request.url.path
        if path.endswith("/async"):
            counters["post_attempts"] += 1
        if response.status_code != 200 or not (path.endswith("/async") or "/poll/" in path):
            return
        await response.aread()
        data = response.json()
        identifier = data.get("task_id")
        if not identifier:
            return
        if path.endswith("/async"):
            counters["accepted_tasks"] += 1
        states[identifier] = data.get("task_status", "pending")
        counters["max_started"] = max(
            counters["max_started"], sum(v == "started" for v in states.values())
        )
        counters["max_inflight"] = max(
            counters["max_inflight"], sum(v in {"pending", "started"} for v in states.values())
        )

    def emit_for(index):
        """Создаёт безопасный счётчик отдельного документа с видимым выводом при pytest -s."""

        async def emit(kind, data):
            """Сохраняет прогресс без OCR, оригинального имени файла и приватных путей."""
            if kind == "ocr_progress":
                event = {"source": index, **data, "seconds": round(perf_counter() - began, 2)}
                row["progress"].append(event)
                print(json.dumps({"case": name, **event}), flush=True)

        return emit

    try:
        async with httpx.AsyncClient(
            trust_env=False, event_hooks={"response": [response_hook]}
        ) as http:
            adapter = DoclingOCR(settings, http)
            pending = [
                asyncio.create_task(adapter.recognize(pages, emit_for(i)))
                for i, pages in enumerate(inputs)
            ]
            try:
                outputs = await asyncio.gather(*pending)
            finally:
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
            row["seconds"] = round(perf_counter() - began, 2)
            row["page_counts"] = [len(output) for output in outputs]
            row["ordered"] = all(
                [(p["file_id"], p["page_number"]) for p in output]
                == [(p["file_id"], p["page_number"]) for p in pages]
                for output, pages in zip(outputs, inputs, strict=True)
            )
            row["unique_pages"] = all(
                len({(p["file_id"], p["page_number"]) for p in output}) == len(output)
                for output in outputs
            )
            row["ocr_hashes"] = [
                [hashlib.sha256(p["ocr_full"].encode()).hexdigest() for p in output]
                for output in outputs
            ]
            row["content_hashes"] = [
                [content_hash(p["ocr_full"]) for p in output] for output in outputs
            ]
            count = counters["accepted_tasks"]
            cached = await asyncio.gather(
                *[adapter.recognize(pages, emit_for(i)) for i, pages in enumerate(inputs)]
            )
            row["cache_reused_without_post"] = count == counters["accepted_tasks"]
            row["cached_results_identical"] = cached == outputs
        expected_tasks = sum(
            (len(pages) + settings.docling_window_pages - 1) // settings.docling_window_pages
            for pages in inputs
        )
        row["checks"] = {
            "ordered": row["ordered"],
            "unique": row["unique_pages"],
            "page_counts": row["page_counts"] == [len(pages) for pages in inputs],
            "accepted_tasks": counters["accepted_tasks"] == expected_tasks,
            "bounded": counters["max_inflight"] <= settings.docling_window_concurrency,
            "started_overlap": counters["max_started"]
            == min(settings.docling_window_concurrency, expected_tasks),
            "cache": row["cache_reused_without_post"] and row["cached_results_identical"],
        }
        row["status"] = "passed" if all(row["checks"].values()) else "failed"
    except Exception as error:
        row.update(status="failed", type=type(error).__name__, code=getattr(error, "code", None))
    row.update(counters)
    row["timings"] = [
        json.loads(record.message)
        for record in caplog.records[offset:]
        if '"event": "ocr_task_timing"' in record.message
    ]
    print(
        json.dumps(
            {
                k: v
                for k, v in row.items()
                if k not in {"progress", "timings", "ocr_hashes", "content_hashes"}
            }
        )
    )
    return row


async def test_real_docling_parallel_windows_and_two_documents(tmp_path, caplog):
    """Реальные замеры отдельно от doubles; отсутствие ключа/примеров при --live — ошибка."""
    settings = Settings()
    assert settings.docling_api_key.get_secret_value(), "Нужен разрешённый DOCLING_API_KEY проекта"
    settings.docling_async = True
    settings.docling_window_pages = 20
    # Более частый poll нужен для наблюдения пересечения started у двух коротких PDF.
    settings.docling_poll_interval_seconds = 1
    caplog.set_level(logging.INFO, logger="intake")
    big, first, second = await asyncio.to_thread(sources)
    report = {"mode": "LIVE_DOCLING", "poll_interval_seconds": 1, "cases": []}
    target = Path("reports/local/ocr-concurrency-live.json")
    await asyncio.to_thread(target.parent.mkdir, parents=True, exist_ok=True)
    for name, selected, concurrency in (
        ("serial173", [big], 1),
        ("parallel173", [big], 2),
        ("two_documents", [first, second], 2),
    ):
        case = await measure(
            settings.model_copy(update={"docling_window_concurrency": concurrency}),
            selected,
            tmp_path / name,
            name,
            caplog,
        )
        report["cases"].append(case)
        if name == "parallel173" and all(c["status"] == "passed" for c in report["cases"]):
            serial, parallel = report["cases"]
            report["speedup"] = round(serial["seconds"] / parallel["seconds"], 3)
            report["identical_ocr_pages"] = sum(
                a == b
                for a, b in zip(serial["ocr_hashes"][0], parallel["ocr_hashes"][0], strict=True)
            )
            report["identical_content_pages"] = sum(
                a == b
                for a, b in zip(
                    serial["content_hashes"][0], parallel["content_hashes"][0], strict=True
                )
            )
            case["checks"]["content_unchanged"] = report["identical_content_pages"] == 173
            if not case["checks"]["content_unchanged"]:
                case["status"] = "failed"
        await asyncio.to_thread(target.write_text, json.dumps(report, indent=2), "utf-8")
    for case in report["cases"]:
        assert case["status"] == "passed", "OCR live отказ: reports/local/ocr-concurrency-live.json"
        assert case["ordered"] and case["unique_pages"]
        assert case["page_counts"] == ([6, 8] if case["case"] == "two_documents" else [173])
        assert case["accepted_tasks"] == (2 if case["case"] == "two_documents" else 9)
        assert case["max_inflight"] <= case["concurrency"]
        assert case["max_started"] == case["concurrency"], (
            "Не подтверждено пересечение started task"
        )
        assert case["cache_reused_without_post"] and case["cached_results_identical"]
