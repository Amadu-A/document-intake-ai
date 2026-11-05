# app/domain/schema.py
"""Экспорт JSON 1.0 и модельные факты; лишние поля, OCR и подбор 1С запрещены."""

from datetime import date as Date
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_serializer, model_validator

from app.domain.taxonomy import LETTER_PATHS

Confidence = Annotated[float, Field(ge=0, le=1)] | None


class StrictModel(BaseModel):
    """Закрывает контракт от неизвестных полей и нечисловых денежных значений."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class DocumentRoot(StrEnum):
    """Утверждённые владельцем шесть родов документов."""

    INTERNAL = "Внутренние документы компании"
    LETTER = "Входящая/Исходящая корреспонденция"
    CONTRACT = "Договоры"
    EXECUTION = "Документы связанные с исполнением договоров"
    PROCEDURAL = "Процессуальные документы"
    COMPONENT = "Составляющие документы договора"


class Alternative(StrictModel):
    """Сохраняет только допустимый альтернативный путь классификации."""

    template_path: list[str]
    confidence: Confidence = None

    @model_validator(mode="after")
    def closed_path(self) -> "Alternative":
        """Проверяет принадлежность альтернативы тому же закрытому каталогу."""
        if self.template_path and tuple(self.template_path) not in LETTER_PATHS:
            raise ValueError("Неизвестный альтернативный template_path")
        return self


class Classification(StrictModel):
    """Проверяет род, направление и полный путь до конечного шаблона письма."""

    document_root: DocumentRoot | None
    direction: Literal["incoming", "outgoing"] | None
    is_letter: bool
    template_path: list[str]
    document_kind: str | None
    document_subkind: str | None = None
    confidence: Confidence = None
    alternatives: list[Alternative] = Field(default_factory=list)

    @model_validator(mode="after")
    def closed_taxonomy(self) -> "Classification":
        """Отвергает выдуманные листы и направление у неписем."""
        if self.is_letter != (self.document_root == DocumentRoot.LETTER):
            raise ValueError("Род и is_letter противоречат друг другу")
        if not self.is_letter or self.direction is None:
            if self.template_path or (not self.is_letter and self.direction is not None):
                raise ValueError("Без направления письма template_path должен быть пуст")
        else:
            expected = "Входящее письмо" if self.direction == "incoming" else "Исходящее письмо"
            path = tuple(self.template_path)
            if path not in LETTER_PATHS or path[1] != expected:
                raise ValueError("Неизвестный template_path или неверное направление")
            if self.document_kind != path[2]:
                raise ValueError("Вид должен совпадать с каталогом template_path")
            if self.document_subkind != (path[3] if len(path) == 4 else None):
                raise ValueError("Подвид должен совпадать с конечным листом каталога")
        return self


class Party(StrictModel):
    """Поисковые реквизиты организации или лица, доступные из документа."""

    name: str | None = None
    inn: str | None = None
    kpp: str | None = None
    ogrn: str | None = None
    address: str | None = None
    email: str | None = None
    phone: str | None = None


class Signatory(StrictModel):
    """Подписант или исполнитель без выдуманного статуса 1С."""

    role: str | None = None
    name: str | None = None
    position: str | None = None


class RelatedEntity(StrictModel):
    """Ссылка на договор, акт, счёт и другие упомянутые документы."""

    number: str | None = None
    date: Date | None = None
    type: str | None = None


class RelatedEntities(StrictModel):
    """Сохраняет поисковые связи без обращения в базу 1С."""

    contracts: list[RelatedEntity] = Field(default_factory=list)
    specifications: list[RelatedEntity] = Field(default_factory=list)
    invoices: list[RelatedEntity] = Field(default_factory=list)
    acts: list[RelatedEntity] = Field(default_factory=list)
    letters: list[RelatedEntity] = Field(default_factory=list)
    orders: list[RelatedEntity] = Field(default_factory=list)
    court_cases: list[RelatedEntity] = Field(default_factory=list)


class Amount(StrictModel):
    """Денежный факт; расчёты выполняются Decimal, экспорт содержит JSON number."""

    type: str
    value: Decimal
    currency: str | None = None

    @field_serializer("value", when_used="json")
    def numeric_value(self, value: Decimal) -> float:
        """Сериализует конечную десятичную сумму в числовой формат исходного контракта."""
        return float(value)


class Attachment(StrictModel):
    """Приложение к регистрации; пустой page_range означает только упоминание."""

    title: str | None = None
    page_range: list[int] = Field(default_factory=list)
    document_kind: str | None = None
    document_root: DocumentRoot | None = None

    @model_validator(mode="after")
    def valid_range(self) -> "Attachment":
        """Разрешает пустой диапазон либо включительные физические границы страниц."""
        if self.page_range and (
            len(self.page_range) != 2 or not 1 <= self.page_range[0] <= self.page_range[1]
        ):
            raise ValueError("page_range: [] либо [начало, конец]")
        return self


class Item(StrictModel):
    """Позиция таблицы поставки с числами в исходном JSON-формате."""

    line_no: int | None = None
    name: str | None = None
    description: str | None = None
    unit: str | None = None
    quantity: Decimal | None = None
    price: Decimal | None = None
    amount: Decimal | None = None
    delivery_term: str | None = None

    @field_serializer("quantity", "price", "amount", when_used="json")
    def numeric_value(self, value: Decimal | None) -> float | None:
        """Оставляет отсутствующие числа null, присутствующие передаёт числом."""
        return float(value) if value is not None else None


class Requisites(StrictModel):
    """Полный утверждённый набор бизнес-реквизитов без полного OCR-текста."""

    title: str | None = None
    subject: str | None = None
    document_number: str | None = None
    document_date: Date | None = None
    document_date_raw: str | None = None
    sender: Party = Field(default_factory=Party)
    recipient: Party = Field(default_factory=Party)
    signatories: list[Signatory] = Field(default_factory=list)
    related_entities: RelatedEntities = Field(default_factory=RelatedEntities)
    amounts: list[Amount] = Field(default_factory=list)
    response_required: bool | None = None
    response_due_date: Date | None = None
    delivery_channel: str | None = None
    attachments: list[Attachment] = Field(default_factory=list)
    items: list[Item] = Field(default_factory=list)
    summary: str | None = None


class OneC(StrictModel):
    """Справочные поля карточки; регистрационные статусы и matching отсутствуют."""

    view_of_document: str | None = None
    folder: str | None = None
    template_leaf: str | None = None
    counterparty: str | None = None
    organization: str | None = None
    addressee: str | None = None
    received_via: str | None = None
    answer_required: bool | None = None
    form: str | None = None
    project: str | None = None
    comment: str | None = None


class Recognition(StrictModel):
    """Честная диагностика распознавания, отдельно от извлечённых фактов."""

    ocr_engine: str | None = None
    languages: list[str] = Field(default_factory=list)
    low_confidence_fields: list[str] = Field(default_factory=list)


class Extracted(StrictModel):
    """Контракт второго прохода VLM; технические идентификаторы и URLs создаёт сервер."""

    classification: Classification
    requisites: Requisites
    one_c: OneC
    recognition: Recognition
    warnings: list[str]


class AttachmentFile(StrictModel):
    """Реальный скачиваемый PDF-комплект для одной регистрации."""

    file_name: str
    file_url: str
    mime_type: Literal["application/pdf"] = "application/pdf"


class Document(Extracted):
    """Регистрационная единица с единым PDF и исходными физическими страницами."""

    document_id: str
    segment_index: int = Field(ge=1)
    pages: list[int]
    split_confidence: Confidence = None
    attachment_file: AttachmentFile


class SourceFile(StrictModel):
    """Исходный файл; сквозной комплект привязан к его первому источнику."""

    file_id: str
    file_name: str
    file_url: str
    mime_type: str
    documents: list[Document]


class BatchResult(StrictModel):
    """Неизменяемое дерево экспорта schema_version 1.0."""

    schema_version: Literal["1.0"] = "1.0"
    batch_id: str | None
    files: list[SourceFile]
