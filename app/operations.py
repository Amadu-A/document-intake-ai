# app/operations.py
"""Эксплуатационные init/preflight/smoke без вывода пользовательских документов."""

import argparse
import asyncio
import hashlib
import json
import os
import secrets
import stat
import sys
import tempfile
import time
import traceback
from collections.abc import Iterator
from contextlib import contextmanager
from io import StringIO
from pathlib import Path

import httpx
import pymupdf
from dotenv.parser import parse_stream

from app.core.container import Container
from app.core.settings import ConfigurationError, Settings


class OperationError(RuntimeError):
    """Содержит только подготовленную безопасную диагностику эксплуатационной ошибки."""


def _verify_owned_file(info: os.stat_result) -> None:
    """Запрещает чтение или изменение ссылок, устройств и файлов другого владельца."""
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise OperationError("Bootstrap принимает только обычные файлы без symlink/hardlink")
    if os.name != "nt" and info.st_uid != os.geteuid():
        raise OperationError("Bootstrap не читает и не изменяет файлы другого владельца")


def _file_revision(info: os.stat_result) -> tuple[int, ...]:
    """Фиксирует inode и метаданные для обнаружения параллельной замены или записи."""
    # Windows fstat/lstat расходятся по ctime после hardlink; отдельно проверяем mtime и bytes.
    changed = info.st_ctime_ns if os.name != "nt" else 0
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, changed


def _owned_descriptor(path: Path, *, create: bool = False, append: bool = False) -> int:
    """Открывает только собственный обычный файл, не следуя symlink и не блокируясь на FIFO."""
    try:
        previous = path.lstat()
    except FileNotFoundError:
        previous = None
    if previous is not None:
        _verify_owned_file(previous)
    flags = os.O_RDWR | os.O_CREAT if create else os.O_RDONLY
    if append:
        flags = os.O_RDWR | os.O_APPEND
    flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        opened = os.fstat(descriptor)
        _verify_owned_file(opened)
        if previous is not None and (previous.st_dev, previous.st_ino) != (
            opened.st_dev,
            opened.st_ino,
        ):
            raise OperationError("Файл bootstrap изменён параллельно; повторите запуск")
        if os.name != "nt" and stat.S_IMODE(opened.st_mode) != 0o600:
            os.fchmod(descriptor, 0o600)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


@contextmanager
def _env_lock(directory: Path) -> Iterator[None]:
    """Сериализует bootstrap системной блокировкой; lock-файл остаётся для стабильного inode."""
    path = directory / ".env.init-lock"
    descriptor = _owned_descriptor(path, create=True)
    try:
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise OperationError(
                "Уже выполняется init-env; дождитесь завершения и повторите"
            ) from exc
        opened, current = os.fstat(descriptor), path.lstat()
        if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
            raise OperationError("Файл блокировки заменён параллельно; повторите запуск")
        yield
    finally:
        os.close(descriptor)


def _env_snapshot(target: Path) -> tuple[bytes, tuple[int, ...] | None]:
    """Читает ограниченный снимок только собственного .env и обнаруживает запись при чтении."""
    try:
        descriptor = _owned_descriptor(target)
    except FileNotFoundError:
        return b"", None
    with os.fdopen(descriptor, "rb") as stream:
        before = _file_revision(os.fstat(stream.fileno()))
        content = stream.read(1024 * 1024 + 1)
        after = _file_revision(os.fstat(stream.fileno()))
    if len(content) > 1024 * 1024:
        raise OperationError("Размер .env превышает безопасный лимит bootstrap 1 МиБ")
    if before != after or after != _file_revision(target.lstat()):
        raise OperationError(".env изменён параллельно; повторите запуск без редактирования файла")
    return content, after


def _missing_secrets(content: bytes) -> dict[str, str]:
    """Валидирует заданные секреты и генерирует только отсутствующие или пустые значения."""
    names = ("DATABASE_PASSWORD", "RABBITMQ_PASSWORD", "SESSION_SECRET", "DOWNLOAD_SECRET")
    values = {}
    try:
        bindings = parse_stream(StringIO(content.decode("utf-8")))
        for binding in bindings:
            if binding.error:
                raise OperationError("Некорректный синтаксис .env; проверьте формат dotenv")
            key = binding.key
            if key is None or key.upper() not in (*names, "API_KEYS"):
                continue
            if key != key.upper() or values.get(key):
                raise OperationError(
                    "Имена собственных секретов должны быть уникальными и UPPER_CASE"
                )
            values[key] = binding.value or ""
    except UnicodeError as exc:
        raise OperationError("Файл .env должен использовать UTF-8") from exc
    for name, value in values.items():
        if not value:
            continue
        if name == "API_KEYS":
            try:
                keys = json.loads(value)
            except ValueError as exc:
                raise OperationError(
                    "API_KEYS: требуется JSON с ключами onec, telegram, evaluation"
                ) from exc
            if (
                not isinstance(keys, dict)
                or not {"onec", "telegram", "evaluation"}.issubset(keys)
                or any(
                    not isinstance(key, str)
                    or len(key) < 32
                    or any(character in key for character in ("\r", "\n", "\0"))
                    for key in keys.values()
                )
                or len(set(keys.values())) != len(keys)
            ):
                raise OperationError(
                    "API_KEYS: нужны разные секреты длиной от 32 символов для интеграций"
                )
        elif len(value) < 32 or any(character in value for character in ("\r", "\n", "\0")):
            raise OperationError(
                f"{name}: заданный секрет невалиден; автоматическая замена запрещена"
            )
    missing = {name: secrets.token_urlsafe(48) for name in names if not values.get(name)}
    if not values.get("API_KEYS"):
        missing["API_KEYS"] = json.dumps(
            {name: secrets.token_urlsafe(48) for name in ("onec", "telegram", "evaluation")}
        )
    return missing


