# tests/test_page_order_api.py
"""REST сохраняет явный порядок, ревизию и provenance; итоговый PDF следует ручному плану."""

import hashlib

import pymupdf
import pytest

from app.worker import process
from tests.test_boundary_api import FOREIGN, KEY, confirm, create, segmentation


async def test_reordered_pdf_and_json_follow_approved_sequence_without_changing_original(
    client, container
):
    """Страница 31 после 32 сохраняется через ACK/confirm и реально меняет порядок PDF."""
    data = await create(client, endpoint="uploads", pages=40)
    identifier = data["job_id"]
    await process(container, identifier)
    saved = await segmentation(client, identifier)
    refs = saved["draft_plan"]["documents"][0]["pages"]
    order = [*refs[:30], refs[31], refs[30], *refs[32:]]
    response = await client.put(
        f"/api/v1/jobs/{identifier}/segmentation",
        headers=KEY,
        json={
            "expected_revision": saved["plan_revision"],
            "documents": [{"client_id": "reordered", "pages": order}],
            "page_order": order,
            "action": "page_move",
        },
    )
    assert response.status_code == 200
    acknowledged = response.json()
    assert acknowledged["draft_plan"]["page_order"] == order and acknowledged["can_confirm"]
    assert (await segmentation(client, identifier))["draft_plan"]["page_order"] == order
    await confirm(client, identifier, acknowledged["plan_revision"])
    denied = await client.put(
        f"/api/v1/jobs/{identifier}/segmentation",
        headers=KEY,
        json={
            "expected_revision": acknowledged["plan_revision"],
            "documents": acknowledged["draft_plan"]["documents"],
            "page_order": order,
        },
    )
    assert denied.status_code == 409 and denied.json()["error"]["code"] == "plan_locked"
    job = await container.database.get(identifier)
    assert [p.page_number for p in job.approved_plan.page_order] == [
        p["page_number"] for p in order
    ]
    await process(container, identifier)
    result = (await client.get(f"/api/v1/jobs/{identifier}/result", headers=KEY)).json()
    assert result["schema_version"] == "1.0" and "page_order" not in result
    doc = result["files"][0]["documents"][0]
    assert doc["pages"] == [p["page_number"] for p in order]
    downloaded = await client.get(doc["attachment_file"]["file_url"])
    with pymupdf.open(stream=downloaded.content) as pdf:
        assert [p.get_text().strip() for p in pdf] == [
            f"SYNTHETIC PAGE {p['page_number']}" for p in order
        ]
    manifest = (
        await client.get(
            f"/api/v1/jobs/{identifier}/documents/{doc['document_id']}/manifest", headers=KEY
        )
    ).json()
    assert manifest["pages"] == order
    assert hashlib.sha256(downloaded.content).hexdigest() == manifest["sha256"]
    original = await client.get(result["files"][0]["file_url"])
    with pymupdf.open(stream=original.content) as pdf:
        assert [p.get_text().strip() for p in pdf] == [f"SYNTHETIC PAGE {n}" for n in range(1, 41)]
    assert not any(call["schema"]["title"] == "PairAnswer" for call in container.vision.calls)


@pytest.mark.parametrize("problem", ["duplicate", "missing", "foreign", "stale", "owner"])
async def test_page_order_obeys_coverage_owner_and_revision(client, container, problem):
    """Перестановка не обходит полное покрытие, принадлежность сессии и конфликт ревизии."""
    data = await create(client, endpoint="uploads", pages=3)
    identifier = data["job_id"]
    await process(container, identifier)
    saved = await segmentation(client, identifier)
    refs = saved["draft_plan"]["documents"][0]["pages"]
    order = list(reversed(refs))
    body = {
        "expected_revision": saved["plan_revision"],
        "documents": [{"client_id": "one", "pages": list(order)}],
        "page_order": list(order),
    }
    expected, headers = 422, KEY
    if problem == "duplicate":
        body["page_order"][0] = body["page_order"][1]
    elif problem == "missing":
        body["page_order"].pop()
    elif problem == "foreign":
        body["page_order"][0] = {"file_id": "foreign", "page_number": 1}
    elif problem == "stale":
        body["expected_revision"] -= 1
        expected = 409
    else:
        expected, headers = 404, FOREIGN
    response = await client.put(
        f"/api/v1/jobs/{identifier}/segmentation", headers=headers, json=body
    )
    assert response.status_code == expected
    assert (await segmentation(client, identifier))["plan_revision"] == saved["plan_revision"]


async def test_page_order_cannot_be_saved_during_partial_preview(client):
    """Новая перестановка требует полного реестра; старый ручной draft остаётся доступным."""
    data = await create(client, endpoint="uploads", pages=3)
    response = await client.put(
        f"/api/v1/jobs/{data['job_id']}/segmentation",
        headers=KEY,
        json={"expected_revision": 0, "documents": [], "page_order": []},
    )
    assert response.status_code == 409 and response.json()["error"]["code"] == "preview_not_ready"
