"""Репозиторій кривої капіталу (equity_point): пакетний запис і читання для панелі та хешу.

Найменування: storage/repositories/equity.py
Призначення: бектест дає точку на кожен бар (~65 тис. на 45 днів 1m), тому запис — COPY
(бінарний протокол, без ON CONFLICT: пара (run_id, ts) унікальна за PK, повтор — помилка).
Автор: Андрій Жук, 2026.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import func, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from fuzzhelm.storage.models import EquityPointModel, table_of
from fuzzhelm.storage.repositories.common import (
    chunks,
    copy_records,
    enum_value,
    from_mapping,
    ns_to_dt,
    ns_to_dt_opt,
    supports_copy,
    to_money18,
    to_numeric,
)

_T = table_of(EquityPointModel)
COLUMNS: tuple[str, ...] = (
    "run_id", "ts", "equity", "cash", "unrealized", "gross_exposure", "leverage", "drawdown",
    "risk_state", "kappa", "var95", "cvar95",
)
COPY_THRESHOLD = 500
INSERT_CHUNK = 2_000

Num = Decimal | float | int | None


@dataclass(frozen=True, slots=True)
class EquityPoint:
    """Точка кривої капіталу (вхід запису). float допустимі для leverage/drawdown/kappa."""

    ts_ns: int
    equity: Decimal
    cash: Decimal | None = None
    unrealized: Decimal | None = None
    gross_exposure: Decimal | None = None
    leverage: Num = None
    drawdown: Num = None
    risk_state: Any = None
    kappa: Num = None
    var95: Decimal | None = None
    cvar95: Decimal | None = None

    def record(self, run_id: UUID) -> tuple[Any, ...]:
        # гроші NUMERIC(38,18) квантуються HALF_EVEN тут, а не округленням СУБД («половина від нуля»):
        # інакше equity_hash, перерахований із БД, розійшовся б із записаним на рівно-половинних значеннях
        return (
            run_id, ns_to_dt(self.ts_ns), to_money18(self.equity), to_money18(self.cash),
            to_money18(self.unrealized), to_money18(self.gross_exposure), to_numeric(self.leverage),
            to_numeric(self.drawdown), enum_value(self.risk_state), to_numeric(self.kappa),
            to_money18(self.var95), to_money18(self.cvar95),
        )


@dataclass(frozen=True, slots=True)
class EquityRow:
    run_id: UUID
    ts_ns: int
    equity: Decimal | None
    cash: Decimal | None
    unrealized: Decimal | None
    gross_exposure: Decimal | None
    leverage: Decimal | None
    drawdown: Decimal | None
    risk_state: str | None
    kappa: Decimal | None
    var95: Decimal | None
    cvar95: Decimal | None


class EquityRepo:
    def __init__(self, session: AsyncSession) -> None:
        self.s = session

    async def insert_many(self, run_id: UUID, points: Sequence[EquityPoint], *,
                          use_copy: bool | None = None) -> int:
        if not points:
            return 0
        records = [p.record(run_id) for p in points]
        want_copy = len(records) >= COPY_THRESHOLD if use_copy is None else use_copy
        if want_copy and supports_copy(self.s):
            return await copy_records(self.s, "equity_point", COLUMNS, records)
        for part in chunks(records, INSERT_CHUNK):
            await self.s.execute(insert(_T), [dict(zip(COLUMNS, r, strict=True)) for r in part])
        return len(records)

    async def curve(self, run_id: UUID, ts_from_ns: int | None = None,
                    ts_to_ns: int | None = None) -> list[EquityRow]:
        q = select(_T).where(_T.c.run_id == run_id)
        if ts_from_ns is not None:
            q = q.where(_T.c.ts >= ns_to_dt_opt(ts_from_ns))
        if ts_to_ns is not None:
            q = q.where(_T.c.ts < ns_to_dt_opt(ts_to_ns))
        res = await self.s.execute(q.order_by(_T.c.ts))
        return [from_mapping(EquityRow, m) for m in res.mappings()]

    async def equity_series(self, run_id: UUID) -> tuple[list[int], list[Decimal]]:
        """(ts_ns, equity) за часом — вхід backtest.manifest.equity_hash.

        Записане значення = quantize_internal(вхід) (HALF_EVEN, 1e−18), тож хеш із БД збігається
        з хешем вхідної кривої для будь-яких Decimal, включно з рівно-половинними хвостами.
        """
        rows = await self.curve(run_id)
        return [r.ts_ns for r in rows], [r.equity if r.equity is not None else Decimal(0) for r in rows]

    async def count(self, run_id: UUID) -> int:
        q = select(func.count()).select_from(_T).where(_T.c.run_id == run_id)
        return int((await self.s.execute(q)).scalar_one())
