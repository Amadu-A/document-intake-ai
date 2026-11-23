# app/domain/document_names.py
"""Имена готовых PDF из проверенных фактов; классификацию и реквизиты не угадывает."""

import re
import unicodedata

from app.domain.errors import IntakeError
from app.domain.schema import Extracted

_FORBIDDEN = str.maketrans(dict(zip('*?"<>|:/\\', "＊？＂＜＞｜：／＼", strict=True)))
_ABBREVIATIONS = {
    word.casefold(): word
    for word in (
        "УПД",
        "УКД",
        "ТОРГ",
        "КС",
        "ОС",
        "М",
        "ГОСТ",
        "СНиП",
        "СП",
        "РФ",
        "СССР",
        "ФССП",
        "ФНС",
        "СФР",
        "ПФР",
        "НДС",
        "НДФЛ",
        "ИНН",
        "КПП",
        "ОГРН",
        "ООО",
        "АО",
        "ПАО",
        "ОАО",
        "ЗАО",
        "ИП",
        "ГК",
        "ТК",
        "АПК",
        "ГПК",
        "КоАП",
    )
}
_ORDINARY_WORDS = set(
    (
        "акт счет счёт лист форма отчет отчёт заказ ордер смета книга вызов отзыв исх "
        "выписка из по на от о об и с со к до за для в во при договор рамочный заявка "
        "перевозку грузов оплату фактура результатам проведения выездной проверки "
        "письмо информационное входящее исходящее исполнительный судебный приказ "
        "договоры поставки изделий спецификация соглашение дополнительное протокол "
        "разногласий приложение накладная транспортная товарная платежное поручение "
        "выполненных работ оказанных услуг сверки взаиморасчетов приемки приёмки передачи "
        "денежных средств кассовый чек план копия служебная записка повестка постановление"
    ).split()
)


def _sentence_case(value: str) -> str:
    """Нормализует обычные слова, сохраняя аббревиатуры и имеющееся написание собственных имён."""

    def word(match: re.Match) -> str:
        """Защищает цитируемые названия и аббревиатуры от преобразования общего CAPS LOCK."""
        token = match.group()
        if token.startswith(("«", "“", '"')):
            return token
        key = token.casefold()
        if key in _ABBREVIATIONS:
            return _ABBREVIATIONS[key]
        if key in _ORDINARY_WORDS or token.isupper() and len(token) > 5:
            return token.lower()
        return token

    value = re.sub(r'«[^»]*»|“[^”]*”|"[^"]*"|[^\W\d_]+', word, value)
    return value[:1].upper() + value[1:]


def filename_part(value: str, *, number: bool = False) -> str:
    """Очищает название; запрещённые знаки официального номера заменяет безопасными аналогами."""
    value = unicodedata.normalize("NFC", value)
    value = "".join(
        " " if c.isspace() else c
        for c in value
        if c.isspace() or not unicodedata.category(c).startswith("C")
    )
    if number:
        return value.translate(_FORBIDDEN).strip()
    value = re.sub(r'[<>:"/\\|?*]+', " ", _sentence_case(value))
    value = " ".join(value.replace("_", " ").split()).lstrip(" .")
    return value[:1].upper() + value[1:]


def _check_length(name: str) -> str:
    """Не обрезает реквизиты молча, если полное имя превышает физический лимит компонента пути."""
    if len(name.encode("utf-8")) > 255:
        raise IntakeError("document_name_too_long", "Полное имя PDF превышает 255 байт", 409)
    return name


def document_basename(facts: Extracted) -> str:
    """Строит полное название № номер от ДД.ММ.ГГГГ.pdf из достоверных собственных реквизитов."""
    classification = facts.classification
    if classification.document_kind:
        kind = classification.document_kind
    elif classification.is_letter:
        kind = {"incoming": "Входящее письмо", "outgoing": "Исходящее письмо"}.get(
            classification.direction, "Письмо"
        )
    else:
        kind = classification.document_kind or "Документ"
    name = filename_part(kind) or "Документ"
    uncertain = set(facts.recognition.low_confidence_fields)
    if facts.requisites.document_number and not uncertain.intersection(
        {"requisites", "document_number", "requisites.document_number"}
    ):
        number = filename_part(facts.requisites.document_number, number=True)
        if number:
            name += " № " + number
    if facts.requisites.document_date and not uncertain.intersection(
        {"requisites", "document_date", "requisites.document_date"}
    ):
        date = facts.requisites.document_date
        name += f" от {date.day:02d}.{date.month:02d}.{date.year:04d}"
    reserved = {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }
    if name.split(".", 1)[0].upper() in reserved:
        name = "Документ " + name
    return _check_length(name + ".pdf")


def document_filename(index: int, facts: Extracted, used_names: set[str] | None = None) -> str:
    """Даёт единое имя PDF; коллизии внутри пачки разрешает суффиксом (2), (3), без смены фактов."""
    name = document_basename(facts)
    if used_names is not None:
        stem, ordinal = name[:-4], 2
        while name.casefold() in used_names:
            name = _check_length(f"{stem} ({ordinal}).pdf")
            ordinal += 1
        used_names.add(name.casefold())
    return name


def document_archive_name(index: int, facts: Extracted, used_names: set[str] | None = None) -> str:
    """Помещает PDF без префикса в папку doc_N; прямой слеш обязателен для ZIP на всех ОС."""
    return f"doc_{index}/{document_filename(index, facts, used_names)}"
