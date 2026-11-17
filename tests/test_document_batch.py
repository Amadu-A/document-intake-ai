# tests/test_document_batch.py
"""Пакет сохраняет все PDF, изолирует непроверенный блок и восстанавливает готовые результаты."""

import hashlib
from datetime import timedelta

import pymupdf
import pytest

from app.domain.errors import LeaseLost
from app.domain.jobs import utcnow
from app.infrastructure.database import JobRow
from app.worker import process
from tests.support import facts, pdf_bytes
from tests.test_document_processing import classification

KEY = {"X-API-Key": "e" * 48}


async def batch(client, container, count=4):
    """Создаёт и явно утверждает по одному документу на каждую синтетическую страницу."""
    response = await client.post(
        "/api/v1/uploads",
        headers=KEY,
        files={"files": ("batch.pdf", pdf_bytes(count), "application/pdf")},
    )
    identifier = response.json()["job_id"]
    await process(container, identifier)
    url = f"/api/v1/jobs/{identifier}/segmentation"
    saved = (await client.get(url, headers=KEY)).json()
    refs = saved["draft_plan"]["documents"][0]["pages"]
    changed = await client.put(
        url,
        headers=KEY,
        json={
            "expected_revision": saved["plan_revision"],
            "documents": [{"client_id": f"doc-{i}", "pages": [ref]} for i, ref in enumerate(refs)],
            "excluded_pages": [],
        },
    )
    response = await client.post(
        url + "/confirm", headers=KEY, json={"expected_revision": changed.json()["plan_revision"]}
    )
    assert response.status_code == 202
    return identifier


async def test_mixed_batch_keeps_valid_letters_placeholders_and_every_pdf(client, container):
    """Отказ одного блока сохраняет остальные; неписьмо получает явную заглушку."""
    identifier = await batch(client, container)
    first, last = facts(), facts()
    first["requisites"]["document_number"] = "PRIVATE_VALID_FIRST"
    last["requisites"]["document_number"] = "PRIVATE_VALID_LAST"
    container.vision.answers.extend(
        [
            classification(),
            first,
            classification(False),
            classification(),
            *[
                {"requisites": {"document_number": "PRIVATE_INVALID"}}
                for _ in range(container.settings.model_attempts)
            ],
            classification(),
            last,
        ]
    )
    await process(container, identifier)
    status = (await client.get(f"/api/v1/jobs/{identifier}", headers=KEY)).json()
    assert status["status"] == "completed_with_warnings" and status["error"] is None
    assert status["work_progress"]["extraction"] == {"completed": 4, "total": 4}
    processing = [a["processing"] for a in status["artifacts"]]
    assert [p["status"] for p in processing] == [
        "recognized",
        "unsupported_template",
        "extraction_failed",
        "recognized",
    ]
    result = (await client.get(f"/api/v1/jobs/{identifier}/result", headers=KEY)).json()
    assert set(result) == {"schema_version", "batch_id", "files"}
    docs = result["files"][0]["documents"]
    assert [d["pages"] for d in docs] == [[1], [2], [3], [4]]
    assert docs[0]["requisites"]["document_number"] == "PRIVATE_VALID_FIRST"
    assert docs[3]["requisites"]["document_number"] == "PRIVATE_VALID_LAST"
    assert docs[1]["requisites"]["related_entities"]["court_cases"] == []
    assert docs[2]["requisites"]["document_number"] is None
    assert "extraction_failed" in str(docs[2]["warnings"])
    for doc, artifact in zip(docs, status["artifacts"], strict=True):
        response = await client.get(doc["attachment_file"]["file_url"])
        assert hashlib.sha256(response.content).hexdigest() == artifact["sha256"]
        with pymupdf.open(stream=response.content) as pdf:
            assert len(pdf) == 1
            assert f"SYNTHETIC PAGE {doc['pages'][0]}" in pdf[0].get_text()
    events = await client.get(f"/api/v1/jobs/{identifier}/events?include_pages=false", headers=KEY)
    assert "document_completed" in events.text
    assert "PRIVATE_VALID_FIRST" not in events.text and "PRIVATE_INVALID" not in events.text
    assert str(container.storage.root) not in events.text


async def test_restart_reuses_completed_block_checkpoints(client, container):
    """После потери worker первые два результата не вызывают повторную классификацию/извлечение."""
    identifier = await batch(client, container, count=3)
    original = container.database.checkpoint
    committed = 0

    async def interrupted(job_id, fence, kind, data):
        """Прерывает worker после фиксации второго проверенного результата."""
        nonlocal committed
        await original(job_id, fence, kind, data)
        if kind == "document_completed":
            committed += 1
            if committed == 2:
                raise LeaseLost()

    container.database.checkpoint = interrupted
    claimed = await container.database.claim(identifier)
    with pytest.raises(LeaseLost):
        await container.analysis.execute(claimed)
    saved = await container.database.get(identifier)
    assert set(saved.document_results) == {"1", "2"}
    assert (
        len([c for c in container.vision.calls if c["schema"]["title"] == "DocumentClassification"])
        == 2
    )
    container.database.checkpoint = original
    async with container.database.sessions.begin() as session:
        row = await session.get(JobRow, identifier)
        row.lease_until = utcnow() - timedelta(seconds=1)
    await container.database.recover()
    await process(container, identifier)
    final = await container.database.get(identifier)
    assert final.status == "completed_with_warnings"
    assert (
        len([c for c in container.vision.calls if c["schema"]["title"] == "DocumentClassification"])
        == 3
    )
    assert (
        len([c for c in container.vision.calls if c["schema"]["title"] == "ExtractionDraft"]) == 3
    )
    assert [d["pages"] for d in final.result["files"][0]["documents"]] == [[1], [2], [3]]
