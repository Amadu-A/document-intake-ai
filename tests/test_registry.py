# tests/test_registry.py
"""Проекция и настоящий XLSX: порядок, неизвестные факты, текстовые номера и безопасность."""

from contextlib import closing
from datetime import date, timedelta
from io import BytesIO
from uuid import uuid4
from zipfile import ZipFile

import pytest
from openpyxl import load_workbook
from openpyxl.worksheet._writer import ALL_TEMP_FILES
from pydantic import ValidationError

from app.application.registry import project_registry, registry_delivery_method, registry_number
from app.domain.document_processing import DocumentProcessing
from app.domain.jobs import Job, utcnow
from app.domain.registry import RegistryResult, RegistryRow, registry_mapping
from app.domain.schema import BatchResult, Document
from app.infrastructure.registry_xlsx import RegistryXlsx
from tests.support import facts


def registry_job(count=3):
    """Синтетические v1-only факты: номера независимы от связанных договоров/имён PDF."""
    documents = []
    for index in range(1, count + 1):
        raw = facts()
        raw["classification"].update(
            document_root="Документы связанные с исполнением договоров",
            is_letter=False,
            document_kind="Счёт-фактура",
        )
        raw["requisites"].update(
            document_number="00042/А",
            document_date="2025-11-27",
            sender={"name": "ООО «Синтетический продавец»"},
            recipient={"name": "ООО «Синтетический покупатель»"},
            delivery_channel="unknown",
            summary="Не использовать как примечание",
        )
        documents.append(
            Document(
                **raw,
                document_id=f"document-{index}",
                segment_index=index,
                pages=[index],
                split_confidence=None,
                attachment_file={"file_name": "Чужое имя.pdf", "file_url": ""},
            )
        )
    # Глобальные регистрации перемешаны между источниками; сортировка ровно одна.
    result = BatchResult(
        batch_id="batch",
        files=[
            {
                "file_id": "a",
                "file_name": "a.pdf",
                "file_url": "",
                "mime_type": "application/pdf",
                "documents": documents[::2],
            },
            {
                "file_id": "b",
                "file_name": "b.pdf",
                "file_url": "",
                "mime_type": "application/pdf",
                "documents": documents[1::2],
            },
        ],
    )
    return Job(
        id=str(uuid4()),
        owner="integration:onec",
        status="completed_with_warnings",
        stage="completed_with_warnings",
        created_at=utcnow(),
        completed_at=utcnow(),
        expires_at=utcnow() + timedelta(hours=24),
        sources=[],
        prompts={},
        result=result.model_dump(mode="json"),
    )


@pytest.mark.parametrize(
    "number,day,expected",
    [
        ("0002647329", date(2025, 11, 27), "0002647329 от 27.11.2025"),
        ("001/Б", None, "001/Б"),
        (None, date(2025, 11, 27), "от 27.11.2025"),
        (None, None, None),
        ("б/н", None, "б/н"),
    ],
)
def test_number_format_preserves_only_known_parts(number, day, expected):
    """Дата получения не смешивается с датой документа, нули и оригинальные слеши не меняются."""
    assert registry_number(number, day) == expected


def test_exact_mapping_global_order_identity_and_manual_nulls():
    """Ровно 14 колонок, все документы при direction=null, технический ID только в JSON."""
    mapping = registry_mapping()
    assert len(mapping.columns) == len(mapping.column_order) == 14
    assert mapping.column_order == [k for k in RegistryRow.model_fields if k != "document_id"]
    job = registry_job()
    original = job.model_dump(mode="json")
    result = project_registry(job)
    assert [r.number for r in result.items] == [1, 2, 3]
    assert [r.document_id for r in result.items] == [f"document-{i}" for i in (1, 2, 3)]
    for row in result.items:
        assert row.document_number == "00042/А от 27.11.2025"
        assert row.sender == "ООО «Синтетический продавец»"
        assert row.recipient == "ООО «Синтетический покупатель»"
        assert row.document_name == "Счёт-фактура" and row.delivery_method is None
        for key, (_, source) in mapping.columns.items():
            if source is False and key != "number":
                assert getattr(row, key) is None
    assert job.model_dump(mode="json") == original
    mapping.columns["sender"] = ("ПОДМЕНА", False)
    assert registry_mapping().columns["sender"][0] == "Отправитель"
    with pytest.raises(ValidationError):
        RegistryRow(document_id="x", number=1, unknown="нельзя")


def test_mapping_matches_approved_excel_contract():
    """Фиксирует названия/источники ТЗ независимо от writer и модели строк."""
    approved = {
        "number": ["№", False],
        "date": ["Дата получения", False],
        "delivery_method": ["Способ получения (Почта / Курьер)", "requisites.delivery_channel"],
        "receipt_city": ["Город получения", False],
        "recipient": ["Получатель", "requisites.recipient.name"],
        "document_name": ["Название документа", "classification.document_kind"],
        "sender": ["Отправитель", "requisites.sender.name"],
        "document_number": [
            "№ документа",
            ["requisites.document_number", "requisites.document_date"],
        ],
        "opening_date": ["Дата вскрытия", False],
        "scanning_date": ["Дата сканирования", False],
        "registration_date": ["Дата внесения в 1С", False],
        "responsible": ["Ответственный (ОМ / Администратор)", False],
        "notes": ["Примечания", False],
        "document_link": ["Ссылка на документ в ДО", False],
    }
    assert registry_mapping().model_dump(mode="json") == {
        "schema_version": "1.0",
        "column_order": list(approved),
        "columns": approved,
    }


