# tests/test_correspondence_api.py
"""REST сохраняет контекст организации, юридический род, состав и программную проекцию 1С."""

import hashlib

import pymupdf
import pytest

from app.domain.boundaries import ManualPlan
from tests.live_support import run_comparison
from tests.support import facts, pdf_bytes
from tests.test_api import FOREIGN_KEY, KEY, run_job
from tests.test_correspondence_projection import ORGANIZATION, procedural
from tests.test_live_support import comparison_case, local_worker_hook


@pytest.mark.parametrize("endpoint", ["jobs", "uploads"])
@pytest.mark.parametrize("organization", [ORGANIZATION, None])
async def test_organization_context_and_noncontiguous_pdfs_through_rest(
    client, container, endpoint, organization
):
    """Синтетическая перемешанная пачка проходит оба API и выдаёт четыре целых PDF без потерь."""
    response = await client.post(
        f"/api/v1/{endpoint}",
        headers=KEY,
        files={"files": ("synthetic.pdf", pdf_bytes(8), "application/pdf")},
        data={"organization_hint": organization} if organization else {},
    )
    assert response.status_code in {201, 202}
    job_id = response.json()["job_id"]
    assert response.json()["organization_hint"] == organization
    file_id = (await container.database.get(job_id)).sources[0].file_id
    groups = [[1, 8], [2, 3, 4], [5, 6], [7]]
    plan = ManualPlan(
        documents=[
            {
                "client_id": f"manual-{i}",
                "pages": [{"file_id": file_id, "page_number": n} for n in numbers],
            }
            for i, numbers in enumerate(groups)
        ]
    )
    answers = []
    for index, _numbers in enumerate(groups, 1):
        value = procedural().model_dump(mode="json") if index < 4 else facts()
        value["requisites"]["sender"]["name"] = "Синтетический отправитель"
        value["requisites"]["recipient"]["name"] = ORGANIZATION
        value["requisites"]["summary"] = "Проверяемый синтетический факт."
        value["classification"]["direction"] = "incoming" if organization else None
        if index == 4 and organization:
            path = [
                "Входящая/Исходящая корреспонденция",
                "Входящее письмо",
                "Переписка с контрагентом",
            ]
            value["classification"].update(template_path=path, document_kind=path[-1])
        answers.append(value)
    # Несмежные связи разрешены только явным ручным составом.
    if endpoint == "uploads":
        await run_job(container, job_id)
    else:
        claimed = await container.database.claim(job_id)
        await container.analysis._prepared(
            claimed,
            lambda kind, data: container.database.checkpoint(job_id, claimed.fence, kind, data),
        )
    job = await container.database.get(job_id)
    changed = await container.database.save_draft(
        job_id, job.owner, job.plan_revision, plan, "manual"
    )
    await container.boundaries.confirm(job_id, job.owner, changed.plan_revision)
    container.vision.answers.extend(answers)
    await run_job(container, job_id)
    snapshot = (await client.get(f"/api/v1/jobs/{job_id}/snapshot", headers=KEY)).json()
    assert "footer_preview" not in str(snapshot)
    assert "header_preview" not in str(snapshot)
    assert snapshot["organization_hint"] == organization
    assert snapshot["status"] == "completed_with_warnings"
    result_response = await client.get(f"/api/v1/jobs/{job_id}/result", headers=KEY)
    assert result_response.status_code == 200
    result = result_response.json()
    assert result["schema_version"] == "1.0"
    assert "primary_readability" not in str(result)
    documents = result["files"][0]["documents"]
    assert [d["pages"] for d in documents] == groups
    for index, document in enumerate(documents):
        assert document["classification"]["direction"] == ("incoming" if organization else None)
        assert document["classification"]["is_letter"] is (index == 3)
        if index < 3:
            assert document["classification"]["document_root"] == "Процессуальные документы"
            assert document["classification"]["template_path"] == []
            assert document["one_c"]["template_leaf"] is None
            assert document["requisites"]["sender"]["name"] is None
            assert document["one_c"]["counterparty"] is None
            assert document["one_c"]["comment"] is None
            assert any("[template_not_supported]" in w for w in document["warnings"])
        else:
            assert document["one_c"]["counterparty"] == "Синтетический отправитель"
            assert document["one_c"]["comment"] == document["requisites"]["summary"]
        assert document["one_c"]["organization"] == organization
        response = await client.get(document["attachment_file"]["file_url"])
        artifact = next(
            a for a in snapshot["artifacts"] if a["document_id"] == document["document_id"]
        )
        assert hashlib.sha256(response.content).hexdigest() == artifact["sha256"]
        with pymupdf.open(stream=response.content) as pdf:
            assert len(pdf) == len(groups[index])
            assert all(f"PAGE {n}" in pdf[i].get_text() for i, n in enumerate(groups[index]))
    assert all(
        call["context"]["organization_hint"] == organization for call in container.vision.calls
    )
    assert (
        await client.get(f"/api/v1/jobs/{job_id}/snapshot", headers=FOREIGN_KEY)
    ).status_code == 404


async def test_live_runner_reports_review_immediately_instead_of_waiting_for_deadline(
    tmp_path, client, container
):
    """Неподтверждённый план фиксируется ошибкой матрицы без автоподтверждения и долгого polling."""

    container.vision.answers.extend(
        [{"solution": "invalid"}] * container.settings.pair_max_attempts
    )
    client.event_hooks["response"].append(local_worker_hook(container))
    source = tmp_path / "synthetic.pdf"
    source.write_bytes(pdf_bytes(2))
    report = await run_comparison(
        client,
        [comparison_case(source.name)],
        tmp_path,
        tmp_path / "private.json",
        headers=KEY,
        job_timeout_seconds=1,
        poll_interval=0,
    )
    assert report["cases"][0]["code"] == "segmentation_requires_review"
    assert any(
        r["field_path"] == "sources.0.sha256" and r["status"] == "match" for r in report["rows"]
    )
