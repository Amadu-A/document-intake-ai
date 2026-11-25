# tests/test_registry_api.py
"""Настоящие REST/DB/PDF/XLSX: отсутствие новых inference, owner/TTL и экспорт legacy v1."""

import asyncio
import errno
import hashlib
from contextlib import closing
from datetime import timedelta
from io import BytesIO
from urllib.parse import unquote

import pytest
from openpyxl import load_workbook

from app.domain import jobs
from app.domain.jobs import utcnow
from app.domain.registry import XLSX_MIME, XLSX_NAME, RegistryResult, registry_mapping
from app.infrastructure.database import JobRow
from tests.test_boundary_api import FOREIGN, KEY, create
from tests.test_document_archive import completed_packet
from tests.test_registry import registry_job


async def saved(client, container):
    """Сохраняет явно synthetic исторический v1-only job, без fake GET handlers."""
    job = registry_job()
    await container.database.create(job, None, job.id)
    return job


def workbook_rows(payload):
    """Считывает реальную книгу для сопоставления с 14 JSON бизнес-ключами."""
    with closing(load_workbook(BytesIO(payload), data_only=False)) as workbook:
        return list(workbook.active.values)


async def assert_same_json_and_xlsx(client, identifier):
    """Проверяет имя, length/SHA/TTL, заголовки и каждое значение обеих выдач."""
    prefix = f"/api/v1/jobs/{identifier}"
    response = await client.get(prefix + "/registry", headers=KEY)
    assert response.status_code == 200
    body = response.json()
    RegistryResult.model_validate(body)
    file = await client.get(prefix + "/registry.xlsx", headers=KEY)
    assert file.status_code == 200 and file.headers["content-type"] == XLSX_MIME
    assert XLSX_NAME in unquote(file.headers["content-disposition"])
    assert len(file.content) == int(file.headers["content-length"])
    assert hashlib.sha256(file.content).hexdigest() == file.headers["x-content-sha256"]
    assert file.headers["x-expires-at"] and file.headers["cache-control"] == "private, no-store"
    mapping = registry_mapping()
    rows = await asyncio.to_thread(workbook_rows, file.content)
    assert list(rows[0]) == [mapping.columns[k][0] for k in mapping.column_order]
    assert [list(row) for row in rows[1:]] == [
        [item[k] for k in mapping.column_order] for item in body["items"]
    ]
    return body


async def test_legacy_json_and_xlsx_repeat_without_fact_job_or_inference_mutation(
    client, container
):
    """v2 не обязателен; повтор не меняет результат и не запускает анализ."""
    job = await saved(client, container)
    before = (await container.database.get(job.id)).model_dump(mode="json")
    assert (await client.get(f"/api/v1/jobs/{job.id}/extractions", headers=KEY)).status_code == 409
    first = await assert_same_json_and_xlsx(client, job.id)
    assert first == await assert_same_json_and_xlsx(client, job.id)
    assert not container.vision.calls
    assert before == (await container.database.get(job.id)).model_dump(mode="json")
    schema = await client.get("/api/v1/registry/schema", headers=KEY)
    assert schema.json() == registry_mapping().model_dump(mode="json")
    status = (await client.get(f"/api/v1/jobs/{job.id}", headers=KEY)).json()
    assert status["links"]["registry_xlsx"] == f"/api/v1/jobs/{job.id}/registry.xlsx"


async def test_expiry_during_workbook_render_denies_download(client, container, monkeypatch):
    """Задание, истёкшее во время генерации книги, не выдаёт файл за пределами TTL."""
    job = await saved(client, container)
    render = container.registry.writer.render

    def expires_while_rendering(result):
        """Завершает настоящий XLSX и переводит часы за фиксированный срок задания."""
        payload = render(result)
        monkeypatch.setattr(jobs, "utcnow", lambda: job.expires_at + timedelta(seconds=1))
        return payload

    container.registry.writer.render = expires_while_rendering
    response = await client.get(f"/api/v1/jobs/{job.id}/registry.xlsx", headers=KEY)
    assert response.status_code == 410 and response.json()["error"]["code"] == "job_expired"


async def test_real_headless_review_reorder_confirm_result_registry_flow(client, container):
    """Реальный редактор/worker/PDF со scripted VLM, межфайловая перестановка и единый порядок."""
    identifier, _ = await completed_packet(client, container)
    inference_calls = len(container.vision.calls)
    registry = await assert_same_json_and_xlsx(client, identifier)
    raw = (await client.get(f"/api/v1/jobs/{identifier}/result", headers=KEY)).json()
    documents = sorted(
        [d for f in raw["files"] for d in f["documents"]], key=lambda d: d["segment_index"]
    )
    assert [r["document_id"] for r in registry["items"]] == [d["document_id"] for d in documents]
    assert registry["items"][0]["document_number"] == "42/Б от 18.11.2026"
    assert len(container.vision.calls) == inference_calls


@pytest.mark.parametrize("suffix", ["registry", "registry.xlsx"])
async def test_registry_authorization_not_ready_expiry_and_terminal_errors(
    client, container, suffix
):
    """Нет публичного XLSX/чужих строк; queue/failed/cancelled не становятся пустым успехом."""
    job = await saved(client, container)
    path = f"/api/v1/jobs/{job.id}/{suffix}"
    assert (await client.get(path)).status_code == 401
    assert (await client.get(path, headers=FOREIGN)).status_code == 404
    pending = (await create(client, endpoint="uploads", pages=1))["job_id"]
    response = await client.get(f"/api/v1/jobs/{pending}/{suffix}", headers=KEY)
    assert response.status_code == 409 and response.json()["error"]["code"] == "registry_not_ready"
    async with container.database.sessions.begin() as session:
        row = await session.get(JobRow, job.id)
        data = dict(row.data)
        data["expires_at"] = (utcnow() - timedelta(seconds=1)).isoformat()
        row.data = data
    assert (await client.get(path, headers=KEY)).status_code == 410
    await container.database.purge(job.id)
    assert (await client.get(path, headers=KEY)).status_code == 404
    for status in ("failed", "cancelled"):
        bad = registry_job(1).model_copy(update={"status": status})
        await container.database.create(bad, None, bad.id)
        assert (await client.get(f"/api/v1/jobs/{bad.id}/{suffix}", headers=KEY)).status_code == 409


@pytest.mark.parametrize(
    "failure,expected",
    [(ValueError("PRIVATE_DOC"), 503), (OSError(errno.ENOSPC, "PRIVATE_DOC"), 507)],
)
async def test_export_error_is_safe_and_retry_does_not_damage_result(
    client, container, failure, expected
):
    """Ошибка XLSX не портит terminal result, не раскрывает текст и не блокирует повтор."""
    job = await saved(client, container)
    before = (await container.database.get(job.id)).model_dump(mode="json")
    render = container.registry.writer.render

    def broken(result):
        """Выбрасывает controlled отказ writer, не меняя business/result path."""
        raise failure

    container.registry.writer.render = broken
    response = await client.get(f"/api/v1/jobs/{job.id}/registry.xlsx", headers=KEY)
    assert response.status_code == expected and "PRIVATE_DOC" not in response.text
    container.registry.writer.render = render
    await assert_same_json_and_xlsx(client, job.id)
    assert before == (await container.database.get(job.id)).model_dump(mode="json")
