# tests/test_correspondence_projection.py
"""Юридический вид/направление независимы; проекция 1С не подменяет проверенные факты."""

import copy

import pytest

from app.application.analysis import validate_extraction
from app.application.one_c_projection import project_one_c, supported_direction
from app.domain.compact_plan import CompactPlan, compact
from app.domain.errors import IntakeError
from app.domain.extraction import ExtractionDraft
from app.domain.jobs import Job, utcnow
from app.domain.schema import Extracted
from app.domain.segmentation import Plan, validate_plan
from tests.support import facts, plan_for

ORGANIZATION = "ООО «Тестовая организация»"


def procedural():
    """Создаёт синтетический входящий судебный акт с известной стороной, без карточки 1С."""
    value = facts()
    value["classification"].update(
        document_root="Процессуальные документы",
        is_letter=False,
        direction="incoming",
        document_kind="Судебная повестка",
        template_path=[],
    )
    value["requisites"]["recipient"]["name"] = ORGANIZATION
    value["requisites"]["sender"]["name"] = "Синтетический районный суд"
    value["requisites"]["summary"] = "Назначено заседание; требуется явка."
    value["warnings"] = ["Маршрут неписьма в 1С не утверждён"]
    return Extracted.model_validate(value)


def group():
    """Возвращает фиксированное модельное множество для технической проверки реквизитов."""
    return Plan.model_validate(plan_for([{"file_id": "source", "page_number": 1}])).documents[0]


def test_nonletter_incoming_preserves_legal_root_and_projects_only_known_fields():
    """Повестка остаётся процессуальным документом, а организация/отправитель проектируются."""
    raw = procedural()
    validate_extraction(raw, group(), ORGANIZATION)
    mapped = project_one_c(raw, ORGANIZATION)
    assert raw.classification.direction == "incoming" and not raw.classification.is_letter
    assert raw.classification.document_root == "Процессуальные документы"
    assert mapped.organization == ORGANIZATION
    assert mapped.counterparty == raw.requisites.sender.name
    assert mapped.comment == raw.requisites.summary
    assert mapped.answer_required is None
    assert mapped.view_of_document is None and mapped.template_leaf is None
    assert all(
        getattr(mapped, name) is None
        for name in ("folder", "addressee", "form", "received_via", "project")
    )
    assert all(value is None for value in raw.one_c.model_dump().values())


def test_letter_incoming_keeps_closed_path_and_correspondence_route():
    """Только письмо получает маршрут и конечный шаблон утверждённого каталога."""
    value = procedural().model_dump()
    path = ["Входящая/Исходящая корреспонденция", "Входящее письмо", "Переписка с контрагентом"]
    value["classification"].update(
        document_root=path[0],
        is_letter=True,
        template_path=path,
        document_kind=path[-1],
    )
    raw = Extracted.model_validate(value)
    validate_extraction(raw, group(), ORGANIZATION)
    mapped = project_one_c(raw, ORGANIZATION)
    assert mapped.view_of_document == "Входящее письмо" and mapped.template_leaf == path[-1]


@pytest.mark.parametrize("is_letter", [False, True])
def test_pdf_recipient_does_not_set_service_organization_without_context(is_letter):
    """Даже известный получатель скана не становится нашей организацией без явного контекста."""
    value = procedural().model_dump()
    value["classification"]["direction"] = None
    if is_letter:
        value["classification"].update(
            document_root="Входящая/Исходящая корреспонденция",
            is_letter=True,
            document_kind=None,
        )
    value["warnings"] = ["Обслуживаемая организация не задана"]
    raw = Extracted.model_validate(value)
    validate_extraction(raw, group(), None)
    mapped = project_one_c(raw, None)
    assert mapped.organization is None and mapped.view_of_document is None
    assert mapped.template_leaf is None and raw.classification.direction is None


@pytest.mark.parametrize("organization", [None, "ООО «Другая организация»"])
def test_direction_requires_actual_service_party_match(organization):
    """Неверный/неустановленный контекст не позволяет уверенно назвать акт входящим."""
    raw = procedural()
    with pytest.raises(IntakeError):
        validate_extraction(raw, group(), organization)
    raw.classification.direction = None
    raw.warnings.append("Организация не совпадает с адресатом")
    validate_extraction(raw, group(), organization)


