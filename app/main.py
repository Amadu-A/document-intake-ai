# app/main.py
"""FastAPI delivery: REST, SSE, health и Jinja2 без бизнес-логики в шаблонах."""

import hashlib
import logging
import os
from contextlib import asynccontextmanager
from importlib.resources import files
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.container import Container
from app.core.observability import configure_logging
from app.core.settings import Settings
from app.domain.errors import IntakeError
from app.presentation.http.middleware import SecurityMiddleware
from app.presentation.http.routes import router

WEB = Path(__file__).with_name("web")


def create_app(container=None) -> FastAPI:
    """Создаёт delivery; test container подменяется целиком, production использует один root."""

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        """Открывает собственные соединения и закрывает их при завершении процесса."""
        root = container or Container(Settings())
        application.state.container = root
        if container is None:
            try:
                await root.rabbit.connect()
            except Exception:
                # /health/live должен отвечать и при временном отказе broker; READY останется false.
                logging.getLogger("intake").warning('{"event":"broker_not_ready"}')
        yield
        if container is None:
            await root.close()

    settings = container.settings if container else Settings()
    application = FastAPI(
        title="Document Intake AI",
        version="1.0.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        description="REST для 1С: multipart → 202 → polling → JSON 1.0 → PDF",
    )
    application.add_middleware(
        SecurityMiddleware,
        max_bytes=(settings.upload_max_mb + 1) * 1024 * 1024,
        upload_timeout=settings.upload_timeout_seconds,
        json_max_bytes=settings.http_json_max_mb * 1024 * 1024,
    )
    application.include_router(router)
    application.mount("/static", StaticFiles(directory=WEB / "static"), name="static")
    application.mount(
        "/swagger-assets",
        StaticFiles(directory=str(files("fastapi_offline") / "static")),
        name="swagger",
    )
    templates = Jinja2Templates(directory=WEB / "templates")

    @application.get("/docs", include_in_schema=False)
    async def documentation(request: Request):
        """Отдаёт Swagger со всеми локальными ресурсами и без inline JavaScript/CDN."""
        return templates.TemplateResponse(request=request, name="docs.html")

    @application.exception_handler(IntakeError)
    async def domain_error(_request: Request, exc: IntakeError):
        """Преобразует только безопасные предметные сведения в стабильный envelope."""
        return JSONResponse(
            {"error": {"code": exc.code, "message": exc.message}}, status_code=exc.status
        )

    @application.exception_handler(RequestValidationError)
    async def request_error(_request: Request, _exc: RequestValidationError):
        """Не отражает входной payload, API-ключ или приватный текст в ошибке валидации."""
        return JSONResponse(
            {"error": {"code": "invalid_request", "message": "Некорректный запрос"}},
            status_code=422,
        )

    @application.exception_handler(StarletteHTTPException)
    async def transport_error(_request: Request, exc: StarletteHTTPException):
        """Приводит ошибки multipart/routing к безопасному общему error envelope."""
        return JSONResponse(
            {
                "error": {
                    "code": "upload_limit" if exc.status_code == 413 else "invalid_request",
                    "message": "Запрос отклонён",
                }
            },
            status_code=exc.status_code,
        )

    @application.exception_handler(Exception)
    async def unexpected(_request: Request, _exc: Exception):
        """Не раскрывает credentials и документные payload через библиотечные исключения."""
        return JSONResponse(
            {"error": {"code": "service_unavailable", "message": "Сервис временно недоступен"}},
            status_code=503,
        )

    @application.get("/", include_in_schema=False)
    async def index(request: Request):
        """Открывает анонимную изолированную web-сессию без login/password экрана."""
        root = request.app.state.container
        token = request.cookies.get("intake_session")
        owner = root.capabilities.session_owner(token)
        if not owner or not await root.database.session_active(owner):
            token = root.capabilities.new_session()
        owner = root.capabilities.session_owner(token)
        await root.database.throttle(
            "network:"
            + hashlib.sha256(
                (request.client.host if request.client else "unknown").encode()
            ).hexdigest()
        )
        await root.database.ensure_session(owner)
        await root.database.throttle(owner)
        response = templates.TemplateResponse(
            request=request,
            name="index.html",
            context={
                "csrf_token": root.capabilities.csrf(token),
                "max_files": settings.upload_max_files,
                "max_mb": settings.upload_max_mb,
                "max_file_mb": settings.upload_max_file_mb,
                "max_file_pages": settings.upload_max_file_pages,
                "max_pages": settings.upload_max_pages,
            },
        )
        response.set_cookie(
            "intake_session",
            token,
            httponly=True,
            samesite="strict",
            secure=settings.cookie_secure,
            max_age=settings.session_ttl_seconds,
        )
        return response

    @application.get("/health/live", tags=["Эксплуатация"])
    async def live():
        """Подтверждает жизнь API-процесса, отдельно от готовности внешних зависимостей."""
        return {"status": "live"}

    @application.get("/health/ready", tags=["Эксплуатация"])
    async def ready(request: Request):
        """Проверяет реальные зависимости и отказывает в READY при неполном контракте."""
        result = await request.app.state.container.health.ready()
        return JSONResponse(result, status_code=200 if result["status"] == "ready" else 503)

    return application


def main() -> None:
    """Запускает API с заданным портом; при конфликте uvicorn завершится с ошибкой."""
    os.umask(0o077)
    settings = Settings()
    configure_logging(settings.log_level)
    uvicorn.run(
        create_app(),
        host=settings.app_bind,
        port=settings.app_port,
        access_log=False,
        log_level="warning",
        log_config=None,
        proxy_headers=False,
    )


if __name__ == "__main__":
    main()
