# tests/test_boundary_api.py
"""Новые headless режимы и ручной-first используют настоящий REST/DB/PDF, внешние AI — doubles."""

import asyncio
import copy
import hashlib
import json

import pymupdf
import pytest
from sqlalchemy import delete

from app.infrastructure.database import JobRow, SignalRow
from app.worker import process
from tests.support import ScriptedVision, facts, pdf_bytes, plan_for

KEY = {"X-API-Key": "e" * 48}
FOREIGN = {"X-API-Key": "f" * 48}


async def create(client, *, mode=None, endpoint="jobs", pages=4, files=1, key=None):
    """Создаёт синтетическую пачку через неизменный multipart files контракт."""
    headers = {**KEY, **({"Idempotency-Key": key} if key else {})}
    response = await client.post(
        f"/api/v1/{endpoint}",
        headers=headers,
        data=mode or {},
        files=[
            ("files", (f"synthetic-{n}.pdf", pdf_bytes(pages), "application/pdf"))
            for n in range(files)
        ],
    )
    assert response.status_code == 202, response.text
    return response.json()


async def segmentation(client, job_id):
    """Получает авторитетную ревизию и draft без обращения к приватному ORM."""
    response = await client.get(f"/api/v1/jobs/{job_id}/segmentation", headers=KEY)
    assert response.status_code == 200
    return response.json()


async def confirm(client, job_id, revision):
    """Подтверждает ровно сохранённую ревизию и проверяет async HTTP 202."""
    response = await client.post(
        f"/api/v1/jobs/{job_id}/segmentation/confirm",
        headers=KEY,
        json={"expected_revision": revision},
    )
    assert response.status_code == 202, response.text
    return response.json()


async def test_mode_change_before_first_claim_keeps_preparation_deliverable(client, container):
    """Ранняя смена режима headless job не оставляет queued+prepare без живого worker."""
    data = await create(client, pages=2)
    changed = await client.patch(
        f"/api/v1/jobs/{data['job_id']}/mode",
        headers=KEY,
        json={"expected_revision": 0, "auto_split": False, "skip_segmentation": True},
    )
    assert changed.status_code == 200
    await process(container, data["job_id"])
    job = await container.database.get(data["job_id"])
    assert job.status == "uploaded" and job.preview_complete
    assert not container.vision.calls
    await container.intake.start(job.id, job.owner)
    await process(container, job.id)
    assert (await container.database.get(job.id)).status == "completed_with_warnings"
    assert all(c["schema"]["title"] != "Plan" for c in container.vision.calls)


@pytest.mark.parametrize(
    "manual", [True, False], ids=["manual-before-preview", "mode-during-preview"]
)
async def test_edit_during_preview_keeps_worker_and_next_command_consistent(
    client, container, manual
):
    """Правка до первого PNG сохраняет подготовку, а следующий этап запускается явной командой."""
    entered, release = asyncio.Event(), asyncio.Event()
    normalize = container.storage.normalize

    async def paused_normalize(job, emit):
        """Останавливает реальный converter после обнаружения страниц до завершения рендеров."""

        async def checkpoint(kind, data):
            """Передаёт durable metadata прежде, чем разрешить следующие страницы."""
            await emit(kind, data)
            if kind == "source_discovered":
                entered.set()
                await release.wait()

        return await normalize(job, checkpoint)

    container.storage.normalize = paused_normalize
    data = await create(client, pages=4)
    worker = asyncio.create_task(process(container, data["job_id"]))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        draft = await segmentation(client, data["job_id"])
        if manual:
            response = await client.put(
                f"/api/v1/jobs/{data['job_id']}/segmentation",
                headers=KEY,
                json={"expected_revision": draft["plan_revision"], **draft["draft_plan"]},
            )
        else:
            response = await client.patch(
                f"/api/v1/jobs/{data['job_id']}/mode",
                headers=KEY,
                json={
                    "expected_revision": draft["plan_revision"],
                    "auto_split": False,
                    "skip_segmentation": True,
                },
            )
        assert response.status_code == 200
        requested = await client.post(f"/api/v1/jobs/{data['job_id']}/start", headers=KEY)
        assert requested.status_code == (409 if manual else 202)
        release.set()
        await asyncio.wait_for(worker, 5)
        job = await container.database.get(data["job_id"])
        assert job.preview_complete and not container.vision.calls
        if manual:
            assert job.status == "awaiting_confirmation"
            await confirm(client, job.id, job.plan_revision)
        else:
            assert job.status == "queued"
        await process(container, job.id)
        assert (await container.database.get(job.id)).status == "completed_with_warnings"
        assert all(c["schema"]["title"] != "Plan" for c in container.vision.calls)
    finally:
        release.set()
        if not worker.done():
            worker.cancel()


