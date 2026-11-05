# app/infrastructure/security.py
"""Сессионные capability и подписи скачивания; секретные значения не журналируются."""

import hashlib
import hmac
import secrets
from datetime import datetime
from urllib.parse import quote

from app.domain.jobs import utcnow


class Capabilities:
    """Изолирует anonymous owners и ограничивает подписанную ссылку конкретным артефактом."""

    def __init__(self, settings) -> None:
        """Получает два независимых секрета и внешний URL из проверенных Settings."""
        self.settings = settings

    def _mac(self, secret: str, value: str) -> str:
        """Подписывает каноническую строку без передачи ключа третьим сторонам."""
        return hmac.new(secret.encode(), value.encode(), hashlib.sha256).hexdigest()

    def new_session(self) -> str:
        """Создаёт случайную подписанную сессию с абсолютным временем истечения."""
        expires = int(utcnow().timestamp()) + self.settings.session_ttl_seconds
        value = f"{secrets.token_urlsafe(32)}.{expires}"
        return value + "." + self._mac(self.settings.session_secret.get_secret_value(), value)

    def session_owner(self, token: str | None) -> str | None:
        """Проверяет подпись и срок cookie, возвращая только необратимый owner id."""
        if not token or len(token) > 200:
            return None
        try:
            random, expires, signature = token.split(".")
            value = random + "." + expires
            expected = self._mac(self.settings.session_secret.get_secret_value(), value)
            if int(expires) <= utcnow().timestamp() or not hmac.compare_digest(expected, signature):
                return None
            return "session:" + hashlib.sha256(token.encode()).hexdigest()
        except (ValueError, TypeError):
            return None

    def csrf(self, token: str) -> str:
        """Привязывает CSRF-токен к конкретной HttpOnly capability cookie."""
        return self._mac(self.settings.session_secret.get_secret_value(), "csrf:" + token)

    def integration_owner(self, value: str) -> str | None:
        """Сравнивает отдельные API-ключи constant-time и возвращает независимый namespace."""
        matched = None
        for name, key in self.settings.api_keys.items():
            if hmac.compare_digest(value, key.get_secret_value()):
                matched = "integration:" + name
        return matched

    def sign(self, path: str, owner: str, expires: datetime) -> str:
        """Создаёт URL, работающий без web cookie до фиксированного artifact TTL."""
        deadline = str(int(expires.timestamp()))
        signature = self._mac(
            self.settings.download_secret.get_secret_value(), f"{owner}\n{path}\n{deadline}"
        )
        return (
            self.settings.app_public_base_url.rstrip("/")
            + path
            + "?token="
            + quote(deadline + "." + signature)
        )

    def verify(
        self,
        path: str,
        owner: str,
        expires: datetime,
        token: str | None,
        allow_expired: bool = False,
    ) -> bool:
        """Не разрешает сменить путь, owner или продлить token за пределы TTL задания."""
        if not token or len(token) > 100:
            return False
        try:
            deadline, signature = token.split(".")
            if int(deadline) > int(expires.timestamp()):
                return False
            if not allow_expired and int(deadline) <= utcnow().timestamp():
                return False
            expected = self._mac(
                self.settings.download_secret.get_secret_value(), f"{owner}\n{path}\n{deadline}"
            )
            return hmac.compare_digest(signature, expected)
        except (ValueError, TypeError):
            return False
