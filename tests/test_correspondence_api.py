# tests/test_correspondence_api.py
"""REST сохраняет контекст организации, юридический род, состав и программную проекцию 1С."""

import hashlib

import pymupdf
import pytest

from tests.live_support import run_comparison
from tests.support import facts, pdf_bytes, plan_for
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
    plan = {"documents": [], "unassigned_pages": [], "warnings": []}
    answers = []
    for index, numbers in enumerate(groups, 1):
        document = plan_for([{"file_id": file_id, "page_number": n} for n in numbers])["documents"][
            0
        ]
        document["document_index"] = index
        plan["documents"].append(document)
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
    if endpoint == "uploads":
        await run_job(container, job_id)
        response = await client.post(f"/api/v1/jobs/{job_id}/start", headers=KEY)
        assert response.status_code == 202
    container.vision.answers.extend([plan, *answers])
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
        assert document["one_c"]["organization"] == organization
        assert document["one_c"]["counterparty"] == "Синтетический отправитель"
        assert document["one_c"]["comment"] == document["requisites"]["summary"]
        response = await client.get(document["attachment_file"]["file_url"])
        artifact = next(
            a for a in snapshot["artifacts"] if a["document_id"] == document["document_id"]
        )
        assert hashlib.sha256(response.content).hexdigest() == artifact["sha256"]
        with pymupdf.open(stream=response.content) as pdf:
            assert len(pdf) == len(groups[index])
            assert all(f"PAGE {n}" in pdf[i].get_text() for i, n in enumerate(groups[index]))
    assert all(
        call["context"]["organization_hint"] == organization for call in container.vision.calls[1:]
    )
    assert (
        await client.get(f"/api/v1/jobs/{job_id}/snapshot", headers=FOREIGN_KEY)
    ).status_code == 404


async def test_live_runner_reports_review_immediately_instead_of_waiting_for_deadline(
    tmp_path, client, container
):
    """Неподтверждённый план фиксируется ошибкой матрицы без автоподтверждения и долгого polling."""

    def uncertain(context):
        """VLM явно сомневается в synthetic плане."""
        plan = plan_for(
            [{"file_id": p["file_id"], "page_number": p["page_number"]} for p in context["pages"]]
        )
        plan["documents"][0]["status"] = "needs_review"
        return plan

    container.vision.answers.append(uncertain)
    client.event_hooks["response"].append(local_worker_hook(container))
    source = tmp_path / "synthetic.pdf"
    source.write_bytes(pdf_bytes())
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
