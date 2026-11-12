# tests/test_api.py
"""Функциональные HTTP-тесты с настоящими DB/storage/PDF и явно synthetic OCR/VLM."""

import hashlib
import json
from datetime import timedelta

import pymupdf
import pytest

from tests.support import facts, pdf_bytes, plan_for

KEY = {"X-API-Key": "e" * 48}
FOREIGN_KEY = {"X-API-Key": "f" * 48}


async def submit(client, count=1, key="request-a", endpoint="jobs"):
    """Загружает синтетический PDF в production endpoint."""
    return await client.post(
        f"/api/v1/{endpoint}",
        headers={**KEY, "Idempotency-Key": key},
        files={"files": ("synthetic.pdf", pdf_bytes(count), "application/pdf")},
    )


async def run_job(container, job_id):
    """Имитирует только AMQP delivery; claim и сам pipeline остаются production-кодом."""
    claimed = await container.database.claim(job_id)
    assert claimed is not None
    await container.analysis.execute(claimed)


async def test_headless_upload_poll_result_download(client, container):
    """1С-подобный клиент без UI получает 202, JSON 1.0 и целый PDF с настоящим checksum."""
    response = await submit(client)
    assert response.status_code == 202
    job_id = response.json()["job_id"]
    assert (await client.get(f"/api/v1/jobs/{job_id}/result", headers=KEY)).status_code == 409
    await run_job(container, job_id)
    status = (await client.get(f"/api/v1/jobs/{job_id}", headers=KEY)).json()
    result = (await client.get(f"/api/v1/jobs/{job_id}/result", headers=KEY)).json()
    assert result["schema_version"] == "1.0"
    assert status["status"] == "completed_with_warnings"
    artifact = result["files"][0]["documents"][0]["attachment_file"]
    downloaded = await client.get(artifact["file_url"])
    assert downloaded.status_code == 200
    assert downloaded.headers["content-type"] == "application/pdf"
    assert int(downloaded.headers["content-length"]) == len(downloaded.content)
    assert hashlib.sha256(downloaded.content).hexdigest() == status["artifacts"][0]["sha256"]
    with pymupdf.open(stream=downloaded.content) as pdf:
        assert len(pdf) == 1
        assert "SYNTHETIC PAGE 1" in pdf[0].get_text()
    source = await client.get(result["files"][0]["file_url"])
    assert hashlib.sha256(source.content).hexdigest() == status["sources"][0]["sha256"]
    assert "one_c_matching" not in json.dumps(result)


async def test_two_independent_letters_produce_two_complete_pdfs(client, container):
    """Два явных решения VLM дают две регистрации и два PDF без повторов/потери страниц."""
    job_id = (await submit(client, 2)).json()["job_id"]
    file_id = (await container.database.get(job_id)).sources[0].file_id
    first = plan_for([{"file_id": file_id, "page_number": 1}])
    second = plan_for([{"file_id": file_id, "page_number": 2}])
    second["documents"][0]["document_index"] = 2
    first["documents"].extend(second["documents"])
    container.vision.answers.extend([first, facts(), facts()])
    await run_job(container, job_id)
    result = (await client.get(f"/api/v1/jobs/{job_id}/result", headers=KEY)).json()
    documents = result["files"][0]["documents"]
    assert len(documents) == 2
    for number, document in enumerate(documents, 1):
        assert document["pages"] == [number]
        response = await client.get(document["attachment_file"]["file_url"])
        with pymupdf.open(stream=response.content) as pdf:
            assert len(pdf) == 1 and f"PAGE {number}" in pdf[0].get_text()


async def test_owner_isolation_and_scoped_download_capabilities(client, container):
    """Другой API owner не читает статус/result и не может перенести token на другой путь."""
    response = await submit(client)
    job_id = response.json()["job_id"]
    source_url = response.json()["sources"][0]["file_url"]
    assert (await client.get(f"/api/v1/jobs/{job_id}", headers=FOREIGN_KEY)).status_code == 404
    assert (
        await client.get(f"/api/v1/jobs/{job_id}/result", headers=FOREIGN_KEY)
    ).status_code == 404
    assert (
        await client.post(f"/api/v1/jobs/{job_id}/cancel", headers=FOREIGN_KEY)
    ).status_code == 404
    assert (await client.get(source_url)).status_code == 200
    invalid = source_url.replace("source-files/", "documents/").replace("?token", "/file?token")
    assert (await client.get(invalid)).status_code == 404
    assert (await client.get(f"/api/v1/jobs/{job_id}")).status_code == 401


