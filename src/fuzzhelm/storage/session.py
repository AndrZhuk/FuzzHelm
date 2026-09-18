"""Підключення до PostgreSQL: AsyncEngine, фабрика сесій, залежність FastAPI.

Найменування: storage/session.py
Призначення: єдина точка створення AsyncEngine (asyncpg) з JSON-серіалізатором, що розуміє Decimal,
і опційним SET ROLE у роль застосунку (fuzzhelm_app — append-only журнал, див. 0003_auth_audit).
Автор: Андрій Жук, 2026.

Межа транзакції — у викликача: репозиторії лише виконують запити в переданій AsyncSession і НІКОЛИ
не комітять самі. Шаблон: `async with factory() as s, s.begin(): await CandleRepo(s).upsert(...)`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from decimal import Decimal
from enum import Enum
from typing import Any

import orjson
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from fuzzhelm.config import get_settings
from fuzzhelm.core.money import dec_str
from fuzzhelm.storage.models import APP_ROLE

__all__ = [
    "APP_ROLE",
    "dispose_default_engine",
    "get_default_engine",
    "get_default_factory",
    "get_session",
    "json_dumps",
    "json_loads",
    "make_engine",
    "session_factory",
    "session_scope",
]


def _json_default(obj: Any) -> Any:
    # orjson не серіалізує Decimal: рядок без експоненти, як у канонічній серіалізації core.digest
    if isinstance(obj, Decimal):
        return dec_str(obj)
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, bytes):
        return obj.hex()
    if isinstance(obj, set | frozenset):
        return sorted(obj)
    raise TypeError(f"type {type(obj).__name__} is not JSON serializable")


def json_dumps(obj: Any) -> str:
    """Серіалізатор JSONB: orjson + Decimal → рядок без експоненти, numpy → списки/скаляри."""
    return orjson.dumps(obj, default=_json_default, option=orjson.OPT_SERIALIZE_NUMPY).decode()


def json_loads(s: str | bytes) -> Any:
    return orjson.loads(s)


def make_engine(
    url: str | None = None,
    *,
    role: str | None = None,
    echo: bool = False,
    null_pool: bool = False,
    pool_size: int = 5,
    max_overflow: int = 5,
    **kwargs: Any,
) -> AsyncEngine:
    """Створити AsyncEngine.

    url   — `postgresql+asyncpg://...`; None → `Settings().database_url` (FUZZHELM_DATABASE_URL / .env);
    role  — виконати кожне підключення від імені цієї ролі (стартовий параметр `role`, еквівалент
            `SET ROLE`): з role="fuzzhelm_app" UPDATE/DELETE журналу подій відхиляє сама СУБД. Це захист
            від помилкових записів, а не межа безпеки: користувач з'єднання (власник) може `SET ROLE` назад;
    null_pool — без пулу (тести з окремим event loop на кожен тест, одноразові скрипти).
    """
    url = url or get_settings().database_url
    connect_args: dict[str, Any] = dict(kwargs.pop("connect_args", {}))
    if role is not None:
        if not url.startswith("postgresql+asyncpg"):
            raise ValueError("role=... is supported only for the asyncpg driver")
        server_settings = dict(connect_args.get("server_settings", {}))
        server_settings["role"] = role
        connect_args["server_settings"] = server_settings
    pool_args: dict[str, Any] = (
        {"poolclass": NullPool} if null_pool
        else {"pool_size": pool_size, "max_overflow": max_overflow, "pool_pre_ping": True}
    )
    return create_async_engine(
        url,
        echo=echo,
        json_serializer=json_dumps,
        json_deserializer=json_loads,
        connect_args=connect_args,
        **pool_args,
        **kwargs,
    )


def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Фабрика сесій; expire_on_commit=False — рядки лишаються читабельними після коміту."""
    return async_sessionmaker(engine, expire_on_commit=False, autoflush=False)


@asynccontextmanager
async def session_scope(factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[AsyncSession]:
    """Одна транзакція: коміт при успіху, відкат при винятку."""
    async with factory() as session, session.begin():
        yield session


class _Default:
    """Процесний engine і фабрика (ліниво, за Settings) — для API/воркерів."""

    engine: AsyncEngine | None = None
    factory: async_sessionmaker[AsyncSession] | None = None


def get_default_engine() -> AsyncEngine:
    """Лінивий процесний AsyncEngine за Settings (FUZZHELM_DATABASE_URL / .env) від імені APP_ROLE.

    Робочий шлях API/воркерів (get_session) іде роллю застосунку, щоб REVOKE UPDATE, DELETE на
    event_journal/audit_log діяв без окремої домовленості з викликачем (без role= підключення власником
    схеми обходить REVOKE повністю). Потрібна схема, мігрована до 0003_auth_audit (роль існує).
    Обмеження: якщо користувач з'єднання — власник/суперкористувач, він може `SET ROLE` назад; захист від
    скомпрометованого застосунку дає лише окрема LOGIN-роль у складі fuzzhelm_app (deviations ST-02).
    """
    if _Default.engine is None:
        _Default.engine = make_engine(role=APP_ROLE)
        _Default.factory = session_factory(_Default.engine)
    return _Default.engine


def get_default_factory() -> async_sessionmaker[AsyncSession]:
    get_default_engine()
    assert _Default.factory is not None
    return _Default.factory


async def dispose_default_engine() -> None:
    """Закрити пул процесного engine (виклик у lifespan-shutdown FastAPI)."""
    if _Default.engine is not None:
        await _Default.engine.dispose()
    _Default.engine = None
    _Default.factory = None


async def get_session() -> AsyncIterator[AsyncSession]:
    """Залежність FastAPI: `session: AsyncSession = Depends(get_session)`; одна транзакція на запит."""
    async with session_scope(get_default_factory()) as session:
        yield session
