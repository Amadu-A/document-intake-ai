# tests/test_bootstrap.py
"""Регрессии sparse .env, блокировок и безопасной диагностики локального bootstrap."""

import json
import os
import re
import stat
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest
import yaml

from app import operations
from app.core.settings import Settings
from app.operations import OperationError, initialize_env


def test_docling_only_env_is_completed_without_changing_existing_text(tmp_path):
    """Ключ Docling, комментарии, спецсимволы и CRLF сохраняются при первом запуске."""
    target = tmp_path / ".env"
    original = b"# Private\r\nDOCLING_API_KEY='synthetic-$# key'\r\n"
    target.write_bytes(original)
    assert initialize_env(tmp_path)
    completed = target.read_bytes()
    assert completed.startswith(original)
    settings = Settings(_env_file=target)
    settings.require_secrets()
    assert settings.docling_api_key.get_secret_value() == "synthetic-$# key"
    assert set(settings.api_keys) == {"onec", "telegram", "evaluation"}
    assert not initialize_env(tmp_path)
    assert target.read_bytes() == completed
    assert [path.name for path in tmp_path.glob(".env.init-*")] == [".env.init-lock"]


@pytest.mark.parametrize("original", [b"", b"DATABASE_PASSWORD=\nAPI_KEYS=''", b"# Comment"])
def test_empty_values_and_missing_final_newline_are_supported(tmp_path, original):
    """Пустые placeholders заполняются один раз без изменения существующего текста."""
    target = tmp_path / ".env"
    target.write_bytes(original)
    assert initialize_env(tmp_path)
    assert target.read_bytes().startswith(original)
    Settings(_env_file=target).require_secrets()
    completed = target.read_bytes()
    assert not initialize_env(tmp_path)
    assert target.read_bytes() == completed


def test_partial_env_keeps_real_password_and_api_keys(tmp_path):
    """Готовые credentials остаются побайтово прежними; создаются только отсутствующие."""
    target = tmp_path / ".env"
    password = "a$ #" * 12
    keys = {"onec": "c" * 48, "telegram": "d" * 48, "evaluation": "e" * 48}
    original = f"DATABASE_PASSWORD='{password}'\nAPI_KEYS='{json.dumps(keys)}'\n".encode()
    target.write_bytes(original)
    assert initialize_env(tmp_path)
    assert target.read_bytes().startswith(original)
    settings = Settings(_env_file=target)
    settings.require_secrets()
    assert settings.database_password.get_secret_value() == password
    assert {name: key.get_secret_value() for name, key in settings.api_keys.items()} == keys
    assert target.read_bytes().count(b"DATABASE_PASSWORD=") == 1
    assert target.read_bytes().count(b"API_KEYS=") == 1


@pytest.mark.parametrize(
    "original",
    [
        "DATABASE_PASSWORD=short\n",
        "SESSION_SECRET='" + "s" * 40 + "\nsecret'\n",
        "API_KEYS=not-json\n",
        "API_KEYS={}\n",
        "API_KEYS='" + json.dumps({"onec": "x" * 40}) + "'\n",
        "API_KEYS='"
        + json.dumps({name: "x" * 40 for name in ("onec", "telegram", "evaluation")})
        + "'\n",
        "DATABASE_PASSWORD='" + "a" * 40 + "'\nDATABASE_PASSWORD='" + "b" * 40 + "'\n",
        "database_password='" + "a" * 40 + "'\n",
        "DOCLING_API_KEY='unterminated\n",
    ],
)
def test_invalid_nonempty_values_are_not_replaced(tmp_path, original):
    """Ошибочный реальный секрет и неоднозначный dotenv не заменяются случайным значением."""
    target = tmp_path / ".env"
    target.write_text(original, encoding="utf-8")
    before = target.read_bytes()
    with pytest.raises(OperationError):
        initialize_env(tmp_path)
    assert target.read_bytes() == before


def test_own_permissions_are_restricted_without_changing_complete_file(tmp_path):
    """На POSIX автоматическое 0600 устраняет ошибку nano; содержимое не меняется."""
    initialize_env(tmp_path)
    target = tmp_path / ".env"
    original = target.read_bytes()
    if os.name != "nt":
        target.chmod(0o644)
    assert not initialize_env(tmp_path)
    assert target.read_bytes() == original
    if os.name != "nt":
        assert stat.S_IMODE(target.stat().st_mode) == 0o600


