# app/presentation/http/middleware.py
"""Ограничение HTTP body до multipart parsing и единые browser security headers."""

import asyncio

from starlette.responses import JSONResponse

from app.domain.errors import IntakeError


class SecurityMiddleware:
    """Ограничивает Content-Length и chunked upload; заголовки не содержат секретов."""

    def __init__(self, app, max_bytes: int, upload_timeout: int) -> None:
        """Фиксирует лимит на весь запрос, включая multipart overhead."""
        self.app, self.max_bytes = app, max_bytes
        self.upload_timeout = upload_timeout

    async def __call__(self, scope, receive, send) -> None:
        """Защищает HTTP, сохраняя ASGI streaming и отказ до чтения oversized payload."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        count = 0
        headers = dict(scope["headers"])
        try:
            length = int(headers.get(b"content-length", b"0"))
        except ValueError:
            length = self.max_bytes + 1
        if length > self.max_bytes:
            await JSONResponse(
                {"error": {"code": "upload_limit", "message": "Превышен размер запроса"}},
                status_code=413,
            )(scope, receive, send)
            return

        async def limited_receive():
            """Считает chunked body без доверия заголовку Content-Length."""
            nonlocal count
            message = await receive()
            count += len(message.get("body", b""))
            if count > self.max_bytes:
                raise IntakeError("upload_limit", "Превышен размер запроса", 413)
            return message

        async def secured_send(message):
            """Добавляет CSP и no-store без логирования request URL/query."""
            if message["type"] == "http.response.start":
                message["headers"] = list(message["headers"]) + [
                    (b"x-content-type-options", b"nosniff"),
                    (b"referrer-policy", b"no-referrer"),
                    (b"cache-control", b"no-store"),
                    (b"x-frame-options", b"DENY"),
                    (
                        b"content-security-policy",
                        b"default-src 'self'; script-src 'self'; "
                        b"style-src 'self'; img-src 'self' blob: data:; object-src 'self' blob:; "
                        b"frame-src 'self' blob:; connect-src 'self'; "
                        b"base-uri 'none'; frame-ancestors 'none'",
                    ),
                ]
            await send(message)

        if scope["method"] in {"POST", "PUT", "PATCH"}:
            try:
                async with asyncio.timeout(self.upload_timeout):
                    await self.app(scope, limited_receive, secured_send)
            except TimeoutError:
                await JSONResponse(
                    {"error": {"code": "upload_timeout", "message": "Истёк лимит загрузки"}},
                    status_code=408,
                )(scope, receive, secured_send)
        else:
            await self.app(scope, limited_receive, secured_send)