async def test_review_releases_worker_and_confirm_reuses_proposal(client, container):
    """Review не публикует JSON до подтверждения; повтор confirm не создаёт новый анализ."""
    data = await create(client, mode={"auto_split": "false", "skip_segmentation": "false"})
    job_id = data["job_id"]
    await process(container, job_id)
    job = await container.database.get(job_id)
    assert job.status == "awaiting_confirmation" and job.expires_at is not None
    assert not await container.database.heartbeat(job_id, job.fence)
    assert job_id not in await container.database.pending()
    assert (await client.get(f"/api/v1/jobs/{job_id}/result", headers=KEY)).status_code == 409
    assert len(container.vision.calls) == 1
    proposed = copy.deepcopy(job.proposed_plan.model_dump())
    first = await confirm(client, job_id, job.plan_revision)
    second = await confirm(client, job_id, job.plan_revision)
    assert first["cursor"] == second["cursor"]
    assert (await container.database.get(job_id)).segmentation_source == "vlm_confirmed"
    await process(container, job_id)
    completed = await container.database.get(job_id)
    assert completed.status == "completed_with_warnings"
    assert not completed.skip_segmentation and not completed.auto_split
    assert completed.proposed_plan.model_dump() == proposed
    assert len([c for c in container.vision.calls if c["schema"]["title"] == "Plan"]) == 1


async def test_skip_is_headless_one_registration_per_source_without_segmentation(client, container):
    """Skip с отдельными файлами не требует confirm и ни разу не вызывает модель границ."""
    data = await create(client, mode={"auto_split": "false", "skip_segmentation": "true"}, files=2)
    await process(container, data["job_id"])
    result = await client.get(f"/api/v1/jobs/{data['job_id']}/result", headers=KEY)
    assert result.status_code == 200
    assert [len(source["documents"]) for source in result.json()["files"]] == [1, 1]
    assert all(c["schema"]["title"] != "Plan" for c in container.vision.calls)
    assert len(container.vision.calls) == 2


async def test_return_to_auto_reuses_vlm_roles_and_original_prompt_snapshots(client, container):
    """Возврат в auto сохраняет VLM-приложения и неизменяемые промпты уже начатого задания."""
    container.vision.answers.append(
        lambda context: plan_for(
            [
                {"file_id": page["file_id"], "page_number": page["page_number"]}
                for page in context["pages"]
            ],
            main=2,
        )
    )
    container.vision.answers.append(
        facts([{"title": "Приложение", "page_range": [n, n]} for n in range(3, 7)])
    )
    data = await create(client, mode={"auto_split": "false", "skip_segmentation": "false"}, pages=6)
    await process(container, data["job_id"])
    job = await container.database.get(data["job_id"])
    snapshots = dict(job.prompts)
    edited = await container.database.save_draft(
        job.id, job.owner, job.plan_revision, job.draft_plan, "manual"
    )
    restored = await container.database.change_mode(
        job.id, job.owner, edited.plan_revision, True, False
    )
    assert restored.segmentation_source == "vlm_auto"
    await container.prompts.save(
        job.owner, {name: text + "\nНовая сессионная правка" for name, text in snapshots.items()}
    )
    await container.intake.start(job.id, job.owner)
    await process(container, job.id)
    completed = await container.database.get(job.id)
    assert completed.status == "completed_with_warnings" and completed.prompts == snapshots
    assert completed.segmentation_source == "vlm_auto"
    assert len([call for call in container.vision.calls if call["schema"]["title"] == "Plan"]) == 1
    assert len(container.vision.calls[-1]["context"]["group"]["main_pages"]) == 2
    assert len(container.vision.calls[-1]["context"]["group"]["attachments"]) == 4