def test_nonletter_outgoing_and_party_name_normalization():
    """Направление доступно неписьму в обе стороны; кавычки/регистр не требуют fuzzy-поиска."""
    raw = procedural()
    raw.requisites.sender.name = "ооо Тестовая организация"
    raw.requisites.recipient.name = "Внешний получатель"
    raw.classification.direction = "outgoing"
    validate_extraction(raw, group(), ORGANIZATION)
    assert supported_direction(raw.requisites, ORGANIZATION) == "outgoing"
    mapped = project_one_c(raw, ORGANIZATION)
    assert mapped.organization == ORGANIZATION and mapped.counterparty is None
    assert mapped.view_of_document is None


@pytest.mark.parametrize("answer", [None, False, True])
def test_answer_required_projects_explicit_fact_instead_of_guessing_from_summary(answer):
    """Явка/удержание в summary не порождают ответное письмо; bool берётся из проверенного поля."""
    raw = procedural()
    raw.requisites.response_required = answer
    raw.requisites.summary = "Требуется явка и исполнение акта."
    assert project_one_c(raw, ORGANIZATION).answer_required is answer


@pytest.mark.parametrize(
    "name", ["organization", "counterparty", "folder", "form", "received_via", "project"]
)
def test_vlm_cannot_fill_one_c_itself(name):
    """Непустая карточка из VLM отклоняется до программной проекции."""
    raw = procedural()
    setattr(raw.one_c, name, "synthetic-untrusted-value")
    with pytest.raises(IntakeError):
        validate_extraction(raw, group(), ORGANIZATION)


def evidence_plan():
    """Описывает синтетические несмежные конверт/акт без номеров или данных реального примера."""
    refs = [{"file_id": "source", "page_number": n} for n in (1, 8)]
    value = plan_for(refs)
    value["documents"][0]["evidence"] = [
        {
            "page": refs[0],
            "role": "carrier",
            "identifiers": [{"kind": "court_case", "value": "TEST-17/2026"}],
            "sender": "Тестовый суд",
            "recipient": ORGANIZATION,
        },
        {
            "page": refs[1],
            "role": "main",
            "identifiers": [{"kind": "court_case", "value": "TEST-17/2026"}],
            "sender": "Тестовый суд",
            "recipient": ORGANIZATION,
        },
    ]
    return value, refs


def test_noncontiguous_carrier_evidence_survives_compact_plan():
    """Подтверждённая моделью связь сохраняет страницы и свидетельства при техническом сжатии."""
    value, refs = evidence_plan()
    plan = Plan.model_validate(value)
    validate_plan(plan, plan.documents[0].pages)
    physical = plan.documents[0].pages
    restored = CompactPlan.model_validate(compact(plan, physical)).expand(physical)
    assert restored == plan and len(restored.documents[0].evidence) == 2
    assert [p.page_number for p in restored.documents[0].pages] == [1, 8]


@pytest.mark.parametrize(
    "problem", ["case_conflict", "no_party", "foreign_page", "duplicate_evidence"]
)
def test_carrier_conflicts_are_rejected_without_backend_regrouping(problem):
    """Backend отказывает неверному свидетельству и оставляет смысловую корректировку VLM."""
    value, _ = evidence_plan()
    document = value["documents"][0]
    if problem == "case_conflict":
        document["evidence"][1]["identifiers"][0]["value"] = "TEST-99/2026"
    elif problem == "no_party":
        document["evidence"][0].update(sender=None, recipient=None)
    elif problem == "foreign_page":
        document["evidence"][0]["page"] = {"file_id": "other", "page_number": 1}
    else:
        document["evidence"].append(copy.deepcopy(document["evidence"][0]))
    plan = Plan.model_validate(value)
    original = plan.model_dump(mode="json")
    with pytest.raises(IntakeError):
        validate_plan(plan, plan.documents[0].pages)
    assert plan.model_dump(mode="json") == original


@pytest.mark.parametrize("readable", [False, True])
async def test_identifier_completeness_gets_one_vlm_review_without_ocr_autofill(
    container, readable
):
    """Наличие метки OCR инициирует один просмотр; неразборчивые цифры остаются null."""
    raw, reviewed = facts(), facts()
    reviewed["requisites"]["sender"]["inn"] = "SYNTHETIC-ID" if readable else None
    container.vision.answers.extend([raw, reviewed])
    job = Job(
        id="synthetic-job",
        owner="synthetic",
        status="running",
        created_at=utcnow(),
        expires_at=None,
        sources=[],
        prompts=container.prompts.base,
    )

    async def emit(kind, data):
        """Не сохраняет synthetic source вне теста."""

    result = await container.analysis._extract(
        job, group(), [{"file_id": "source", "page_number": 1, "ocr": "ИНН: UNTRUSTED-OCR"}], emit
    )
    assert len(container.vision.calls) == 2
    assert container.vision.calls[-1]["context"]["verification_fields"] == ["requisites.sender.inn"]
    assert result.requisites.sender.inn == ("SYNTHETIC-ID" if readable else None)


