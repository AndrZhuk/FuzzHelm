"""FastAPI-застосунок FuzzHelm: збирання маршрутів, життєвий цикл, OpenAPI, обробка помилок.

Найменування: api/main.py
Призначення: `fuzzhelm.api.main:app` для uvicorn (docker-compose, сервіс api). `create_app(services)` дає
змогу тестам підставити реалізацію сервісів у пам'яті (або через app.dependency_overrides[get_services]).
Автор: Андрій Жук, 2026.

Жодних з'єднань із БД під час імпорту чи старту: engine створюється ліниво, LISTEN — з першим SSE-клієнтом,
тож /docs і /healthz працюють і без PostgreSQL. Опис OpenAPI — англійською (ЗК5), коментарі — українською.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Iterator, Sequence
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from sqlalchemy.exc import DBAPIError, IntegrityError, OperationalError
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from fuzzhelm.api.auth import Permission
from fuzzhelm.api.routers import audit, auth, backtests, decisions, dq, market, risk, runs, strategies, stream
from fuzzhelm.api.services import ApiServices, build_db_services
from fuzzhelm.core.errors import PermissionDeniedError
from fuzzhelm.storage.repositories.common import (
    SQLSTATE_FOREIGN_KEY_VIOLATION,
    SQLSTATE_INSUFFICIENT_PRIVILEGE,
    SQLSTATE_UNIQUE_VIOLATION,
    sqlstate,
)

log = logging.getLogger(__name__)

API_VERSION = "0.1.0"
SQLSTATE_CLASS_DATA_EXCEPTION = "22"  # PostgreSQL: 22003 numeric_value_out_of_range, 22008 datetime overflow…
DEV_JWT_SECRETS = frozenset({"dev-only-change-me", "change-me", ""})
MIN_JWT_SECRET_CHARS = 32  # HS256: ключ ≥ 256 біт (RFC 7518 §3.2)
DEFAULT_CORS_ORIGINS: tuple[str, ...] = ("http://localhost:5173", "http://127.0.0.1:5173")
PUBLIC_PATHS: frozenset[str] = frozenset(
    {"/auth/login", "/auth/demo", "/auth/demo/{login}", "/healthz", "/docs", "/docs/oauth2-redirect",
     "/redoc", "/openapi.json"}
)

DESCRIPTION = """
**FuzzHelm** — market-data aggregation service with a fuzzy-logic (Mamdani, 45 rules) decision core and a
deterministic, provably non-escalating risk loop. **Paper / testnet only — no mainnet, no real funds.**

* Authenticate with `POST /auth/login` (or the **Authorize** button) and send `Authorization: Bearer <JWT>`.
* Four roles — `operator`, `analyst`, `auditor`, `admin` — with an explicit endpoint × role access matrix
  (see `docs/security.md`); every state-changing action is written to an append-only audit log with the
  JSON state before and after.
* `GET /decisions/{id}/explain` is the formal derivation of a trading decision: membership degrees, fired
  rules with activation α, the aggregated output set μ_agg(u) and its centroid, and a Ukrainian narrative.

