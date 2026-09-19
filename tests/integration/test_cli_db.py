"""CLI `fuzzhelm user` на справжньому PostgreSQL: bcrypt-хеш у app_user, audit_log з актором «cli»,
вхід в API.

Найменування: tests/integration/test_cli_db.py
Автор: Андрій Жук, 2026.

Потребує `docker compose -f docker-compose.test.yml up -d --wait` (порт 5443); без БД — skip (conftest).
CLI працює роллю fuzzhelm_app (як у робочій збірці), тож перевіряються і справжні гранти: INSERT/UPDATE
app_user та лише INSERT в audit_log. Тести синхронні: `cli.main` сам запускає asyncio.run.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import sys
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError

from fuzzhelm import cli
from fuzzhelm.api.deps import get_services
from fuzzhelm.api.main import create_app
from fuzzhelm.api.services import build_db_services
from fuzzhelm.config import Settings
from fuzzhelm.storage.models import APP_ROLE
from fuzzhelm.storage.repositories import UserRepo
from fuzzhelm.storage.repositories.user import PWD_CONTEXT
from fuzzhelm.storage.session import make_engine, session_factory, session_scope

pytestmark = pytest.mark.integration

PASSWORD = "Integr4tion-pass-phrase"
SECRET = "integration-secret-" + "c" * 32


@pytest.fixture
def users_db(sync_engine: Engine) -> Iterator[Engine]:
    with sync_engine.begin() as conn:
        conn.execute(text("TRUNCATE app_user, audit_log RESTART IDENTITY CASCADE"))
    yield sync_engine


def run_cli(db_url: str, argv: list[str], monkeypatch: pytest.MonkeyPatch, stdin: str | None = None) -> int:
    if stdin is not None:
        monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
    return cli.main(["--database-url", db_url, "user", *argv])


def audit_rows(eng: Engine) -> list[Any]:
    with eng.connect() as conn:
        return list(conn.execute(text(
            "SELECT id, action, target, user_id, before_json, after_json, ip FROM audit_log ORDER BY id")))


async def api_login(db_url: str, login: str, password: str) -> httpx.Response:
    app_engine = make_engine(db_url, null_pool=True, role=APP_ROLE)
    services = build_db_services(Settings(jwt_secret=SECRET, _env_file=None),  # type: ignore[call-arg]
                                 engine=app_engine, listen=False)
    app = create_app(build_default=False)
    app.dependency_overrides[get_services] = lambda: services
    try:
        transport = httpx.ASGITransport(app=app, client=("198.51.100.9", 40000))
        async with httpx.AsyncClient(transport=transport, base_url="http://api.test") as client:
            return await client.post("/auth/login", data={"username": login, "password": password})
    finally:
        await services.aclose()
        await app_engine.dispose()


def test_user_cli_creates_bcrypt_user_audits_and_user_can_log_in(
        db_url: str, users_db: Engine, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
        caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    assert run_cli(db_url, ["add", "--login", "root", "--role", "admin", "--password-stdin"], monkeypatch,
                   PASSWORD + "\n") == 0
    out, err = capsys.readouterr()
    assert "created user id=1 login='root' role=admin (audit_log #1)" in out
    with users_db.connect() as conn:
        row = conn.execute(text("SELECT id, login, pwd_hash, role, created_at FROM app_user")).one()
    assert (row.id, row.login, row.role) == (1, "root", "admin") and row.created_at is not None
    assert row.pwd_hash.startswith("$2b$12$") and PWD_CONTEXT.verify(PASSWORD, row.pwd_hash)
    (rec,) = audit_rows(users_db)
    assert (rec.action, rec.target, rec.user_id, rec.before_json, rec.ip) == (
        "user.create", "user/root", None, None, None)
    assert rec.after_json["role"] == "admin" and rec.after_json["_actor"]["login"] == "cli"
    audit_text = json.dumps([dict(r._mapping) for r in audit_rows(users_db)], default=str)
    leaked = out + err + caplog.text + audit_text
    assert PASSWORD not in leaked and row.pwd_hash not in leaked
    # створений через CLI користувач справді входить в API (той самий bcrypt, роль fuzzhelm_app)
    r = asyncio.run(api_login(db_url, "root", PASSWORD))
    assert r.status_code == 200 and r.json()["role"] == "admin"
    assert asyncio.run(api_login(db_url, "root", PASSWORD + "x")).status_code == 401


def test_user_cli_set_role_list_and_duplicate_on_real_db(
        db_url: str, users_db: Engine, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    for login, role in (("root", "admin"), ("ann", "analyst")):
        assert run_cli(db_url, ["add", "--login", login, "--role", role, "--password-stdin"], monkeypatch,
                       PASSWORD + "\n") == 0
    assert run_cli(db_url, ["set-role", "--login", "ann", "--role", "operator"], monkeypatch) == 0
    # останнього адміністратора понизити не можна
    assert run_cli(db_url, ["set-role", "--login", "root", "--role", "analyst"], monkeypatch) == 2
    capsys.readouterr()
    assert run_cli(db_url, ["list", "--json"], monkeypatch) == 0
    listed = json.loads(capsys.readouterr().out)
    assert [(u["login"], u["role"]) for u in listed] == [("root", "admin"), ("ann", "operator")]
    assert all(u["created_at"].endswith("Z") for u in listed)
    # дубль логіна: перевірка до INSERT
    assert run_cli(db_url, ["add", "--login", "ann", "--role", "admin", "--password-stdin"], monkeypatch,
                   PASSWORD + "\n") == 2
    assert "already exists" in capsys.readouterr().err
    rows = audit_rows(users_db)
    assert [r.action for r in rows] == ["user.create", "user.create", "user.set_role", "user.list"]
    set_role = rows[2]
    assert set_role.before_json == {"id": 2, "login": "ann", "role": "analyst"}
    assert set_role.after_json["role"] == "operator" and set_role.after_json["_actor"]["login"] == "cli"
    assert rows[3].after_json["count"] == 2


def test_user_cli_unique_violation_race_hides_driver_details(
        db_url: str, users_db: Engine, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    """Гонка «перевірили — вставили»: UNIQUE(login) СУБД відхиляє дубль, CLI дає exit 2 без SQL і bcrypt-хеша
    у виводі, а транзакція (разом із записом аудиту) відкочується."""
    assert run_cli(db_url, ["add", "--login", "ann", "--role", "analyst", "--password-stdin"], monkeypatch,
                   PASSWORD + "\n") == 0

    async def not_found(self: UserRepo, login: str) -> None:
        return None

    monkeypatch.setattr(UserRepo, "get_by_login", not_found)          # імітує паралельну вставку
    capsys.readouterr()
    assert run_cli(db_url, ["add", "--login", "ann", "--role", "admin", "--password-stdin"], monkeypatch,
                   PASSWORD + "-2\n") == 2
    out, err = capsys.readouterr()
    assert "already exists" in err
    assert "INSERT" not in out + err and "$2b$" not in out + err and PASSWORD not in out + err
    assert [r.action for r in audit_rows(users_db)] == ["user.create"]
    with users_db.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM app_user")).scalar_one() == 1


async def lock_admins_then_probe(db_url: str) -> tuple[list[str], str | None]:
    """S1 (роль fuzzhelm_app) бере рядки admin через `admins_for_update` і тримає транзакцію; S2 з
    lock_timeout 200 мс пробує взяти рядок другого admin — поки S1 не закомітила, це неможливо."""
    eng1 = make_engine(db_url, null_pool=True, role=APP_ROLE)
    eng2 = make_engine(db_url, null_pool=True, role=APP_ROLE)
    try:
        async with session_scope(session_factory(eng1)) as s1:
            locked = [u.login or "" for u in await UserRepo(s1).admins_for_update()]
            async with eng2.connect() as c2:
                tx = await c2.begin()
                await c2.execute(text("SET LOCAL lock_timeout = '200ms'"))
                try:
                    await c2.execute(text("SELECT id FROM app_user WHERE login = 'second' FOR UPDATE"))
                    state = None
                except DBAPIError as e:
                    state = getattr(e.orig, "sqlstate", None)
                await tx.rollback()
        return locked, state
    finally:
        await eng1.dispose()
        await eng2.dispose()


def test_user_set_role_locks_admin_rows_against_concurrent_demotion(
        db_url: str, users_db: Engine, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    """«Останнього admin понизити не можна» тримається і під конкуренцією: set-role блокує рядки всіх admin
    (SELECT … FOR UPDATE) до COMMIT, тож паралельне пониження іншого admin чекає і бачить уже одного."""
    for login in ("root", "second"):
        assert run_cli(db_url, ["add", "--login", login, "--role", "admin", "--password-stdin"], monkeypatch,
                       PASSWORD + "\n") == 0
    locked, state = asyncio.run(lock_admins_then_probe(db_url))
    assert locked == ["root", "second"]
    assert state == "55P03"                           # lock_not_available: рядок admin заблоковано
    assert run_cli(db_url, ["set-role", "--login", "second", "--role", "analyst"], monkeypatch) == 0
    assert run_cli(db_url, ["set-role", "--login", "root", "--role", "analyst"], monkeypatch) == 2
    assert "last admin" in capsys.readouterr().err
    with users_db.connect() as conn:
        admins = conn.execute(text("SELECT login FROM app_user WHERE role = 'admin'")).scalars().all()
    assert admins == ["root"]
