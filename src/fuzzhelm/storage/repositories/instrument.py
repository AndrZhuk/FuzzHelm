"""Репозиторій довідника інструментів (upsert за venue + symbol_venue).

Найменування: storage/repositories/instrument.py
Призначення: специфікація контракту з exchangeInfo (tick/step/minNotional/mmr) → таблиця instrument;
мапа symbol_canon → id для репозиторіїв свічок, рішень і ризик-подій.
Автор: Андрій Жук, 2026.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from fuzzhelm.core.dto import Instrument
from fuzzhelm.core.enums import ContractType, Venue
from fuzzhelm.storage.models import InstrumentModel, table_of
from fuzzhelm.storage.repositories.common import from_mapping, ns_to_dt_opt, trim_decimal

_T = table_of(InstrumentModel)


@dataclass(frozen=True, slots=True)
class InstrumentRow:
    id: int
    venue: str
    symbol_venue: str
    symbol_canon: str
    base_asset: str | None
    quote_asset: str | None
    contract_type: str | None
    tick_size: Decimal
    step_size: Decimal
    min_notional: Decimal
    mmr: Decimal
    maint_amount: Decimal
    max_leverage: int | None
    active: bool | None
    spec_fetched_at_ns: int | None

    def to_dto(self) -> Instrument:
        """Назад у канонічний DTO (Decimal нормалізуються до мінімального масштабу без експоненти)."""
        return Instrument(
            venue=Venue(self.venue), symbol_venue=self.symbol_venue, symbol_canon=self.symbol_canon,
            base_asset=self.base_asset or "", quote_asset=self.quote_asset or "",
            contract_type=ContractType(self.contract_type or ContractType.PERP),
            tick_size=trim_decimal(self.tick_size), step_size=trim_decimal(self.step_size),
            min_notional=trim_decimal(self.min_notional), mmr=trim_decimal(self.mmr),
            maint_amount=trim_decimal(self.maint_amount),
            max_leverage=self.max_leverage if self.max_leverage is not None else 3,
        )


class InstrumentRepo:
    def __init__(self, session: AsyncSession) -> None:
        self.s = session

    async def upsert(self, inst: Instrument, *, spec_fetched_at_ns: int | None = None,
                     active: bool = True) -> int:
        """INSERT ... ON CONFLICT (venue, symbol_venue) DO UPDATE; повертає id інструмента."""
        values = {
            "venue": inst.venue.value, "symbol_venue": inst.symbol_venue, "symbol_canon": inst.symbol_canon,
            "base_asset": inst.base_asset, "quote_asset": inst.quote_asset,
            "contract_type": inst.contract_type.value, "tick_size": inst.tick_size,
            "step_size": inst.step_size, "min_notional": inst.min_notional, "mmr": inst.mmr,
            "maint_amount": inst.maint_amount, "max_leverage": inst.max_leverage, "active": active,
            "spec_fetched_at": ns_to_dt_opt(spec_fetched_at_ns),
        }
        stmt = pg_insert(_T).values(**values)
        update_cols = {k: stmt.excluded[k] for k in values if k not in ("venue", "symbol_venue")}
        stmt = stmt.on_conflict_do_update(index_elements=["venue", "symbol_venue"], set_=update_cols)
        res = await self.s.execute(stmt.returning(_T.c.id))
        return int(res.scalar_one())

    async def get(self, instrument_id: int) -> InstrumentRow | None:
        res = await self.s.execute(select(_T).where(_T.c.id == instrument_id))
        m = res.mappings().one_or_none()
        return None if m is None else from_mapping(InstrumentRow, m)

    async def get_by_canon(self, symbol_canon: str) -> InstrumentRow | None:
        res = await self.s.execute(select(_T).where(_T.c.symbol_canon == symbol_canon))
        m = res.mappings().one_or_none()
        return None if m is None else from_mapping(InstrumentRow, m)

    async def get_by_venue_symbol(self, venue: Venue | str, symbol_venue: str) -> InstrumentRow | None:
        v = venue.value if isinstance(venue, Venue) else venue
        res = await self.s.execute(select(_T).where(_T.c.venue == v, _T.c.symbol_venue == symbol_venue))
        m = res.mappings().one_or_none()
        return None if m is None else from_mapping(InstrumentRow, m)

    async def list(self, *, active_only: bool = False) -> list[InstrumentRow]:
        q = select(_T).order_by(_T.c.id)
        if active_only:
            q = q.where(_T.c.active.is_(True))
        res = await self.s.execute(q)
        return [from_mapping(InstrumentRow, m) for m in res.mappings()]

    async def id_map(self) -> dict[str, int]:
        """symbol_canon → id (для перетворення DTO з полем `instrument` у FK)."""
        res = await self.s.execute(select(_T.c.symbol_canon, _T.c.id))
        return {sym: int(i) for sym, i in res.all()}

    async def resolve_id(self, symbol_canon: str) -> int:
        res = await self.s.execute(select(_T.c.id).where(_T.c.symbol_canon == symbol_canon))
        i = res.scalar_one_or_none()
        if i is None:
            raise LookupError(f"unknown instrument {symbol_canon!r}; upsert it first")
        return int(i)
