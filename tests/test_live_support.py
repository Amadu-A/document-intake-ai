# tests/test_live_support.py
"""Offline-регрессии runner: настоящий ASGI API/SQLite/PDF и явно синтетические OCR/VLM."""

import asyncio
import json

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from app.domain.errors import IntakeError
from tests.live_support import run_comparison
from tests.support import SyntheticOCR, facts, pdf_bytes, plan_for

KEY = {"X-API-Key": "e" * 48}


def comparison_case(name, *, pages=1, registrations=1, attachments=0, expected=None):
    """Задаёт минимальный синтетический критерий без пользовательских реквизитов."""
    return {
        "file": name,
        "pages": pages,
        "registrations": registrations,
        "physical_attachments": attachments,
        "checks": [{"path": "requisites.document_number", "expected": expected, "page": 1}],
    }


def local_worker_hook(container):
    """Заменяет только AMQP delivery: production Analyze выполняется после submit через ASGI."""

    async def execute(response):
        """Обрабатывает созданное задание, сохраняя обычные claim, manifest и download endpoints."""
        if response.request.method == "POST" and response.request.url.path == "/api/v1/jobs":
            await response.aread()
            if response.status_code == 202:
                job = await container.database.claim(response.json()["job_id"])
                assert job is not None
                await container.analysis.execute(job)

    return execute


async def test_runner_continues_all_cases_and_preserves_private_failure_report(
    tmp_path, client, container
):
    """OCR-сбой, отсутствующий файл и mismatch не мешают двум следующим успешным кейсам."""
    marker = "synthetic-private-document-and-error-payload"

    class FirstOCRFailure(SyntheticOCR):
        """Отклоняет один synthetic OCR без обращений к общей инфраструктуре."""

        failed = False

        async def recognize(self, pages, emit):
            """Создаёт ожидаемый production-статус failed, затем использует обычный double."""
            if not self.failed:
                self.failed = True
                raise IntakeError("docling_unavailable", marker)
            return await super().recognize(pages, emit)

    container.analysis.ocr = FirstOCRFailure()
    client.event_hooks["response"].append(local_worker_hook(container))
    downloads = []

    async def inspect_download(request):
        """Подтверждает удаление default credentials при скачивании capability URL."""
        if "/source-files/" in request.url.path or request.url.path.endswith("/file"):
            downloads.append(request.url.path)
            assert not {"x-api-key", "authorization", "cookie"}.intersection(request.headers)

    client.event_hooks["request"].append(inspect_download)
    client.headers["Authorization"] = "Bearer " + marker
    client.cookies.set("private-cookie", marker)
    cases = [
        comparison_case("first-ocr-failed.pdf"),
        comparison_case("missing.pdf"),
        comparison_case("field-mismatch.pdf", expected="UNEXPECTED"),
        comparison_case("success-four.pdf"),
        comparison_case("success-five.pdf"),
    ]
    for case in cases:
        if case["file"] != "missing.pdf":
            (tmp_path / case["file"]).write_bytes(pdf_bytes())
    report_path = tmp_path / "report.json"
    report = await run_comparison(
        client,
        cases,
        tmp_path,
        report_path,
        headers=KEY,
        job_timeout_seconds=5,
        poll_interval=0,
        mode="OFFLINE_ASGI_SYNTHETIC",
    )
    assert [case["status"] for case in report["cases"]] == [
        "error",
        "error",
        "mismatch",
        "match",
        "match",
    ]
    assert report["cases"][0]["code"] == "docling_unavailable"
    assert report["cases"][1]["error_type"] == "FileNotFoundError"
    assert len(report["timings"]) == 5
    assert downloads
    assert any(
        row["file"] == cases[0]["file"]
        and row["field_path"] == "sources.0.sha256"
        and row["status"] == "match"
        for row in report["rows"]
    )
    assert any(
        row["file"] == cases[0]["file"] and row["kind"] == "field" and row["status"] == "not_run"
        for row in report["rows"]
    )
    text = report_path.read_text("utf-8")
    assert json.loads(text) == report
    assert marker not in text
    assert "?token=" not in text and "http://testserver" not in text


@pytest.mark.parametrize("failed_download", [False, True])
async def test_runner_matches_reversed_artifacts_by_document_id(
    tmp_path, client, container, monkeypatch, failed_download
):
    """Все PDF проверяются даже при порядке status.artifacts, обратном result.documents."""
    case = comparison_case("two-letters.pdf", pages=3, registrations=2, expected="FIRST")
    (tmp_path / case["file"]).write_bytes(pdf_bytes(3))

    def two_documents(context):
        """Возвращает две явные группы разного размера для различимых PDF hashes."""
        refs = [
            {"file_id": page["file_id"], "page_number": page["page_number"]}
            for page in context["pages"]
        ]
        first, second = plan_for(refs[:1]), plan_for(refs[1:])
        second["documents"][0]["document_index"] = 2
        first["documents"].extend(second["documents"])
        return first

    first, second = facts(), facts()
    first["requisites"]["document_number"], second["requisites"]["document_number"] = (
        "FIRST",
        "SECOND",
    )
    container.vision.answers.extend([two_documents, first, second])
    original = container.exports.status

    def reversed_artifacts(job):
        """Меняет только порядок публичного массива, сохраняя реальные metadata и файлы."""
        status = original(job)
        status["artifacts"].reverse()
        return status

    monkeypatch.setattr(container.exports, "status", reversed_artifacts)
    if failed_download:
        original_artifact = container.exports.artifact
        failure = []

        async def fail_first_document(*arguments, **keywords):
            """Отклоняет один download; следующий PDF и manifests остаются доступны."""
            if arguments[1] == "document" and not failure:
                failure.append(True)
                raise IntakeError("artifact_not_found", "synthetic-private-download-error", 404)
            return await original_artifact(*arguments, **keywords)

        monkeypatch.setattr(container.exports, "artifact", fail_first_document)
    client.event_hooks["response"].append(local_worker_hook(container))
    report = await run_comparison(
        client,
        [case],
        tmp_path,
        tmp_path / "report.json",
        headers=KEY,
        job_timeout_seconds=5,
        poll_interval=0,
        mode="OFFLINE_ASGI_SYNTHETIC",
    )
    assert report["cases"][0]["status"] == ("mismatch" if failed_download else "match")
    for index in (0, 1):
        if index == 0 and failed_download:
            error = next(
                row for row in report["rows"] if row["field_path"] == "documents.0.download"
            )
            assert error["status"] == "error" and error["http_status"] == 404
            continue
        for field in ("byte_size", "sha256", "pdf_pages", "manifest_pages"):
            row = next(
                row for row in report["rows"] if row["field_path"] == f"documents.{index}.{field}"
            )
            assert row["status"] == "match"


