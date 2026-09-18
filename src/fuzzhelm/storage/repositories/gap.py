"""Репозиторій прогалин інжесту (ingest_gap): відкрити, оновити статус, список відкритих.

Найменування: storage/repositories/gap.py
Призначення: доказ надійності конвеєра — кожна виявлена прогалина має запис і кінцевий статус
(FILLED / PARTIAL / UNFILLABLE); нічний добір бере відкриті через частковий індекс ix_gap_open.
Автор: Андрій Жук, 2026.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from fuzzhelm.core.enums import GapDetectorKind, GapStatus, Stream
from fuzzhelm.storage.models import IngestGapModel, table_of
from fuzzhelm.storage.repositories.common import from_mapping, ns_to_dt, ns_to_dt_opt

_T = table_of(IngestGapModel)

OPEN_STATUSES: frozenset[GapStatus] = frozenset({GapStatus.OPEN, GapStatus.FILLING})
TERMINAL_STATUSES: frozenset[GapStatus] = frozenset(
    {GapStatus.FILLED, GapStatus.PARTIAL, GapStatus.UNFILLABLE})


@dataclass(frozen=True, slots=True)
class GapRow:
    id: int
    instrument_id: int | None
    stream: str | None
    ts_lo_ns: int | None
    ts_hi_ns: int | None
    expected_count: int | None
    filled_rows: int | None
    detector: str | None
    status: str | None
    attempts: int | None
    detected_at_ns: int | None
    closed_at_ns: int | None


class GapRepo:
    def __init__(self, session: AsyncSession) -> None:
        self.s = session

    async def open(self, instrument_id: int, stream: Stream | str, ts_lo_ns: int, ts_hi_ns: int, *,
                   expected_count: int | None = None, detector: GapDetectorKind | str = GapDetectorKind.TIME,
                   detected_at_ns: int | None = None) -> int:
        """Зареєструвати прогалину [ts_lo, ts_hi] зі статусом OPEN; повертає id."""
        values: dict[str, object] = {
            "instrument_id": instrument_id, "stream": Stream(stream).value,
            "ts_lo": ns_to_dt(ts_lo_ns), "ts_hi": ns_to_dt(ts_hi_ns),
            "expected_count": expected_count, "detector": GapDetectorKind(detector).value,
            "status": GapStatus.OPEN.value,
        }
        if detected_at_ns is not None:
            values["detected_at"] = ns_to_dt(detected_at_ns)
        res = await self.s.execute(insert(_T).values(**values).returning(_T.c.id))
        return int(res.scalar_one())

    async def update_status(self, gap_id: int, status: GapStatus | str, *, filled_rows: int | None = None,
                            count_attempt: bool = True, at_ns: int | None = None) -> GapRow:
        """Новий статус; кінцевий статус ставить closed_at (at_ns або now() СУБД), відкритий — знімає.

        count_attempt=True збільшує attempts (кожна спроба добору — одна спроба).
        """
        st = GapStatus(status)
        values: dict[str, object] = {"status": st.value}
        if filled_rows is not None:
            values["filled_rows"] = filled_rows
        if count_attempt:
            values["attempts"] = _T.c.attempts + 1
        if st in TERMINAL_STATUSES:
            values["closed_at"] = ns_to_dt_opt(at_ns) if at_ns is not None else func.now()
        else:
            values["closed_at"] = None
        res = await self.s.execute(update(_T).where(_T.c.id == gap_id).values(**values).returning(_T))
        m = res.mappings().one_or_none()
        if m is None:
            raise LookupError(f"ingest_gap {gap_id} not found")
        return from_mapping(GapRow, m)

    async def get(self, gap_id: int) -> GapRow | None:
        m = (await self.s.execute(select(_T).where(_T.c.id == gap_id))).mappings().one_or_none()
        return None if m is None else from_mapping(GapRow, m)

    async def list_open(self, instrument_id: int | None = None, *, limit: int = 1_000) -> list[GapRow]:
        """Відкриті прогалини (OPEN/FILLING), найновіші першими — предикат збігається з ix_gap_open."""
        q = select(_T).where(_T.c.status.in_([s.value for s in OPEN_STATUSES]))
        if instrument_id is not None:
            q = q.where(_T.c.instrument_id == instrument_id)
        res = await self.s.execute(q.order_by(_T.c.detected_at.desc()).limit(limit))
        return [from_mapping(GapRow, m) for m in res.mappings()]

    async def stats(self) -> dict[str, int]:
        """Кількість прогалин за статусом (для health-панелі і звіту)."""
        res = await self.s.execute(select(_T.c.status, func.count()).group_by(_T.c.status))
        return {str(s): int(n) for s, n in res.all()}
