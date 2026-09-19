"""Репозиторій користувачів (app_user): створення з bcrypt-хешем, пошук за логіном, автентифікація.

Найменування: storage/repositories/user.py
Призначення: 4 ролі (operator, analyst, auditor, admin); пароль ніколи не зберігається і не
логується — лише bcrypt-хеш (passlib). Перевірка пароля — у сталому часі (passlib.verify).
Автор: Андрій Жук, 2026.

bcrypt обрізає пароль до 72 байт (обмеження алгоритму): довший пароль відхиляємо явно, щоб
«пароль + будь-який хвіст» не проходив перевірку.
"""

from __future__ import annotations

from dataclasses import dataclass

from passlib.context import CryptContext
from sqlalchemy import func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from fuzzhelm.core.enums import Role
from fuzzhelm.storage.models import AppUserModel, table_of
from fuzzhelm.storage.repositories.common import from_mapping

_T = table_of(AppUserModel)
BCRYPT_MAX_BYTES = 72

PWD_CONTEXT = CryptContext(schemes=["bcrypt"], deprecated="auto", bcrypt__rounds=12)


@dataclass(frozen=True, slots=True)
class UserRow:
    id: int
    login: str | None
    pwd_hash: str | None
    role: str | None
    created_at_ns: int | None

    @property
    def role_enum(self) -> Role:
        if self.role is None:
            raise ValueError(f"user {self.login!r} has no role")
        return Role(self.role)


def _check_password(password: str) -> None:
    if not password:
        raise ValueError("password must not be empty")
    if len(password.encode("utf-8")) > BCRYPT_MAX_BYTES:
        raise ValueError(f"password longer than {BCRYPT_MAX_BYTES} bytes is not supported by bcrypt")


def hash_password(password: str, context: CryptContext = PWD_CONTEXT) -> str:
    _check_password(password)
    return str(context.hash(password))


def verify_password(password: str, pwd_hash: str | None, context: CryptContext = PWD_CONTEXT) -> bool:
    if not pwd_hash or not password or len(password.encode("utf-8")) > BCRYPT_MAX_BYTES:
        return False
    return bool(context.verify(password, pwd_hash))


class UserRepo:
    def __init__(self, session: AsyncSession, context: CryptContext = PWD_CONTEXT) -> None:
        self.s = session
        self.ctx = context

    async def create(self, login: str, password: str, role: Role | str) -> UserRow:
        res = await self.s.execute(insert(_T).values(
            login=login, pwd_hash=hash_password(password, self.ctx), role=Role(role).value,
            created_at=func.now(),
        ).returning(_T))
        return from_mapping(UserRow, res.mappings().one())

    async def get(self, user_id: int) -> UserRow | None:
        m = (await self.s.execute(select(_T).where(_T.c.id == user_id))).mappings().one_or_none()
        return None if m is None else from_mapping(UserRow, m)

    async def get_by_login(self, login: str) -> UserRow | None:
        m = (await self.s.execute(select(_T).where(_T.c.login == login))).mappings().one_or_none()
        return None if m is None else from_mapping(UserRow, m)

    async def authenticate(self, login: str, password: str) -> UserRow | None:
        """Користувач, якщо логін існує і пароль правильний; інакше None (без розрізнення причин)."""
        user = await self.get_by_login(login)
        if user is None:
            # вирівнюємо час відповіді: хеш рахується і для неіснуючого логіна
            self.ctx.dummy_verify()
            return None
        return user if verify_password(password, user.pwd_hash, self.ctx) else None

    async def set_password(self, user_id: int, password: str) -> None:
        pwd_hash = hash_password(password, self.ctx)
        await self.s.execute(update(_T).where(_T.c.id == user_id).values(pwd_hash=pwd_hash))

    async def set_role(self, user_id: int, role: Role | str) -> None:
        await self.s.execute(update(_T).where(_T.c.id == user_id).values(role=Role(role).value))

    async def admins_for_update(self) -> list[UserRow]:
        """Адміністратори з блокуванням їхніх рядків до кінця транзакції (SELECT … FOR UPDATE, за id).

        Без блокування дві паралельні зміни ролі (READ COMMITTED) обидві бачили б «лишається ще один admin» і
        разом прибрали б останнього. З блокуванням друга чекає на першу, а тоді PostgreSQL перечитує умову
        `role = 'admin'` для оновлених рядків і бачить уже меншу множину. Порядок за id — без взаємоблокувань.
        """
        q = select(_T).where(_T.c.role == Role.ADMIN.value).order_by(_T.c.id).with_for_update()
        res = await self.s.execute(q)
        return [from_mapping(UserRow, m) for m in res.mappings()]

    async def list(self) -> list[UserRow]:
        res = await self.s.execute(select(_T).order_by(_T.c.id))
        return [from_mapping(UserRow, m) for m in res.mappings()]
