"""Репозиторій позицій (position): відкриття, оновлення, закриття з причиною виходу.

Найменування: storage/repositories/position.py
Призначення: журнал угод прогону — від відкриття до ExitReason (SIGNAL/STOP/TP/RISK_VETO/HALT/LIQUIDATION).
Автор: Андрій Жук, 2026.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from fuzzhelm.core.enums import ExitReason, Side
from fuzzhelm.storage.models import PositionModel, table_of
from fuzzhelm.storage.repositories.common import chunks, from_mapping, ns_to_dt_opt, to_numeric

_T = table_of(PositionModel)
INSERT_CHUNK = 2_000


def position_values(*, run_id: UUID | None, instrument_id: int | None, side: Side | int,
                    qty: Decimal, avg_entry: Decimal, opened_at_ns: int | None,
                    leverage: Decimal | float | None = None, allocated_margin: Decimal | None = None,
                    stop_price: Decimal | None = None, tp_price: Decimal | None = None,
                    liq_price: Decimal | None = None, closed_at_ns: int | None = None,
                    exit_reason: ExitReason | str | None = None, realized_pnl: Decimal | None = None,
                    funding_paid: Decimal | None = None,
                    max_adverse_excursion: Decimal | None = None) -> dict[str, Any]:
    """Рядок position цілком (відкриття + закриття) для пакетного запису готового прогону (insert_many).

    Порожні realized_pnl/funding_paid → 0 (як server_default)."""
    return {
        "run_id": run_id, "instrument_id": instrument_id, "side": int(side), "qty": qty,
        "avg_entry": avg_entry, "leverage": to_numeric(leverage), "allocated_margin": allocated_margin,
        "stop_price": stop_price, "tp_price": tp_price, "liq_price": liq_price,
        "realized_pnl": realized_pnl if realized_pnl is not None else Decimal(0),
        "funding_paid": funding_paid if funding_paid is not None else Decimal(0),
        "max_adverse_excursion": max_adverse_excursion, "opened_at": ns_to_dt_opt(opened_at_ns),
        "closed_at": ns_to_dt_opt(closed_at_ns),
        "exit_reason": None if exit_reason is None else ExitReason(exit_reason).value,
    }

# поля, які можна змінювати під час життя позиції (ідентичність і відкриття — ні)
MUTABLE_NUMERIC: frozenset[str] = frozenset({
    "qty", "avg_entry", "leverage", "allocated_margin", "stop_price", "tp_price", "liq_price",
    "realized_pnl", "funding_paid", "max_adverse_excursion",
})


@dataclass(frozen=True, slots=True)
class PositionRow:
    id: int
    run_id: UUID | None
    instrument_id: int | None
    side: int | None
    qty: Decimal | None
    avg_entry: Decimal | None
    leverage: Decimal | None
    allocated_margin: Decimal | None
    stop_price: Decimal | None
    tp_price: Decimal | None
    liq_price: Decimal | None
    realized_pnl: Decimal | None
    funding_paid: Decimal | None
    max_adverse_excursion: Decimal | None
    opened_at_ns: int | None
    closed_at_ns: int | None
    exit_reason: str | None


class PositionRepo:
    def __init__(self, session: AsyncSession) -> None:
        self.s = session

    async def open(self, *, run_id: UUID | None, instrument_id: int | None, side: Side | int, qty: Decimal,
                   avg_entry: Decimal, opened_at_ns: int, leverage: Decimal | float | None = None,
                   allocated_margin: Decimal | None = None, stop_price: Decimal | None = None,
                   tp_price: Decimal | None = None, liq_price: Decimal | None = None) -> int:
        res = await self.s.execute(insert(_T).values(
            run_id=run_id, instrument_id=instrument_id, side=int(side), qty=qty, avg_entry=avg_entry,
            leverage=to_numeric(leverage), allocated_margin=allocated_margin, stop_price=stop_price,
            tp_price=tp_price, liq_price=liq_price, opened_at=ns_to_dt_opt(opened_at_ns),
        ).returning(_T.c.id))
        return int(res.scalar_one())

    async def insert_many(self, rows: Sequence[Mapping[str, Any]]) -> int:
        """Пакетний запис рядків `position_values(...)` (executemany частинами по INSERT_CHUNK)."""
        for part in chunks(rows, INSERT_CHUNK):
            await self.s.execute(insert(_T), [dict(r) for r in part])
        return len(rows)

    async def update(self, position_id: int, **fields: Decimal | float | int | None) -> PositionRow:
        """Змінити числові поля (MUTABLE_NUMERIC); невідоме поле → ValueError."""
        unknown = set(fields) - MUTABLE_NUMERIC
        if unknown:
            raise ValueError(f"cannot update position fields {sorted(unknown)}")
        return await self._update(position_id, {k: to_numeric(v) for k, v in fields.items()})

    async def close(self, position_id: int, *, closed_at_ns: int, exit_reason: ExitReason | str,
                    realized_pnl: Decimal | None = None, funding_paid: Decimal | None = None) -> PositionRow:
        values: dict[str, Any] = {
            "closed_at": ns_to_dt_opt(closed_at_ns), "exit_reason": ExitReason(exit_reason).value}
        if realized_pnl is not None:
            values["realized_pnl"] = realized_pnl
        if funding_paid is not None:
            values["funding_paid"] = funding_paid
        return await self._update(position_id, values)

    async def _update(self, position_id: int, values: dict[str, Any]) -> PositionRow:
        res = await self.s.execute(update(_T).where(_T.c.id == position_id).values(**values).returning(_T))
        m = res.mappings().one_or_none()
        if m is None:
            raise LookupError(f"position {position_id} not found")
        return from_mapping(PositionRow, m)

    async def get(self, position_id: int) -> PositionRow | None:
        m = (await self.s.execute(select(_T).where(_T.c.id == position_id))).mappings().one_or_none()
        return None if m is None else from_mapping(PositionRow, m)

    async def open_positions(self, run_id: UUID, instrument_id: int | None = None) -> list[PositionRow]:
        q = select(_T).where(_T.c.run_id == run_id, _T.c.closed_at.is_(None))
        if instrument_id is not None:
            q = q.where(_T.c.instrument_id == instrument_id)
        res = await self.s.execute(q.order_by(_T.c.id))
        return [from_mapping(PositionRow, m) for m in res.mappings()]

    async def list_for_run(self, run_id: UUID) -> list[PositionRow]:
        res = await self.s.execute(select(_T).where(_T.c.run_id == run_id).order_by(_T.c.id))
        return [from_mapping(PositionRow, m) for m in res.mappings()]
