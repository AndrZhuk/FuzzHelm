"""Репозиторій рішень ядра з повним трасуванням (decision) — джерело /explain.

Найменування: storage/repositories/decision.py
Призначення: зберегти входи T/R/V, узгодженість і κ, u_raw/u_final, JSONB виходів детекторів,
ступенів належності і спрацьованих правил, а також ціль сайзера; прочитати рішення за id.
Автор: Андрій Жук, 2026.

float ядра (u, κ, T/R/V) перетворюються в NUMERIC через найкоротше repr (common.to_numeric);
PostgreSQL округлює їх до масштабу колонки (NUMERIC(8,5) / NUMERIC(6,4)). Точні значення лишаються
в JSONB (detector_outputs/memberships/fired_rules — float як є).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import func, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from fuzzhelm.storage.models import DecisionModel, table_of
from fuzzhelm.storage.repositories.common import chunks, from_mapping, ns_to_dt_opt, to_numeric

_T = table_of(DecisionModel)
INSERT_CHUNK = 1_000

Num = Decimal | float | int | None


@dataclass(frozen=True, slots=True)
class DecisionRecord:
    """Вхід для запису одного рішення (поля — колонки decision, час — ns)."""

    run_id: UUID
    instrument_id: int | None
    open_time_ns: int | None
    detector_outputs: list[dict[str, Any]] = field(default_factory=list)
    memberships: dict[str, Any] = field(default_factory=dict)
    fired_rules: list[dict[str, Any]] = field(default_factory=list)
    t_in: Num = None
    r_in: Num = None
    v_in: Num = None
    agreement: Num = None
    kappa: Num = None
    u_raw: Num = None
    u_final: Num = None
    target_side: int | None = None
    target_qty: Decimal | None = None
    binding_constraint: str | None = None
    stop_price: Decimal | None = None
    tp_price: Decimal | None = None
    liq_price: Decimal | None = None

    @classmethod
    def from_trace(cls, trace: Any, *, run_id: UUID, instrument_id: int | None,
                   target_side: int | None = None, target_qty: Decimal | None = None,
                   binding_constraint: str | None = None, stop_price: Decimal | None = None,
                   tp_price: Decimal | None = None, liq_price: Decimal | None = None) -> DecisionRecord:
        """З decision.trace.DecisionTrace (або його to_dict()) — без імпорту пакета decision.

        agreement := A_g (1 − нормована ентропія), kappa := κ; ціль сайзера береться з явних аргументів.
        """
        d: Mapping[str, Any] = trace.to_dict() if hasattr(trace, "to_dict") else trace
        inputs = d.get("inputs") or {}
        agr = d.get("agreement") or {}
        outputs = [
            {k: o.get(k) for k in ("name", "group", "s", "c", "weight", "features")}
            for o in d.get("detector_outputs") or []
        ]
        fired = [
            {k: r.get(k) for k in ("rule_id", "alpha", "consequent", "antecedent")}
            for r in d.get("fired_rules") or []
        ]
        return cls(
            run_id=run_id, instrument_id=instrument_id, open_time_ns=d.get("open_time_ns"),
            detector_outputs=outputs, memberships=dict(d.get("memberships") or {}), fired_rules=fired,
            t_in=inputs.get("T"), r_in=inputs.get("R"), v_in=inputs.get("V"),
            agreement=agr.get("A_g"), kappa=d.get("kappa"), u_raw=d.get("u_raw"), u_final=d.get("u_final"),
            target_side=target_side, target_qty=target_qty, binding_constraint=binding_constraint,
            stop_price=stop_price, tp_price=tp_price, liq_price=liq_price,
        )

    def to_values(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id, "instrument_id": self.instrument_id,
            "open_time": ns_to_dt_opt(self.open_time_ns),
            "t_in": to_numeric(self.t_in), "r_in": to_numeric(self.r_in), "v_in": to_numeric(self.v_in),
            "agreement": to_numeric(self.agreement), "kappa": to_numeric(self.kappa),
            "u_raw": to_numeric(self.u_raw), "u_final": to_numeric(self.u_final),
            "detector_outputs": self.detector_outputs, "memberships": self.memberships,
            "fired_rules": self.fired_rules, "target_side": self.target_side,
            "target_qty": self.target_qty, "binding_constraint": self.binding_constraint,
            "stop_price": self.stop_price, "tp_price": self.tp_price, "liq_price": self.liq_price,
        }


@dataclass(frozen=True, slots=True)
class DecisionRow:
    id: int
    run_id: UUID
    instrument_id: int | None
    open_time_ns: int | None
    t_in: Decimal | None
    r_in: Decimal | None
    v_in: Decimal | None
    agreement: Decimal | None
    kappa: Decimal | None
    u_raw: Decimal | None
    u_final: Decimal | None
    detector_outputs: list[dict[str, Any]]
    memberships: dict[str, Any]
    fired_rules: list[dict[str, Any]]
    target_side: int | None
    target_qty: Decimal | None
    binding_constraint: str | None
    stop_price: Decimal | None
    tp_price: Decimal | None
    liq_price: Decimal | None


class DecisionRepo:
    def __init__(self, session: AsyncSession) -> None:
        self.s = session

    async def insert(self, rec: DecisionRecord) -> int:
        res = await self.s.execute(insert(_T).values(**rec.to_values()).returning(_T.c.id))
        return int(res.scalar_one())

    async def insert_many(self, recs: Sequence[DecisionRecord]) -> list[int]:
        """Пачка рішень; id повертаються у порядку входу (FK для sim_order.decision_id)."""
        ids: list[int] = []
        for part in chunks(recs, INSERT_CHUNK):
            stmt = insert(_T).returning(_T.c.id, sort_by_parameter_order=True)
            res = await self.s.execute(stmt, [r.to_values() for r in part])
            ids.extend(int(i) for i in res.scalars().all())
        return ids

    async def get(self, decision_id: int) -> DecisionRow | None:
        """Рішення за id — усе, що потрібно для /explain."""
        m = (await self.s.execute(select(_T).where(_T.c.id == decision_id))).mappings().one_or_none()
        return None if m is None else from_mapping(DecisionRow, m)

    async def get_at(self, run_id: UUID, instrument_id: int, open_time_ns: int) -> DecisionRow | None:
        q = select(_T).where(_T.c.run_id == run_id, _T.c.instrument_id == instrument_id,
                             _T.c.open_time == ns_to_dt_opt(open_time_ns))
        m = (await self.s.execute(q)).mappings().one_or_none()
        return None if m is None else from_mapping(DecisionRow, m)

    async def list_for_run(self, run_id: UUID, *, after_id: int | None = None, limit: int = 500,
                           nonzero_only: bool = False) -> list[DecisionRow]:
        q = select(_T).where(_T.c.run_id == run_id)
        if after_id is not None:
            q = q.where(_T.c.id > after_id)
        if nonzero_only:
            q = q.where(_T.c.target_side != 0)
        res = await self.s.execute(q.order_by(_T.c.id).limit(limit))
        return [from_mapping(DecisionRow, m) for m in res.mappings()]

    async def find_by_rule(self, run_id: UUID, rule_id: str, *, limit: int = 500) -> list[DecisionRow]:
        """Рішення, де спрацювало правило: fired_rules @> '[{"rule_id": ...}]' (GIN jsonb_path_ops)."""
        q = select(_T).where(_T.c.run_id == run_id, _T.c.fired_rules.contains([{"rule_id": rule_id}]))
        res = await self.s.execute(q.order_by(_T.c.id).limit(limit))
        return [from_mapping(DecisionRow, m) for m in res.mappings()]

    async def count(self, run_id: UUID) -> int:
        q = select(func.count()).select_from(_T).where(_T.c.run_id == run_id)
        return int((await self.s.execute(q)).scalar_one())
