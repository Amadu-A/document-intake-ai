# tests/test_extractions_v2.py
"""Типизированные профили, evidence и полный headless цикл; AI здесь явно synthetic."""

import io
import json
import zipfile
from copy import deepcopy
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.application.extraction_validation import validate_details, validate_selection
from app.domain.document_kinds import categories, kind_metadata
from app.domain.document_names import document_archive_name
from app.domain.document_processing import DocumentClassification
from app.domain.errors import IntakeError
from app.domain.extractions import DetailedExtraction, DetailedResult
from app.domain.schema import BatchResult, DocumentRoot, Extracted
from app.domain.segmentation import PageRef
from app.worker import process
from tests.support import ScriptedVision, facts, pdf_bytes
from tests.test_boundary_api import FOREIGN, KEY, segmentation


def selected(kind="Счёт на оплату", code="D03", root="Документы связанные с исполнением договоров"):
    """Задаёт явный synthetic ответ классификации, отдельно от извлечённых фактов."""
    classification = facts()["classification"]
    classification.update(document_root=root, is_letter=False, document_kind=kind)
    return DocumentClassification(
        classification=classification,
        kind_code=code,
        continuation_summary="Тестовый блок",
        warnings=[],
    )


def extracted(selection, number="97", date="2026-08-13", ref=None, details=None, amounts=None):
    """Готовит typed fixture без имитации настоящего PDF акта/счёта из пользовательских сканов."""
    ref = ref or {"file_id": "source", "page_number": 1}
    value = facts()
    value["classification"] = selection.classification.model_dump(mode="json")
    value["requisites"].update(document_number=number, document_date=date, document_date_raw=date)
    if details is not None:
        value["details"] = details
    if amounts is not None:
        value["requisites"]["amounts"] = amounts
    value = ScriptedVision.readability(value)
    return ScriptedVision.detailed(
        value, {"selected_kind_code": selection.kind_code, "pages": [ref]}
    )


def test_catalog_is_single_immutable_source_and_reference_examples_validate():
    """Все Dxx загружены из одного файла; предоставленные JSON1/2 примеры валидны."""
    assert len(categories()) == 11
    for code, entry in categories().items():
        assert kind_metadata(code) == (entry["kind_name"], list(entry["path_1c"]))
    with pytest.raises(TypeError):
        categories()["D03"]["kind_name"] = "подмена"
    for version, model in (("v1", BatchResult), ("v2", DetailedResult)):
        path = Path("examples") / f"result_{version}_invoice_example.json"
        if path.exists():
            model.model_validate_json(path.read_text("utf-8"))


def test_invoice_details_decimal_evidence_and_projection_are_separate():
    """Счёт D03 сохраняет точные деньги/собственные реквизиты, а v1 не получает новые поля."""
    selection = selected()
    raw = extracted(
        selection,
        details={
            "profile": "D03",
            "primary_document_type": "Счёт на оплату",
            "primary_document_role": "payment_request",
            "total_amount": "123456789012345678.91",
            "vat_amount": "20.10",
        },
        amounts=[{"type": "total", "value": "123456789012345678.91", "currency": "RUB"}],
    )
    value = DetailedExtraction.model_validate(raw)
    validate_details(value, selection, [{"file_id": "source", "page_number": 1}])
    actual = value.facts().model_dump(mode="json")
    assert actual["details"]["primary_document_role"] == "payment_request"
    assert actual["details"]["total_amount"] == "123456789012345678.91"
    assert actual["requisites"]["amounts"][0]["value"] == "123456789012345678.91"
    legacy = value.facts().legacy().model_dump(mode="json")
    assert set(legacy) == set(Extracted.model_fields)
    assert isinstance(legacy["requisites"]["amounts"][0]["value"], float)
    assert document_archive_name(2, value.facts()) == "doc_2/Счёт_на_оплату_97_2026-08-13.pdf"


@pytest.mark.parametrize(
    "problem",
    [
        "foreign_page",
        "wrong_role",
        "unknown_field",
        "confidence",
        "no_quote",
        "missing_evidence",
        "bad_inn",
        "bad_kpp",
    ],
)
def test_invalid_profile_evidence_and_identifiers_are_not_accepted(problem):
    """Подмена источника/роли, confidence и неверные идентификаторы требуют перечитывания."""
    selection = selected()
    raw = extracted(selection)
    record = raw["field_evidence"]["requisites.document_number"][0]
    if problem == "foreign_page":
        record["page_number"] = 2
    elif problem == "wrong_role":
        raw["details"]["primary_document_role"] = "primary"
    elif problem == "unknown_field":
        raw["field_evidence"]["details.unknown"] = [record]
    elif problem == "confidence":
        record["confidence"] = 0.99
    elif problem == "no_quote":
        record["raw"] = None
    elif problem == "missing_evidence":
        raw["field_evidence"] = {}
    elif problem == "bad_inn":
        raw["requisites"]["sender"]["inn"] = "123"
    else:
        raw["requisites"]["sender"]["kpp"] = "ABC"
    with pytest.raises(IntakeError):
        validate_details(
            DetailedExtraction.model_validate(raw),
            selection,
            [{"file_id": "source", "page_number": 1}],
        )