async def test_manual_first_atomic_mode_exclusion_and_fixed_pdf_groups(client, container):
    """Подготовка не трогает AI; ручные страницы/исключение сохраняются в PDF и manifest."""
    data = await create(client, endpoint="uploads", pages=5)
    job_id = data["job_id"]
    assert data["status"] == "uploaded" and data["stage"] == "preparing_preview"
    denied = await client.post(
        f"/api/v1/jobs/{job_id}/segmentation/confirm", headers=KEY, json={"expected_revision": 0}
    )
    assert denied.status_code == 409 and denied.json()["error"]["code"] == "preview_not_ready"
    await process(container, job_id)
    assert not container.vision.calls
    plan = await segmentation(client, job_id)
    refs = plan["draft_plan"]["documents"][0]["pages"]
    response = await client.put(
        f"/api/v1/jobs/{job_id}/segmentation",
        headers=KEY,
        json={
            "expected_revision": plan["plan_revision"],
            "documents": [
                {"client_id": "first", "pages": refs[:2]},
                {"client_id": "second", "pages": refs[3:]},
            ],
            "excluded_pages": [refs[2]],
            "action": "exclude",
        },
    )
    assert response.status_code == 200
    saved = response.json()
    assert not saved["auto_split"] and saved["skip_segmentation"]
    assert saved["segmentation_source"] == "manual" and saved["can_confirm"]
    assert saved["assigned_count"] == 4 and saved["excluded_count"] == 1
    assert not container.vision.calls
    await confirm(client, job_id, saved["plan_revision"])
    locked = await client.patch(
        f"/api/v1/jobs/{job_id}/mode",
        headers=KEY,
        json={
            "expected_revision": saved["plan_revision"],
            "auto_split": True,
            "skip_segmentation": False,
        },
    )
    assert locked.status_code == 409 and locked.json()["error"]["code"] == "plan_locked"
    await process(container, job_id)
    response = await client.get(f"/api/v1/jobs/{job_id}/result", headers=KEY)
    assert response.status_code == 200
    result = response.json()
    assert set(result) == {"schema_version", "batch_id", "files"}
    docs = result["files"][0]["documents"]
    assert [d["pages"] for d in docs] == [[1, 2], [4, 5]]
    for document in docs:
        download = await client.get(document["attachment_file"]["file_url"])
        with pymupdf.open(stream=download.content) as pdf:
            assert len(pdf) == 2
            assert "SYNTHETIC PAGE 3" not in "".join(page.get_text() for page in pdf)
        manifest = (
            await client.get(
                f"/api/v1/jobs/{job_id}/documents/{document['document_id']}/manifest", headers=KEY
            )
        ).json()
        assert manifest["excluded_pages"][0]["page_number"] == 3
        assert hashlib.sha256(download.content).hexdigest() == manifest["sha256"]
    assert all(call["schema"]["title"] != "Plan" for call in container.vision.calls)


