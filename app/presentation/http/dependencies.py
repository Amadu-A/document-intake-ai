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


def request_budget(request: Request) -> tuple[str, int]:
    """Выбирает бюджет по доверенному маршруту, независимо от query и пользовательских ID."""
    settings = services(request).settings
    if request.method in {"GET", "HEAD"}:
        route = getattr(request.scope.get("route"), "path", "")
        if route in {
            "/api/v1/jobs/{job_id}/pages/{file_id}/{page}/preview",
            "/api/v1/jobs/{job_id}/pages/{file_id}/{page}/thumbnail",
        }:
            return "preview", settings.preview_rate_limit_per_minute
        return "read", settings.read_rate_limit_per_minute
    return "control", settings.rate_limit_per_minute


async def optional_owner(request: Request, key: Annotated[str | None, Security(api_key_header)]):
    """Проверяет API key или сессию; cookie mutation требует точных Origin и CSRF."""
    return await authenticate(request, key)


async def authenticate(request: Request, key: str | None) -> str | None:
    """Переиспользуется до multipart parsing; повторная dependency читает тот же контекст."""
    if getattr(request.state, "auth_checked", False):
        return request.state.auth_owner
    container = services(request)
    category, limit = request_budget(request)
    await container.database.throttle(
        f"{category}:network:"
        + hashlib.sha256(
            (request.client.host if request.client else "unknown").encode()
        ).hexdigest(),
        limit=limit,
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
        await container.database.throttle(f"{category}:{owner}", limit=limit)
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
