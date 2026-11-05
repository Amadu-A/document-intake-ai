# tests/test_configuration.py
"""Settings precedence, atomic sparse secrets и безопасное неизменение RabbitMQ credentials."""

import os
import stat
from pathlib import Path

import pytest
import yaml

from app.core.settings import Settings
from app.operations import initialize_env


def test_settings_catalog_private_environment_precedence(tmp_path, monkeypatch):
    """Process env перекрывает sparse .env, тот перекрывает общий каталог .env.example."""
    catalog, private = tmp_path / ".env.example", tmp_path / ".env"
    catalog.write_text("APP_PORT=9001\nLOG_LEVEL=INFO\n", "utf-8")
    private.write_text("APP_PORT=9002\nDATABASE_PASSWORD='pa$$w0rd#with spaces'\n", "utf-8")
    settings = Settings(_env_file=(catalog, private))
    assert settings.app_port == 9002 and settings.log_level == "INFO"
    assert settings.database_password.get_secret_value() == "pa$$w0rd#with spaces"
    monkeypatch.setenv("APP_PORT", "9003")
    assert Settings(_env_file=(catalog, private)).app_port == 9003


def test_vlm_thinking_defaults_and_layered_overrides(tmp_path, monkeypatch):
    """Приложение запрашивает прямой JSON; thinking включается явным частным override."""
    monkeypatch.delenv("VLM_ENABLE_THINKING", raising=False)
    assert Settings(_env_file=None).vlm_enable_thinking is False
    catalog, private = tmp_path / ".env.example", tmp_path / ".env"
    catalog.write_text("VLM_ENABLE_THINKING=false\n", "utf-8")
    private.write_text("VLM_ENABLE_THINKING=true\n", "utf-8")
    assert Settings(_env_file=(catalog, private)).vlm_enable_thinking is True
    monkeypatch.setenv("VLM_ENABLE_THINKING", "false")
    assert Settings(_env_file=(catalog, private)).vlm_enable_thinking is False


def test_docling_ocr_settings_defaults_and_overrides(tmp_path, monkeypatch):
    """Проверенные runtime presets/языки настраиваются через тот же layered Pydantic каталог."""
    monkeypatch.delenv("DOCLING_OCR_PRESET", raising=False)
    monkeypatch.delenv("DOCLING_OCR_LANGUAGES", raising=False)
    defaults = Settings(_env_file=None)
    assert defaults.docling_ocr_preset == "tesseract"
    assert defaults.docling_ocr_languages == ("iso:ru", "iso:en")
    catalog, private = tmp_path / ".env.example", tmp_path / ".env"
    catalog.write_text(
        'DOCLING_OCR_PRESET=tesseract\nDOCLING_OCR_LANGUAGES=["iso:ru","iso:en"]\n', "utf-8"
    )
    private.write_text('DOCLING_OCR_PRESET=rapidocr\nDOCLING_OCR_LANGUAGES=["en"]\n', "utf-8")
    settings = Settings(_env_file=(catalog, private))
    assert settings.docling_ocr_preset == "rapidocr"
    assert settings.docling_ocr_languages == ("en",)
    monkeypatch.setenv("DOCLING_OCR_PRESET", "auto")
    monkeypatch.setenv("DOCLING_OCR_LANGUAGES", '["iso:en"]')
    settings = Settings(_env_file=(catalog, private))
    assert settings.docling_ocr_preset == "auto"
    assert settings.docling_ocr_languages == ("iso:en",)


@pytest.mark.parametrize(
    "changes",
    [
        {"docling_ocr_preset": ""},
        {"docling_ocr_preset": "preset with spaces"},
        {"docling_ocr_languages": ()},
        {"docling_ocr_languages": ("ru with spaces",)},
        {"docling_ocr_languages": ("en",) * 9},
    ],
)
def test_docling_ocr_settings_reject_invalid_configuration(changes):
    """Пустой или неограниченный OCR-конфиг отклоняется до HTTP-запросов."""
    with pytest.raises(ValueError):
        Settings(_env_file=None, **changes)


def test_bootstrap_is_atomic_sparse_and_never_overwrites(tmp_path):
    """Повторный bootstrap не меняет ни байта настоящего .env и создаёт разные ключи интеграций."""
    assert initialize_env(tmp_path)
    original = (tmp_path / ".env").read_bytes()
    assert not initialize_env(tmp_path)
    assert (tmp_path / ".env").read_bytes() == original
    settings = Settings(_env_file=tmp_path / ".env")
    settings.require_secrets()
    assert set(settings.api_keys) == {"onec", "telegram", "evaluation"}
    assert len({key.get_secret_value() for key in settings.api_keys.values()}) == 3
    assert b"APP_PORT" not in original and b"DATABASE_HOST" not in original
    if os.name != "nt":
        assert stat.S_IMODE((tmp_path / ".env").stat().st_mode) == 0o600


def test_catalog_compose_settings_have_same_supported_variables():
    """Compose передаёт все настройки с приоритетом environment > .env > .env.example."""
    catalog = {
        line.split("=", 1)[0]
        for line in Path(".env.example").read_text("utf-8").splitlines()
        if line and not line.startswith("#")
    }
    assert catalog == {name.upper() for name in Settings.model_fields}
    compose = yaml.safe_load(Path("compose.yaml").read_text("utf-8"))
    environment = compose["services"]["api"]["environment"]
    assert set(environment) == catalog
    assert all(value == "${" + name + "}" for name, value in environment.items())
    assert set(compose["services"]) == {"api", "worker", "postgres", "migrate"}
    assert "ports" not in compose["services"]["postgres"]
    assert compose["services"]["postgres"]["networks"] == ["private"]


def test_up_has_no_credential_replacement_or_shared_lifecycle():
    """Операционный скрипт не содержит разрушительных команд из исторического PDRD."""
    script = Path("scripts/up.sh").read_text("utf-8")
    for forbidden in (
        "change_password",
        "delete_user",
        "delete_vhost",
        "down -v",
        "system prune",
        "--remove-orphans",
        "source .env",
    ):
        assert forbidden not in script
    assert "authenticate_user" in script and "list_user_permissions" in script
    assert "--env-file .env.example --env-file .env" in script
    assert "set +x" in script and "secret_file" in script and "cat >" in script


@pytest.mark.parametrize("value", ["guest", "admin", "root"])
def test_settings_reject_shared_admin_accounts(value):
    """Общий broker administrator нельзя случайно использовать как runtime user приложения."""
    with pytest.raises(ValueError):
        Settings(_env_file=None, rabbitmq_user=value)