@pytest.mark.parametrize("name", [".env", ".env.init-lock"])
def test_directory_is_rejected_as_env_or_lock(tmp_path, name):
    """Специальный путь не открывается как секретный файл или bootstrap lock."""
    (tmp_path / name).mkdir()
    with pytest.raises(OperationError, match="обычные файлы"):
        initialize_env(tmp_path)


@pytest.mark.parametrize("link_type", ["symlink", "hardlink"])
@pytest.mark.parametrize("name", [".env", ".env.init-lock"])
def test_links_are_rejected_without_touching_referenced_file(tmp_path, link_type, name):
    """Bootstrap не меняет чужой секрет или его mode через ссылку внутри проекта."""
    external = tmp_path / "external"
    external.write_bytes(b"DOCLING_API_KEY='synthetic-external'\n")
    before = external.stat()
    try:
        if link_type == "symlink":
            (tmp_path / name).symlink_to(external)
        else:
            os.link(external, tmp_path / name)
    except OSError:
        pytest.skip("Файловая система не разрешает создание тестовой ссылки")
    with pytest.raises(OperationError, match="обычные файлы"):
        initialize_env(tmp_path)
    assert external.read_bytes() == b"DOCLING_API_KEY='synthetic-external'\n"
    assert stat.S_IMODE(external.stat().st_mode) == stat.S_IMODE(before.st_mode)


def test_foreign_ownership_is_rejected_before_opening_or_chmod(tmp_path, monkeypatch):
    """POSIX-проверка владельца выполняется до чтения credentials или изменения mode."""
    target = tmp_path / ".env"
    target.write_bytes(b"synthetic-private-data")
    original = target.stat()
    simulated = {**vars(os), "name": "posix", "geteuid": lambda: original.st_uid + 1}
    monkeypatch.setattr(operations, "os", SimpleNamespace(**simulated))
    with pytest.raises(OperationError, match="другого владельца"):
        operations._owned_descriptor(target)
    assert target.read_bytes() == b"synthetic-private-data"
    assert stat.S_IMODE(target.stat().st_mode) == stat.S_IMODE(original.st_mode)


def test_concurrent_initialization_does_not_generate_competing_secrets(tmp_path, monkeypatch):
    """Второй bootstrap отказывает до генерации; после первого секреты остаются стабильными."""
    started, proceed = Event(), Event()
    original = operations.secrets.token_urlsafe

    def paused_generator(length):
        """Останавливает первый bootstrap после захвата системной блокировки."""
        started.set()
        assert proceed.wait(timeout=5)
        return original(length)

    monkeypatch.setattr(operations.secrets, "token_urlsafe", paused_generator)
    with ThreadPoolExecutor(max_workers=1) as executor:
        first = executor.submit(initialize_env, tmp_path)
        try:
            assert started.wait(timeout=5)
            with pytest.raises(OperationError, match="Уже выполняется"):
                initialize_env(tmp_path)
        finally:
            proceed.set()
        assert first.result(timeout=5)
    completed = (tmp_path / ".env").read_bytes()
    assert not initialize_env(tmp_path)
    assert (tmp_path / ".env").read_bytes() == completed


@pytest.mark.parametrize("exists", [False, True])
def test_external_update_is_not_overwritten(tmp_path, monkeypatch, exists):
    """Изменение редактором перед публикацией сохраняется, bootstrap завершается ошибкой."""
    target = tmp_path / ".env"
    if exists:
        target.write_bytes(b"# Original\n")
    updated = b"DOCLING_API_KEY='synthetic-concurrent-change'\n"
    original = operations.secrets.token_urlsafe

    def concurrent_update(length):
        """Имитирует несогласованный внешний редактор во время генерации secrets."""
        target.write_bytes(updated)
        return original(length)

    monkeypatch.setattr(operations.secrets, "token_urlsafe", concurrent_update)
    with pytest.raises(OperationError, match="изменён параллельно"):
        initialize_env(tmp_path)
    assert target.read_bytes() == updated
    assert [path.name for path in tmp_path.glob(".env.init-*")] == [".env.init-lock"]


