"""Репозиторій погодинного скору якості даних (dq_score).

Найменування: storage/repositories/dq.py
Призначення: upsert рядка (instrument_id, hour_start) — перерахунок тієї самої години замінює
попередній результат (ідемпотентно); історія Q для панелі якості та StaleDataGuard.
Автор: Андрій Жук, 2026.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from fuzzhelm.storage.models import DqScoreModel, table_of
from fuzzhelm.storage.repositories.common import from_mapping, ns_to_dt, to_numeric

_T = table_of(DqScoreModel)
_METRICS: tuple[str, ...] = (
    "expected_buckets", "observed_buckets", "invalid_count", "gap_seconds", "lag_p95_ms",
    "completeness", "validity", "timeliness", "continuity", "score",
)
_NUMERIC: frozenset[str] = frozenset({
    "gap_seconds", "lag_p95_ms", "completeness", "validity", "timeliness", "continuity", "score",
})


@dataclass(frozen=True, slots=True)
class DqRow:
    instrument_id: int
    hour_start_ns: int
    expected_buckets: int | None = None
    observed_buckets: int | None = None
    invalid_count: int | None = None
    gap_seconds: Decimal | float | None = None
    lag_p95_ms: Decimal | float | None = None
    completeness: Decimal | float | None = None
    validity: Decimal | float | None = None
    timeliness: Decimal | float | None = None
    continuity: Decimal | float | None = None
    score: Decimal | float | None = None


class DqRepo:
    def __init__(self, session: AsyncSession) -> None:
        self.s = session

    async def upsert(self, row: DqRow) -> None:
        """INSERT ... ON CONFLICT (instrument_id, hour_start) DO UPDATE (скор ∉ [0,1] відхилить CHECK)."""
        values: dict[str, object] = {
            "instrument_id": row.instrument_id, "hour_start": ns_to_dt(row.hour_start_ns)}
        for name in _METRICS:
            v = getattr(row, name)
            values[name] = to_numeric(v) if name in _NUMERIC else v
        stmt = pg_insert(_T).values(**values)
        stmt = stmt.on_conflict_do_update(index_elements=["instrument_id", "hour_start"],
                                          set_={name: stmt.excluded[name] for name in _METRICS})
        await self.s.execute(stmt)

    async def range(self, instrument_id: int, ts_from_ns: int | None = None,
                    ts_to_ns: int | None = None) -> list[DqRow]:
        q = select(_T).where(_T.c.instrument_id == instrument_id)
        if ts_from_ns is not None:
            q = q.where(_T.c.hour_start >= ns_to_dt(ts_from_ns))
        if ts_to_ns is not None:
            q = q.where(_T.c.hour_start < ns_to_dt(ts_to_ns))
        res = await self.s.execute(q.order_by(_T.c.hour_start))
        return [from_mapping(DqRow, m) for m in res.mappings()]

    async def latest(self, instrument_id: int) -> DqRow | None:
        res = await self.s.execute(select(_T).where(_T.c.instrument_id == instrument_id)
                                   .order_by(_T.c.hour_start.desc()).limit(1))
        m = res.mappings().one_or_none()
        return None if m is None else from_mapping(DqRow, m)
