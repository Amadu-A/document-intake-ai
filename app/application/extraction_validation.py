# app/application/extraction_validation.py
"""Проверяет совместимость профиля и локальные свидетельства, не выбирая тип вместо VLM."""

from app.domain.document_kinds import categories
from app.domain.document_processing import DocumentClassification
from app.domain.errors import IntakeError
from app.domain.extractions import DetailedExtraction
from app.domain.identities import name_key
from app.domain.segmentation import PageRef


def validate_selection(
    value: DocumentClassification, organization: str | None, pages: list[dict] | None = None
) -> None:
    """Не разрешает случайный Dxx, неверный род или договорную роль без контекста/свидетельства."""
    organization = " ".join((organization or "").split()) or None
    code, classification = value.kind_code, value.classification
    if not organization and classification.direction is not None:
        raise IntakeError(
            "schema_validation_failed", "Без организации направление должно быть null"
        )
    if code is None:
        return
    item = categories().get(code)
    if not item or classification.document_root != item["document_root"]:
        raise IntakeError("schema_validation_failed", "Код профиля не соответствует реестру/роду")
    if code == "D01" and classification.direction != "incoming":
        raise IntakeError("schema_validation_failed", "D01 — только подтверждённое входящее письмо")
    if code == "D02" and (classification.document_kind or "").casefold() != "исполнительный лист":
        raise IntakeError("schema_validation_failed", "D02 применим только к исполнительному листу")
    if code in {"D04", "D05", "D06", "D07", "D08"} and (
        not organization
        or not value.our_role
        or not value.role_evidence
        or not value.role_source_page
    ):
        raise IntakeError(
            "schema_validation_failed",
            "Договорный профиль требует организации и подтверждённой роли; иначе kind_code=null",
        )
    if code in {"D04", "D05", "D06", "D07", "D08"}:
        expected = {
            "D04": "supplier",
            "D05": "customer",
            "D06": "service_provider",
            "D07": "supplier",
            "D08": "customer",
        }[code]
        allowed = {PageRef(file_id=p["file_id"], page_number=p["page_number"]) for p in pages or []}
        if (
            value.our_role != expected
            or name_key(organization) not in name_key(value.role_evidence)
            or value.role_source_page not in allowed
        ):
            raise IntakeError(
                "schema_validation_failed",
                "Роль/цитата/страница не подтверждают профиль организации; иначе kind_code=null",
            )