@pytest.mark.parametrize("code", ["D04", "D05", "D06", "D07", "D08"])
def test_contract_profile_requires_verified_role_and_organization(code):
    """Заголовок договора/спецификации сам по себе не устанавливает доходную/расходную роль."""
    root = categories()[code]["document_root"]
    value = selected("Договор-заявка на перевозку грузов", code, root)
    with pytest.raises(IntakeError):
        validate_selection(value, None)
    with pytest.raises(IntakeError):
        validate_selection(value, "ООО Компания")
    value.our_role = {
        "D04": "supplier",
        "D05": "customer",
        "D06": "service_provider",
        "D07": "supplier",
        "D08": "customer",
    }[code]
    value.role_evidence = "ООО Компания — заказчик/поставщик по тестовому контексту"
    value.role_source_page = PageRef(file_id="source", page_number=1)
    value = DocumentClassification.model_validate(value.model_dump())
    validate_selection(value, "ООО Компания", [{"file_id": "source", "page_number": 1}])


@pytest.mark.parametrize("kind", ["Постановление ФССП", "Судебная повестка", "Решение суда"])
def test_other_procedural_documents_are_not_enforcement_writ(kind):
    """D02 не подставляется для других процессуальных документов."""
    value = selected(kind, "D02", "Процессуальные документы")
    with pytest.raises(IntakeError):
        validate_selection(value, None)


@pytest.mark.parametrize("code", list(categories()))
def test_all_registered_profiles_have_typed_details_and_common_facts(code):
    """Все одиннадцать профилей сохраняют общие поля и типизированные собственные детали."""
    item = categories()[code]
    selection = selected(
        item["examples"][0],
        code if code != "D01" else None,
        item["document_root"] if code != "D01" else "Договоры",
    )
    if code == "D01":
        selection.kind_code = code
        selection.classification.document_root = DocumentRoot(item["document_root"])
        selection.classification.is_letter = True
        selection.classification.direction = "incoming"
        selection.classification.template_path = [
            "Входящая/Исходящая корреспонденция",
            "Входящее письмо",
            "Переписка с контрагентом",
        ]
    raw = extracted(selection)
    value = DetailedExtraction.model_validate(raw)
    validate_details(value, selection, [{"file_id": "source", "page_number": 1}])
    assert value.details.profile == code
    assert value.requisites.document_number == "97"
    if "total_amount" in type(value.details).model_fields:
        broken = deepcopy(raw)
        broken["details"]["total_amount"] = 0.1
        with pytest.raises(ValidationError):
            DetailedExtraction.model_validate(broken)


@pytest.mark.parametrize("kind", ["CON", "NUL.foo", "AUX", "COM1", "😀" * 100])
def test_pdf_basename_is_safe_on_windows_and_utf8_filesystems(kind):
    """ZIP basename не содержит Windows device name и укладывается в лимит UTF-8 имени."""
    value = DetailedExtraction.model_validate(extracted(selected(kind))).facts()
    value.requisites.document_number = "😀" * 100
    name = document_archive_name(1, value).split("/", 1)[1]
    assert len(name.encode("utf-8")) < 255
    value.requisites.document_number = None
    value.requisites.document_date = None
    name = document_archive_name(1, value).split("/", 1)[1]
    assert name.split(".", 1)[0] not in {"CON", "NUL", "AUX", "COM1"}


def test_contract_details_cannot_change_selected_organization_role():
    """Доходная роль не превращается в покупателя при втором запросе VLM."""
    selection = selected("Договор поставки", "D04", "Договоры")
    value = DetailedExtraction.model_validate(
        extracted(selection, details={"profile": "D04", "our_role": "customer"})
    )
    with pytest.raises(IntakeError):
        validate_details(value, selection, [{"file_id": "source", "page_number": 1}])


