"""Залежності FastAPI: сервіси застосунку, автентифікація за Bearer-токеном, дозволи матриці доступу.

Найменування: api/deps.py
Призначення: кожен захищений маршрут оголошує рівно одну залежність `require(Permission.X)`; вона
перевіряє токен, роль за ACCESS_MATRIX і — для дозволів на зміну — актуальність ролі в БД.
Відповіді: немає/невалідний/прострочений токен → 401 (з WWW-Authenticate), роль без дозволу → 403.
Автор: Андрій Жук, 2026.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Awaitable, Callable
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordBearer

from fuzzhelm.api.auth import (
    ACCESS_MATRIX,
    MUTATING_PERMISSIONS,
    AuthError,
    Permission,
    Principal,
    decode_token,
)
from fuzzhelm.api.services import ApiServices, build_db_services, role_of

oauth2_scheme = OAuth2PasswordBearer(
    tokenUrl="auth/login", auto_error=False, description="JWT from POST /auth/login (HS256, 8 h)."
)


def get_services(request: Request) -> ApiServices:
    """Сервіси застосунку (app.state.services); у тестах підміняються через dependency_overrides."""
    services: ApiServices | None = getattr(request.app.state, "services", None)
    if services is None:
        services = build_db_services()
        request.app.state.services = services
    return services


ServicesDep = Annotated[ApiServices, Depends(get_services)]


def _unauthorized(reason: str) -> HTTPException:
    return HTTPException(status.HTTP_401_UNAUTHORIZED, detail=reason, headers={"WWW-Authenticate": "Bearer"})


async def get_principal(
    services: ServicesDep, token: Annotated[str | None, Depends(oauth2_scheme)]
) -> Principal:
    if not token:
        raise _unauthorized("Not authenticated")
    try:
        return decode_token(
            token, secret=services.settings.jwt_secret.get_secret_value(), clock=services.clock
        )
    except AuthError as e:
        # причину не деталізуємо клієнту (прострочений vs підроблений) — лише «невалідний токен»
        raise _unauthorized("Invalid or expired token") from e


PermissionDep = Callable[..., Awaitable[Principal]]


def require(permission: Permission) -> PermissionDep:
    """Залежність «маршрут вимагає дозволу»; атрибут `.permission` читає тест матриці доступу."""

    async def dependency(
        principal: Annotated[Principal, Depends(get_principal)], services: ServicesDep
    ) -> Principal:
        if principal.role not in ACCESS_MATRIX[permission]:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                detail=f"role '{principal.role.value}' lacks permission '{permission.value}'",
            )
        if permission in MUTATING_PERMISSIONS:
            # роль із токена могла застаріти (понижено/видалено) — для змін звіряємося з БД
            async with services.uow() as repos:
                user = await repos.users.get(principal.uid)
            if user is None or user.login != principal.login:
                raise _unauthorized("User no longer exists")
            current = role_of(user)
            if current is None or current not in ACCESS_MATRIX[permission]:
                raise HTTPException(status.HTTP_403_FORBIDDEN, detail="role was changed; permission revoked")
        return principal

    dependency.permission = permission  # type: ignore[attr-defined]
    dependency.__name__ = f"require_{permission.name.lower()}"
    return dependency


def client_ip(request: Request) -> str | None:
    """IP клієнта для audit_log. X-Forwarded-For НЕ довіряємо (підробляється); за reverse proxy
    uvicorn запускається з --proxy-headers --forwarded-allow-ips=<ip проксі>, і тоді client.host вже
    правильний. Не-IP значення (unix-сокет, «testclient») → None: колонка audit_log.ip має тип INET."""
    host = request.client.host if request.client else None
    if host is None:
        return None
    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        return None