@pytest.mark.parametrize("wrong_boundaries", [False, True])
async def test_runner_checks_exact_claim_main_and_attachments(
    tmp_path, client, container, wrong_boundaries
):
    """Одной регистрации и четырёх приложений недостаточно: проверяются точные страницы."""
    case = comparison_case("4-2591.pdf", pages=6, attachments=4)
    (tmp_path / case["file"]).write_bytes(pdf_bytes(6))

    def claim(context):
        """Имитирует корректный либо семантически неверный, но технически целостный VLM-план."""
        refs = [
            {"file_id": page["file_id"], "page_number": page["page_number"]}
            for page in context["pages"]
        ]
        plan = plan_for(refs, main=2)
        if wrong_boundaries:
            plan["documents"][0]["main_pages"] = refs[:1]
            plan["documents"][0]["attachments"][0]["pages"] = refs[1:3]
        return plan

    attachments = [
        {"title": "Synthetic attachment", "page_range": [number, number]} for number in (3, 4, 5, 6)
    ]
    if wrong_boundaries:
        attachments[0]["page_range"] = [2, 3]
    container.vision.answers.extend([claim, facts(attachments)])
    client.event_hooks["response"].append(local_worker_hook(container))
    report = await run_comparison(
        client,
        [case],
        tmp_path,
        tmp_path / "report.json",
        headers=KEY,
        job_timeout_seconds=5,
        poll_interval=0,
        mode="OFFLINE_ASGI_SYNTHETIC",
    )
    expected = "mismatch" if wrong_boundaries else "match"
    assert report["cases"][0]["status"] == expected
    for field in (
        "plan.0.main_pages",
        "plan.0.attachment_pages",
        "requisites.attachments.page_ranges",
    ):
        assert (
            next(row for row in report["rows"] if row["field_path"] == field)["status"] == expected
        )
    assert (
        next(
            row for row in report["rows"] if row["field_path"] == "documents.physical_attachments"
        )["status"]
        == "match"
    )


async def test_http_failure_report_has_code_and_status_without_response_body(tmp_path):
    """Ошибка HTTP фиксируется для каждого кейса без signed URL или message из JSON body."""
    app = FastAPI()
    marker = "synthetic-private-error-body"

    @app.post("/api/v1/jobs")
    async def unavailable():
        """Возвращает контролируемый ответ, тело которого запрещено сохранять в отчёт."""
        return JSONResponse({"message": marker}, status_code=503)

    cases = [comparison_case(f"case-{number}.pdf") for number in range(2)]
    for case in cases:
        (tmp_path / case["file"]).write_bytes(pdf_bytes())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        report = await run_comparison(
            client,
            cases,
            tmp_path,
            tmp_path / "report.json",
            headers=KEY,
            job_timeout_seconds=1,
            poll_interval=0,
            mode="OFFLINE_ASGI_SYNTHETIC",
        )
    assert len(report["cases"]) == 2
    assert all(
        case["http_status"] == 503 and case["code"] == "submit_http_error"
        for case in report["cases"]
    )
    assert marker not in (tmp_path / "report.json").read_text("utf-8")


async def test_cancellation_propagates_after_final_report_is_saved(tmp_path):
    """Отмена evaluation не теряет частный отчёт и не маскируется под успешный прогон."""
    app = FastAPI()
    entered = asyncio.Event()

    @app.post("/api/v1/jobs")
    async def submit():
        """Создаёт synthetic ID без действительного анализа внешними моделями."""
        return JSONResponse({"job_id": "synthetic-job"}, status_code=202)

    @app.get("/api/v1/jobs/{job_id}")
    async def wait_status(job_id: str):
        """Ждёт внешней отмены, имитируя незавершённый HTTP polling."""
        entered.set()
        await asyncio.Event().wait()

    case = comparison_case("cancelled.pdf")
    (tmp_path / case["file"]).write_bytes(pdf_bytes())
    report_path = tmp_path / "report.json"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        task = asyncio.create_task(
            run_comparison(
                client,
                [case],
                tmp_path,
                report_path,
                headers=KEY,
                job_timeout_seconds=5,
                mode="OFFLINE_ASGI_SYNTHETIC",
            )
        )
        await asyncio.wait_for(entered.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    report = json.loads(report_path.read_text("utf-8"))
    assert report["cases"][0]["status"] == "error"
    assert report["cases"][0]["code"] == "evaluation_cancelled"
    assert report["cases"][0]["error_type"] == "CancelledError"
