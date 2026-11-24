# tests/upd_support.py
"""Частный эталон счёта-фактуры: только тестовые адаптеры и стабильные бизнес-проверки."""

import hashlib
import io
import json
import re
import zipfile
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
from urllib.parse import unquote

import pymupdf
import pytest

from app.domain.extractions import DetailedFacts
from app.domain.schema import OneC
from app.worker import process

KEY = {"X-API-Key": "e" * 48}


def reference(required=False):
    """Читает приватные fixtures; отсутствие при явном live-запуске является ошибкой."""
    base = Path("examples") / "УПД"
    paths = [
        Path(str(base) + suffix)
        for suffix in (
            ".pdf",
            ".json",
            ".v2.json",
            ".expected.assertions.json",
            ".adjacent.expected.json",
        )
    ]
    if not all(p.is_file() for p in paths):
        message = "NOT_RUN: нужны пять приватных fixtures examples/УПД.* из ТЗ"
        if required:
            pytest.fail(message)
        pytest.skip(message)
    return paths, [json.loads(p.read_text("utf-8")) for p in paths[1:]]


def fingerprint(paths):
    """Позволяет доказать, что тест не исправил исходный PDF или эталоны под ответ модели."""
    return [hashlib.sha256(p.read_bytes()).hexdigest() for p in paths]


def answer_from_reference(document, file_id):
    """Переносит ID источника в controlled VLM-ответ, оставляя все факты эталонными."""
    raw = json.loads(json.dumps(document).replace("SOURCE_FILE_FIXTURE", file_id))
    raw = {k: v for k, v in raw.items() if k in DetailedFacts.model_fields}
    # Решение владельца 24.11.2026: без организации counterparty=null.
    # Исходные эталоны не редактируются. Проекцию выполняет production use-case.
    raw["one_c"] = OneC().model_dump(mode="json")
    raw["warnings"] = [
        *raw["warnings"],
        "Без организации направление неизвестно; вид предварительный",
    ]
    raw["primary_readability"] = {
        "document_number": "legible",
        "document_date": "legible",
        "handwritten_fields": [],
    }
    return raw


class ReferenceVision:
    """Выдаёт независимые controlled ответы стадий, не вставляет результаты в REST/DB."""

    def __init__(self, document):
        """Сохраняет приватный JSON только в памяти текущего теста."""
        self.document, self.calls = document, []

    async def request(self, prompt, context, pages, schema, emit):
        """Проверяет границы вызова и отсутствие смешения классификации с извлечением."""
        self.calls.append({"schema": schema["title"], "pages": deepcopy(pages), "context": context})
        if schema["title"] == "PairAnswer":
            assert not pages
            return {"solution": True}
        assert [p["page_number"] for p in pages] == [1, 2]
        raw = answer_from_reference(self.document, pages[0]["file_id"])
        if schema["title"] == "DocumentClassification":
            assert context["task"] == "classify_document"
            return {
                "classification": raw["classification"],
                "kind_code": "D03",
                "continuation_summary": None,
                "warnings": [],
                "page_rotations": [
                    {
                        "file_id": p["file_id"],
                        "page_number": p["page_number"],
                        "clockwise_degrees": 270,
                    }
                    for p in pages
                ],
            }
        assert schema["title"] == "DetailedExtraction"
        assert context["task"] == "extract_document"
        assert all(p["ocr_rotation"] == 270 for p in pages)
        return raw


def assert_business(document, expected, *, projected=True):
    """Проверяет вид, собственные реквизиты, точные деньги и физические источники фактов."""
    c, r, d = document["classification"], document["requisites"], document["details"]
    assert c["document_kind"] == expected["document_kind"], "CLASSIFICATION: неверный вид"
    assert c["document_root"] == expected["document_root"]
    assert not c["is_letter"] and c["direction"] is None
    assert document["kind_code"] == d["profile"] == expected["kind_code"]
    assert d["primary_document_type"] == c["document_kind"]
    assert d["primary_document_role"] == expected["primary_document_role"]
    for field in ("document_number", "document_date"):
        assert r[field] == expected[field], f"EXTRACTION: {field}"
    for party, prefix in (("sender", "seller"), ("recipient", "buyer")):
        for field in ("inn", "kpp"):
            assert r[party][field] == expected[f"{prefix}_{field}"], f"EXTRACTION: {party}.{field}"
    assert len(r["items"]) == expected["items_count"]
    item = r["items"][0]
    for field, key in (
        ("quantity", "quantity"),
        ("price", "unit_price"),
        ("amount", "subtotal_excluding_vat"),
    ):
        assert isinstance(item[field], str)
        assert Decimal(item[field]) == Decimal(expected[key]), f"EXTRACTION: item.{field}"
    sums = {a["type"]: Decimal(a["value"]) for a in r["amounts"]}
    for field, key in (
        ("subtotal_excluding_vat", "subtotal_excluding_vat"),
        ("vat", "vat_amount"),
        ("total", "total_amount"),
    ):
        assert sums[field] == Decimal(expected[key]), f"EXTRACTION: {field}"
    for field in ("total_amount", "vat_amount"):
        assert Decimal(d[field]) == Decimal(expected[field]), f"EXTRACTION: details.{field}"
    assert Decimal(item["price"]) * Decimal(item["quantity"]) != Decimal(item["amount"])
    assert re.search(r"22(?:[.,]0{1,2})?\s*%", r["summary"]), "EXTRACTION: ставка НДС"
    assert [p["page_number"] for p in document["source_pages"]] == expected["expected_pages"]
    for path, number in (
        ("classification.document_kind", 1),
        ("requisites.document_number", 1),
        ("requisites.document_date", 1),
        ("requisites.items.0", 2),
        ("details.vat_amount", 2),
    ):
        records = document["field_evidence"][path]
        assert any(
            e["page_number"] == number and e["raw"] and e["status"] == "found" for e in records
        ), f"EVIDENCE: {path}"
    for records in document["field_evidence"].values():
        assert all(e["page_number"] in (1, 2) for e in records)
    assert document["one_c"]["organization"] is None
    if projected:
        assert document["one_c"]["counterparty"] is None, "Утверждённое исключение из эталона"
    assert document["attachment_file"]["file_name"] == expected["expected_pdf_basename"]