async def test_manual_override_keeps_original_proposal_and_stale_revision_fails(client, container):
    """Правка предложения атомарно включает skip, а старый revision не перезаписывает draft."""
    data = await create(client, mode={"auto_split": "false"})
    job_id = data["job_id"]
    await process(container, job_id)
    plan = await segmentation(client, job_id)
    original = copy.deepcopy(plan["proposed_plan"])
    refs = plan["draft_plan"]["documents"][0]["pages"]
    body = {
        "expected_revision": plan["plan_revision"],
        "documents": [{"client_id": "a", "pages": refs[:1]}, {"client_id": "b", "pages": refs[1:]}],
        "excluded_pages": [],
    }
    response = await client.put(f"/api/v1/jobs/{job_id}/segmentation", headers=KEY, json=body)
    assert response.status_code == 200
    assert response.json()["segmentation_source"] == "manual_override"
    assert response.json()["proposed_plan"] == original
    stale = await client.put(f"/api/v1/jobs/{job_id}/segmentation", headers=KEY, json=body)
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "plan_version_conflict"
    await confirm(client, job_id, response.json()["plan_revision"])
    await process(container, job_id)
    assert len([c for c in container.vision.calls if c["schema"]["title"] == "Plan"]) == 1
    assert (await segmentation(client, job_id))["proposed_plan"] == original


async def test_owner_pagination_thumbnail_and_no_private_sse_paths(client, container):
    """Новые endpoints проверяют owner; небольшие PNG имеют отдельные capability URLs."""
    data = await create(client, endpoint="uploads")
    job_id = data["job_id"]
    await process(container, job_id)
    for path in ("segmentation", "pages", "snapshot?include_pages=false"):
        assert (
            await client.get(f"/api/v1/jobs/{job_id}/{path}", headers=FOREIGN)
        ).status_code == 404
    page = (await client.get(f"/api/v1/jobs/{job_id}/pages?offset=1&limit=2", headers=KEY)).json()
    assert page["total"] == 4 and [p["global_position"] for p in page["pages"]] == [2, 3]
    icon = await client.get(page["pages"][0]["thumbnail_url"])
    assert icon.status_code == 200 and icon.headers["content-type"] == "image/png"
    small = (
        await client.get(f"/api/v1/jobs/{job_id}/snapshot?include_pages=false", headers=KEY)
    ).json()
    assert small["pages"] == [] and small["pages_total"] == 4
    await container.database.cancel(job_id, "integration:onec")
    events = await client.get(f"/api/v1/jobs/{job_id}/events?include_pages=false", headers=KEY)
    assert str(container.storage.root) not in events.text


async def test_mode_is_part_of_submit_idempotency_and_invalid_flags_do_not_store_files(
    client, container
):
    """Новая семантика входит в digest; невозможное сочетание отвергается до ingest."""
    bad = await client.post(
        "/api/v1/jobs",
        headers=KEY,
        data={"auto_split": "true", "skip_segmentation": "true"},
        files={"files": ("a.pdf", pdf_bytes(), "application/pdf")},
    )
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "invalid_segmentation_mode"
    assert not list(container.storage.root.iterdir())
    payload = pdf_bytes()
    for mode, expected in [({}, 202), ({"auto_split": "false", "skip_segmentation": "true"}, 409)]:
        response = await client.post(
            "/api/v1/jobs",
            headers={**KEY, "Idempotency-Key": "mode-key"},
            data=mode,
            files={"files": ("same.pdf", payload, "application/pdf")},
        )
        assert response.status_code == expected


async def test_old_generation_cannot_override_manual_draft_during_vlm(client, container):
    """Ручной ACK отзывает fence долгого VLM; поздний ответ не публикует старый план."""
    entered, release = asyncio.Event(), asyncio.Event()

    class DelayedVision(ScriptedVision):
        """Задерживает только первый segmentation запрос, сохраняя production pipeline."""

        async def request(self, prompt, context, pages, schema, emit):
            """Позволяет проверить переключение режима во время внешнего семантического вызова."""
            if schema.get("title") == "Plan" and not entered.is_set():
                entered.set()
                await release.wait()
            return await super().request(prompt, context, pages, schema, emit)

    container.vision = container.analysis.vision = DelayedVision()
    data = await create(client)
    job_id = data["job_id"]
    running = asyncio.create_task(process(container, job_id))
    await asyncio.wait_for(entered.wait(), timeout=10)
    plan = await segmentation(client, job_id)
    body = {"expected_revision": plan["plan_revision"], **plan["draft_plan"]}
    response = await client.put(f"/api/v1/jobs/{job_id}/segmentation", headers=KEY, json=body)
    assert response.status_code == 200
    release.set()
    await asyncio.wait_for(running, timeout=5)
    current = await container.database.get(job_id)
    assert current.segmentation_source == "manual" and current.proposed_plan is None
    await confirm(client, job_id, current.plan_revision)
    await process(container, job_id)
    assert (await container.database.get(job_id)).status == "completed_with_warnings"