This is not investment advice.
"""

ROUTER_MODULES = (auth, market, dq, strategies, backtests, runs, decisions, risk, stream, audit)

TAGS: list[dict[str, str]] = [
    {"name": "auth", "description": "JWT (HS256, 8 h) login and the caller's identity."},
    {"name": "market", "description": "Stored candles and pipeline health."},
    {"name": "data quality", "description": "Hourly data-quality score Q (weighted sum of 4 components)."},
    {"name": "strategies", "description": "Rules as data: validated, versioned Mamdani rule bases and MFs."},
    {"name": "backtests", "description": "Asynchronous backtest submission."},
    {"name": "runs", "description": "Run passports, metrics and equity curves."},
    {"name": "decisions", "description": "Explainable decisions (`/explain`)."},
    {
        "name": "risk",
        "description": "Risk state, verdict journal, limits (admin) and kill-switch release (admin).",
    },
    {"name": "stream", "description": "Server-Sent Events from PostgreSQL LISTEN/NOTIFY."},
    {"name": "audit", "description": "Append-only audit log (auditor, admin)."},
    {"name": "service", "description": "Liveness probe."},
]


class SecurityHeadersMiddleware:
    """Мінімальні заголовки безпеки для JSON-API (чистий ASGI — не буферизує SSE-потік)."""

    HEADERS: tuple[tuple[bytes, bytes], ...] = (
        (b"x-content-type-options", b"nosniff"),
        (b"referrer-policy", b"no-referrer"),
        (b"x-frame-options", b"DENY"),
        (b"cache-control", b"no-store"),
    )

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                present = {k.lower() for k, _ in message.get("headers", [])}
                extra = [(k, v) for k, v in self.HEADERS if k not in present]
                message = {**message, "headers": [*message.get("headers", []), *extra]}
            await send(message)

        await self.app(scope, receive, send_with_headers)


def weak_jwt_secret(secret: str) -> bool:
    """Типовий секрет розробки або коротший за 32 символи — попередження під час старту."""
    return secret in DEV_JWT_SECRETS or len(secret) < MIN_JWT_SECRET_CHARS


def _error(code: int, detail: str) -> JSONResponse:
    return JSONResponse(status_code=code, content={"detail": detail})


def _install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(PermissionDeniedError)
    async def _permission(_: Request, exc: PermissionDeniedError) -> JSONResponse:
        return _error(status.HTTP_403_FORBIDDEN, str(exc))

    @app.exception_handler(LookupError)
    async def _lookup(_: Request, exc: LookupError) -> JSONResponse:
        # репозиторії сигналізують «не знайдено» саме LookupError; KeyError/IndexError — це дефект коду,
        # його не маскуємо під 404
        if type(exc) is not LookupError:
            log.error("unhandled %s", type(exc).__name__, exc_info=exc)
            return _error(status.HTTP_500_INTERNAL_SERVER_ERROR, "internal error")
        return _error(status.HTTP_404_NOT_FOUND, "not found")

    @app.exception_handler(IntegrityError)
    async def _integrity(_: Request, exc: IntegrityError) -> JSONResponse:
        code = sqlstate(exc)
        if code in (SQLSTATE_UNIQUE_VIOLATION, SQLSTATE_FOREIGN_KEY_VIOLATION):
            return _error(status.HTTP_409_CONFLICT, "conflict with stored data")
        log.error("integrity error sqlstate=%s", code)
        return _error(status.HTTP_422_UNPROCESSABLE_CONTENT, "data violates a database constraint")

    @app.exception_handler(OperationalError)
    async def _db_down(_: Request, exc: OperationalError) -> JSONResponse:
        log.error("database unavailable: %s", type(exc.orig).__name__ if exc.orig else type(exc).__name__)
        return _error(status.HTTP_503_SERVICE_UNAVAILABLE, "database unavailable")

    @app.exception_handler(DBAPIError)
    async def _db_error(_: Request, exc: DBAPIError) -> JSONResponse:
        code = sqlstate(exc)
        if code == SQLSTATE_INSUFFICIENT_PRIVILEGE:
            return _error(status.HTTP_403_FORBIDDEN, "operation not permitted by the database role")
        if code is not None and code.startswith(SQLSTATE_CLASS_DATA_EXCEPTION):
            # клас 22 (data exception: число поза типом колонки тощо) — помилка вводу, а не збій БД;
            # межі схем запитів мають відсікати це раніше, тут — страховка для нових параметрів
            log.warning("rejected input value sqlstate=%s", code)
            return _error(status.HTTP_422_UNPROCESSABLE_CONTENT, "input value out of range for the database")
        log.error("database error sqlstate=%s", code)
        return _error(status.HTTP_503_SERVICE_UNAVAILABLE, "database error")

    @app.exception_handler(OSError)
    async def _os_error(_: Request, exc: OSError) -> JSONResponse:
        # ConnectionRefusedError БД, недоступний файл лімітів: деталі — лише в журнал сервера
        log.error("I/O error: %s", type(exc).__name__)
        return _error(status.HTTP_503_SERVICE_UNAVAILABLE, "service dependency unavailable")


def create_app(
    services: ApiServices | None = None,
    *,
    cors_origins: Sequence[str] = DEFAULT_CORS_ORIGINS,
    build_default: bool = True,
) -> FastAPI:
    """Зібрати застосунок. services=None → робоча збірка (PostgreSQL) під час старту (lifespan)."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if getattr(app.state, "services", None) is None and build_default:
            app.state.services = build_db_services()
        svc: ApiServices | None = getattr(app.state, "services", None)
        if svc is not None and weak_jwt_secret(svc.settings.jwt_secret.get_secret_value()):
            log.warning("FUZZHELM_JWT_SECRET is a development default or shorter than %d chars; "
                        "set a random secret in .env", MIN_JWT_SECRET_CHARS)
        if svc is not None and svc.settings.demo_login:
            log.warning("FUZZHELM_DEMO_LOGIN is on: demo users can sign in without a password "
                        "(local stand only, never expose this API)")
        try:
            yield
        finally:
            if svc is not None:
                await svc.aclose()

    app = FastAPI(
        title="FuzzHelm API",
        version=API_VERSION,
        description=DESCRIPTION,
        openapi_tags=TAGS,
        lifespan=lifespan,
        contact={"name": "Andrii Zhuk"},
        license_info={"name": "Academic project (Lviv Polytechnic, 2026)"},
    )
    app.state.services = services
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(cors_origins),
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT"],
        allow_headers=["Authorization", "Content-Type", "Last-Event-ID"],
    )
    app.add_middleware(SecurityHeadersMiddleware)
    _install_error_handlers(app)

    @app.get(
        "/healthz",
        tags=["service"],
        summary="Liveness probe",
        description="Returns 200 while the process is up; does not touch the database.",
    )
    async def healthz() -> dict[str, Any]:
        return {"status": "ok", "service": "fuzzhelm", "version": API_VERSION}

    for module in ROUTER_MODULES:
        app.include_router(module.router)
    return app


def iter_api_routes() -> Iterator[APIRoute]:
    """Усі маршрути API (з префіксами роутерів) — для тесту матриці доступу і таблиці в docs/security.md."""
    for module in ROUTER_MODULES:
        for route in module.router.routes:
            if isinstance(route, APIRoute):
                yield route


def route_permission(route: APIRoute) -> Permission | None:
    """Дозвіл, якого вимагає маршрут (залежність require(...)), або None для публічного маршруту."""
    stack = list(route.dependant.dependencies)
    found: list[Permission] = []
    while stack:
        dep = stack.pop()
        perm = getattr(dep.call, "permission", None)
        if isinstance(perm, Permission):
            found.append(perm)
        stack.extend(dep.dependencies)
    if len(found) > 1:
        raise ValueError(f"route {route.path} declares several permissions: {found}")
    return found[0] if found else None


app = create_app()
