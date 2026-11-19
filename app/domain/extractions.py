# app/domain/extractions.py
"""Типизированный внутренний результат и отдельный JSON 2.0 с профилями и свидетельствами."""

from datetime import date as Date
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BeforeValidator, Field, field_serializer, field_validator

from app.domain.document_kinds import categories
from app.domain.extraction import ExtractionDraft
from app.domain.schema import (
    Amount,
    AttachmentFile,
    Classification,
    Extracted,
    Item,
    OneC,
    Recognition,
    Requisites,
    StrictModel,
)
from app.domain.segmentation import PageRef


def exact_decimal(value: object) -> object:
    """Не принимает бинарный float: его десятичную точность уже нельзя восстановить."""
    if isinstance(value, float):
        raise ValueError("Денежное/табличное значение передаётся десятичной строкой, не float")
    return value


ExactDecimal = Annotated[Decimal, Field(allow_inf_nan=False), BeforeValidator(exact_decimal)]
DecimalValue = ExactDecimal | None
FieldStatus = Literal[
    "found", "not_present", "unreadable", "ambiguous", "explicit_no_number", "not_applicable"
]


class Evidence(StrictModel):
    """Короткое свидетельство конкретной физической страницы; confidence не выдумывается."""

    file_id: str
    page_number: int = Field(ge=1)
    raw: str | None = Field(None, max_length=500)
    status: FieldStatus
    source: Literal["image", "ocr", "context"]
    confidence: float | None = Field(None, ge=0, le=1)


class ContractReference(StrictModel):
    """Номер/дата договора из скана без ссылки или GUID базы 1С."""

    number: str | None = None
    date: Date | None = None
    kind: str | None = None


class LetterDetails(StrictModel):
    """Письма используют общие реквизиты; профиль не дублирует их отдельной карточкой."""

    profile: Literal["D01"]


class EnforcementDetails(StrictModel):
    """Специальные факты именно исполнительного листа."""

    profile: Literal["D02"]
    court_name: str | None = None
    court_case_number: str | None = None
    issue_date: Date | None = None
    claimant: str | None = None
    debtor: str | None = None
    enforcement_subject: str | None = None


class PrimaryDetails(StrictModel):
    """Различает первичку, налоговые документы, сверку и запрос платежа в одной категории D03."""

    profile: Literal["D03"]
    primary_document_type: str | None = None
    primary_document_role: Literal["primary", "tax", "payment_request", "reconciliation"] | None = (
        None
    )
    parent_contract: ContractReference | None = None
    total_amount: DecimalValue = None
    vat_amount: DecimalValue = None
    currency: str | None = None
    payment_purpose: str | None = None
    payment_terms: str | None = None
    bank_details: str | None = None


class ContractDetails(StrictModel):
    """Срок и предмет договора; роль организации требует подтверждённого контекста."""

    profile: Literal["D04", "D05"]
    contract_subject: str | None = None
    our_role: Literal["supplier", "customer"] | None = None
    valid_from: Date | None = None
    valid_until: Date | None = None
    total_amount: DecimalValue = None
    currency: str | None = None


class ServiceDetails(StrictModel):
    """Доходный сервисный договор, а не произвольный подряд."""

    profile: Literal["D06"]
    service_subject: str | None = None
    our_role: Literal["service_provider"] | None = None
    valid_from: Date | None = None
    valid_until: Date | None = None
    total_amount: DecimalValue = None
    currency: str | None = None


class SpecificationDetails(StrictModel):
    """Спецификация содержит только напечатанные условия договора-основания и сумму."""

    profile: Literal["D07", "D08"]
    parent_contract: ContractReference | None = None
    total_amount: DecimalValue = None
    currency: str | None = None


class AmendmentDetails(StrictModel):
    """Предмет и начало действия изменения договора."""

    profile: Literal["D09"]
    parent_contract: ContractReference | None = None
    change_subject: str | None = None
    effective_date: Date | None = None
    total_amount: DecimalValue = None


