"""Регрессии единого имени PDF: полные названия, номера, регистр, дубликаты и REST/ZIP."""

import io
import zipfile
from copy import deepcopy
from urllib.parse import unquote

import pytest

from app.domain.document_names import document_archive_name, document_filename
from app.domain.errors import IntakeError
from app.domain.schema import Extracted
from app.worker import process
from tests.support import facts
from tests.test_boundary_api import KEY, create


def named(kind, number=None, date=None):
    """Готовит проверенные synthetic факты; источник даты и номера — только собственные поля."""
    value = facts()
    value["classification"]["document_kind"] = kind
    value["requisites"].update(document_number=number, document_date=date)
    return Extracted.model_validate(value)


@pytest.mark.parametrize(
    ("kind", "number", "date", "expected"),
    [
        (
            "Рамочный договор",
            "2026-10-23128-VLT-PML",
            "2026-10-21",
            "Рамочный договор № 2026-10-23128-VLT-PML от 21.10.2026.pdf",
        ),
        ("УПД", "1072", "2026-11-16", "УПД № 1072 от 16.11.2026.pdf"),
        ("Акт", "97", "2026-08-13", "Акт № 97 от 13.08.2026.pdf"),
        ("Счёт на оплату", "97", "2026-08-13", "Счёт на оплату № 97 от 13.08.2026.pdf"),
        (
            "Договор-заявка на перевозку грузов",
            "220",
            "2026-08-10",
            "Договор-заявка на перевозку грузов № 220 от 10.08.2026.pdf",
        ),
        (
            "Выписка из акта по результатам проведения выездной проверки",
            "77260925000023692738",
            "2026-11-12",
            "Выписка из акта по результатам проведения выездной проверки "
            "№ 77260925000023692738 от 12.11.2026.pdf",
        ),
        ("Исх.", "ИСХ-775/ДВ", "2026-11-18", "Исх. № ИСХ-775／ДВ от 18.11.2026.pdf"),
        ("Акт", None, "2025-02-05", "Акт от 05.02.2025.pdf"),
        ("Акт", "97", None, "Акт № 97.pdf"),
        ("Акт", None, None, "Акт.pdf"),
        ("СЧЁТ НА ОПЛАТУ", None, None, "Счёт на оплату.pdf"),
        ("АКТ", None, None, "Акт.pdf"),
        ("Исходящее Письмо", None, None, "Исходящее письмо.pdf"),
        ("РАМОЧНЫЙ ДОГОВОР Поставки", None, None, "Рамочный договор поставки.pdf"),
        ("Акт", None, "0005-02-05", "Акт от 05.02.0005.pdf"),
        ("акт ТОРГ-12 и КС-2", None, None, "Акт ТОРГ-12 и КС-2.pdf"),
        ("акт упд и укд", None, None, "Акт УПД и УКД.pdf"),
        ("Договор с ИП Петровым", None, None, "Договор с ИП Петровым.pdf"),
        ("ДОГОВОР С ООО «ВЗЛЕТ-МСК»", None, None, "Договор с ООО «ВЗЛЕТ-МСК».pdf"),
        ('  Акт * ? " < > | : / \\  ', None, None, "Акт.pdf"),
        ("Акт", 'N*?"<>|:/\\', None, "Акт № N＊？＂＜＞｜：／＼.pdf"),
        (
            "Акт",
            "2026-10-23128-VLT-PML-12345678901234567890",
            None,
            "Акт № 2026-10-23128-VLT-PML-12345678901234567890.pdf",
        ),
    ],
)
def test_required_full_names_and_filename_safe_official_numbers(kind, number, date, expected):
    """Все примеры ТЗ и опасные знаки номера оформляются без сокращений и изменения JSON."""
    value = named(kind, number, date)
    before = value.model_dump(mode="json")
    assert document_filename(1, value) == expected
    assert document_archive_name(1, value) == "doc_1/" + expected
    assert value.model_dump(mode="json") == before
    assert not set('*?"<>|:/\\').intersection(expected)


@pytest.mark.parametrize(
    "field",
    [
        "document_number",
        "requisites.document_number",
        "document_date",
        "requisites.document_date",
        "requisites",
    ],
)
def test_uncertain_primary_values_are_omitted_without_changing_facts(field):
    """Помеченные сомнением реквизиты не попадают в имя и остаются неизменными в данных."""
    value = named("Акт", "97", "2026-08-13")
    value.recognition.low_confidence_fields = [field]
    before = value.model_dump(mode="json")
    expected = "Акт"
    if field not in {"document_number", "requisites.document_number", "requisites"}:
        expected += " № 97"
    if field not in {"document_date", "requisites.document_date", "requisites"}:
        expected += " от 13.08.2026"
    assert document_filename(1, value) == expected + ".pdf"
    assert value.model_dump(mode="json") == before


