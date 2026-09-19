"""Репозиторій ордерів (sim_order): створення з рішення ядра, квитанції і виконання.

Найменування: storage/repositories/order.py
Призначення: кожен ордер посилається на рішення (decision_id NOT NULL + FK, ST-01) і має
client_order_id UNIQUE — повторна подача того самого ордера відхиляється на рівні СУБД.
Автор: Андрій Жук, 2026.

Часткові виконання накопичуються атомарно одним UPDATE:
    filled' = filled + q;  avg' = (avg·filled + p·q) / filled';  fee' = fee + f;
    status = FILLED, якщо filled' ≥ qty, інакше PARTIAL.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import case, func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from fuzzhelm.core.dto import Fill, OrderAck, OrderRequest
from fuzzhelm.core.enums import OrderStatus
from fuzzhelm.storage.models import SimOrderModel, table_of
from fuzzhelm.storage.repositories.common import chunks, enum_value, from_mapping, ns_to_dt, ns_to_dt_opt

_T = table_of(SimOrderModel)
INSERT_CHUNK = 2_000


def order_values(req: OrderRequest, *, decision_id: int, run_id: UUID | None = None,
                 instrument_id: int | None = None, status: OrderStatus | str = OrderStatus.NEW,
                 reject_code: str | None = None, venue_order_id: str | None = None,
                 filled_qty: Decimal | None = None, avg_fill_price: Decimal | None = None,
                 fee: Decimal | None = None, slippage_bps: Decimal | None = None, liquidity: Any = None,
                 ts_filled_ns: int | None = None) -> dict[str, Any]:
    """Рядок sim_order з УЖЕ накопиченим станом виконання (пакетний запис готового прогону, insert_many).

    Порожні filled_qty/fee → 0 (як server_default), щоб executemany мав однаковий набір колонок.
    """
    return {
        "run_id": run_id, "decision_id": decision_id, "client_order_id": req.client_order_id,
        "venue_order_id": venue_order_id, "instrument_id": instrument_id, "side": int(req.side),
        "otype": req.otype.value, "qty": req.qty,
        "filled_qty": filled_qty if filled_qty is not None else Decimal(0),
        "avg_fill_price": avg_fill_price, "fee": fee if fee is not None else Decimal(0),
        "slippage_bps": slippage_bps, "liquidity": enum_value(liquidity),
        "status": OrderStatus(status).value, "reject_code": reject_code,
        "ts_created": ns_to_dt(req.ts_created_ns), "ts_filled": ns_to_dt_opt(ts_filled_ns),
    }


@dataclass(frozen=True, slots=True)
class OrderRow:
    id: int
    run_id: UUID | None
    decision_id: int
    client_order_id: UUID
    venue_order_id: str | None
    instrument_id: int | None
    side: int | None
    otype: str | None
    qty: Decimal | None
    filled_qty: Decimal | None
    avg_fill_price: Decimal | None
    fee: Decimal | None
    slippage_bps: Decimal | None
    liquidity: str | None
    status: str | None
    reject_code: str | None
    ts_created_ns: int | None
    ts_filled_ns: int | None


class OrderRepo:
    def __init__(self, session: AsyncSession) -> None:
        self.s = session

    async def create(self, req: OrderRequest, *, decision_id: int, run_id: UUID | None = None,
                     instrument_id: int | None = None, status: OrderStatus = OrderStatus.NEW,
                     reject_code: str | None = None) -> int:
        """Записати ордер ядра; decision_id обов'язковий (NOT NULL + FK на decision)."""
        res = await self.s.execute(insert(_T).values(
            run_id=run_id, decision_id=decision_id, client_order_id=req.client_order_id,
            instrument_id=instrument_id, side=int(req.side), otype=req.otype.value, qty=req.qty,
            status=OrderStatus(status).value, reject_code=reject_code, ts_created=ns_to_dt(req.ts_created_ns),
        ).returning(_T.c.id))
        return int(res.scalar_one())

    async def insert_many(self, rows: Sequence[Mapping[str, Any]]) -> int:
        """Пакетний запис рядків `order_values(...)` (executemany частинами по INSERT_CHUNK).

        decision_id — NOT NULL + FK і тут: рядок без рішення відхилить СУБД (ST-01)."""
        for part in chunks(rows, INSERT_CHUNK):
            await self.s.execute(insert(_T), [dict(r) for r in part])
        return len(rows)

    async def apply_ack(self, ack: OrderAck) -> OrderRow:
        values: dict[str, object] = {"status": ack.status.value, "reject_code": ack.reject_code}
        if ack.venue_order_id is not None:
            values["venue_order_id"] = ack.venue_order_id
        return await self._update(ack.client_order_id, values)

    async def apply_fill(self, fill: Fill) -> OrderRow:
        filled = func.coalesce(_T.c.filled_qty, 0)
        new_filled = filled + fill.qty
        values: dict[str, object] = {
            "filled_qty": new_filled,
            "avg_fill_price": (
                (func.coalesce(_T.c.avg_fill_price, 0) * filled + fill.price * fill.qty) / new_filled),
            "fee": func.coalesce(_T.c.fee, 0) + fill.fee,
            "slippage_bps": fill.slippage_bps,
            "liquidity": enum_value(fill.liquidity),
            "ts_filled": ns_to_dt(fill.ts_fill_ns),
            "status": case((new_filled >= _T.c.qty, OrderStatus.FILLED.value),
                           else_=OrderStatus.PARTIAL.value),
        }
        if fill.venue_order_id is not None:
            values["venue_order_id"] = fill.venue_order_id
        return await self._update(fill.client_order_id, values)

    async def cancel(self, client_order_id: UUID, *, at_ns: int | None = None) -> OrderRow:
        values: dict[str, object] = {"status": OrderStatus.CANCELED.value}
        if at_ns is not None:
            values["ts_filled"] = ns_to_dt_opt(at_ns)
        return await self._update(client_order_id, values)

    async def _update(self, client_order_id: UUID, values: dict[str, object]) -> OrderRow:
        res = await self.s.execute(
            update(_T).where(_T.c.client_order_id == client_order_id).values(**values).returning(_T))
        m = res.mappings().one_or_none()
        if m is None:
            raise LookupError(f"order {client_order_id} not found")
        return from_mapping(OrderRow, m)

    async def get(self, order_id: int) -> OrderRow | None:
        m = (await self.s.execute(select(_T).where(_T.c.id == order_id))).mappings().one_or_none()
        return None if m is None else from_mapping(OrderRow, m)

    async def get_by_client_id(self, client_order_id: UUID) -> OrderRow | None:
        q = select(_T).where(_T.c.client_order_id == client_order_id)
        m = (await self.s.execute(q)).mappings().one_or_none()
        return None if m is None else from_mapping(OrderRow, m)

    async def list_for_decision(self, decision_id: int) -> list[OrderRow]:
        res = await self.s.execute(select(_T).where(_T.c.decision_id == decision_id).order_by(_T.c.id))
        return [from_mapping(OrderRow, m) for m in res.mappings()]

    async def list_for_run(self, run_id: UUID, *, limit: int = 10_000) -> list[OrderRow]:
        res = await self.s.execute(select(_T).where(_T.c.run_id == run_id).order_by(_T.c.id).limit(limit))
        return [from_mapping(OrderRow, m) for m in res.mappings()]
