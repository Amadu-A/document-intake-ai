# app/presentation/http/dependencies.py
"""Получение собранных сценариев и authentication context; CSRF не заменяет owner isolation."""

import hashlib
import hmac
from typing import Annotated

from fastapi import Depends, Request, Security
from fastapi.security import APIKeyHeader

from app.domain.errors import IntakeError

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def services(request: Request):
    """Возвращает composition root процесса без создания инфраструктуры в роутере."""
    return request.app.state.container


async def optional_owner(request: Request, key: Annotated[str | None, Security(api_key_header)]):
    """Проверяет API key или сессию; cookie mutation требует точных Origin и CSRF."""
    return await authenticate(request, key)


async def authenticate(request: Request, key: str | None) -> str | None:
    """Переиспользуется до multipart parsing; повторная dependency читает тот же контекст."""
    if getattr(request.state, "auth_checked", False):
        return request.state.auth_owner
    container = services(request)
    await container.database.throttle(
        "network:"
        + hashlib.sha256(
            (request.client.host if request.client else "unknown").encode()
        ).hexdigest()
    )
    if key is not None:
        owner = container.capabilities.integration_owner(key)
        if not owner:
            raise IntakeError("access_denied", "Неверный API-ключ", 401)
    else:
        token = request.cookies.get("intake_session")
        owner = container.capabilities.session_owner(token)
        if owner and not await container.database.session_active(owner):
            owner = None
        if owner and request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin", "")
            csrf = request.headers.get("x-csrf-token", "")
            if origin != container.settings.app_public_base_url.rstrip(
                "/"
            ) or not hmac.compare_digest(csrf, container.capabilities.csrf(token)):
                raise IntakeError("csrf_failed", "Origin или CSRF не подтверждён", 403)
    if owner:
        await container.database.throttle(owner)
    request.state.auth_owner = owner
    request.state.auth_checked = True
    return owner


async def required_owner(owner: Annotated[str | None, Depends(optional_owner)]) -> str:
    """Требует изолированный namespace для пользовательских API endpoints."""
    if not owner:
        raise IntakeError("access_denied", "Требуется API-ключ или анонимная web-сессия", 401)
    return owner


Owner = Annotated[str, Depends(required_owner)]
OptionalOwner = Annotated[str | None, Depends(optional_owner)]
Services = Annotated[object, Depends(services)]
