# app/application/one_c_projection.py
"""Проекция проверенных реквизитов в 1С; справочники и регистрация не запрашиваются."""

import unicodedata

from app.domain.identities import name_key
from app.domain.schema import Extracted, OneC, Requisites


def canonical_organization(value: str | None) -> str | None:
    """Нормализует только явно переданный контекст, не выбирая организацию из текста PDF."""
    text = " ".join(unicodedata.normalize("NFKC", value or "").split())
    return text or None


def organization_key(value: str | None) -> str:
    """Сравнивает полные имена без различий регистра/кавычек, без fuzzy-поиска организаций."""
    return name_key(value)


def supported_direction(requisites: Requisites, organization: str | None) -> str | None:
    """Подтверждает направление только совпадением обслуживаемой организации с одной стороной."""
    key = organization_key(organization)
    if not key:
        return None
    sender = organization_key(requisites.sender.name) == key
    recipient = organization_key(requisites.recipient.name) == key
    if sender == recipient:
        return None
    return "outgoing" if sender else "incoming"


def project_one_c(facts: Extracted, organization: str | None) -> OneC:
    """Проектирует текстовые факты и доверенную организацию; неизвестные поля 1С остаются null."""
    organization = canonical_organization(organization)
    classification, requisites = facts.classification, facts.requisites
    path = (
        classification.template_path
        if classification.is_letter and classification.direction
        else []
    )
    sender = requisites.sender.name
    if organization and organization_key(sender) == organization_key(organization):
        sender = None
    summary = " ".join((requisites.summary or "").split()) or None
    return OneC(
        organization=organization,
        counterparty=sender,
        view_of_document=path[1] if path else None,
        template_leaf=path[-1] if path else None,
        answer_required=requisites.response_required,
        comment=summary,
    )
