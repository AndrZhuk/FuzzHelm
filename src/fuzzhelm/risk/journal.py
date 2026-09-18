"""Журнал ризик-подій (форма рядка таблиці risk_event) і записи аудиту (форма audit_log).

Найменування: risk/journal.py
Призначення: аудит кожного відхилення — КОЖНА перевірка правила (не лише VETO) і кожен перехід автомата
пишеться з observed і limit, щоб на питання «чому угоду зменшено» відповідала таблиця, а не пам'ять.
Автор: Андрій Жук, 2026.

RiskJournal не знає про БД: `sink` (напр. репозиторій storage) отримує кожен запис у момент додавання;
опційно запис дублюється в EventJournal (хеш-ланцюг, kind="risk_event"). Для grid-бектесту з мільйонами
барів — keep=False (записи не накопичуються в пам'яті, лише йдуть у sink).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from uuid import UUID

from fuzzhelm.core.digest import to_canonical
from fuzzhelm.core.enums import RiskState, Role, VerdictKind
from fuzzhelm.core.journal import EventJournal

if TYPE_CHECKING:
    from fuzzhelm.risk.state import Transition
    from fuzzhelm.risk.verdict import RuleVerdict

RISK_EVENT_KIND = "risk_event"


@dataclass(frozen=True, slots=True)
class RiskEventRecord:
    """Рядок таблиці risk_event (limit_value — межа правила). Імена полів = колонки DDL, крім ts_ns (→ ts) і
    instrument (канонічний символ → instrument_id): ці два відображає шар storage."""

    ts_ns: int
    rule: str
    verdict: VerdictKind | None
    factor: Decimal | None
    observed: Decimal | None
    limit_value: Decimal | None
    instrument: str | None = None
    state_from: RiskState | None = None
    state_to: RiskState | None = None
    dwell_bars: int | None = None
    actor: str | None = None
    payload: Mapping[str, Any] = field(default_factory=dict)
    run_id: UUID | None = None

    def to_row(self) -> dict[str, Any]:
        """Канонічний словник (Decimal → рядок без експоненти, enum → значення) з іменами колонок DDL."""
        row: dict[str, Any] = {
            "run_id": self.run_id, "ts_ns": self.ts_ns, "instrument": self.instrument,
            "rule": self.rule, "verdict": self.verdict, "factor": self.factor,
            "observed": self.observed, "limit_value": self.limit_value,
            "state_from": self.state_from, "state_to": self.state_to,
            "dwell_bars": self.dwell_bars, "actor": self.actor, "payload": dict(self.payload),
        }
        out = to_canonical(row)
        assert isinstance(out, dict)
        return out


@dataclass(frozen=True, slots=True)
class AuditRecord:
    """Рядок audit_log (user_id/ip додає шар API): хто, що, над чим, стан до і після."""

    ts_ns: int
    action: str
    target: str
    actor_role: Role
    actor: str | None
    before: Mapping[str, Any]
    after: Mapping[str, Any]

    def to_row(self) -> dict[str, Any]:
        out = to_canonical({
            "ts_ns": self.ts_ns, "action": self.action, "target": self.target,
            "actor_role": self.actor_role, "actor": self.actor,
            "before_json": dict(self.before), "after_json": dict(self.after),
        })
        assert isinstance(out, dict)
        return out


AuditSink = Callable[[AuditRecord], None]


class RiskJournal:
    """Append-only журнал ризик-подій."""

    def __init__(self, run_id: UUID | None = None, sink: Callable[[RiskEventRecord], None] | None = None,
                 *, event_journal: EventJournal | None = None, keep: bool = True) -> None:
        self.run_id = run_id
        self._sink = sink
        self._events = event_journal
        self._keep = keep
        self._count = 0
        self.records: list[RiskEventRecord] = []

    def __len__(self) -> int:
        return self._count

    def append(self, record: RiskEventRecord) -> RiskEventRecord:
        self._count += 1
        if self._keep:
            self.records.append(record)
        if self._sink is not None:
            self._sink(record)
        if self._events is not None:
            self._events.append(RISK_EVENT_KIND, record.to_row(), record.ts_ns, record.ts_ns)
        return record

    @property
    def has_consumers(self) -> bool:
        """Чи хтось читає записи (keep / sink / EventJournal). Ні — лише лічильник (grid-бектест)."""
        return self._keep or self._sink is not None or self._events is not None

    def record_rule(self, ts_ns: int, instrument: str | None, rv: RuleVerdict) -> RiskEventRecord | None:
        if not self.has_consumers:
            self._count += 1                             # запис нікому не потрібен — не будуємо його
            return None
        return self.append(RiskEventRecord(
            ts_ns=ts_ns, rule=rv.rule, verdict=rv.verdict.kind, factor=rv.verdict.factor_dec,
            observed=rv.observed, limit_value=rv.limit, instrument=instrument,
            payload={**rv.payload, "halt": rv.halt}, run_id=self.run_id,
        ))

    def record_transition(self, tr: Transition, instrument: str | None = None) -> RiskEventRecord:
        return self.append(RiskEventRecord(
            ts_ns=tr.ts_ns, rule="risk_state", verdict=None, factor=None,
            observed=tr.drawdown, limit_value=None, instrument=instrument,
            state_from=tr.state_from, state_to=tr.state_to, dwell_bars=tr.dwell_bars,
            actor=tr.actor, payload=tr.payload(), run_id=self.run_id,
        ))

    def by_rule(self, rule: str) -> list[RiskEventRecord]:
        return [r for r in self.records if r.rule == rule]

    def vetoes(self) -> list[RiskEventRecord]:
        return [r for r in self.records if r.verdict is VerdictKind.VETO]