async def test_full_api_review_edit_confirm_v1_v2_and_zip_without_browser(client, container):
    """Внешний клиент проходит весь workflow, сохраняет страницы и получает оба контракта."""
    payload = pdf_bytes(3)
    created = await client.post(
        "/api/v1/jobs",
        headers={**KEY, "Idempotency-Key": "v2-headless"},
        files={"files": ("synthetic.pdf", payload, "application/pdf")},
    )
    assert created.status_code == 202
    identifier = created.json()["job_id"]
    container.vision.answers.extend([{"solution": False}, {"solution": False}])
    await process(container, identifier)
    plan = await segmentation(client, identifier)
    assert not plan["locked"] and plan["can_confirm"]
    assert (
        await client.get(f"/api/v1/jobs/{identifier}/extractions", headers=KEY)
    ).status_code == 409
    pages = [p for doc in plan["draft_plan"]["documents"] for p in doc["pages"]]
    changed = await client.post(
        f"/api/v1/jobs/{identifier}/segmentation/actions",
        headers=KEY,
        json={
            "expected_revision": plan["plan_revision"],
            "action": "move",
            "pages": [pages[2]],
            "target": pages[0],
            "side": "before",
        },
    )
    assert changed.status_code == 200
    plan = changed.json()
    assert [p["page_number"] for p in plan["draft_plan"]["page_order"]] == [3, 1, 2]
    assert [[p["page_number"] for p in d["pages"]] for d in plan["draft_plan"]["documents"]] == [
        [3, 1],
        [2],
    ]
    split = await client.post(
        f"/api/v1/jobs/{identifier}/segmentation/actions",
        headers=KEY,
        json={"expected_revision": plan["plan_revision"], "action": "split", "pages": [pages[2]]},
    )
    assert split.status_code == 200
    plan = split.json()
    selections = [
        selected("Акт"),
        selected(),
        selected("Договор-заявка на перевозку грузов", None, "Договоры"),
    ]
    dates = ["2026-08-13", "2026-08-13", "2026-08-10"]
    numbers = ["97", "97", "220"]
    for index, selection in enumerate(selections):
        ref = plan["draft_plan"]["documents"][index]["pages"][0]
        container.vision.answers.extend(
            [
                selection.model_dump(mode="json"),
                extracted(selection, numbers[index], dates[index], ref),
            ]
        )
    confirmation = await client.post(
        f"/api/v1/jobs/{identifier}/segmentation/confirm",
        headers=KEY,
        json={"expected_revision": plan["plan_revision"]},
    )
    assert confirmation.status_code == 202
    await process(container, identifier)
    status = await client.get(f"/api/v1/jobs/{identifier}", headers=KEY)
    assert status.json()["status"] in {"completed", "completed_with_warnings"}, status.json()[
        "error"
    ]
    old = (await client.get(f"/api/v1/jobs/{identifier}/result", headers=KEY)).json()
    BatchResult.model_validate(old)
    assert "details" not in json.dumps(old) and "field_evidence" not in json.dumps(old)
    new = (await client.get(f"/api/v1/jobs/{identifier}/extractions", headers=KEY)).json()
    result = DetailedResult.model_validate(new)
    assert [d.kind_code for d in result.documents] == ["D03", "D03", None]
    assert [d.classification.document_kind for d in result.documents] == [
        v.classification.document_kind for v in selections
    ]
    assert [d.source_pages[0].page_number for d in result.documents] == [3, 1, 2]
    assert all(
        d.requisites.document_number == number
        for d, number in zip(result.documents, numbers, strict=True)
    )
    assert result.documents[-1].details is None and result.documents[-1].warnings
    downloaded = await client.get(f"/api/v1/jobs/{identifier}/archive", headers=KEY)
    with zipfile.ZipFile(io.BytesIO(downloaded.content)) as archive:
        assert archive.namelist() == [
            "doc_1/Акт_97_2026-08-13.pdf",
            "doc_2/Счёт_на_оплату_97_2026-08-13.pdf",
            "doc_3/Договор-заявка_на_перевозку_грузов_220_2026-08-10.pdf",
        ]
        assert all(info.date_time[0] >= 2026 for info in archive.infolist())
    history = (await client.get("/api/v1/jobs", headers=KEY)).json()
    assert [item["job_id"] for item in history["jobs"]] == [identifier]
    assert not (await client.get("/api/v1/jobs", headers=FOREIGN)).json()["jobs"]
    assert (
        await client.get(f"/api/v1/jobs/{identifier}/extractions", headers=FOREIGN)
    ).status_code == 404
    assert (
        await client.post(
            f"/api/v1/jobs/{identifier}/segmentation/actions",
            headers=KEY,
            json={
                "expected_revision": plan["plan_revision"],
                "action": "split",
                "pages": [pages[0]],
            },
        )
    ).status_code == 409
