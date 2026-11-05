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
