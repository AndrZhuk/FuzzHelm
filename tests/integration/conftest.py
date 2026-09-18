"""Фікстури інтеграційних тестів storage (група M): PostgreSQL із docker-compose.test.yml.

Найменування: tests/integration/conftest.py
Автор: Андрій Жук, 2026.

БД: FUZZHELM_TEST_DATABASE_URL (за замовчуванням порт 5443, база fuzzhelm_test). Якщо БД недосяжна —
тести, що її потребують, пропускаються (skip), а не падають. Схему створює `alembic upgrade head`
один раз за сесію; кожен тест починає з TRUNCATE усіх таблиць (незалежність від порядку).
Кожен тест має власний event loop (pytest-asyncio, scope=function), тому engine — без пулу.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import asyncpg
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, make_url, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from fuzzhelm.storage.models import ALL_TABLES
from fuzzhelm.storage.session import make_engine, session_factory

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_TEST_DB_URL = "postgresql+asyncpg://fuzzhelm:fuzzhelm@localhost:5443/fuzzhelm_test"
TEST_DB_URL = os.environ.get("FUZZHELM_TEST_DATABASE_URL", DEFAULT_TEST_DB_URL)


def alembic_config(url: str) -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    cfg.attributes["configure_logger"] = False
    return cfg


def sync_url(url: str) -> str:
    """Той самий DSN для синхронного psycopg (інспекція схеми в синхронних тестах міграцій)."""
    return make_url(url).set(drivername="postgresql+psycopg").render_as_string(hide_password=False)


def _unreachable_reason(url: str) -> str | None:
    dsn = make_url(url).set(drivername="postgresql").render_as_string(hide_password=False)

    async def ping() -> None:
        conn = await asyncpg.connect(dsn, timeout=3)
        await conn.close()

    try:
        asyncio.run(ping())
    except Exception as e:  # будь-яка причина недосяжності → skip
        return f"{type(e).__name__}: {e}"
    return None


@pytest.fixture(scope="session")
def db_url() -> str:
    reason = _unreachable_reason(TEST_DB_URL)
    if reason is not None:
        pytest.skip(f"test PostgreSQL unreachable at {make_url(TEST_DB_URL).render_as_string()}: {reason}")
    command.upgrade(alembic_config(TEST_DB_URL), "head")
    return TEST_DB_URL


@pytest.fixture
def sync_engine(db_url: str) -> Iterator[Engine]:
    eng = create_engine(sync_url(db_url))
    yield eng
    eng.dispose()


@pytest.fixture
async def engine(db_url: str) -> AsyncIterator[AsyncEngine]:
    eng = make_engine(db_url, null_pool=True)
    async with eng.begin() as conn:
        await conn.execute(text(f"TRUNCATE {', '.join(ALL_TABLES)} RESTART IDENTITY CASCADE"))
    yield eng
    await eng.dispose()


@pytest.fixture
def factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return session_factory(engine)
