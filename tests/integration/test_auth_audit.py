"""Користувачі (bcrypt, 4 ролі) і журнал аудиту з before/after (ревізія 0003_auth_audit).

Найменування: tests/integration/test_auth_audit.py
Автор: Андрій Жук, 2026.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from passlib.context import CryptContext
from sqlalchemy import insert, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from tests.integration._data import T0_NS

from fuzzhelm.core.enums import Role
from fuzzhelm.risk.journal import AuditRecord
from fuzzhelm.storage.models import APP_ROLE, AppUserModel
from fuzzhelm.storage.repositories.audit import AuditRepo
from fuzzhelm.storage.repositories.common import (
    SQLSTATE_CHECK_VIOLATION,
    SQLSTATE_INSUFFICIENT_PRIVILEGE,
    SQLSTATE_UNIQUE_VIOLATION,
    sqlstate,
)
from fuzzhelm.storage.repositories.user import PWD_CONTEXT, UserRepo, hash_password, verify_password
from fuzzhelm.storage.session import session_scope

pytestmark = pytest.mark.integration

FAST = CryptContext(schemes=["bcrypt"], bcrypt__rounds=4)  # мінімальна вартість — лише для швидкості тестів


async def test_user_bcrypt_hash_roles_and_authentication(factory: async_sessionmaker[AsyncSession]) -> None:
    async with session_scope(factory) as s:
        repo = UserRepo(s, FAST)
        admin = await repo.create("admin", "s3cret-пароль", Role.ADMIN)
        await repo.create("analyst", "an4lyst", "analyst")
        assert admin.pwd_hash is not None and admin.pwd_hash.startswith("$2b$04$")
        assert "s3cret" not in admin.pwd_hash and admin.role_enum is Role.ADMIN and admin.created_at_ns
        assert (await repo.authenticate("admin", "s3cret-пароль")) == admin
        assert await repo.authenticate("admin", "s3cret-пароль!") is None
        assert await repo.authenticate("nobody", "s3cret-пароль") is None
        await repo.set_password(admin.id, "n3w")
        assert await repo.authenticate("admin", "s3cret-пароль") is None
        assert (await repo.authenticate("admin", "n3w")) is not None
        await repo.set_role(admin.id, Role.AUDITOR)
        assert (await repo.get(admin.id)).role == "auditor"  # type: ignore[union-attr]
        assert [u.login for u in await repo.list()] == ["admin", "analyst"]
        with pytest.raises(ValueError):
            await repo.create("x", "p", "root")  # роль поза Role
        with pytest.raises(ValueError, match="72 bytes"):
            await repo.create("y", "я" * 40, Role.OPERATOR)  # 80 байт UTF-8 — bcrypt обрізав би
    with pytest.raises(IntegrityError) as ei:
        async with session_scope(factory) as s:
            await UserRepo(s, FAST).create("admin", "other", Role.OPERATOR)
    assert sqlstate(ei.value) == SQLSTATE_UNIQUE_VIOLATION
    with pytest.raises(IntegrityError) as ei:  # CHECK ролі на рівні СУБД
        async with session_scope(factory) as s:
            await s.execute(insert(AppUserModel.__table__).values(login="z", pwd_hash="h", role="root"))
    assert sqlstate(ei.value) == SQLSTATE_CHECK_VIOLATION
    # робочий контекст — bcrypt із вартістю 12
    h = hash_password("pw", PWD_CONTEXT)
    assert h.startswith("$2b$12$") and verify_password("pw", h) and not verify_password("pW", h)


async def test_audit_log_before_after_and_append_only(factory: async_sessionmaker[AsyncSession]) -> None:
    async with session_scope(factory) as s:
        user = await UserRepo(s, FAST).create("admin", "pw", Role.ADMIN)
        repo = AuditRepo(s)
        aid = await repo.append(
            "risk_limits.update",
            "risk_limits",
            before={"max_gross_leverage": Decimal("3")},
            after={"max_gross_leverage": Decimal("2.5")},
            user_id=user.id,
            ip="10.0.0.7",
        )
        await repo.append_record(
            AuditRecord(
                ts_ns=T0_NS,
                action="risk.release",
                target="risk_state",
                actor_role=Role.ADMIN,
                actor="admin",
                before={"state": "HALTED"},
                after={"state": "COOLDOWN"},
            ),
            user_id=user.id,
        )
    async with session_scope(factory) as s:
        repo = AuditRepo(s)
        rows = await repo.list(target="risk_limits")
        assert [r.id for r in rows] == [aid]
        assert rows[0].before_json == {"max_gross_leverage": "3"} and rows[0].after_json == {
            "max_gross_leverage": "2.5"
        }
        assert rows[0].ip == "10.0.0.7" and rows[0].ts_ns is not None and rows[0].user_id == user.id
        rel = await repo.list(action="risk.release")
        assert rel[0].ts_ns == T0_NS and rel[0].after_json == {
            "state": "COOLDOWN",
            "_actor": {"role": "admin", "login": "admin"},
        }
        assert len(await repo.list(user_id=user.id)) == 2
    # роль застосунку може дописувати аудит, але не переписувати і не стирати його
    async with session_scope(factory) as s:
        await s.execute(text(f"SET LOCAL ROLE {APP_ROLE}"))
        await AuditRepo(s).append("login", "auth", after={"ok": True})
    for sql in ("UPDATE audit_log SET action = 'x'", "DELETE FROM audit_log"):
        async with factory() as s:
            await s.execute(text(f"SET LOCAL ROLE {APP_ROLE}"))
            with pytest.raises(DBAPIError) as ei:
                await s.execute(text(sql))
            assert sqlstate(ei.value) == SQLSTATE_INSUFFICIENT_PRIVILEGE
            await s.rollback()
