# tests/test_extraction_configuration.py
"""Pydantic/Compose лимиты извлечения и ёмкость существующего общего HTTP клиента."""

import httpx
import pytest

from app.core.container import Container
from app.core.settings import Settings


def test_extraction_limits_defaults_and_settings_precedence(tmp_path, monkeypatch):
    """Новые лимиты имеют тот же порядок каталог → private override → environment."""
    for key in ("DOCUMENT_EXTRACTION_CONCURRENCY", "VLM_REQUEST_CONCURRENCY", "WORKER_CONCURRENCY"):
        monkeypatch.delenv(key, raising=False)
    defaults = Settings(_env_file=None)
    assert (
        defaults.document_extraction_concurrency,
        defaults.vlm_request_concurrency,
        defaults.worker_concurrency,
        defaults.pair_concurrency,
        defaults.docling_window_concurrency,
    ) == (3, 4, 3, 4, 2)
    catalog, private = tmp_path / ".env.example", tmp_path / ".env"
    catalog.write_text("DOCUMENT_EXTRACTION_CONCURRENCY=3\nVLM_REQUEST_CONCURRENCY=4\n", "utf-8")
    private.write_text("DOCUMENT_EXTRACTION_CONCURRENCY=1\n", "utf-8")
    assert Settings(_env_file=(catalog, private)).document_extraction_concurrency == 1
    monkeypatch.setenv("DOCUMENT_EXTRACTION_CONCURRENCY", "2")
    monkeypatch.setenv("VLM_REQUEST_CONCURRENCY", "5")
    configured = Settings(_env_file=(catalog, private))
    assert (
        configured.document_extraction_concurrency == 2 and configured.vlm_request_concurrency == 5
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"document_extraction_concurrency": 0},
        {"document_extraction_concurrency": 9},
        {"vlm_request_concurrency": 0},
        {"vlm_request_concurrency": 17},
    ],
)
def test_invalid_limits_are_rejected_before_processing(changes):
    """Нулевой/неограниченный пул не может попасть в worker."""
    with pytest.raises(ValueError):
        Settings(_env_file=None, **changes)


async def test_http_pool_fits_maximum_configured_vlm_plus_ocr(settings, monkeypatch):
    """Проверяет реальные параметры создаваемого AsyncClient, без соединений с shared сервисами."""
    settings.vlm_request_concurrency = 16
    supplied = []
    original = httpx.AsyncClient

    def client(**kwargs):
        """Наблюдает публичный httpx.Limits, не подменяя HTTP реализацию."""
        supplied.append(kwargs["limits"])
        return original(**kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)
    root = Container(settings)
    try:
        assert supplied[0].max_connections >= 16 + settings.docling_window_concurrency
    finally:
        await root.close()
