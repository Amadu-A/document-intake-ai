# app/core/settings.py
"""Единственный каталог Settings; секреты не включаются в диагностику."""

import re
from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import URL


class ConfigurationError(RuntimeError):
    """Несёт только безопасную диагностику отсутствующих проектных секретов."""


class Settings(BaseSettings):
    """Читает каталог, sparse .env и окружение в порядке возрастания приоритета."""

    model_config = SettingsConfigDict(
        env_file=(".env.example", ".env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="forbid",
        hide_input_in_errors=True,
    )
    compose_project_name: str = "document-intake-ai"
    app_bind: str = "0.0.0.0"  # noqa: S104
    app_port: int = Field(8084, ge=1024, le=65535)
    app_public_base_url: str = "http://192.168.55.3:8084"
    cookie_secure: bool = False
    log_level: str = "INFO"
    data_dir: Path = Path(".data")
    database_host: str = "postgres"
    database_port: int = 5432
    database_name: str = "document_intake"
    database_user: str = "document_intake"
    database_password: SecretStr = SecretStr("")
    rabbitmq_host: str = "rabbitmq"
    rabbitmq_port: int = 5672
    rabbitmq_container: str = "shared-rabbitmq-1"
    rabbitmq_user: str = "document_intake"
    rabbitmq_vhost: str = "document-intake"
    rabbitmq_queue: str = "document-intake.jobs"
    rabbitmq_password: SecretStr = SecretStr("")
    rabbitmq_timeout_seconds: int = Field(20, ge=3, le=120)
    api_keys: dict[str, SecretStr] = Field(default_factory=dict)
    session_secret: SecretStr = SecretStr("")
    download_secret: SecretStr = SecretStr("")
    docling_base_url: str = "http://docling-eval:5001"
    docling_api_key: SecretStr = SecretStr("")
    docling_ocr_preset: str = Field("tesseract", pattern=r"^[A-Za-z][A-Za-z0-9_.-]{0,127}$")
    docling_ocr_languages: tuple[
        Annotated[str, Field(pattern=r"^[A-Za-z][A-Za-z0-9:_-]{0,31}$")], ...
    ] = Field(("iso:ru", "iso:en"), min_length=1, max_length=8)
    docling_timeout_seconds: int = Field(180, ge=1, le=600)
    docling_window_pages: int = Field(20, ge=1, le=50)
    docling_max_request_mb: int = Field(45, ge=1, le=50)
    docling_async: bool = True
    docling_task_timeout_seconds: int = Field(600, ge=30, le=1800)
    docling_poll_interval_seconds: float = Field(5, ge=0.1, le=30)
    docling_max_attempts: int = Field(5, ge=1, le=8)
    docling_retry_max_seconds: int = Field(60, ge=1, le=300)
    docling_request_interval_seconds: float = Field(1, ge=0, le=60)
    shared_vlm_base_url: str = "http://shared-vlm:8000/v1"
    shared_vlm_model: str = "shared-vlm"
    vlm_enable_thinking: bool = False
    vlm_timeout_seconds: int = Field(180, ge=1, le=600)
    vlm_window_pages: int = Field(8, ge=2, le=32)
    vlm_overlap_pages: int = Field(2, ge=1)
    vlm_reconciliation_windows: int = Field(4, ge=2, le=8)
    vlm_segmentation_timeout_seconds: int = Field(900, ge=10, le=3600)
    vlm_max_tokens: int = Field(16000, ge=1024, le=32768)
    model_attempts: int = Field(3, ge=1, le=5)
    worker_concurrency: int = Field(1, ge=1, le=4)
    worker_lease_seconds: int = Field(60, ge=15, le=300)
    worker_max_attempts: int = Field(3, ge=1, le=5)
    queue_max_depth: int = Field(100, ge=1, le=1000)
    job_timeout_seconds: int = Field(14400, ge=30, le=86400)
    queue_timeout_seconds: int = Field(1800, ge=30, le=14400)
    preview_timeout_seconds: int = Field(3600, ge=30, le=14400)
    confirmation_timeout_seconds: int = Field(3600, ge=3600, le=3600)
    retention_seconds: int = Field(3600, ge=3600, le=3600)
    session_ttl_seconds: int = Field(86400, ge=3600)
    upload_max_mb: int = Field(1024, ge=1, le=8192)
    upload_max_file_mb: int = Field(400, ge=1, le=400)
    upload_max_file_pages: int = Field(1000, ge=1, le=1000)
    document_max_mb: int = Field(400, ge=1, le=400)
    document_max_pages: int = Field(1000, ge=1, le=1000)
    upload_max_files: int = Field(20, ge=1, le=100)
    upload_max_pages: int = Field(2000, ge=1, le=10000)
    upload_timeout_seconds: int = Field(600, ge=10, le=3600)
    http_json_max_mb: int = Field(4, ge=1, le=16)
    storage_min_free_mb: int = Field(1024, ge=0, le=65536)
    image_max_pixels: int = Field(40_000_000, ge=1_000_000, le=100_000_000)
    render_max_side: int = Field(1600, ge=400, le=2400)
    conversion_timeout_seconds: int = Field(900, ge=5, le=7200)
    rate_limit_per_minute: int = Field(120, ge=5)
    read_rate_limit_per_minute: int = Field(600, ge=5)
    preview_rate_limit_per_minute: int = Field(2400, ge=5)
    max_owner_active_jobs: int = Field(4, ge=1, le=100)
    health_cache_seconds: int = Field(60, ge=5, le=300)

    @model_validator(mode="after")
    def validate_contract(self) -> "Settings":
        """Отвергает опасные URLs, общие credentials и несовместимые окна."""
        for value in (self.app_public_base_url, self.docling_base_url, self.shared_vlm_base_url):
            url = urlsplit(value)
            if url.scheme not in {"http", "https"} or not url.hostname:
                raise ValueError("Требуется HTTP(S) URL")
            if url.username or url.password or url.query or url.fragment:
                raise ValueError("URL не должен содержать credentials, query или fragment")
        if self.vlm_overlap_pages >= self.vlm_window_pages:
            raise ValueError("Перекрытие должно быть меньше окна VLM")
        if self.rabbitmq_user in {"guest", "admin", "root"} or self.rabbitmq_vhost == "/":
            raise ValueError("Нужны выделенные RabbitMQ user и vhost")
        for value in (
            self.rabbitmq_user,
            self.rabbitmq_vhost,
            self.rabbitmq_queue,
            self.rabbitmq_container,
        ):
            if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,127}", value):
                raise ValueError("RabbitMQ namespace должен быть безопасным отдельным именем")
        if not re.fullmatch(r"document-intake[a-z0-9_-]*", self.compose_project_name):
            raise ValueError("COMPOSE_PROJECT_NAME должен принадлежать document-intake")
        if len(set(v.get_secret_value() for v in self.api_keys.values())) != len(self.api_keys):
            raise ValueError("API-ключи интеграций должны различаться")
        return self

    def require_secrets(self) -> None:
        """Проверяет готовность конфигурации, сообщая только имена отсутствующих секретов."""
        required = {
            "DATABASE_PASSWORD": self.database_password,
            "RABBITMQ_PASSWORD": self.rabbitmq_password,
            "SESSION_SECRET": self.session_secret,
            "DOWNLOAD_SECRET": self.download_secret,
        }
        absent = [name for name, value in required.items() if len(value.get_secret_value()) < 32]
        if not self.api_keys or any(len(v.get_secret_value()) < 32 for v in self.api_keys.values()):
            absent.append("API_KEYS")
        if absent:
            raise ConfigurationError("Не настроены безопасные секреты: " + ", ".join(absent))

    def database_url(self) -> URL:
        """Строит URL драйвера без ручной интерполяции спецсимволов пароля."""
        return URL.create(
            "postgresql+asyncpg",
            username=self.database_user,
            password=self.database_password.get_secret_value(),
            host=self.database_host,
            port=self.database_port,
            database=self.database_name,
        )