def test_related_references_and_title_do_not_replace_own_number_or_date():
    """Номер связанного договора и заголовок не подставляются в отсутствующие собственные поля."""
    value = named("Акт")
    value.requisites = type(value.requisites).model_validate(
        {
            **value.requisites.model_dump(),
            "title": "Акт № 999 от 01.01.2026",
            "related_entities": {"contracts": [{"number": "CONTRACT-555", "date": "2026-01-01"}]},
        }
    )
    assert document_filename(1, value) == "Акт.pdf"


def test_duplicate_names_are_unique_case_insensitive_and_deterministic():
    """Суффиксы учитывают имена, уже содержащие (2), и правила регистра Windows."""
    used = set()
    values = [named("АКТ"), named("Акт (2)"), named("акт"), named("Акт")]
    before = [value.model_dump() for value in values]
    names = [document_filename(i, value, used) for i, value in enumerate(values, 1)]
    assert names == ["Акт.pdf", "Акт (2).pdf", "Акт (3).pdf", "Акт (4).pdf"]
    assert before == [value.model_dump() for value in values]
    other = set()
    assert [document_archive_name(i, value, other) for i, value in enumerate(values, 1)] == [
        f"doc_{i}/{name}" for i, name in enumerate(names, 1)
    ]


def test_filesystem_limit_does_not_silently_truncate_official_number():
    """Физически невозможное имя даёт явную диагностику вместо порчи номера или названия."""
    value = named("Акт", "Я" * 200)
    with pytest.raises(IntakeError) as error:
        document_filename(1, value)
    assert error.value.code == "document_name_too_long"
    assert value.requisites.document_number == "Я" * 200


def test_unicode_replacement_collision_keeps_distinct_original_numbers():
    """Номера со слешем и полноширинным аналогом получают разные файлы без изменения JSON."""
    first, second = named("Акт", "A/1"), named("Акт", "A／1")
    used = set()
    assert document_filename(1, first, used) == "Акт № A／1.pdf"
    assert document_filename(2, second, used) == "Акт № A／1 (2).pdf"
    assert first.requisites.document_number == "A/1" and second.requisites.document_number == "A／1"


async def test_same_names_in_headers_status_json_v2_and_zip_for_legacy_jobs(
    client, container, monkeypatch
):
    """Новое оформление действует и для старого metadata-кеша; PDF и исходные факты не меняются."""
    created = await create(client, pages=2)
    identifier = created["job_id"]
    value = facts()
    value["classification"]["document_kind"] = "Исх."
    value["requisites"].update(document_number="ИСХ-775/ДВ", document_date="2026-11-18")
    container.vision.answers.extend([{"solution": False}, deepcopy(value), deepcopy(value)])
    await process(container, identifier)
    get_job = container.database.get
    original = await get_job(identifier)
    old = original.model_copy(deep=True)
    for source in old.result["files"]:
        for document in source["documents"]:
            document["attachment_file"]["file_name"] = "old_name.pdf"
    for artifact in old.manifest["documents"].values():
        artifact["file_name"] = "old_name.pdf"
    saved = old.model_dump(mode="json")

    async def legacy_job(job_id):
        """Эмулирует сохранённое старой версией имя, сохраняя остальные production-механизмы."""
        return old if job_id == identifier else await get_job(job_id)

    monkeypatch.setattr(container.database, "get", legacy_job)
    base = f"/api/v1/jobs/{identifier}"
    status = (await client.get(base, headers=KEY)).json()
    result = (await client.get(base + "/result", headers=KEY)).json()
    extended = (await client.get(base + "/extractions", headers=KEY)).json()
    names = ["Исх. № ИСХ-775／ДВ от 18.11.2026.pdf", "Исх. № ИСХ-775／ДВ от 18.11.2026 (2).pdf"]
    documents = [d for source in result["files"] for d in source["documents"]]
    assert [d["attachment_file"]["file_name"] for d in documents] == names
    assert [d["attachment_file"]["file_name"] for d in extended["documents"]] == names
    assert [a["file_name"] for a in status["artifacts"]] == names
    archive_response = await client.get(base + "/archive", headers=KEY)
    assert archive_response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(archive_response.content)) as archive:
        assert archive.namelist() == [f"doc_{i}/{name}" for i, name in enumerate(names, 1)]
        for i, document in enumerate(documents, 1):
            response = await client.get(document["attachment_file"]["file_url"])
            assert response.status_code == 200
            actual = unquote(response.headers["content-disposition"].split("filename*=UTF-8''")[1])
            assert actual == names[i - 1]
            assert response.content == archive.read(f"doc_{i}/{actual}")
            assert document["requisites"]["document_number"] == "ИСХ-775/ДВ"
    assert old.model_dump(mode="json") == saved