async def test_idempotency_replay_and_conflicting_payload(client, container):
    """Одинаковые bytes/key не создают второе задание; иной payload с тем же ключом даёт 409."""
    payload = pdf_bytes()

    async def request(value):
        """Повторяет тот же API-запрос с заданным телом файла."""
        return await client.post(
            "/api/v1/jobs",
            headers={**KEY, "Idempotency-Key": "same"},
            files={"files": ("same.pdf", value, "application/pdf")},
        )

    first, replay = await request(payload), await request(payload)
    assert first.json()["job_id"] == replay.json()["job_id"]
    assert (await request(pdf_bytes(2))).status_code == 409
    assert len(list(container.storage.root.iterdir())) == 1


async def test_prompt_sessions_and_immutable_job_snapshots(client, container):
    """Две независимые вкладки изолированы owner; запущенное задание сохраняет прежние тексты."""
    original = (await client.get("/api/v1/session/prompts", headers=KEY)).json()["prompts"]
    edited = {
        "segmentation": original["segmentation"] + "\nТестовая правка",
        "extraction": original["extraction"],
    }
    assert (
        await client.put("/api/v1/session/prompts", headers=KEY, json=edited)
    ).status_code == 200
    response = await submit(client)
    job_id = response.json()["job_id"]
    await client.post("/api/v1/session/prompts/segmentation/restore", headers=KEY)
    job = await container.database.get(job_id)
    assert job.prompts == edited
    other = (await client.get("/api/v1/session/prompts", headers=FOREIGN_KEY)).json()["prompts"]
    assert other == original


async def test_browser_cookie_csrf_and_origin(client):
    """Anonymous UI не имеет login, но cookie mutation требует и CSRF, и точный Origin."""
    page = await client.get("/")
    assert page.status_code == 200
    cookie = page.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=strict" in cookie
    import re

    csrf = re.search('name="csrf-token" content="([^"]+)"', page.text).group(1)
    prompts = (await client.get("/api/v1/session/prompts")).json()["prompts"]
    assert (await client.put("/api/v1/session/prompts", json=prompts)).status_code == 403
    assert (
        await client.put(
            "/api/v1/session/prompts",
            headers={"X-CSRF-Token": csrf, "Origin": "http://foreign"},
            json=prompts,
        )
    ).status_code == 403
    assert (
        await client.put(
            "/api/v1/session/prompts",
            headers={"X-CSRF-Token": csrf, "Origin": "http://testserver"},
            json=prompts,
        )
    ).status_code == 200


async def test_missing_pages_trigger_retry_then_unverified_page_draft(client, container):
    """После ограниченных повторов backend предлагает лишь черновик для явной проверки."""
    response = await submit(client, 2)
    job_id = response.json()["job_id"]
    job = await container.database.get(job_id)
    bad = plan_for([{"file_id": job.sources[0].file_id, "page_number": 1}])
    container.vision.answers.extend([bad, bad])
    await run_job(container, job_id)
    status = (await client.get(f"/api/v1/jobs/{job_id}", headers=KEY)).json()
    assert status["status"] == "awaiting_confirmation"
    assert status["segmentation_source"] == "page_fallback"
    assert status["segmentation_fallback"]["code"] == "invalid_segmentation"
    assert status["error"] is None and not status["plan_locked"]
    assert status["sources"][0]["file_url"].startswith("http://testserver/api/")
    assert (await client.get(f"/api/v1/jobs/{job_id}/result", headers=KEY)).status_code == 409


async def test_invalid_plan_is_retried_with_feedback(client, container):
    """Повторный запрос получает список ошибок, после корректного ответа обработка завершается."""
    response = await submit(client, 2)
    job_id = response.json()["job_id"]
    file_id = (await container.database.get(job_id)).sources[0].file_id
    container.vision.answers.extend(
        [
            plan_for([{"file_id": file_id, "page_number": 1}]),
            plan_for([{"file_id": file_id, "page_number": n} for n in (1, 2)]),
            facts(),
        ]
    )
    await run_job(container, job_id)
    assert container.vision.calls[1]["context"]["validation_errors"]
    assert (await container.database.get(job_id)).status == "completed_with_warnings"


