# tests/correspondence_support.py
"""Частная матрица предметных фактов через REST; UUID, токены и confidence не сравниваются."""

import asyncio
import hashlib
import json
import re
from collections import Counter
from datetime import date
from decimal import Decimal
from pathlib import Path
from time import monotonic

import httpx
import pymupdf

from app.domain.schema import BatchResult


def summary_dates(value: str | None) -> set[str]:
    """Сравнивает факты дат в summary без требования одного русского/ISO оформления."""
    text = (value or "").casefold()
    found = []
    for year, month, day in re.findall(r"\b(\d{4})-(\d{2})-(\d{2})\b", text):
        found.append((int(year), int(month), int(day)))
    for day, month, year in re.findall(r"\b(\d{1,2})[./](\d{1,2})[./](\d{4})\b", text):
        found.append((int(year), int(month), int(day)))
    months = (
        "января",
        "февраля",
        "марта",
        "апреля",
        "мая",
        "июня",
        "июля",
        "августа",
        "сентября",
        "октября",
        "ноября",
        "декабря",
    )
    for number, name in enumerate(months, 1):
        for day, year in re.findall(rf"\b(\d{{1,2}})\s+{name}\s+(\d{{4}})\b", text):
            found.append((int(year), number, int(day)))
    result = set()
    for parts in found:
        try:
            result.add(date(*parts).isoformat())
        except ValueError:
            continue
    return result


def summary_times(value: str | None) -> set[str]:
    """Приводит часы/минуты повестки к HH:MM, не принимая календарную дату за время."""
    text = (value or "").casefold()
    values = re.findall(r"(?<!\d)(\d{1,2}):([0-5]\d)(?!\d)", text)
    values += re.findall(r"\b(\d{1,2})\s*ч(?:ас(?:а|ов|ам)?)?\.?(?![а-яё])\s*([0-5]\d)?", text)
    return {f"{int(hour):02}:{int(minute or 0):02}" for hour, minute in values if int(hour) < 24}


def field(value: dict, path: str):
    """Читает существующий предметный путь без привязки к динамическому document_id."""
    for part in path.split("."):
        value = value[part]
    return value


