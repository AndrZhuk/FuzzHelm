"""Середовище Alembic (async, asyncpg) для міграцій FuzzHelm.

Найменування: alembic/env.py
Призначення: виконати ревізії 0001–0003 через AsyncEngine; URL береться з (за пріоритетом)
`alembic -x url=...` → `sqlalchemy.url`, виставленого програмно (тести) → FUZZHELM_DATABASE_URL /
`.env` через `fuzzhelm.config.Settings`. У alembic.ini URL свідомо немає: жодних креденшлів у репозиторії.
Автор: Андрій Жук, 2026.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from fuzzhelm.config import Settings
from fuzzhelm.storage.models import metadata

config = context.config

if config.config_file_name is not None and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = metadata


def database_url() -> str:
    x_url = context.get_x_argument(as_dictionary=True).get("url")
    if x_url:
        return x_url
    ini_url = config.get_main_option("sqlalchemy.url")
    if ini_url:
        return ini_url
    return Settings().database_url


def run_migrations_offline() -> None:
    """`alembic upgrade head --sql`: згенерувати SQL-скрипт без підключення до БД."""
    context.configure(
        url=database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    engine = create_async_engine(database_url(), poolclass=pool.NullPool)
    try:
        async with engine.connect() as connection:
            await connection.run_sync(do_run_migrations)
    finally:
        await engine.dispose()


def run_migrations_online() -> None:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(run_async_migrations())
        return
    # викликано з уже запущеного event loop (напр. async-тест): власний цикл в окремому потоці
    with ThreadPoolExecutor(max_workers=1) as ex:
        ex.submit(asyncio.run, run_async_migrations()).result()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
