"""Репозиторій стратегій: правила як дані, версіонування і активація.

Найменування: storage/repositories/strategy.py
Призначення: кожна зміна правил/МФ через UI — НОВА версія (version = max + 1 у межах name),
старі версії незмінні (на них посилаються паспорти прогонів). rules_hash = BLAKE2b-256 від
канонічної пари текстів (rules_yaml, membership_yaml) і UNIQUE: ідентичний набір не дублюється.
Автор: Андрій Жук, 2026.

Валідацію YAML (45 правил, терми, w = 1.0) робить fuzzy.rules.load_rulebase у шарі API до запису;
репозиторій зберігає вже перевірені тексти дослівно.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from fuzzhelm.core.digest import digest
from fuzzhelm.core.errors import FuzzHelmError
from fuzzhelm.storage.models import StrategyModel, table_of
from fuzzhelm.storage.repositories.common import from_mapping

_T = table_of(StrategyModel)


class StrategyConflictError(FuzzHelmError):
    """Такий самий набір правил уже збережено (UNIQUE rules_hash) — API відповідає 409."""

    def __init__(self, existing_id: int, name: str, version: int) -> None:
        super().__init__(f"identical rules already stored as {name!r} v{version} (id={existing_id})")
        self.existing_id = existing_id
        self.name = name
        self.version = version


@dataclass(frozen=True, slots=True)
class StrategyRow:
    id: int
    name: str
    version: int
    rules_yaml: str
    membership_yaml: str
    rules_hash: bytes
    created_by: str | None
    created_at_ns: int | None
    is_active: bool | None


def rules_hash(rules_yaml: str, membership_yaml: str) -> bytes:
    """BLAKE2b-256(canonical_json([rules_yaml, membership_yaml])) — однозначне кодування пари текстів."""
    return digest([rules_yaml, membership_yaml], size=32)


class StrategyRepo:
    def __init__(self, session: AsyncSession) -> None:
        self.s = session

    async def create_version(self, name: str, rules_yaml: str, membership_yaml: str, *,
                             created_by: str | None = None, activate: bool = False) -> StrategyRow:
        """Нова версія стратегії `name` (1 для першої). Той самий набір текстів → StrategyConflictError."""
        h = rules_hash(rules_yaml, membership_yaml)
        existing = await self.get_by_hash(h)
        if existing is not None:
            raise StrategyConflictError(existing.id, existing.name, existing.version)
        # серіалізуємо конкурентні створення версій одного name до кінця транзакції (max+1 без гонки)
        await self.s.execute(select(func.pg_advisory_xact_lock(func.hashtextextended(name, 0))))
        cur = (await self.s.execute(select(func.max(_T.c.version)).where(_T.c.name == name))).scalar_one()
        version = (cur or 0) + 1
        res = await self.s.execute(insert(_T).values(
            name=name, version=version, rules_yaml=rules_yaml, membership_yaml=membership_yaml,
            rules_hash=h, created_by=created_by, is_active=False,
        ).returning(_T))
        row = from_mapping(StrategyRow, res.mappings().one())
        if activate:
            row = await self.activate(row.id)
        return row

    async def activate(self, strategy_id: int) -> StrategyRow:
        """Зробити версію активною; решта версій того самого name — неактивні (одним UPDATE)."""
        name = (await self.s.execute(select(_T.c.name).where(_T.c.id == strategy_id))).scalar_one_or_none()
        if name is None:
            raise LookupError(f"strategy {strategy_id} not found")
        await self.s.execute(update(_T).where(_T.c.name == name).values(is_active=(_T.c.id == strategy_id)))
        got = await self.get(strategy_id)
        assert got is not None
        return got

    async def get(self, strategy_id: int) -> StrategyRow | None:
        m = (await self.s.execute(select(_T).where(_T.c.id == strategy_id))).mappings().one_or_none()
        return None if m is None else from_mapping(StrategyRow, m)

    async def get_by_hash(self, h: bytes) -> StrategyRow | None:
        m = (await self.s.execute(select(_T).where(_T.c.rules_hash == h))).mappings().one_or_none()
        return None if m is None else from_mapping(StrategyRow, m)

    async def get_version(self, name: str, version: int) -> StrategyRow | None:
        m = (await self.s.execute(
            select(_T).where(_T.c.name == name, _T.c.version == version))).mappings().one_or_none()
        return None if m is None else from_mapping(StrategyRow, m)

    async def latest(self, name: str) -> StrategyRow | None:
        m = (await self.s.execute(select(_T).where(_T.c.name == name)
                                  .order_by(_T.c.version.desc()).limit(1))).mappings().one_or_none()
        return None if m is None else from_mapping(StrategyRow, m)

    async def get_active(self, name: str | None = None) -> StrategyRow | None:
        q = select(_T).where(_T.c.is_active.is_(True))
        if name is not None:
            q = q.where(_T.c.name == name)
        m = (await self.s.execute(q.order_by(_T.c.id.desc()).limit(1))).mappings().one_or_none()
        return None if m is None else from_mapping(StrategyRow, m)

    async def list_versions(self, name: str) -> list[StrategyRow]:
        res = await self.s.execute(select(_T).where(_T.c.name == name).order_by(_T.c.version))
        return [from_mapping(StrategyRow, m) for m in res.mappings()]

    async def list_names(self) -> list[str]:
        res = await self.s.execute(select(_T.c.name).distinct().order_by(_T.c.name))
        return [str(n) for n in res.scalars()]

    async def delete(self, strategy_id: int) -> bool:
        """Видалити версію, на яку не посилається жоден прогін (інакше IntegrityError від FK run)."""
        res = await self.s.execute(delete(_T).where(_T.c.id == strategy_id))
        return bool(res.rowcount)  # type: ignore[attr-defined]