@pytest.mark.parametrize("field", ["document_number", "document_date"])
@pytest.mark.parametrize("short_path", [False, True])
async def test_uncertain_primary_number_or_date_requires_consistent_vlm_retry(
    container, field, short_path
):
    """Неоднозначность и заполненное значение не выдаются вместе; исправление решает VLM."""
    raw = facts()
    raw["requisites"][field] = "2026-01-02" if field == "document_date" else "SYNTHETIC-17"
    path = field if short_path else f"requisites.{field}"
    raw["recognition"]["low_confidence_fields"] = [path]
    raw["primary_readability"] = {
        "document_number": "missing",
        "document_date": "missing",
        field: "ambiguous",
        "handwritten_fields": [],
    }
    original = copy.deepcopy(raw)
    corrected = copy.deepcopy(raw)
    corrected["requisites"][field] = None
    container.vision.answers.extend([raw, corrected])
    job = Job(
        id="synthetic-job",
        owner="synthetic",
        status="running",
        created_at=utcnow(),
        expires_at=None,
        sources=[],
        prompts=container.prompts.base,
    )

    async def emit(kind, data):
        """Не выводит синтетический документ в production-журнал."""

    result = await container.analysis._extract(
        job, group(), [{"file_id": "source", "page_number": 1}], emit
    )
    assert getattr(result.requisites, field) is None
    assert raw == original
    assert len(container.vision.calls) == 2
    assert (
        f"requisites.{field}"
        in container.vision.calls[-1]["context"]["validation_errors"][0]["message"]
    )


@pytest.mark.parametrize("field", ["document_number", "document_date"])
def test_legible_primary_fact_can_keep_low_confidence_without_public_metadata(field):
    """Низкая уверенность не обнуляет однозначное чтение и не добавляет служебных полей в 1.0."""
    raw = facts()
    raw["requisites"][field] = "2026-01-02" if field == "document_date" else "SYNTHETIC-17"
    raw["recognition"]["low_confidence_fields"] = [f"requisites.{field}"]
    raw["primary_readability"] = {
        "document_number": "missing",
        "document_date": "missing",
        field: "legible",
        "handwritten_fields": [],
    }
    draft = ExtractionDraft.model_validate(raw)
    validate_extraction(draft, group(), None)
    public = draft.facts()
    assert getattr(public.requisites, field) is not None
    assert "primary_readability" not in public.model_dump()
    assert "primary_readability" not in Extracted.model_json_schema()["properties"]


@pytest.mark.parametrize("uncertain", ["document_number", "document_date"])
async def test_retry_receives_candidate_and_keeps_independent_legible_fact(container, uncertain):
    """Повтор исправляет одно поле с предыдущим ответом, не теряя независимый читаемый реквизит."""
    raw = facts()
    raw["requisites"].update(document_number="SYNTHETIC-17", document_date="2031-01-12")
    raw["recognition"]["low_confidence_fields"] = [f"requisites.{uncertain}"]
    raw["primary_readability"] = {
        "document_number": "legible",
        "document_date": "legible",
        uncertain: "ambiguous",
        "handwritten_fields": [uncertain],
    }
    original = copy.deepcopy(raw)
    corrected = copy.deepcopy(raw)
    corrected["requisites"][uncertain] = None
    independent = "document_date" if uncertain == "document_number" else "document_number"

    def revise(context):
        """Проверяет наличие исходного кандидата и конкретной причины ограниченного повтора."""
        assert context["candidate"] == original
        assert context["candidate"]["requisites"][independent] == raw["requisites"][independent]
        assert context["validation_errors"]
        assert context["revision_instruction"]
        return corrected

    container.vision.answers.extend([raw, revise])
    job = Job(
        id="synthetic-job",
        owner="synthetic",
        status="running",
        created_at=utcnow(),
        expires_at=None,
        sources=[],
        prompts=container.prompts.base,
    )

    async def emit(kind, data):
        """Не сохраняет синтетический источник и текст модели в журнале."""

    result = await container.analysis._extract(
        job, group(), [{"file_id": "source", "page_number": 1}], emit
    )
    assert getattr(result.requisites, uncertain) is None
    assert result.requisites.model_dump(mode="json")[independent] == raw["requisites"][independent]
    assert raw == original
    assert "candidate" not in container.vision.calls[0]["context"]
    assert not container.vision.calls[0]["context"]["validation_errors"]
    assert len(container.vision.calls) == 2