class DisagreementDetails(StrictModel):
    """Разногласия с сохранением точного вида протокола."""

    profile: Literal["D10"]
    parent_contract: ContractReference | None = None
    disputed_terms: list[str] = Field(default_factory=list)
    total_amount: DecimalValue = None


class AppendixDetails(StrictModel):
    """Самостоятельное утверждённое приложение к договору."""

    profile: Literal["D11"]
    parent_contract: ContractReference | None = None
    attachment_subject: str | None = None
    total_amount: DecimalValue = None


Details = Annotated[
    LetterDetails
    | EnforcementDetails
    | PrimaryDetails
    | ContractDetails
    | ServiceDetails
    | SpecificationDetails
    | AmendmentDetails
    | DisagreementDetails
    | AppendixDetails,
    Field(discriminator="profile"),
]


class DetailedAmount(Amount):
    """JSON 2.0 сохраняет точное десятичное представление суммы."""

    value: ExactDecimal

    @field_serializer("value", when_used="json")
    def numeric_value(self, value: Decimal) -> str:
        """Не пропускает деньги через бинарный float при расширенном экспорте."""
        return str(value)


class DetailedItem(Item):
    """Товарные строки JSON 2.0 сохраняют десятичные строки."""

    quantity: DecimalValue = None
    price: DecimalValue = None
    amount: DecimalValue = None

    @field_serializer("quantity", "price", "amount", when_used="json")
    def numeric_value(self, value: Decimal | None) -> str | None:
        """Сохраняет точность и null отдельно от фактического нуля."""
        return str(value) if value is not None else None


class DetailedRequisites(Requisites):
    """Тот же набор общих полей, но точная денежная сериализация расширенного контракта."""

    amounts: list[DetailedAmount] = Field(default_factory=list)
    items: list[DetailedItem] = Field(default_factory=list)


class DetailedFacts(Extracted):
    """Проверенные факты блока; служебные данные v2 не добавляются в экспорт v1."""

    requisites: DetailedRequisites
    kind_code: str | None = Field(None, json_schema_extra={"enum": [None, *categories()]})
    details: Details | None = None
    field_evidence: dict[str, list[Evidence]] = Field(default_factory=dict)

    @field_validator("kind_code")
    @classmethod
    def known_kind(cls, value: str | None) -> str | None:
        """Принимает только коды единственного серверного каталога."""
        if value is not None and value not in categories():
            raise ValueError("Код отсутствует в реестре видов")
        return value

    def legacy(self) -> Extracted:
        """Проецирует строго известные поля JSON 1.0, сохраняя Decimal до сериализации v1."""
        return Extracted.model_validate(
            {
                key: value
                for key, value in self.model_dump(mode="python").items()
                if key in Extracted.model_fields
            }
        )


class DetailedExtraction(ExtractionDraft):
    """Отдельный внутренний VLM-ответ: v1-свидетельства чтения плюс расширенные факты."""

    requisites: DetailedRequisites
    kind_code: str | None = Field(None, json_schema_extra={"enum": [None, *categories()]})
    details: Details | None = None
    field_evidence: dict[str, list[Evidence]] = Field(default_factory=dict)

    def facts(self) -> DetailedFacts:
        """Удаляет только служебную читаемость, сохраняя проверенные детали/свидетельства."""
        return DetailedFacts.model_validate(
            self.model_dump(mode="python", exclude={"primary_readability"})
        )


class DetailedDocument(StrictModel):
    """Одна регистрация JSON 2.0 с полным provenance, классификацией и PDF."""

    document_id: str
    segment_index: int = Field(ge=1)
    source_pages: list[PageRef]
    classification: Classification
    kind_code: str | None
    kind_name: str | None
    kind_path: list[str]
    requisites: DetailedRequisites
    details: Details | None
    field_evidence: dict[str, list[Evidence]]
    one_c: OneC
    recognition: Recognition
    warnings: list[str]
    attachment_file: AttachmentFile


class DetailedResult(StrictModel):
    """Отдельная расширенная выдача; старые задания не мигрируются в неё по догадке."""

    schema_version: Literal["2.0"] = "2.0"
    batch_id: str
    documents: list[DetailedDocument]