def test_failed_append_preserves_existing_file_without_temporary_secrets(tmp_path, monkeypatch):
    """Отказ до write не меняет исходный .env и не оставляет временных файлов с secrets."""
    target = tmp_path / ".env"
    original = b"DOCLING_API_KEY='synthetic-initial'\n"
    target.write_bytes(original)

    def failed_append(*_arguments):
        """Имитирует отказ filesystem до добавления secrets в существующий .env."""
        raise OSError("synthetic-sensitive-error")

    monkeypatch.setattr(operations.os, "write", failed_append)
    with pytest.raises(OSError):
        initialize_env(tmp_path)
    assert target.read_bytes() == original
    assert [path.name for path in tmp_path.glob(".env.init-*")] == [".env.init-lock"]


def test_last_moment_editor_write_is_preserved_and_stops_bootstrap(tmp_path, monkeypatch):
    """Запись редактора после snapshot сохраняется и требует проверки владельцем."""
    target = tmp_path / ".env"
    original = b"DOCLING_API_KEY='synthetic-initial'\n"
    late = b"DATABASE_PASSWORD='" + b"x" * 48 + b"'\n"
    target.write_bytes(original)
    original_write = operations.os.write

    def late_editor_update(descriptor, content):
        """Имитирует редактор, не соблюдающий bootstrap lock, непосредственно перед write."""
        with target.open("ab") as editor:
            editor.write(late)
        return original_write(descriptor, content)

    monkeypatch.setattr(operations.os, "write", late_editor_update)
    with pytest.raises(OperationError, match="байты сохранены"):
        initialize_env(tmp_path)
    assert target.read_bytes().startswith(original + late)
    monkeypatch.setattr(operations.os, "write", original_write)
    with pytest.raises(OperationError, match="уникальными"):
        initialize_env(tmp_path)
    assert target.read_bytes().startswith(original + late)


def test_env_created_after_last_snapshot_is_not_overwritten(tmp_path, monkeypatch):
    """Создание отказывает при гонке непосредственно перед link без потери чужой записи."""
    target = tmp_path / ".env"
    updated = b"DOCLING_API_KEY='synthetic-last-moment-change'\n"

    def concurrent_create(*_arguments):
        """Создаёт .env в последнем окне гонки перед атомарной публикацией."""
        target.write_bytes(updated)
        raise FileExistsError

    monkeypatch.setattr(operations.os, "link", concurrent_create)
    with pytest.raises(OperationError, match="создан параллельно"):
        initialize_env(tmp_path)
    assert target.read_bytes() == updated
    assert [path.name for path in tmp_path.glob(".env.init-*")] == [".env.init-lock"]


@pytest.mark.parametrize("content", [b"\xff", b"#" * (1024 * 1024 + 1)])
def test_unsupported_env_encoding_or_size_does_not_change_content(tmp_path, content):
    """Большой или не UTF-8 файл завершается безопасной диагностикой без перезаписи."""
    target = tmp_path / ".env"
    target.write_bytes(content)
    with pytest.raises(OperationError):
        initialize_env(tmp_path)
    assert target.read_bytes() == content


@pytest.mark.parametrize("kind", [TypeError, RuntimeError])
def test_operations_unexpected_error_reports_frames_without_secrets(
    tmp_path, monkeypatch, capsys, kind
):
    """Диагностика показывает место сбоя, исключая текст ошибки, source line и locals."""
    marker = "synthetic-private-secret-7429"

    def unexpected_failure(_directory):
        """Передаёт приватную строку в сообщение неожиданного стороннего исключения."""
        raise kind(marker)

    monkeypatch.setattr(operations, "initialize_env", unexpected_failure)
    monkeypatch.setattr(operations.sys, "argv", ["operations", "init-env"])
    assert operations.main() == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert marker not in output.err
    diagnostic = json.loads(output.err.splitlines()[1])
    assert diagnostic["type"] == kind.__name__
    assert any(
        re.fullmatch(r"test_bootstrap.py:\d+:unexpected_failure", frame)
        for frame in diagnostic["frames"]
    )
    assert set(diagnostic) == {"event", "type", "frames"}


def test_container_has_one_init_for_compose_and_standalone_runs():
    """Compose наследует image Tini вместо запуска второго init перед ним."""
    compose = yaml.safe_load(Path("compose.yaml").read_text("utf-8"))
    for name in ("api", "worker", "migrate"):
        assert not compose["services"][name].get("init")
        assert "entrypoint" not in compose["services"][name]
    entrypoint = next(
        line.removeprefix("ENTRYPOINT ")
        for line in Path("Dockerfile").read_text("utf-8").splitlines()
        if line.startswith("ENTRYPOINT ")
    )
    assert json.loads(entrypoint) == ["/usr/bin/tini", "-g", "--"]
