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
from pathlib import Path

import httpx
import pymupdf

from app.core.container import Container
from app.core.settings import ConfigurationError, Settings


def initialize_env(directory: Path) -> bool:
    """Атомарно создаёт sparse .env 0600, никогда не заменяет существующий файл/секрет."""
    target = directory / ".env"
    if target.is_symlink():
        raise RuntimeError(".env должен быть обычным приватным файлом, а не symlink")
    if target.exists():
        if os.name != "nt" and stat.S_IMODE(target.stat().st_mode) & 0o077:
            raise RuntimeError("Ограничьте доступ к своему .env: chmod 600 .env")
        return False
    values = {
        name: secrets.token_urlsafe(48)
        for name in ("DATABASE_PASSWORD", "RABBITMQ_PASSWORD", "SESSION_SECRET", "DOWNLOAD_SECRET")
    }
    values["API_KEYS"] = json.dumps(
        {name: secrets.token_urlsafe(48) for name in ("onec", "telegram", "evaluation")}
    )
    temporary = directory / (".env.init-" + secrets.token_hex(12))
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write("# .env\n# Собственные секреты; не выводить в логи.\n")
            for name, value in values.items():
                stream.write(f"{name}='{value}'\n")
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, target)
            return True
        except FileExistsError:
            return False
    finally:
        temporary.unlink(missing_ok=True)


async def smoke(settings: Settings) -> None:
    """Проверяет внешний HTTP, настоящий pipeline и скачивание PDF без cookie."""
    key = settings.api_keys.get("evaluation")
    if not key:
        raise RuntimeError("Для smoke требуется отдельный API_KEYS.evaluation")
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
                raise RuntimeError(f"HTTP smoke: {path} вернул {response.status_code}")
        response = await client.post(
            base + "/api/v1/jobs",
            headers={"X-API-Key": key.get_secret_value()},
            files={"files": ("synthetic.pdf", payload, "application/pdf")},
        )
        if response.status_code != 202:
            raise RuntimeError("HTTP smoke: submit не вернул 202")
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
                raise RuntimeError(
                    "HTTP smoke: " + (status.get("error") or {}).get("code", "failed")
                )
            if status["status"] in {"completed", "completed_with_warnings"}:
                break
            await asyncio.sleep(2)
        else:
            raise RuntimeError("HTTP smoke: timeout задания")
        result = await client.get(
            base + f"/api/v1/jobs/{job_id}/result", headers={"X-API-Key": key.get_secret_value()}
        )
        result.raise_for_status()
        data = result.json()
        if data["schema_version"] != "1.0" or not data["files"][0]["documents"]:
            raise RuntimeError("HTTP smoke: неверный экспорт")
        # Отдельный клиент без cookie/API key имитирует скачивание 1С по capability URL.
        async with httpx.AsyncClient(timeout=30, trust_env=False) as downloader:
            for artifact in status["artifacts"] + status["sources"]:
                download = await downloader.get(artifact["file_url"])
                download.raise_for_status()
                if (
                    hashlib.sha256(download.content).hexdigest() != artifact["sha256"]
                    or len(download.content) != artifact["byte_size"]
                ):
                    raise RuntimeError("HTTP smoke: checksum/byte_size не совпали")
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
                raise RuntimeError("Контракт Docling OCR или VLM vision/JSON не подтверждён")
        elif command == "ready":
            result = await container.health.ready(force=True)
            print(json.dumps(result, ensure_ascii=False))
            if result["status"] != "ready":
                raise RuntimeError("Зависимости не готовы")
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
            print("Собственный .env создан либо сохранён без перезаписи")
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
                raise RuntimeError(
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
                raise RuntimeError("RabbitMQ значения не должны содержать перевод строки")
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
        # ValidationError способен содержать входные значения: никогда не печатаем его текст.
        safe = (
            str(exc) if isinstance(exc, (ConfigurationError, RuntimeError)) else type(exc).__name__
        )
        print("Проверка не пройдена: " + safe, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