def compare_correspondence(expected: dict, actual: dict, organization: str | None) -> list[dict]:
    """Сравнивает конкретные факты; тексты summary/warnings проверяются по наличию, не дословно."""
    rows = []

    def check(path: str, wanted, received, pages=None):
        """Сохраняет проверяемое утверждение в частном отчёте без signed URL."""
        rows.append(
            {
                "pages": pages,
                "field": path,
                "expected": wanted,
                "actual": received,
                "match": wanted == received,
            }
        )

    expected_documents = [d for f in expected["files"] for d in f["documents"]]
    actual_documents = [d for f in actual["files"] for d in f["documents"]]
    check("document_count", len(expected_documents), len(actual_documents))
    check(
        "coverage",
        sorted(p for d in expected_documents for p in d["pages"]),
        sorted(p for d in actual_documents for p in d["pages"]),
    )
    indexed = {tuple(d["pages"]): d for d in actual_documents}
    for wanted in expected_documents:
        pages = wanted["pages"]
        document = indexed.get(tuple(pages))
        check("pages", pages, document["pages"] if document else None, pages)
        if document is None:
            continue
        for path in (
            "classification.document_root",
            "classification.is_letter",
            "requisites.document_number",
            "requisites.document_date",
            "requisites.response_required",
            "requisites.response_due_date",
            "one_c.answer_required",
        ):
            check(path, field(wanted, path), field(document, path), pages)
        check(
            "classification.direction",
            "incoming" if organization else None,
            document["classification"]["direction"],
            pages,
        )
        check("one_c.organization", organization, document["one_c"]["organization"], pages)
        path = document["classification"]["template_path"]
        if organization and wanted["classification"]["is_letter"]:
            allowed = [wanted["classification"]["template_path"]]
            if allowed[0][1] == "Входящее письмо":
                allowed.append([*allowed[0][:2], "Переписка с контрагентом"])
            check("template_path_allowed", True, path in allowed, pages)
            check(
                "one_c.view_of_document",
                path[1] if path else None,
                document["one_c"]["view_of_document"],
                pages,
            )
            check(
                "one_c.template_leaf",
                path[-1] if path else None,
                document["one_c"]["template_leaf"],
                pages,
            )
        else:
            check("classification.template_path", [], path, pages)
            for name in ("view_of_document", "template_leaf"):
                check("one_c." + name, None, document["one_c"][name], pages)
        if not wanted["classification"]["is_letter"]:
            check(
                "classification.document_kind",
                wanted["classification"]["document_kind"],
                document["classification"]["document_kind"],
                pages,
            )
        for name in ("folder", "addressee", "form", "received_via", "project"):
            check("one_c." + name, None, document["one_c"][name], pages)
        check(
            "one_c.counterparty_from_sender",
            document["requisites"]["sender"]["name"],
            document["one_c"]["counterparty"],
            pages,
        )
        check(
            "one_c.comment_from_summary",
            " ".join((document["requisites"]["summary"] or "").split()) or None,
            document["one_c"]["comment"],
            pages,
        )
        for name in ("inn", "kpp", "ogrn"):
            value = wanted["requisites"]["sender"][name]
            if value is not None:
                check(
                    "requisites.sender." + name,
                    value,
                    document["requisites"]["sender"][name],
                    pages,
                )
        expected_cases = {
            c["number"] for c in wanted["requisites"]["related_entities"]["court_cases"]
        }
        actual_cases = {
            c["number"] for c in document["requisites"]["related_entities"]["court_cases"]
        }
        check("court_case_numbers_present", True, expected_cases.issubset(actual_cases), pages)
        actual_links = {
            c["number"]: c for c in document["requisites"]["related_entities"]["court_cases"]
        }
        for index, related in enumerate(wanted["requisites"]["related_entities"]["court_cases"]):
            check(
                f"court_cases[{index}].date",
                related["date"],
                actual_links.get(related["number"], {}).get("date"),
                pages,
            )
        wanted_amounts = Counter(Decimal(str(a["value"])) for a in wanted["requisites"]["amounts"])
        received_amounts = Counter(
            Decimal(str(a["value"])) for a in document["requisites"]["amounts"]
        )
        check("amount_values_present", True, not (wanted_amounts - received_amounts), pages)
        kind = (wanted["classification"]["document_kind"] or "").casefold()
        summary = document["requisites"]["summary"] or ""
        if "повестка" in kind:
            dates = summary_dates(wanted["requisites"]["summary"])
            times = summary_times(wanted["requisites"]["summary"])
            check(
                "hearing_date_present", True, bool(dates) and dates <= summary_dates(summary), pages
            )
            check(
                "hearing_time_present", True, bool(times) and times <= summary_times(summary), pages
            )
            address = wanted["requisites"]["sender"]["address"] or ""
            match = re.search(r"(?:улица|ул\.?)\s*([А-ЯЁа-яё-]+)", address)
            if match:
                street = match[1].casefold()
                actual_address = (document["requisites"]["sender"]["address"] or "").casefold()
                check("sender_street_from_envelope", True, street in actual_address, pages)
                house = re.search(r"\d+", address[match.end() :])
                if house:
                    check(
                        "sender_house_from_envelope",
                        True,
                        bool(re.search(rf"(?<!\d){house[0]}(?!\d)", actual_address)),
                        pages,
                    )
        if "пристав" in kind:

            def percentages(text):
                """Проверяет процент удержания как условие, отдельно от денежных сумм."""
                return {
                    Decimal(x.replace(",", "."))
                    for x in re.findall(r"(\d+(?:[.,]\d+)?)\s*(?:%|процент)", text or "")
                }

            required = percentages(wanted["requisites"]["summary"])
            check(
                "withholding_percentage_in_summary",
                True,
                bool(required) and required <= percentages(summary),
                pages,
            )
        historical = [
            a
            for a in wanted["requisites"]["amounts"]
            if any(s in a["type"].casefold() for s in ("прекращ", "отмен", "истор"))
        ]
        if historical:
            words = ("прекращ", "отмен", "истор", "cancel", "terminat", "histor", "closed")
            check(
                "termination_in_summary", True, any(s in summary.casefold() for s in words), pages
            )
            for index, amount in enumerate(historical):
                check(
                    f"historical_amount[{index}].role",
                    True,
                    any(
                        Decimal(str(a["value"])) == Decimal(str(amount["value"]))
                        and any(s in a["type"].casefold() for s in words)
                        for a in document["requisites"]["amounts"]
                    ),
                    pages,
                )
        for path in wanted["recognition"]["low_confidence_fields"]:
            check(
                "low_confidence:" + path,
                True,
                path in document["recognition"]["low_confidence_fields"],
                pages,
            )
        if not organization:
            check("unknown_organization_warning", True, bool(document["warnings"]), pages)
    return rows