def _append_env(target: Path, content: bytes, revision: tuple[int, ...], additions: bytes) -> None:
    """Добавляет secrets одним O_APPEND write, сохраняя пользовательские байты даже при late update.

    Не выполняет replace или truncate существующего файла. Внешний редактор,
    игнорирующий lock, выявляется повторным чтением; при гонке обе записи остаются
    владельцу для проверки, bootstrap завершается ошибкой и не настраивает сервисы.
    """
    descriptor = _owned_descriptor(target, append=True)
    with os.fdopen(descriptor, "r+b") as stream:
        if (
            _file_revision(os.fstat(stream.fileno())) != revision
            or stream.read(1024 * 1024 + 1) != content
            or _file_revision(target.lstat()) != revision
        ):
            raise OperationError(".env изменён параллельно; изменения пользователя сохранены")
        written = os.write(stream.fileno(), additions)
        os.fsync(stream.fileno())
        stream.seek(0)
        after = stream.read(1024 * 1024 + 1)
        if (
            written != len(additions)
            or after != content + additions
            or _file_revision(os.fstat(stream.fileno())) != _file_revision(target.lstat())
        ):
            raise OperationError(
                ".env изменён параллельно; байты сохранены без rollback. "
                "Проверьте файл и дубликаты секретов перед повторным запуском"
            )


def initialize_env(directory: Path) -> bool:
    """Создаёт .env атомарно, дополняет свой файл без replace и сохраняет заданные секреты."""
    target = directory / ".env"
    with _env_lock(directory):
        content, revision = _env_snapshot(target)
        missing = _missing_secrets(content)
        if not missing:
            return False
        original = content or "# .env\n# Собственные секреты; не выводить в логи.\n".encode()
        if not original.endswith(b"\n"):
            original += b"\n"
        additions = "".join(f"{name}='{value}'\n" for name, value in missing.items())
        completed = original + additions.encode("utf-8")
        if revision is not None:
            _append_env(target, content, revision, completed[len(content) :])
            return True
        temporary = directory / (".env.init-" + secrets.token_hex(12))
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                if os.name != "nt":
                    os.fchmod(stream.fileno(), 0o600)
                stream.write(completed)
                stream.flush()
                os.fsync(stream.fileno())
            if _env_snapshot(target) != (content, revision):
                raise OperationError(".env изменён параллельно; изменения пользователя сохранены")
            try:
                os.link(temporary, target)
            except FileExistsError as exc:
                raise OperationError(".env создан параллельно; повторите запуск") from exc
            if os.name != "nt":
                directory_descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory_descriptor)
                finally:
                    os.close(directory_descriptor)
            return True
        finally:
            temporary.unlink(missing_ok=True)


async def smoke(settings: Settings) -> None:
    """Проверяет внешний HTTP, настоящий pipeline и скачивание PDF без cookie."""
    key = settings.api_keys.get("evaluation")
    if not key:
        raise OperationError("Для smoke требуется отдельный API_KEYS.evaluation")
    base = settings.app_public_base_url.rstrip("/")
    document = pymupdf.open()
    page = document.new_page()
    page.insert_text(
        (50, 70),
        "INFORMATION LETTER\nDocument number SYNTHETIC-7429\n"
        "To: Test recipient\nPlease confirm delivery.",
    )
    payload = document.tobytes()
    document.close()
    async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
        for path in ("/", "/docs", "/health/live", "/health/ready"):
            response = await client.get(
                base + path,
                timeout=(
                    settings.docling_timeout_seconds + settings.vlm_timeout_seconds + 15
                    if path == "/health/ready"
                    else 30
                ),
            )
            if response.status_code != 200:
                raise OperationError(f"HTTP smoke: {path} вернул {response.status_code}")
        response = await client.post(
            base + "/api/v1/jobs",
            headers={"X-API-Key": key.get_secret_value()},
            files={"files": ("synthetic.pdf", payload, "application/pdf")},
        )
        if response.status_code != 202:
            raise OperationError("HTTP smoke: submit не вернул 202")
        job_id = response.json()["job_id"]
        status = {}
        deadline = time.monotonic() + settings.job_timeout_seconds
        while time.monotonic() < deadline:
            response = await client.get(
                base + f"/api/v1/jobs/{job_id}", headers={"X-API-Key": key.get_secret_value()}
            )
            response.raise_for_status()
            status = response.json()
            if status["status"] in {"failed", "cancelled"}:
                raise OperationError(
                    "HTTP smoke: " + (status.get("error") or {}).get("code", "failed")
                )
            if status["status"] in {"completed", "completed_with_warnings"}:
                break
            await asyncio.sleep(2)
        else:
            raise OperationError("HTTP smoke: timeout задания")
        result = await client.get(
            base + f"/api/v1/jobs/{job_id}/result", headers={"X-API-Key": key.get_secret_value()}
        )
        result.raise_for_status()
        data = result.json()
        if data["schema_version"] != "1.0" or not data["files"][0]["documents"]:
            raise OperationError("HTTP smoke: неверный экспорт")
        # Отдельный клиент без cookie/API key имитирует скачивание 1С по capability URL.
        async with httpx.AsyncClient(timeout=30, trust_env=False) as downloader:
            for artifact in status["artifacts"] + status["sources"]:
                download = await downloader.get(artifact["file_url"])
                download.raise_for_status()
                if (
                    hashlib.sha256(download.content).hexdigest() != artifact["sha256"]
                    or len(download.content) != artifact["byte_size"]
                ):
                    raise OperationError("HTTP smoke: checksum/byte_size не совпали")
    print("HTTP UI/API, реальный pipeline и скачивание по внешним URLs: PASS")