async def test_claim_with_four_attachments_exports_six_pages(client, container):
    """Регрессия структуры 4-2591: один документ, четыре приложения, PDF из шести страниц."""
    response = await submit(client, 6)
    job_id = response.json()["job_id"]
    file_id = (await container.database.get(job_id)).sources[0].file_id
    refs = [{"file_id": file_id, "page_number": n} for n in range(1, 7)]
    attachments = [
        {
            "title": f"Приложение {n}",
            "page_range": [n, n],
            "document_kind": "Акт",
            "document_root": "Документы связанные с исполнением договоров",
        }
        for n in range(3, 7)
    ]
    container.vision.answers.extend([plan_for(refs, 2), facts(attachments)])
    await run_job(container, job_id)
    result = (await client.get(f"/api/v1/jobs/{job_id}/result", headers=KEY)).json()
    documents = result["files"][0]["documents"]
    assert len(documents) == 1 and len(documents[0]["requisites"]["attachments"]) == 4
    pdf_response = await client.get(documents[0]["attachment_file"]["file_url"])
    with pymupdf.open(stream=pdf_response.content) as pdf:
        assert len(pdf) == 6
        assert all(f"PAGE {n}" in pdf[n - 1].get_text() for n in range(1, 7))


async def test_ttl_denies_access_before_physical_cleanup(client, container, monkeypatch):
    """Файл ещё на диске, но API уже отклоняет даже ранее корректную подписанную ссылку."""
    response = await submit(client, endpoint="uploads")
    job_id = response.json()["job_id"]
    url = response.json()["sources"][0]["file_url"]
    await run_job(container, job_id)
    expired_at = (await container.database.get(job_id)).expires_at + timedelta(seconds=1)
    monkeypatch.setattr("app.domain.jobs.utcnow", lambda: expired_at)
    monkeypatch.setattr("app.infrastructure.security.utcnow", lambda: expired_at)
    monkeypatch.setattr("app.application.exports.utcnow", lambda: expired_at)
    assert container.storage.directory(job_id).exists()
    assert (await client.get(url)).status_code == 410
    assert (await client.get(f"/api/v1/jobs/{job_id}", headers=KEY)).status_code == 410


async def test_cancel_is_idempotent_and_prevents_claim(client, container):
    """Повторная отмена сохраняет один TTL и не позволяет worker захватить отменённое задание."""
    job_id = (await submit(client)).json()["job_id"]
    first = (await client.post(f"/api/v1/jobs/{job_id}/cancel", headers=KEY)).json()
    second = (await client.post(f"/api/v1/jobs/{job_id}/cancel", headers=KEY)).json()
    assert first["expires_at"] == second["expires_at"]
    assert first["status"] == "cancelled"
    assert await container.database.claim(job_id) is None


async def test_sse_replay_excludes_ocr_and_private_paths(client, container):
    """Журнал replay монотонен; normalized-событие не раскрывает filesystem paths/OCR."""
    job_id = (await submit(client)).json()["job_id"]
    await run_job(container, job_id)
    response = await client.get(f"/api/v1/jobs/{job_id}/events?after=1", headers=KEY)
    assert response.status_code == 200
    ids = [int(line[4:]) for line in response.text.splitlines() if line.startswith("id: ")]
    assert ids == sorted(set(ids)) and min(ids) > 1
    assert "text_delta" in response.text and '"validated": false' in response.text
    assert str(container.storage.root) not in response.text
    assert "SYNTHETIC OCR" not in response.text


@pytest.mark.parametrize(
    "name,mime,payload",
    [
        ("bad.pdf", "application/pdf", b"not a pdf"),
        ("archive.zip", "application/zip", b"PK"),
        ("bad.png", "application/pdf", b"\x89PNG\r\n\x1a\n"),
    ],
)
async def test_signature_mime_and_format_limits(client, name, mime, payload):
    """Не принимает подмену сигнатуры/MIME и архив вместо разрешённого документа."""
    response = await client.post(
        "/api/v1/jobs", headers=KEY, files={"files": (name, payload, mime)}
    )
    assert response.status_code == 422


async def test_upload_size_is_checked_before_multipart_parser(client, container):
    """Слишком большой Content-Length отклоняется до spool файлов на диск."""
    response = await client.post(
        "/api/v1/jobs",
        headers={
            **KEY,
            "Content-Length": str((container.settings.upload_max_mb + 2) * 1024 * 1024),
        },
        content=b"x",
    )
    assert response.status_code == 413
    assert not list(container.storage.root.iterdir())
