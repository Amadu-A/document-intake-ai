# app/domain/extraction.py
"""Внутренний ответ VLM с явной читаемостью реквизитов; публичный JSON не расширяется."""

from typing import Literal

from pydantic import Field

from app.domain.schema import Extracted, StrictModel


class PrimaryReadability(StrictModel):
    """Отличает неоднозначное чтение от отсутствия и низкой уверенности в читаемом факте."""

    document_number: Literal["legible", "missing", "ambiguous"]
    document_date: Literal["legible", "missing", "ambiguous"]
    handwritten_fields: list[Literal["document_number", "document_date"]] = Field(max_length=2)


class ExtractionDraft(Extracted):
    """Служебные свидетельства второго VLM-этапа доступны только до проверки/экспорта."""

    primary_readability: PrimaryReadability

    def uncertain_handwriting(self) -> set[str]:
        """Выбирает только рукописные поля, в неуверенности которых уже призналась VLM."""
        uncertain = set(self.recognition.low_confidence_fields)
        return {
            field
            for field in self.primary_readability.handwritten_fields
            if uncertain.intersection({field, f"requisites.{field}"})
        }

    def facts(self) -> Extracted:
        """Удаляет внутренние свидетельства, сохраняя утверждённые поля JSON 1.0."""
        return Extracted.model_validate(self.model_dump(exclude={"primary_readability"}))