async def operate(command: str, settings: Settings) -> None:
    """Выполняет реальные dependency checks, не заменяя их проверкой конфигурационного реестра."""
    container = Container(settings)
    try:
        await container.rabbit.connect()
        if command == "broker-check":
            await container.rabbit.require_consumer()
            print("AMQP и consumer собственной очереди: PASS")
        elif command == "contracts":
            checks = await container.health._contracts()
            print(json.dumps(checks, ensure_ascii=False))
            if not all(value == "PASS" for value in checks.values()):
                raise OperationError("Контракт Docling OCR или VLM vision/JSON не подтверждён")
        elif command == "ready":
            result = await container.health.ready(force=True)
            print(json.dumps(result, ensure_ascii=False))
            if result["status"] != "ready":
                raise OperationError("Зависимости не готовы")
    finally:
        await container.close()


def main() -> int:
    """Печатает только безопасные итоги; broker-config — закрытый stdin-протокол scripts/up.sh."""
    parser = argparse.ArgumentParser(description="Проверки Document Intake AI")
    parser.add_argument(
        "command",
        choices=[
            "init-env",
            "config",
            "broker-config",
            "broker-check",
            "contracts",
            "ready",
            "smoke",
            "api-health",
            "worker-health",
            "public-url",
            "port",
        ],
    )
    arguments = parser.parse_args()
    try:
        if arguments.command == "init-env":
            initialize_env(Path.cwd())
            print("Собственный .env проверен; недостающие секреты созданы, заданные сохранены")
            return 0
        settings = Settings()
        if arguments.command == "api-health":
            with httpx.Client(trust_env=False, timeout=3) as client:
                return (
                    0
                    if client.get(f"http://127.0.0.1:{settings.app_port}/health/live").status_code
                    == 200
                    else 1
                )
        if arguments.command == "worker-health":
            return (
                0
                if time.time()
                - (Path(tempfile.gettempdir()) / "intake-worker-heartbeat").stat().st_mtime
                < 25
                else 1
            )
        settings.require_secrets()
        if arguments.command == "config":
            if not settings.docling_api_key.get_secret_value():
                raise OperationError(
                    "Укажите разрешённый DOCLING_API_KEY в своём .env; чужие .env не читаются"
                )
            print("Settings: PASS")
        elif arguments.command == "broker-config":
            # Вызывается только внутри command substitution без set -x; не направлять в терминал.
            values = [
                settings.rabbitmq_container,
                settings.rabbitmq_user,
                settings.rabbitmq_vhost,
                settings.rabbitmq_password.get_secret_value(),
                str(settings.rabbitmq_timeout_seconds),
            ]
            if any("\n" in value or "\r" in value for value in values):
                raise OperationError("RabbitMQ значения не должны содержать перевод строки")
            print("\n".join(values))
        elif arguments.command == "public-url":
            print(settings.app_public_base_url.rstrip("/"))
        elif arguments.command == "port":
            print(settings.app_port)
        elif arguments.command == "smoke":
            asyncio.run(smoke(settings))
        else:
            asyncio.run(operate(arguments.command, settings))
        return 0
    except Exception as exc:
        # Чужие RuntimeError/ValidationError могут содержать секреты: разрешён только свой тип.
        safe = (
            str(exc)
            if isinstance(exc, (ConfigurationError, OperationError))
            else type(exc).__name__
        )
        print("Проверка не пройдена: " + safe, file=sys.stderr)
        if not isinstance(exc, (ConfigurationError, OperationError)):
            print(
                json.dumps(
                    {
                        "event": "operation_failed",
                        "type": type(exc).__name__,
                        "frames": [
                            f"{Path(frame.filename).name}:{frame.lineno}:{frame.name}"
                            for frame in traceback.extract_tb(exc.__traceback__)
                        ],
                    },
                    ensure_ascii=False,
                ),
                file=sys.stderr,
            )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