async def run_correspondence(
    client: httpx.AsyncClient,
    source: Path,
    expected: Path,
    headers: dict,
    organization: str | None,
    timeout_seconds: int,
) -> dict:
    """Прогоняет новый API с настоящими или явно тестовыми адаптерами и проверяет скачанные PDF."""
    golden = json.loads(await asyncio.to_thread(expected.read_text, "utf-8"))
    began = monotonic()
    with source.open("rb") as stream:
        response = await client.post(
            "/api/v1/jobs",
            headers=headers,
            files={"files": (source.name, stream, "application/pdf")},
            data={"organization_hint": organization} if organization else {},
        )
    response.raise_for_status()
    identifier = response.json()["job_id"]
    while monotonic() - began < timeout_seconds:
        response = await client.get(
            f"/api/v1/jobs/{identifier}/snapshot?include_pages=false", headers=headers
        )
        response.raise_for_status()
        status = response.json()
        if status["status"] in {
            "completed",
            "completed_with_warnings",
            "failed",
            "cancelled",
            "awaiting_confirmation",
        }:
            break
        await asyncio.sleep(0.5)
    else:
        raise TimeoutError("Не завершён прогон корреспонденции")
    report = {
        "status": status["status"],
        "organization": organization,
        "error_code": (status.get("error") or {}).get("code"),
        "fallback_code": (status.get("segmentation_fallback") or {}).get("code"),
        "seconds": round(monotonic() - began, 2),
        "checks": [],
    }
    if status["status"] not in {"completed", "completed_with_warnings"}:
        return report
    response = await client.get(f"/api/v1/jobs/{identifier}/result", headers=headers)
    response.raise_for_status()
    result = BatchResult.model_validate(response.json()).model_dump(mode="json")
    report["checks"] = compare_correspondence(golden, result, organization)
    # Сырой result остаётся только в private отчёте вызывающего runner, без токенов.
    report["documents"] = [
        {
            key: doc[key]
            for key in ("pages", "classification", "requisites", "one_c", "recognition", "warnings")
        }
        for f in result["files"]
        for doc in f["documents"]
    ]
    artifacts = {a["document_id"]: a for a in status["artifacts"]}
    with pymupdf.open(source) as original:
        for f in result["files"]:
            for doc in f["documents"]:
                response = await client.get(doc["attachment_file"]["file_url"])
                response.raise_for_status()
                artifact = artifacts[doc["document_id"]]
                valid = hashlib.sha256(response.content).hexdigest() == artifact["sha256"]
                with pymupdf.open(stream=response.content, filetype="pdf") as pdf:
                    valid &= len(pdf) == len(doc["pages"]) and all(
                        pdf[n].get_pixmap().samples == original[page - 1].get_pixmap().samples
                        for n, page in enumerate(doc["pages"])
                    )
                report["checks"].append(
                    {
                        "pages": doc["pages"],
                        "field": "pdf_integrity",
                        "expected": True,
                        "actual": valid,
                        "match": valid,
                    }
                )
    return report
