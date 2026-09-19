"""Маршрути автентифікації: POST /auth/login (OAuth2-форма або JSON) → JWT; GET /auth/me.

Найменування: api/routers/auth.py
Призначення: обмін логіна/пароля на токен доступу з роллю; кожна спроба входу (успішна і невдала)
пишеться в audit_log (STRIDE: Repudiation), перебір обмежує LoginRateLimiter (STRIDE: Spoofing).
Автор: Андрій Жук, 2026.

Повідомлення про помилку однакове для «немає користувача» і «хибний пароль», а bcrypt рахується і для
неіснуючого логіна (dummy_verify) — перелік користувачів не витікає ні текстом, ні часом відповіді.
"""

from __future__ import annotations

import asyncio
import math
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from passlib.context import CryptContext
from pydantic import ValidationError

from fuzzhelm.api.auth import ACCESS_MATRIX, Permission, Principal, issue_token
from fuzzhelm.api.deps import ServicesDep, client_ip, require
from fuzzhelm.api.schemas import ErrorResponse, LoginJson, MeResponse, TokenResponse
from fuzzhelm.api.services import role_of
from fuzzhelm.storage.repositories import UserRow
from fuzzhelm.storage.repositories.user import verify_password

router = APIRouter(prefix="/auth", tags=["auth"])

_FORM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["username", "password"],
    "properties": {
        "username": {"type": "string", "maxLength": 128},
        "password": {"type": "string", "format": "password", "maxLength": 128},
        "grant_type": {"type": "string", "enum": ["password"]},
    },
}
_LOGIN_BODY: dict[str, Any] = {
    "requestBody": {
        "required": True,
        "content": {
            "application/x-www-form-urlencoded": {"schema": _FORM_SCHEMA},
            "application/json": {
                "schema": {
                    "type": "object",
                    "required": ["username", "password"],
                    "properties": _FORM_SCHEMA["properties"],
                }
            },
        },
    }
}


def check_password(user: UserRow | None, password: str, context: CryptContext) -> bool:
    """Та сама логіка, що UserRepo.authenticate (dummy_verify для невідомого логіна), але синхронна —
    для виклику в потоці."""
    if user is None:
        context.dummy_verify()
        return False
    return verify_password(password, user.pwd_hash, context)


async def _read_credentials(request: Request) -> LoginJson:
    ctype = request.headers.get("content-type", "").split(";")[0].strip().lower()
    try:
        if ctype == "application/json":
            raw = await request.json()
            if not isinstance(raw, dict):
                raise ValueError("JSON body must be an object")
            data = {"username": raw.get("username", raw.get("login")), "password": raw.get("password")}
        else:
            form = await request.form()
            data = {"username": form.get("username"), "password": form.get("password")}
        return LoginJson.model_validate(data)
    except (ValidationError, ValueError, TypeError) as e:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=[{"loc": ["body"], "msg": "username and password are required", "type": "value_error"}],
        ) from e


@router.post(
    "/login",
    response_model=TokenResponse,
    openapi_extra=_LOGIN_BODY,
    summary="Exchange login and password for a JWT",
    description="Accepts an OAuth2 password form (`application/x-www-form-urlencoded`, used by the "
    "**Authorize** button of /docs) or JSON `{username, password}`. Returns an HS256 JWT valid "
    "for `jwt_ttl_hours` (8 h) with claims `sub`, `uid`, `role`. Wrong credentials give the same "
    "401 for unknown users and wrong passwords; after 10 failures in 5 minutes per IP or login "
    "the endpoint answers 429 with `Retry-After`. Every attempt is written to the audit log.",
    responses={401: {"model": ErrorResponse}, 429: {"model": ErrorResponse}},
)
async def login(request: Request, services: ServicesDep) -> TokenResponse:
    creds = await _read_credentials(request)
    ip = client_ip(request)
    keys = (f"ip:{ip}", f"login:{creds.username.lower()}")
    wait = services.login_limiter.retry_after_s(*keys)
    if wait > 0:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many failed login attempts",
            headers={"Retry-After": str(math.ceil(wait))},
        )
    async with services.uow() as repos:
        candidate = await repos.users.get_by_login(creds.username)
    # bcrypt (вартість 12, ~0,2 с CPU) — в окремому потоці і ПОЗА транзакцією: перебір паролів не
    # зупиняє event loop і не тримає з'єднання пулу БД на час хешування; для неіснуючого логіна
    # рахується фіктивний хеш — час відповіді однаковий
    ok = await asyncio.to_thread(check_password, candidate, creds.password, services.password_context)
    user = candidate if ok else None
    role = role_of(user)
    async with services.uow() as repos:
        if user is None or role is None or user.login is None:
            await repos.audit.append(
                "auth.login_failed",
                f"user/{creds.username[:128]}",
                after={"login": creds.username[:128]},
                ip=ip,
            )
            failed = True
        else:
            issued = issue_token(
                uid=user.id,
                login=user.login,
                role=role,
                secret=services.settings.jwt_secret.get_secret_value(),
                ttl_hours=services.settings.jwt_ttl_hours,
                clock=services.clock,
            )
            await repos.audit.append(
                "auth.login",
                f"user/{user.login}",
                user_id=user.id,
                ip=ip,
                after={"role": role.value, "jti": issued.jti, "expires_at_s": issued.expires_at_s},
            )
            failed = False
    if failed:
        services.login_limiter.record_failure(*keys)
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    services.login_limiter.reset(keys[1])
    assert user is not None and user.login is not None and role is not None
    return TokenResponse(
        access_token=issued.token, expires_in=issued.expires_in_s, role=role, login=user.login
    )


@router.get(
    "/me",
    response_model=MeResponse,
    summary="Current user and permissions",
    description="Decoded token of the caller and the list of permissions its role has in the access matrix.",
    responses={401: {"model": ErrorResponse}},
)
async def me(principal: Annotated[Principal, Depends(require(Permission.SELF_READ))]) -> MeResponse:
    perms = sorted(p.value for p, roles in ACCESS_MATRIX.items() if principal.role in roles)
    return MeResponse(
        uid=principal.uid,
        login=principal.login,
        role=principal.role,
        permissions=perms,
        expires_at_s=principal.exp_s,
    )