def test_low_confidence_and_failed_blocks_keep_rows_without_guessing():
    """Ненадёжный номер/сторона скрыты независимо; классифицированный ошибочный блок остаётся."""
    job = registry_job()
    documents = {d["document_id"]: d for f in job.result["files"] for d in f["documents"]}
    documents["document-1"]["recognition"]["low_confidence_fields"] = [
        "document_number",
        "recipient.name",
        "requisites.sender.inn",
    ]
    documents["document-2"]["requisites"]["delivery_channel"] = "Курьер"
    job.document_processing["document-3"] = DocumentProcessing(
        segment_index=3, status="extraction_failed", stage="extraction"
    )
    result = project_registry(job)
    assert result.items[0].document_number == "от 27.11.2025"
    assert result.items[0].recipient is None and result.items[0].sender is not None
    assert result.items[1].delivery_method == "Курьер"
    assert result.items[2].document_name == "Счёт-фактура"
    assert result.items[2].document_number is result.items[2].sender is None
    assert len(result.items) == 3


@pytest.mark.parametrize(
    "value,expected",
    [
        ("postal_mail", "Почта"),
        ("courier", "Курьер"),
        ("Направить ответ почтой", None),
        ("email", None),
        (None, None),
    ],
)
def test_delivery_uses_only_explicit_recognized_channel(value, expected):
    """Инструкция о будущем отправлении не становится фактическим способом получения."""
    assert registry_delivery_method(value) == expected


@pytest.mark.parametrize("status", ["classification_failed", "unsupported_template"])
def test_incomplete_block_remains_in_registry_with_untrusted_values_empty(status):
    """Ошибочный/неподдержанный блок остаётся строкой без доверия к реквизитам заглушки."""
    job = registry_job(1)
    document = job.result["files"][0]["documents"][0]
    document["recognition"]["low_confidence_fields"] = ["classification.document_kind"]
    job.document_processing["document-1"] = DocumentProcessing(
        segment_index=1, status=status, stage="classification"
    )
    row = project_registry(job).items[0]
    assert row.number == 1 and row.document_id == "document-1"
    assert row.document_name is row.document_number is row.sender is row.recipient is None


@pytest.mark.parametrize("literal", ["=1+1", "+SUM(A1:A2)", "-1+2", "@SUM(1)", "001/23"])
def test_workbook_schema_styles_literal_text_dates_and_no_external_content(literal):
    """Читает настоящий XLSX обратно: формульные строки сохранены строками, даты Excel date."""
    row = RegistryRow(
        document_id="synthetic",
        number=1,
        sender=literal,
        document_number="00042/А от 27.11.2025",
        date=date(2025, 11, 27),
    )
    content = RegistryXlsx().render(RegistryResult(job_id="job", items=[row]))
    with closing(load_workbook(BytesIO(content), data_only=False)) as workbook:
        assert workbook.sheetnames == ["Реестр"]
        sheet = workbook.active
        mapping = registry_mapping()
        assert [c.value for c in sheet[1]] == [mapping.columns[k][0] for k in mapping.column_order]
        assert sheet.max_column == 14 and sheet.max_row == 2
        assert sheet.freeze_panes == "A2" and sheet.auto_filter.ref == "A1:N2"
        assert sheet["G2"].value == literal and sheet["G2"].data_type == "s"
        assert sheet["H2"].value == row.document_number and sheet["H2"].number_format == "@"
        assert sheet["B2"].value.date() == row.date
        assert sheet["B2"].number_format == "DD.MM.YYYY" and sheet["D2"].value is None
        assert sheet["A2"].value == 1 and sheet["A2"].data_type == "n"
        assert sheet["G2"].alignment.wrap_text
    with ZipFile(BytesIO(content)) as archive:
        assert not any("vba" in n.lower() for n in archive.namelist())
        xml = archive.read("xl/worksheets/sheet1.xml")
        assert b"<f>" not in xml and b"hyperlink" not in xml
        assert all(
            b'TargetMode="External"' not in archive.read(n)
            for n in archive.namelist()
            if n.endswith(".rels")
        )


def test_workbook_failure_cleans_own_temporary_xml():
    """Ошибочный непомещающийся текст не оставляет private XML до перезапуска API."""
    before = set(ALL_TEMP_FILES)
    row = RegistryRow(document_id="synthetic", number=1, sender="x" * 32768)
    with pytest.raises(ValueError):
        RegistryXlsx().render(RegistryResult(job_id="job", items=[row]))
    assert set(ALL_TEMP_FILES) == before


def test_largest_supported_registry_has_every_row_without_extra_columns():
    """10000 регистраций соответствуют максимальному числу страниц; строки не теряются."""
    rows = [
        RegistryRow(document_id=str(i), number=i, sender="Синтетический отправитель")
        for i in range(1, 10001)
    ]
    content = RegistryXlsx().render(RegistryResult(job_id="synthetic", items=rows))
    workbook = load_workbook(BytesIO(content), read_only=True)
    try:
        values = list(workbook.active.values)
        assert len(values) == 10001 and all(len(r) == 14 for r in values)
        assert [r[0] for r in values[1:]] == list(range(1, 10001))
    finally:
        workbook.close()
