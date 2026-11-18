# tests/test_document_archive.py
"""Имена PDF и ZIP: настоящий REST/DB/storage, глобальный порядок, целостность и TTL без live AI."""

import asyncio
import hashlib
import io
import re
import zipfile
from datetime import timedelta

import pymupdf
import pytest

from app.domain.document_names import document_filename
from app.domain.schema import Extracted
from app.worker import process
from tests.support import facts
from tests.test_boundary_api import FOREIGN, KEY, confirm, create, segmentation
from tests.test_document_processing import classification


@pytest.mark.parametrize(
    ("kind", "number", "date", "expected"),
    [
        (None, None, None, "doc_3_Письмо.pdf"),
        ("Счёт", None, None, "doc_3_Счёт.pdf"),
        ("УПД", "42/Б", "2026-11-18", "doc_3_УПД_42_Б_2026-11-18.pdf"),
        ("Договор", None, "2026-01-02", "doc_3_Договор_2026-01-02.pdf"),
        ("../../Счёт:\x00?\\", "../A*<1>\u202e", None, "doc_3_Счёт_A_1.pdf"),
    ],
)
def test_document_names_use_only_verified_primary_facts(kind, number, date, expected):
    """При null номер/дата опускаются; значения не становятся путями или control chars."""
    value = facts()
    if kind:
        value["classification"].update(
            is_letter=False, document_root="Договоры", document_kind=kind
        )
    value["requisites"].update(document_number=number, document_date=date)
    assert document_filename(3, Extracted.model_validate(value)) == expected


def test_name_length_and_letter_direction_are_windows_safe():
    """Кириллица помещается в basename; направление берётся из классификации."""
    value = facts()
    value["classification"].update(
        is_letter=False, document_root="Договоры", document_kind="Д" * 500
    )
    value["requisites"]["document_number"] = "Я" * 500
    assert len(document_filename(10000, Extracted.model_validate(value)).encode()) < 240
    for direction, label in (("incoming", "Входящее"), ("outgoing", "Исходящее")):
        parsed = Extracted.model_validate(facts())
        parsed.classification.direction = direction
        assert document_filename(1, parsed) == f"doc_1_{label}_письмо.pdf"


async def completed_packet(client, container):
    """Создаёт два межфайловых блока в ручной перестановке, затем штатно утверждает/обрабатывает."""
    data = await create(client, endpoint="uploads", pages=3, files=2)
    identifier = data["job_id"]
    await process(container, identifier)
    plan = await segmentation(client, identifier)
    a, b = [d["pages"] for d in plan["draft_plan"]["documents"]]
    order = [b[0], a[2], a[0], a[1], b[2], b[1]]
    saved = await client.put(
        f"/api/v1/jobs/{identifier}/segmentation",
        headers=KEY,
        json={
            "expected_revision": plan["plan_revision"],
            "page_order": order,
            "documents": [
                {"client_id": "first", "pages": order[:3]},
                {"client_id": "second", "pages": order[3:]},
            ],
        },
    )
    assert saved.status_code == 200
    await confirm(client, identifier, saved.json()["plan_revision"])
    first = facts()
    first["requisites"].update(document_number="42/Б", document_date="2026-11-18")
    other = classification(False)
    other["classification"]["document_kind"] = "Счёт"
    container.vision.answers.extend([first, other])
    await process(container, identifier)
    assert (await container.database.get(identifier)).status == "completed_with_warnings"
    return identifier, order


async def test_zip_follows_approved_global_order_names_hashes_and_page_sequence(client, container):
    """ZIP сохраняет имена блоков и ручной порядок из разных исходников."""
    identifier, order = await completed_packet(client, container)
    path = f"/api/v1/jobs/{identifier}/archive"
    calls = len(container.vision.calls)
    prepared = await asyncio.gather(*[client.post(path, headers=KEY) for _ in range(2)])
    assert all(r.status_code == 200 for r in prepared)
    info = prepared[0].json()
    assert set(info) == {"file_name", "byte_size", "sha256", "file_url"}
    assert info["sha256"] == prepared[1].json()["sha256"]
    response = await client.get(info["file_url"])
    assert response.status_code == 200 and response.headers["content-type"] == "application/zip"
    assert len(response.content) == info["byte_size"]
    assert hashlib.sha256(response.content).hexdigest() == info["sha256"]
    assert response.headers["x-content-sha256"] == info["sha256"]
    result = (await client.get(f"/api/v1/jobs/{identifier}/result", headers=KEY)).json()
    assert result["schema_version"] == "1.0" and "archive" not in result
    documents = sorted(
        (d for f in result["files"] for d in f["documents"]), key=lambda d: d["segment_index"]
    )
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert archive.namelist() == ["doc_1_Письмо_42_Б_2026-11-18.pdf", "doc_2_Счёт.pdf"]
        for index, document in enumerate(documents):
            name = document["attachment_file"]["file_name"]
            raw = archive.read(name)
            single = await client.get(document["attachment_file"]["file_url"])
            assert raw == single.content
            with pymupdf.open(stream=raw) as pdf:
                assert [p.get_text().strip() for p in pdf] == [
                    f"SYNTHETIC PAGE {p['page_number']}" for p in order[index * 3 : index * 3 + 3]
                ]
    assert len(list(container.storage.directory(identifier).glob("archive-*.zip"))) == 1
    assert not list(container.storage.directory(identifier).glob("*.tmp"))
    assert len(container.vision.calls) == calls


async def test_archive_owner_capability_and_expiration(client, container, monkeypatch):
    """Архив не обходит изоляцию/CSRF/scope/TTL; доступ закрыт и при сохранённом ZIP на диске."""
    identifier, _ = await completed_packet(client, container)
    path = f"/api/v1/jobs/{identifier}/archive"
    assert (await client.post(path, headers=FOREIGN)).status_code == 404
    assert (await client.get(path, headers=FOREIGN)).status_code == 404
    assert (await client.post(path)).status_code in {401, 403}
    info = (await client.post(path, headers=KEY)).json()
    source = (await client.get(f"/api/v1/jobs/{identifier}", headers=KEY)).json()["sources"][0]
    wrong = re.sub(r"/source-files/[^?]+", "/archive", source["file_url"])
    assert (await client.get(wrong, headers=FOREIGN)).status_code == 404
    assert (await client.get(info["file_url"])).status_code == 200
    expired = (await container.database.get(identifier)).expires_at + timedelta(seconds=1)
    for module in ("app.domain.jobs", "app.infrastructure.security", "app.application.exports"):
        monkeypatch.setattr(module + ".utcnow", lambda: expired)
    assert (await client.get(info["file_url"])).status_code == 410
    assert (await client.post(path, headers=KEY)).status_code == 410


async def test_archive_requires_completed_blocks_and_rejects_damaged_pdf(client, container):
    """До завершения — 409; повреждённый PDF не публикуется даже в частичном ZIP."""
    initial = await create(client, endpoint="uploads", pages=1)
    path = f"/api/v1/jobs/{initial['job_id']}/archive"
    assert (await client.post(path, headers=KEY)).status_code == 409
    assert (await client.get(path, headers=KEY)).status_code == 409
    identifier, _ = await completed_packet(client, container)
    job = await container.database.get(identifier)
    artifact = next(iter(job.manifest["documents"].values()))
    target = container.storage.resolve(artifact["path"])
    target.write_bytes(target.read_bytes() + b"changed")
    response = await client.post(f"/api/v1/jobs/{identifier}/archive", headers=KEY)
    assert response.status_code == 409 and response.json()["error"]["code"] == "artifact_integrity"
    assert not list(container.storage.directory(identifier).glob("archive-*"))