async def test_manual_internal_attachments_stay_within_approved_pages(client, container):
    """В skip VLM распознаёт приложения внутри группы без фиктивного первоначального плана."""
    data = await create(client, pages=6, mode={"auto_split": "false", "skip_segmentation": "true"})
    container.vision.answers.append(facts([{"title": "Приложение", "page_range": [3, 6]}]))
    await process(container, data["job_id"])
    assert (await container.database.get(data["job_id"])).status == "completed_with_warnings"


@pytest.mark.parametrize("start", [False, True], ids=["legacy-upload", "legacy-submit"])
async def test_legacy_idempotency_and_completed_plan_survive_upgrade(client, container, start):
    """Старая запись допускает повтор ключа, headless start и просмотр готового плана."""
    payload = pdf_bytes(2)
    path = "/api/v1/jobs" if start else "/api/v1/uploads"
    headers = {**KEY, "Idempotency-Key": "legacy-request"}
    first = await client.post(
        path, headers=headers, files={"files": ("legacy.pdf", payload, "application/pdf")}
    )
    assert first.status_code == 202
    job_id = first.json()["job_id"]
    job = await container.database.get(job_id)
    old_fields = {
        "id",
        "owner",
        "status",
        "created_at",
        "expires_at",
        "deadline_at",
        "completed_at",
        "stage",
        "progress",
        "attempt",
        "fence",
        "sources",
        "prompts",
        "organization_hint",
        "result",
        "manifest",
        "error",
        "cursor",
    }
    old_digest = hashlib.sha256(
        json.dumps(
            {
                "sources": [("legacy.pdf", "application/pdf", hashlib.sha256(payload).hexdigest())],
                "organization": None,
                "start": start,
                "prompts": job.prompts if start else {},
            },
            sort_keys=True,
            ensure_ascii=False,
        ).encode()
    ).hexdigest()
    async with container.database.sessions.begin() as session:
        row = await session.get(JobRow, job_id)
        row.data = {name: value for name, value in row.data.items() if name in old_fields}
        row.data = {**row.data, "stage": "upload"}
        row.digest = old_digest
        if not start:
            await session.execute(delete(SignalRow).where(SignalRow.job_id == job_id))
    repeated = await client.post(
        path, headers=headers, files={"files": ("legacy.pdf", payload, "application/pdf")}
    )
    assert repeated.status_code == 202 and repeated.json()["job_id"] == job_id
    if not start:
        assert repeated.json()["stage"] == "upload"
        await container.intake.start(job_id, job.owner)
    await process(container, job_id)
    assert (await container.database.get(job_id)).status == "completed_with_warnings"
    async with container.database.sessions.begin() as session:
        row = await session.get(JobRow, job_id)
        row.data = {name: value for name, value in row.data.items() if name in old_fields}
    legacy_plan = await segmentation(client, job_id)
    assert legacy_plan["locked"] and legacy_plan["document_count"] == 1
    assert legacy_plan["segmentation_source"] == "vlm_auto"
    assert len(legacy_plan["proposed_plan"]["documents"][0]["pages"]) == 2
    assert (await client.get(f"/api/v1/jobs/{job_id}/result", headers=KEY)).status_code == 200