@pytest.mark.parametrize("field", ["document_number", "document_date"])
@pytest.mark.parametrize("explicit_ambiguity", [False, True])
async def test_retry_cannot_wash_away_declared_handwriting_uncertainty(
    container, field, explicit_ambiguity
):
    """Снятие low-confidence на повторе не разрешает ранее признанную рукописную догадку."""
    raw = facts()
    raw["requisites"][field] = "2026-01-02" if field == "document_date" else "SYNTHETIC-17"
    raw["recognition"]["low_confidence_fields"] = [f"requisites.{field}"]
    raw["primary_readability"] = {
        "document_number": "missing",
        "document_date": "missing",
        field: "legible",
        "handwritten_fields": [field],
    }
    if explicit_ambiguity:
        raw["recognition"]["low_confidence_fields"] = []
        raw["primary_readability"][field] = "ambiguous"
    washed = copy.deepcopy(raw)
    washed["recognition"]["low_confidence_fields"] = []
    washed["primary_readability"][field] = "legible"
    corrected = copy.deepcopy(raw)
    corrected["requisites"][field] = None
    corrected["primary_readability"][field] = "ambiguous"
    corrected["recognition"]["low_confidence_fields"] = [f"requisites.{field}"]
    container.settings.model_attempts = 3
    container.vision.answers.extend([raw, washed, corrected])
    job = Job(
        id="synthetic-job",
        owner="synthetic",
        status="running",
        created_at=utcnow(),
        expires_at=None,
        sources=[],
        prompts=container.prompts.base,
    )

    async def emit(kind, data):
        """Не публикует synthetic значения в обычные логи."""

    result = await container.analysis._extract(
        job, group(), [{"file_id": "source", "page_number": 1}], emit
    )
    assert getattr(result.requisites, field) is None
    assert len(container.vision.calls) == 3
    schemas = [
        call["schema"]["$defs"]["Requisites"]["properties"][field]
        for call in container.vision.calls
    ]
    assert schemas[0] != {"type": "null"}
    assert schemas[1] == schemas[2] == {"type": "null"}


@pytest.mark.parametrize("present", [False, True])
async def test_invoice_label_gets_one_review_without_creating_a_fact_from_ocr(container, present):
    """Метка вызывает просмотр; VLM подтверждает ссылку, банковский счёт не становится счётом."""
    raw, checked = facts(), facts()
    if present:
        checked["requisites"]["related_entities"]["invoices"] = [
            {"number": "SYNTHETIC-INVOICE", "date": None, "type": "Счёт на оплату"}
        ]
    container.vision.answers.extend([raw, checked])
    job = Job(
        id="synthetic-job",
        owner="synthetic",
        status="running",
        created_at=utcnow(),
        expires_at=None,
        sources=[],
        prompts=container.prompts.base,
    )

    async def emit(kind, data):
        """Сохраняет синтетические данные внутри теста."""

    result = await container.analysis._extract(
        job, group(), [{"file_id": "source", "page_number": 1, "ocr": "Счёт № UNTRUSTED"}], emit
    )
    assert len(container.vision.calls) == 2
    assert container.vision.calls[-1]["context"]["verification_fields"] == [
        "requisites.related_entities.invoices"
    ]
    assert [r.number for r in result.requisites.related_entities.invoices] == (
        ["SYNTHETIC-INVOICE"] if present else []
    )


@pytest.mark.parametrize("terminated", [False, True])
async def test_procedural_fine_context_is_decided_by_vlm_instead_of_backend(container, terminated):
    """Одинаковый bare fine не означает ни действующий, ни отменённый штраф без решения VLM."""
    raw = procedural().model_dump(mode="json")
    raw["requisites"]["amounts"] = [{"type": "fine", "value": 500, "currency": "RUB"}]
    checked = copy.deepcopy(raw)
    wanted = "Штраф по прекращённому делу" if terminated else "Штраф по действующему постановлению"
    checked["requisites"]["amounts"][0]["type"] = wanted
    container.vision.answers.extend([raw, checked])
    job = Job(
        id="synthetic-job",
        owner="synthetic",
        status="running",
        created_at=utcnow(),
        expires_at=None,
        sources=[],
        prompts=container.prompts.base,
        organization_hint=ORGANIZATION,
    )

    async def emit(kind, data):
        """Сохраняет только synthetic события внутри теста."""

    result = await container.analysis._extract(
        job, group(), [{"file_id": "source", "page_number": 1}], emit
    )
    assert result.requisites.amounts[0].type == wanted
    assert result.requisites.amounts[0].value == 500
    assert len(container.vision.calls) == 2
