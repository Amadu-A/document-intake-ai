"""Headless редактор: все команды, полнота страниц, revision и доступ owner."""

from app.worker import process
from tests.support import pdf_bytes
from tests.test_boundary_api import FOREIGN, KEY


async def test_all_server_actions_preserve_identity_coverage_and_revision(client, container):
    """Каждая операторская команда атомарна и доступна по тому же API без JS."""
    response = await client.post(
        "/api/v1/uploads",
        headers=KEY,
        files={"files": ("actions.pdf", pdf_bytes(4), "application/pdf")},
    )
    identifier = response.json()["job_id"]
    await process(container, identifier)
    url = f"/api/v1/jobs/{identifier}/segmentation"
    plan = (await client.get(url, headers=KEY)).json()
    refs = plan["initial_plan"]["documents"][0]["pages"]

    async def action(name, pages=None, target=None):
        """Отправляет актуальную revision, затем проверяет её монотонность и точное покрытие."""
        nonlocal plan
        revision = plan["plan_revision"]
        body = {"expected_revision": revision, "action": name, "pages": pages or []}
        if target:
            body["target"] = target
        result = await client.post(url + "/actions", headers=KEY, json=body)
        assert result.status_code == 200, result.json()
        plan = result.json()
        assert plan["plan_revision"] > revision and not plan["locked"]
        assigned = [p for d in plan["draft_plan"]["documents"] for p in d["pages"]]
        excluded = [
            {k: p[k] for k in ("file_id", "page_number")}
            for p in plan["draft_plan"]["excluded_pages"]
        ]
        assert sorted(p["page_number"] for p in assigned + excluded) == [1, 2, 3, 4]
        assert all(p["file_id"] == refs[0]["file_id"] for p in assigned + excluded)
        conflict = await client.post(url + "/actions", headers=KEY, json=body)
        assert conflict.status_code == 409
        assert (
            await client.post(
                url + "/actions",
                headers=FOREIGN,
                json={**body, "expected_revision": plan["plan_revision"]},
            )
        ).status_code == 404

    await action("manual")
    await action("split", [refs[0]])
    assert plan["document_count"] == 2
    await action("merge", [refs[0]])
    assert plan["document_count"] == 1
    await action("exclude", [refs[2]])
    assert plan["excluded_count"] == 1
    await action("restore", [refs[2]])
    assert plan["excluded_count"] == 0 and plan["document_count"] == 2
    await action("create", [refs[0], refs[2]])
    assert [p["page_number"] for p in plan["draft_plan"]["documents"][0]["pages"]] == [1, 3]
    await action("move", [refs[3]], refs[0])
    assert [p["page_number"] for p in plan["draft_plan"]["page_order"]] == [1, 4, 2, 3]
    await action("reset")
    assert [[p["page_number"] for p in d["pages"]] for d in plan["draft_plan"]["documents"]] == [
        [1, 2, 3, 4]
    ]
    invalid = await client.post(
        url + "/actions",
        headers=KEY,
        json={
            "expected_revision": plan["plan_revision"],
            "action": "exclude",
            "pages": [{"file_id": "foreign", "page_number": 1}],
        },
    )
    assert invalid.status_code == 422
