"""Репозиторій ризик-подій (risk_event): кожен вердикт правила і кожен перехід автомата.

Найменування: storage/repositories/risk.py
Призначення: відповідь на «чому угоду зменшено» дає таблиця: rule, verdict, factor, observed,
limit_value, state_from/state_to, dwell_bars, actor, payload. Записи приходять із risk.journal.RiskJournal
(через BufferedSink) і пишуться пачкою.
Автор: Андрій Жук, 2026.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from fuzzhelm.core.enums import VerdictKind
from fuzzhelm.storage.models import RiskEventModel, table_of
from fuzzhelm.storage.repositories.common import (
    chunks,
    enum_value,
    from_mapping,
    ns_to_dt,
    ns_to_dt_opt,
    to_numeric,
)

_T = table_of(RiskEventModel)
INSERT_CHUNK = 2_000
STATE_RULE = "risk_state"       # rule переходу автомата (risk.journal.RiskJournal.record_transition)


class RiskRecordLike(Protocol):
    """Структурний тип risk.journal.RiskEventRecord."""

    ts_ns: int
    rule: str
    verdict: Any
    factor: Decimal | None
    observed: Decimal | None
    limit_value: Decimal | None
    instrument: str | None
    state_from: Any
    state_to: Any
    dwell_bars: int | None
    actor: str | None
    payload: Mapping[str, Any]
    run_id: UUID | None


@dataclass(frozen=True, slots=True)
class RiskEventRow:
    id: int
    run_id: UUID | None
    ts_ns: int | None
    instrument_id: int | None
    rule: str
    verdict: str | None
    factor: Decimal | None
    observed: Decimal | None
    limit_value: Decimal | None
    state_from: str | None
    state_to: str | None
    dwell_bars: int | None
    actor: str | None
    payload: dict[str, Any] | None


def record_values(r: RiskRecordLike, *, run_id: UUID | None = None,
                  instrument_ids: Mapping[str, int] | None = None) -> dict[str, Any]:
    """RiskEventRecord → значення колонок; instrument (symbol_canon) → instrument_id за мапою."""
    iid: int | None = None
    if r.instrument is not None:
        if instrument_ids is None or r.instrument not in instrument_ids:
            raise LookupError(f"no instrument_id for {r.instrument!r}; pass instrument_ids")
        iid = instrument_ids[r.instrument]
    return {
        "run_id": r.run_id if r.run_id is not None else run_id, "ts": ns_to_dt(r.ts_ns),
        "instrument_id": iid, "rule": r.rule, "verdict": enum_value(r.verdict),
        "factor": to_numeric(r.factor), "observed": to_numeric(r.observed),
        "limit_value": to_numeric(r.limit_value), "state_from": enum_value(r.state_from),
        "state_to": enum_value(r.state_to), "dwell_bars": r.dwell_bars, "actor": r.actor,
        "payload": dict(r.payload),
    }


class RiskEventRepo:
    def __init__(self, session: AsyncSession) -> None:
        self.s = session

    async def insert_many(self, records: Sequence[RiskRecordLike], *, run_id: UUID | None = None,
                          instrument_ids: Mapping[str, int] | None = None) -> int:
        """Пачка записів (напр. BufferedSink.drain()); run_id — для записів без власного run_id."""
        for part in chunks(records, INSERT_CHUNK):
            rows = [record_values(r, run_id=run_id, instrument_ids=instrument_ids) for r in part]
            await self.s.execute(insert(_T), rows)
        return len(records)

    async def insert(self, record: RiskRecordLike, *, run_id: UUID | None = None,
                     instrument_ids: Mapping[str, int] | None = None) -> int:
        values = record_values(record, run_id=run_id, instrument_ids=instrument_ids)
        res = await self.s.execute(insert(_T).values(**values).returning(_T.c.id))
        return int(res.scalar_one())

    async def list_for_run(self, run_id: UUID, *, since_ns: int | None = None, limit: int = 500,
                           rule: str | None = None) -> list[RiskEventRow]:
        """Найновіші першими (індекс ix_risk_run_ts)."""
        q = select(_T).where(_T.c.run_id == run_id)
        if since_ns is not None:
            q = q.where(_T.c.ts >= ns_to_dt_opt(since_ns))
        if rule is not None:
            q = q.where(_T.c.rule == rule)
        res = await self.s.execute(q.order_by(_T.c.ts.desc(), _T.c.id.desc()).limit(limit))
        return [from_mapping(RiskEventRow, m) for m in res.mappings()]

    async def vetoes(self, run_id: UUID, *, limit: int = 500) -> list[RiskEventRow]:
        """Журнал відхилень (частковий індекс ix_risk_veto)."""
        q = select(_T).where(_T.c.run_id == run_id, _T.c.verdict == VerdictKind.VETO.value)
        res = await self.s.execute(q.order_by(_T.c.ts.desc(), _T.c.id.desc()).limit(limit))
        return [from_mapping(RiskEventRow, m) for m in res.mappings()]

    async def transitions(self, run_id: UUID) -> list[RiskEventRow]:
        q = select(_T).where(_T.c.run_id == run_id, _T.c.rule == STATE_RULE).order_by(_T.c.ts, _T.c.id)
        res = await self.s.execute(q)
        return [from_mapping(RiskEventRow, m) for m in res.mappings()]