async def run_packet(client, container, pdf):
    """Выполняет штатный upload/OCR/proposal/edit/confirm, не обходя use-cases и fence."""
    response = await client.post(
        "/api/v1/jobs",
        headers=KEY,
        files={"files": (pdf.name, pdf.read_bytes(), "application/pdf")},
        data={"auto_split": "false", "skip_segmentation": "false"},
    )
    assert response.status_code == 202
    identifier = response.json()["job_id"]
    await process(container, identifier)
    job = await container.database.get(identifier)
    assert job.status == "awaiting_confirmation", f"OCR/SEGMENTATION: {job.status} {job.error}"
    assert [[p.page_number for p in d.pages] for d in job.draft_plan.documents] == [[1, 2]]
    url = f"/api/v1/jobs/{identifier}/segmentation"
    plan = (await client.get(url, headers=KEY)).json()
    response = await client.put(
        url,
        headers=KEY,
        json={
            "expected_revision": plan["plan_revision"],
            "documents": [
                {"client_id": "checked", "pages": [p.model_dump() for p in job.page_refs()]}
            ],
            "excluded_pages": [],
            "action": "edit",
        },
    )
    assert response.status_code == 200
    confirmed = await client.post(
        url + "/confirm", headers=KEY, json={"expected_revision": response.json()["plan_revision"]}
    )
    assert confirmed.status_code == 202
    await process(container, identifier)
    return identifier


async def check_exports(client, identifier, expected, original):
    """Сверяет четыре выдачи имени, PDF/ZIP/SHA и неизменность страниц исходного скана."""
    url = f"/api/v1/jobs/{identifier}"
    status = (await client.get(url, headers=KEY)).json()
    assert status["status"] in {"completed", "completed_with_warnings"}, status["error"]
    result = (await client.get(url + "/result", headers=KEY)).json()
    detailed = (await client.get(url + "/extractions", headers=KEY)).json()
    assert result["schema_version"] == "1.0" and detailed["schema_version"] == "2.0"
    assert len(result["files"]) == len(detailed["documents"]) == 1
    legacy = result["files"][0]["documents"]
    assert len(legacy) == 1 and legacy[0]["pages"] == [1, 2]
    document = detailed["documents"][0]
    assert_business(document, expected)
    assert legacy[0]["requisites"]["document_number"] == expected["document_number"]
    assert isinstance(legacy[0]["requisites"]["amounts"][0]["value"], float)
    assert legacy[0]["one_c"]["counterparty"] is None
    name = expected["expected_pdf_basename"]
    assert legacy[0]["attachment_file"]["file_name"] == name
    assert status["artifacts"][0]["file_name"] == name
    response = await client.get(document["attachment_file"]["file_url"])
    assert response.status_code == 200
    assert name in unquote(response.headers["content-disposition"])
    assert hashlib.sha256(response.content).hexdigest() == status["artifacts"][0]["sha256"]
    with pymupdf.open(original) as source, pymupdf.open(stream=response.content) as pdf:
        assert len(pdf) == 2
        for before, after in zip(source, pdf, strict=True):
            assert before.get_pixmap().samples == after.get_pixmap().samples
    prepared = (await client.post(url + "/archive", headers=KEY)).json()
    zipped = await client.get(prepared["file_url"])
    assert zipped.status_code == 200
    assert hashlib.sha256(zipped.content).hexdigest() == prepared["sha256"]
    with zipfile.ZipFile(io.BytesIO(zipped.content)) as archive:
        assert archive.namelist() == [expected["expected_archive_entry"]]
        assert archive.read(archive.namelist()[0]) == response.content
    return detailed
