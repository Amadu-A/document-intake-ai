# tests/live_support.py
"""Общий runner сравнений через REST/ASGI; ошибки кейса не скрывают результаты остальных файлов."""

import asyncio
import hashlib
import json
import re
from collections import Counter
from decimal import Decimal
from pathlib import Path
from time import monotonic
from urllib.parse import urlsplit

import httpx
import pymupdf


class ComparisonFailure(Exception):
    """Несёт технический код проверки без payload, URL, текста исключения или документов."""

    def __init__(self, code: str, http_status: int | None = None) -> None:
        """Фиксирует безопасный код и HTTP-статус для частного отчёта."""
        super().__init__(code)
        self.code, self.http_status = code, http_status


def field_value(value, path):
    """Читает критерий матрицы с индексами и выбором суммы по семантическому типу."""
    for part in path.split("."):
        match = re.fullmatch(r"(\w+)\[type=(\w+)\]", part)
        if match:
            value = next(item for item in value[match[1]] if item["type"] == match[2])
        elif part.isdigit():
            value = value[int(part)]
        else:
            value = value[part]
    return value


def _matches(actual, expected) -> bool:
    """Сравнивает числа без погрешности float, сохраняя различие boolean и numeric."""
    if isinstance(actual, bool) != isinstance(expected, bool):
        return False
    if isinstance(actual, bool):
        return actual == expected
    if isinstance(actual, (int, float)) and isinstance(expected, (int, float)):
        return Decimal(str(actual)) == Decimal(str(expected))
    return actual == expected