def validate_details(
    value: DetailedExtraction,
    selection: DocumentClassification | None,
    pages: list[dict],
    main_pages: list[PageRef] | None = None,
) -> None:
    """Проверяет профиль, денежную роль и адреса evidence внутри утверждённого блока."""
    code = selection.kind_code if selection else None
    if value.kind_code != code:
        raise IntakeError("schema_validation_failed", "Извлечение не меняет выбранный kind_code")
    if code is None and value.details is not None:
        raise IntakeError("schema_validation_failed", "Для неизвестного профиля details=null")
    if code and value.details is None:
        value.warnings.append("Подробные реквизиты профиля не получены; сохранены общие факты")
    if code and value.details is not None and value.details.profile != code:
        raise IntakeError(
            "schema_validation_failed", "Детали должны соответствовать выбранному профилю"
        )
    if code == "D03" and value.details is not None:
        kind = (value.classification.document_kind or "").casefold().replace("ё", "е")
        if kind == "счет на оплату" and value.details.primary_document_role not in {
            None,
            "payment_request",
        }:
            raise IntakeError(
                "schema_validation_failed",
                "Счёт на оплату — payment_request, не подтверждение исполнения",
            )
        if (
            value.details.primary_document_type is not None
            and value.details.primary_document_type != value.classification.document_kind
        ):
            raise IntakeError(
                "schema_validation_failed", "Точный вид D03 должен сохраняться в деталях"
            )
    if code in {"D04", "D05", "D06"} and value.details is not None:
        role = {"D04": "supplier", "D05": "customer", "D06": "service_provider"}[code]
        if value.details.our_role is not None and value.details.our_role != role:
            raise IntakeError(
                "schema_validation_failed", "Детали не меняют подтверждённую роль организации"
            )
    allowed = {PageRef(file_id=p["file_id"], page_number=p["page_number"]) for p in pages}
    raw = value.model_dump(mode="python")
    missing = []

    def inspect_fields(node: object, path: str) -> None:
        """Собирает пробелы provenance значения либо родительского объекта/строки таблицы."""
        if isinstance(node, dict):
            for key, child in node.items():
                if key != "profile":
                    inspect_fields(child, path + "." + key)
        elif isinstance(node, list):
            for index, child in enumerate(node):
                inspect_fields(child, path + "." + str(index))
        elif (
            node is not None
            and ".page_range." not in path
            and not (path == "requisites.delivery_channel" and node == "unknown")
        ):
            parts = path.split(".")
            prefixes = [".".join(parts[:length]) for length in range(2, len(parts) + 1)]
            if not any(
                record.status == "found"
                for prefix in prefixes
                for record in value.field_evidence.get(prefix, [])
            ):
                missing.append(path)

    inspect_fields(raw["requisites"], "requisites")
    if raw["details"]:
        inspect_fields(raw["details"], "details")
    if missing:
        # Контракт допускает неполную карту provenance; отсутствие вторичного
        # свидетельства не превращает весь блок в отказ. Собственные реквизиты
        # и вид ниже по-прежнему требуют отдельного подтверждения.
        value.warnings.append("Неполная карта свидетельств для полей: " + ", ".join(missing[:20]))
    for path, fact in (
        ("requisites.document_number", value.requisites.document_number),
        ("requisites.document_date", value.requisites.document_date),
        ("classification.document_kind", value.classification.document_kind),
    ):
        if fact is not None and not any(
            record.status == "found" for record in value.field_evidence.get(path, [])
        ):
            raise IntakeError(
                "schema_validation_failed",
                f"{path}: требуется свидетельство found на странице блока; для вида это "
                "короткая цитата содержания, обосновывающая смысл выбранной классификации, "
                "а для собственного номера/даты — их буквальное визуальное чтение",
            )
    for path, records in value.field_evidence.items():
        if not path.startswith(("requisites.", "details.", "classification.")):
            raise IntakeError("schema_validation_failed", "Недопустимый путь evidence")
        current = raw
        for key in path.split("."):
            if isinstance(current, dict) and key in current:
                current = current[key]
            elif isinstance(current, list) and key.isdigit() and int(key) < len(current):
                current = current[int(key)]
            else:
                raise IntakeError(
                    "schema_validation_failed", "Evidence указывает на отсутствующее поле"
                )
        for record in records:
            if record.confidence is not None:
                raise IntakeError(
                    "schema_validation_failed",
                    "Некалиброванный confidence свидетельства должен быть null",
                )
            if PageRef(file_id=record.file_id, page_number=record.page_number) not in allowed:
                raise IntakeError(
                    "schema_validation_failed", "Evidence выходит за утверждённый блок"
                )
            if (
                path in {"requisites.document_number", "requisites.document_date"}
                and record.status == "found"
            ):
                if (
                    record.source != "image"
                    or main_pages
                    and PageRef(file_id=record.file_id, page_number=record.page_number)
                    not in main_pages
                ):
                    raise IntakeError(
                        "schema_validation_failed",
                        "Собственный номер/дата требуют изображения основной части, "
                        "не приложения или контекста",
                    )
            if record.status == "found" and (current is None or not record.raw):
                raise IntakeError(
                    "schema_validation_failed", "Found требует значения и буквального свидетельства"
                )
            if (
                record.status
                in {
                    "ambiguous",
                    "unreadable",
                    "not_present",
                    "explicit_no_number",
                    "not_applicable",
                }
                and current is not None
            ):
                raise IntakeError(
                    "schema_validation_failed", "Неподтверждённый реквизит должен быть null"
                )
    for party in (value.requisites.sender, value.requisites.recipient):
        for field, lengths in (("inn", {10, 12}), ("kpp", {9}), ("ogrn", {13, 15})):
            identifier = getattr(party, field)
            if identifier is not None and (
                not identifier.isascii()
                or not identifier.isdigit()
                or len(identifier) not in lengths
            ):
                raise IntakeError(
                    "schema_validation_failed",
                    "Неверный формат ИНН/КПП/ОГРН; перечитай источник или верни null с warning",
                )
