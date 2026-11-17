# tests/test_processing_progress.py
"""Общий default 1С/Telegram и сохраняемые фактические счётчики этапов."""

import asyncio

import pytest

from app.domain.errors import LeaseLost
from app.worker import process
from tests.support import MemoryUpload, pdf_bytes


async def test_openapi_uploads_advertise_review_defaults(client):
    """Swagger для обоих multipart endpoints соответствует фактическому review-default."""
    schema = (await client.get("/openapi.json")).json()
    for endpoint in ("jobs", "uploads"):
        body = schema["paths"][f"/api/v1/{endpoint}"]["post"]["requestBody"]
        reference = body["content"]["multipart/form-data"]["schema"]["$ref"].split("/")[-1]
        properties = schema["components"]["schemas"][reference]["properties"]
        assert properties["auto_split"]["default"] is False
        assert properties["skip_segmentation"]["default"] is False


@pytest.mark.parametrize("key", ["e", "f"], ids=["onec", "telegram"])
@pytest.mark.parametrize("endpoint", ["jobs", "uploads"])
async def test_default_client_requires_revision_confirmation(client, container, key, endpoint):
    """Клиент без flags проходит предложение, запрет раннего результата, PUT и confirm."""
    headers = {"X-API-Key": key * 48}
    response = await client.post(
        f"/api/v1/{endpoint}",
        headers=headers,
        files={"files": ("review.pdf", pdf_bytes(2), "application/pdf")},
    )
    assert response.status_code == 202
    identifier = response.json()["job_id"]
    assert response.json()["mode"] == {"auto_split": False, "skip_segmentation": False}
    await process(container, identifier)
    if endpoint == "uploads":
        assert not container.vision.calls
        await client.post(f"/api/v1/jobs/{identifier}/start", headers=headers)
        await process(container, identifier)
    snapshot = (await client.get(f"/api/v1/jobs/{identifier}/snapshot", headers=headers)).json()
    assert snapshot["status"] == "awaiting_confirmation"
    assert snapshot["work_progress"] == {"ocr": {"completed": 2, "total": 2}}
    assert all(call["schema"]["title"] == "PairAnswer" for call in container.vision.calls)
    assert (
        await client.get(f"/api/v1/jobs/{identifier}/result", headers=headers)
    ).status_code == 409
    assert (
        await client.post(f"/api/v1/jobs/{identifier}/start", headers=headers)
    ).status_code == 409
    url = f"/api/v1/jobs/{identifier}/segmentation"
    segmentation = (await client.get(url, headers=headers)).json()
    assert segmentation["can_confirm"] and segmentation["proposed_plan"]
    original = segmentation["proposed_plan"]
    if endpoint == "uploads":
        refs = segmentation["draft_plan"]["documents"][0]["pages"]
        response = await client.put(
            url,
            headers=headers,
            json={
                "expected_revision": segmentation["plan_revision"],
                "documents": [
                    {"client_id": f"doc-{i}", "pages": [ref]} for i, ref in enumerate(refs)
                ],
                "excluded_pages": [],
                "action": "edit",
            },
        )
        assert response.status_code == 200
        segmentation = response.json()
        assert segmentation["proposed_plan"] == original
    response = await client.post(
        url + "/confirm", headers=headers, json={"expected_revision": segmentation["plan_revision"]}
    )
    assert response.status_code == 202
    await process(container, identifier)
    result = await client.get(f"/api/v1/jobs/{identifier}/result", headers=headers)
    assert result.status_code == 200 and result.json()["schema_version"] == "1.0"
    final = (await client.get(f"/api/v1/jobs/{identifier}/snapshot", headers=headers)).json()
    count = 2 if endpoint == "uploads" else 1
    assert final["work_progress"]["extraction"] == {"completed": count, "total": count}
    assert [d["pages"] for f in result.json()["files"] for d in f["documents"]] == (
        [[1], [2]] if endpoint == "uploads" else [[1, 2]]
    )


async def test_ocr_progress_is_independent_of_preview_and_survives_snapshot(client, container):
    """Готовые превью не означают готовый OCR; отменённый worker не меняет счётчик."""
    job = await container.intake.upload(
        "integration:onec", [MemoryUpload(pdf_bytes(3))], None, None, True
    )
    entered, release = asyncio.Event(), asyncio.Event()
    original = container.analysis.ocr.recognize

    async def paused(pages, emit):
        """Останавливает OCR после одной реально заявленной адаптером страницы."""
        await emit("ocr_progress", {"completed": 1, "total": 3})
        entered.set()
        await release.wait()
        return await original(pages, emit)

    container.analysis.ocr.recognize = paused
    task = asyncio.create_task(process(container, job.id))
    try:
        await asyncio.wait_for(entered.wait(), 10)
        headers = {"X-API-Key": "e" * 48}
        for _ in range(2):
            data = (
                await client.get(
                    f"/api/v1/jobs/{job.id}/snapshot?include_pages=false", headers=headers
                )
            ).json()
            assert data["pages_ready"] == 3
            assert data["work_progress"] == {"ocr": {"completed": 1, "total": 3}}
        worker = await container.database.get(job.id)
        await container.database.cancel(job.id, job.owner)
        with pytest.raises(LeaseLost):
            await container.database.checkpoint(
                job.id, worker.fence, "ocr_progress", {"completed": 3, "total": 3}
            )
    finally:
        release.set()
        await asyncio.wait_for(task, 5)