def _safe_error(exc: Exception) -> dict:
    """Оставляет только тип, ограниченный технический код и HTTP-статус без response body."""
    code = getattr(exc, "code", "unexpected_error")
    if not isinstance(code, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", code):
        code = "unrecognized_error"
    output = {"error_type": type(exc).__name__, "code": code}
    status = getattr(exc, "http_status", None)
    if isinstance(status, int):
        output["http_status"] = status
    return output


def _row(rows: list, field: str, expected, actual, *, kind: str = "structure") -> None:
    """Добавляет явную структурную проверку; подписанные URLs никогда не передаются сюда."""
    rows.append(
        {
            "kind": kind,
            "field_path": field,
            "expected": expected,
            "actual": actual,
            "status": "match" if _matches(actual, expected) else "mismatch",
        }
    )


def _refs(pages: list[dict]) -> list[list]:
    """Отбирает только глобальные физические адреса, исключая OCR и пояснения модели."""
    return [[page["file_id"], page["page_number"]] for page in pages]


async def _json(client: httpx.AsyncClient, path: str, headers: dict) -> dict:
    """Проверяет HTTP и JSON-контракт без добавления response payload в диагностику."""
    response = await client.get(path, headers=headers)
    if response.status_code != 200:
        raise ComparisonFailure("http_error", response.status_code)
    result = response.json()
    if not isinstance(result, dict):
        raise ComparisonFailure("invalid_json_contract")
    return result


def _pdf_pages(content: bytes) -> int:
    """Проверяет реальное число страниц скачанного PDF и закрывает reader."""
    with pymupdf.open(stream=content, filetype="pdf") as document:
        return len(document)


async def _download_checks(
    client: httpx.AsyncClient,
    rows: list,
    prefix: str,
    url: str,
    metadata: dict,
    pages: int,
) -> None:
    """Проверяет capability, размер, SHA-256, headers и PDF без остановки соседних скачиваний."""
    try:
        parsed = urlsplit(url)
        valid = (
            parsed.scheme in {"http", "https"}
            and bool(parsed.hostname)
            and parsed.path.startswith("/api/v1/jobs/")
            and not parsed.fragment
            and not parsed.username
            and not parsed.password
        )
        _row(rows, prefix + ".download_url", True, bool(valid), kind="download")
        if not valid:
            raise ComparisonFailure("invalid_download_url")
        request = client.build_request("GET", url)
        for header in ("X-API-Key", "Authorization", "Cookie"):
            request.headers.pop(header, None)
        response = await client.send(request, follow_redirects=False)
        _row(rows, prefix + ".http_status", 200, response.status_code, kind="download")
        if response.status_code != 200:
            raise ComparisonFailure("download_http_error", response.status_code)
        content = response.content
        digest = hashlib.sha256(content).hexdigest()
        _row(rows, prefix + ".byte_size", metadata.get("byte_size"), len(content), kind="download")
        _row(rows, prefix + ".sha256", metadata.get("sha256"), digest, kind="download")
        _row(
            rows,
            prefix + ".content_type",
            True,
            response.headers.get("content-type", "").startswith("application/pdf"),
            kind="download",
        )
        length = response.headers.get("content-length", "")
        _row(
            rows,
            prefix + ".content_length",
            len(content),
            int(length) if length.isdigit() else None,
            kind="download",
        )
        _row(
            rows,
            prefix + ".checksum_header",
            digest,
            response.headers.get("x-content-sha256"),
            kind="download",
        )
        _row(
            rows,
            prefix + ".pdf_pages",
            pages,
            await asyncio.to_thread(_pdf_pages, content),
            kind="download",
        )
    except Exception as exc:
        rows.append(
            {
                "kind": "download",
                "field_path": prefix + ".download",
                "status": "error",
                **_safe_error(exc),
            }
        )


async def _sources(
    client: httpx.AsyncClient, status: dict, payload: bytes, case: dict, rows: list
) -> None:
    """Проверяет сохранённый исходник даже при ошибке анализа; input bytes не попадают в отчёт."""
    sources = status.get("sources", [])
    _row(rows, "sources.count", 1, len(sources))
    for index, source in enumerate(sources):
        prefix = f"sources.{index}"
        _row(rows, prefix + ".input_byte_size", len(payload), source.get("byte_size"))
        _row(
            rows,
            prefix + ".input_sha256",
            hashlib.sha256(payload).hexdigest(),
            source.get("sha256"),
        )
        await _download_checks(
            client, rows, prefix, source.get("file_url", ""), source, case["pages"]
        )


def _plan_checks(status: dict, case: dict, rows: list) -> None:
    """Независимо проверяет покрытие и принадлежность main/attachments по глобальным page refs."""
    plan = status.get("plan") or {}
    groups = plan.get("documents", [])
    expected = [
        [source["file_id"], number]
        for source in status["sources"]
        for number in range(1, source["page_count"] + 1)
    ]
    actual = [page for group in groups for page in _refs(group["pages"])]
    _row(rows, "plan.registrations", case["registrations"], len(groups))
    _row(rows, "plan.unassigned_pages", [], _refs(plan.get("unassigned_pages", [])))
    _row(
        rows,
        "plan.page_coverage",
        True,
        Counter(map(tuple, expected)) == Counter(map(tuple, actual)),
    )
    positions = {tuple(page): index for index, page in enumerate(expected)}
    for index, group in enumerate(groups):
        pages = _refs(group["pages"])
        nested = _refs(group["main_pages"]) + [
            page for attachment in group["attachments"] for page in _refs(attachment["pages"])
        ]
        _row(
            rows,
            f"plan.{index}.nested_page_coverage",
            True,
            Counter(map(tuple, pages)) == Counter(map(tuple, nested)),
        )
        order = [positions.get(tuple(page), -1) for page in pages]
        _row(rows, f"plan.{index}.page_order", sorted(order), order)
    if case["file"] == "4-2591.pdf" and groups and status["sources"]:
        group, file_id = groups[0], status["sources"][0]["file_id"]
        _row(rows, "plan.0.main_pages", [[file_id, 1], [file_id, 2]], _refs(group["main_pages"]))
        _row(
            rows,
            "plan.0.attachment_pages",
            [[[file_id, number]] for number in (3, 4, 5, 6)],
            [_refs(attachment["pages"]) for attachment in group["attachments"]],
        )


async def _documents(
    client: httpx.AsyncClient, status: dict, result: dict, case: dict, rows: list, headers: dict
) -> None:
    """Проверяет все регистрации и PDF, связывая artifacts по document_id."""
    documents = [
        (source, document) for source in result["files"] for document in source["documents"]
    ]
    _row(rows, "schema_version", "1.0", result.get("schema_version"))
    _row(rows, "registrations", case["registrations"], len(documents))
    artifacts = status.get("artifacts", [])
    by_id = {artifact["document_id"]: artifact for artifact in artifacts}
    ids = [document["document_id"] for _, document in documents]
    _row(
        rows,
        "artifacts.document_ids",
        sorted(ids),
        sorted(artifact["document_id"] for artifact in artifacts),
    )
    _row(rows, "artifacts.unique_ids", len(artifacts), len(by_id))
    source_metadata = {source["file_id"]: source for source in status["sources"]}
    _row(
        rows,
        "result.source_ids",
        sorted(source_metadata),
        sorted(source["file_id"] for source in result["files"]),
    )
    for index, source in enumerate(result["files"]):
        await _download_checks(
            client,
            rows,
            f"result_sources.{index}",
            source["file_url"],
            source_metadata.get(source["file_id"], {}),
            case["pages"],
        )
    expected_pages = [
        [source["file_id"], number]
        for source in status["sources"]
        for number in range(1, source["page_count"] + 1)
    ]
    actual_pages = [
        [source["file_id"], number]
        for source, document in documents
        for number in document["pages"]
    ]
    _row(
        rows,
        "documents.page_coverage",
        True,
        Counter(map(tuple, expected_pages)) == Counter(map(tuple, actual_pages)),
    )
    attachments = [
        attachment
        for _, document in documents
        for attachment in document["requisites"]["attachments"]
        if attachment["page_range"]
    ]
    _row(rows, "documents.physical_attachments", case["physical_attachments"], len(attachments))
    if case["file"] == "4-2591.pdf":
        _row(
            rows,
            "requisites.attachments.page_ranges",
            [[3, 3], [4, 4], [5, 5], [6, 6]],
            [attachment["page_range"] for attachment in attachments],
        )
    for index, (source, document) in enumerate(documents):
        prefix = f"documents.{index}"
        artifact = by_id.get(document["document_id"], {})
        _row(rows, prefix + ".artifact_present", True, bool(artifact))
        await _download_checks(
            client,
            rows,
            prefix,
            document["attachment_file"]["file_url"],
            artifact,
            len(document["pages"]),
        )
        manifest = await _json(
            client,
            f"/api/v1/jobs/{status['job_id']}/documents/{document['document_id']}/manifest",
            headers,
        )
        _row(
            rows,
            prefix + ".manifest_pages",
            [[source["file_id"], number] for number in document["pages"]],
            _refs(manifest["pages"]),
        )
        _row(rows, prefix + ".manifest_sha256", artifact.get("sha256"), manifest.get("sha256"))
        _row(
            rows,
            prefix + ".manifest_byte_size",
            artifact.get("byte_size"),
            manifest.get("byte_size"),
        )
        planned = next(
            (
                group
                for group in status["plan"]["documents"]
                if group["document_index"] == document["segment_index"]
            ),
            {},
        )
        _row(
            rows,
            prefix + ".manifest_main_pages",
            _refs(planned.get("main_pages", [])),
            _refs(manifest["group"]["main_pages"]),
        )
        _row(
            rows,
            prefix + ".manifest_attachment_pages",
            [_refs(attachment["pages"]) for attachment in planned.get("attachments", [])],
            [_refs(attachment["pages"]) for attachment in manifest["group"]["attachments"]],
        )


async def _run_case(
    client: httpx.AsyncClient,
    case: dict,
    examples_dir: Path,
    headers: dict,
    job_timeout_seconds: float,
    poll_interval: float,
    rows: list,
) -> None:
    """Запускает один PDF через headless API и добавляет проверки независимо от полевых mismatch."""
    path = examples_dir / case["file"]
    payload = await asyncio.to_thread(path.read_bytes)
    response = await client.post(
        "/api/v1/jobs", headers=headers, files={"files": (path.name, payload, "application/pdf")}
    )
    _row(rows, "submit.http_status", 202, response.status_code)
    if response.status_code != 202:
        raise ComparisonFailure("submit_http_error", response.status_code)
    job_id = response.json()["job_id"]
    deadline = monotonic() + job_timeout_seconds
    while True:
        status = await _json(client, f"/api/v1/jobs/{job_id}", headers)
        if status["status"] in {
            "completed",
            "completed_with_warnings",
            "failed",
            "cancelled",
            "awaiting_confirmation",
        }:
            break
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise ComparisonFailure("job_timeout")
        await asyncio.sleep(min(poll_interval, remaining))
    completed = status["status"] in {"completed", "completed_with_warnings"}
    _row(rows, "processing.completed", True, completed)
    await _sources(client, status, payload, case, rows)
    if status["status"] == "awaiting_confirmation":
        raise ComparisonFailure("segmentation_requires_review")
    if not completed:
        raise ComparisonFailure((status.get("error") or {}).get("code", "job_failed"))
    _plan_checks(status, case, rows)
    result = await _json(client, f"/api/v1/jobs/{job_id}/result", headers)
    documents = [document for source in result["files"] for document in source["documents"]]
    for row in rows:
        if row["kind"] != "field":
            continue
        try:
            actual = field_value(documents[0], row["field_path"])
        except (KeyError, IndexError, StopIteration, TypeError):
            actual = "MISSING"
        if isinstance(actual, str):
            actual = actual[:512]
        elif actual is not None and not isinstance(actual, (int, float, bool)):
            actual = "UNSUPPORTED_VALUE"
        row.update(
            actual=actual, status="match" if _matches(actual, row["expected"]) else "mismatch"
        )
    await _documents(client, status, result, case, rows, headers)


async def run_comparison(
    client: httpx.AsyncClient,
    cases: list[dict],
    examples_dir: Path,
    report_path: Path,
    *,
    headers: dict,
    job_timeout_seconds: float,
    poll_interval: float = 2,
    mode: str = "LIVE",
) -> dict:
    """Сравнивает все кейсы; сохраняет отчёт в finally и после каждого файла без raw JSON/URLs.

    Клиент задаёт base_url и при необходимости ASGITransport. Ключи передаются
    только через headers; скачивание удаляет cookie и authorization из request.
    Возвращает cases/rows/timings; ошибка кейса фиксируется без текста и не
    останавливает остальные. Отмена задачи распространяется после записи отчёта.
    """
    report = {"mode": mode, "cases": [], "rows": [], "timings": []}
    await asyncio.to_thread(report_path.parent.mkdir, parents=True, exist_ok=True)

    async def save() -> None:
        """Записывает только частную матрицу, не печатая содержимое в обычные логи."""
        content = json.dumps(report, ensure_ascii=False, indent=2)
        await asyncio.to_thread(report_path.write_text, content, "utf-8")

    try:
        for case in cases:
            started = monotonic()
            rows = [
                {
                    "kind": "field",
                    "field_path": criterion["path"],
                    "source_page": criterion["page"],
                    "expected": criterion["expected"],
                    "actual": None,
                    "status": "not_run",
                }
                for criterion in case["checks"]
            ]
            outcome = {"file": case["file"], "status": "error"}
            try:
                await _run_case(
                    client, case, examples_dir, headers, job_timeout_seconds, poll_interval, rows
                )
                outcome["status"] = (
                    "match"
                    if rows and all(row["status"] == "match" for row in rows)
                    else "mismatch"
                )
            except Exception as exc:
                outcome.update(_safe_error(exc))
            except asyncio.CancelledError:
                outcome.update(error_type="CancelledError", code="evaluation_cancelled")
                raise
            finally:
                elapsed = round(monotonic() - started, 2)
                outcome["total_seconds"] = elapsed
                report["cases"].append(outcome)
                report["rows"].extend({"file": case["file"], **row} for row in rows)
                report["timings"].append({"file": case["file"], "total_seconds": elapsed})
                await save()
    finally:
        await save()
    return report
