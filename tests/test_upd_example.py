# tests/test_upd_example.py
"""Регрессия частного повёрнутого счёта-фактуры; controlled и настоящий live разделены."""

import httpx
import pymupdf
import pytest

from app.application.analysis import Analyze
from app.core.settings import Settings
from app.domain.extractions import DetailedFacts, DetailedResult
from app.domain.schema import BatchResult
from app.infrastructure.docling import DoclingOCR
from app.infrastructure.vision import SharedVision
from app.main import create_app
from tests.upd_support import (
    KEY,
    ReferenceVision,
    assert_business,
    check_exports,
    fingerprint,
    reference,
    run_packet,
)

pytestmark = pytest.mark.private


@pytest.fixture
def settings(settings, tmp_path_factory):
    """Короткий приватный путь позволяет проверять Windows с отключёнными long paths."""
    settings.data_dir = tmp_path_factory.mktemp("u") / "d"
    return settings


def test_private_invoice_reference_schemas_projection_money_and_source_are_consistent():
    """Проверяет полный контракт эталонов и округление, не объявляя это точностью AI."""
    paths, (v1, v2, expected, adjacent) = reference()
    before = fingerprint(paths)
    BatchResult.model_validate(v1)
    DetailedResult.model_validate(v2)
    assert before[0] == expected["original_sha256"]
    with pymupdf.open(paths[0]) as pdf:
        assert len(pdf) == expected["physical_page_count"] == 2
    assert adjacent == {"range": [[1, 2]]}
    document = v2["documents"][0]
    assert_business(document, expected, projected=False)
    facts = DetailedFacts.model_validate(
        {k: v for k, v in document.items() if k in DetailedFacts.model_fields}
    )
    legacy = v1["files"][0]["documents"][0]
    for key, value in facts.legacy().model_dump(mode="json").items():
        assert legacy[key] == value, f"JSON1/JSON2: {key}"
    assert fingerprint(paths) == before


async def test_private_invoice_controlled_headless_full_flow_and_restore(client, container):
    """Настоящие API/DB/storage/PDF; OCR и два ответа VLM — явные controlled doubles."""
    paths, (_, v2, expected, _) = reference()
    before = fingerprint(paths)
    vision = ReferenceVision(v2["documents"][0])
    container.vision = container.analysis.vision = vision
    container.analysis.pairwise.vision = vision
    # ProcessDocument использует bound callback Analyze; подмена endpoint не требуется.
    identifier = await run_packet(client, container, paths[0])
    detailed = await check_exports(client, identifier, expected, paths[0])
    assert [c["schema"] for c in vision.calls] == [
        "PairAnswer",
        "DocumentClassification",
        "DetailedExtraction",
    ]
    calls = len(vision.calls)
    restored = create_app(container)
    restored.state.container = container
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=restored), base_url="http://testserver"
    ) as reopened:
        again = (await reopened.get(f"/api/v1/jobs/{identifier}/extractions", headers=KEY)).json()
        assert again["documents"][0]["requisites"] == detailed["documents"][0]["requisites"]
        await check_exports(reopened, identifier, expected, paths[0])
    assert len(vision.calls) == calls
    assert fingerprint(paths) == before


@pytest.mark.live
async def test_private_invoice_real_docling_vlm_and_download_accuracy(client, container):
    """Opt-in: настоящие OCR/vision, ручное сохранение группы и точная проверка бизнес-полей."""
    paths, (_, _, expected, _) = reference(required=True)
    before = fingerprint(paths)
    settings = Settings().model_copy(update={"data_dir": container.settings.data_dir})
    assert settings.docling_api_key.get_secret_value(), "NOT_RUN: нужен локальный DOCLING_API_KEY"
    async with httpx.AsyncClient(trust_env=False) as external:
        vision = SharedVision(settings, external)
        container.analysis = Analyze(
            container.database, container.storage, vision, DoclingOCR(settings, external), settings
        )
        identifier = await run_packet(client, container, paths[0])
        await check_exports(client, identifier, expected, paths[0])
    assert fingerprint(paths) == before
