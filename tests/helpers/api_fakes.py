"""Реалізація сервісів API в пам'яті для офлайн e2e-тестів (tests/e2e/test_api.py) і тестів планувальника.

Найменування: tests/helpers/api_fakes.py
Призначення: ті самі методи й типи рядків (storage.repositories.*Row), що й у репозиторіях PostgreSQL,
але над словниками. Одиниця роботи транзакційна: при винятку стан відкочується до знімка, а події
pg_notify доставляються лише після «COMMIT» — як у PostgreSQL. Паролі хешуються bcrypt із вартістю 4
(швидко для тестів; робоча вартість 12 — у storage.repositories.user.PWD_CONTEXT).
Автор: Андрій Жук, 2026.
"""

from __future__ import annotations

import contextlib
import copy
from collections.abc import AsyncIterator, Iterable, Mapping
from dataclasses import dataclass, field, replace
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

import orjson
from passlib.context import CryptContext

from fuzzhelm.api.auth import LoginRateLimiter
from fuzzhelm.api.backtests import BacktestJob, JobStatus
from fuzzhelm.api.limits import FileLimitsStore
from fuzzhelm.api.live import LIVE_CHANNEL, LiveHub
from fuzzhelm.api.services import ApiServices
from fuzzhelm.config import Settings
from fuzzhelm.core.clock import ManualClock, SeededIdGenerator
from fuzzhelm.core.enums import GapStatus, Role, RunKind, RunStatus, VerdictKind
from fuzzhelm.storage.repositories import (
    AuditRow,
    CandlePage,
    CandleRow,
    DecisionRecord,
    DecisionRow,
    DqRow,
    EquityRow,
    GapRow,
    InstrumentRow,
    RiskEventRow,
    RunRow,
    StrategyRow,
    UserRow,
)
from fuzzhelm.storage.repositories.risk import exact_factor_payload
from fuzzhelm.storage.repositories.strategy import StrategyConflictError, rules_hash
from fuzzhelm.storage.repositories.user import hash_password
from fuzzhelm.storage.session import json_dumps

FAST_PWD = CryptContext(schemes=["bcrypt"], deprecated="auto", bcrypt__rounds=4)
T0_NS = 1_758_153_600_000_000_000  # 2025-09-18 00:00:00 UTC
NS_PER_MIN = 60_000_000_000
JWT_SECRET = "test-secret-" + "x" * 32


def jsonb(obj: Any) -> Any:
    """Те, що повернула б колонка JSONB (Decimal → рядок, кортежі → списки)."""
    return None if obj is None else orjson.loads(json_dumps(obj))


def numeric(x: Any, places: int) -> Decimal | None:
    """Округлення колонки NUMERIC(p, places): PostgreSQL — «половина від нуля»."""
    if x is None:
        return None
    d = x if isinstance(x, Decimal) else Decimal(repr(float(x)))
    return d.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)


@dataclass
class MemoryState:
    users: dict[int, UserRow] = field(default_factory=dict)
    audit: list[AuditRow] = field(default_factory=list)
    strategies: list[StrategyRow] = field(default_factory=list)
    runs: dict[UUID, RunRow] = field(default_factory=dict)
    metrics: dict[UUID, dict[str, float | None]] = field(default_factory=dict)
    equity: dict[UUID, list[EquityRow]] = field(default_factory=dict)
    decisions: dict[int, DecisionRow] = field(default_factory=dict)
    candles: dict[tuple[int, str], dict[int, CandleRow]] = field(default_factory=dict)
    instruments: dict[int, InstrumentRow] = field(default_factory=dict)
    dq: dict[tuple[int, int], DqRow] = field(default_factory=dict)
    gaps: dict[int, GapRow] = field(default_factory=dict)
    risk_events: list[RiskEventRow] = field(default_factory=list)
    control: list[tuple[str, str, dict[str, Any]]] = field(default_factory=list)
    seq: dict[str, int] = field(default_factory=dict)
    now_ns: int = T0_NS

    def next_id(self, name: str) -> int:
        self.seq[name] = self.seq.get(name, 0) + 1
        return self.seq[name]


# ------------------------------------------------------------------ сховища


class MemUsers:
    def __init__(self, st: MemoryState) -> None:
        self.st = st

    async def create(self, login: str, password: str, role: Role | str) -> UserRow:
        row = UserRow(
            id=self.st.next_id("user"),
            login=login,
            pwd_hash=hash_password(password, FAST_PWD),
            role=Role(role).value,
            created_at_ns=self.st.now_ns,
        )
        self.st.users[row.id] = row
        return row

    async def get(self, user_id: int) -> UserRow | None:
        return self.st.users.get(user_id)

    async def get_by_login(self, login: str) -> UserRow | None:
        return next((u for u in self.st.users.values() if u.login == login), None)

    async def set_role(self, user_id: int, role: Role | str) -> None:
        self.st.users[user_id] = replace(self.st.users[user_id], role=Role(role).value)


class MemAudit:
    def __init__(self, st: MemoryState) -> None:
        self.st = st

    async def append(
        self,
        action: str,
        target: str,
        *,
        before: Mapping[str, Any] | None = None,
        after: Mapping[str, Any] | None = None,
        user_id: int | None = None,
        ip: str | None = None,
        ts_ns: int | None = None,
    ) -> int:
        row = AuditRow(
            id=self.st.next_id("audit"),
            ts_ns=ts_ns if ts_ns is not None else self.st.now_ns,
            user_id=user_id,
            action=action,
            target=target,
            before_json=jsonb(before),
            after_json=jsonb(after),
            ip=ip,
        )
        self.st.audit.append(row)
        return row.id

    async def list(
        self,
        *,
        target: str | None = None,
        action: str | None = None,
        user_id: int | None = None,
        limit: int = 200,
    ) -> list[AuditRow]:
        rows = [
            r
            for r in reversed(self.st.audit)
            if (target is None or r.target == target)
            and (action is None or r.action == action)
            and (user_id is None or r.user_id == user_id)
        ]
        return rows[:limit]


class MemStrategies:
    def __init__(self, st: MemoryState) -> None:
        self.st = st

    async def create_version(
        self,
        name: str,
        rules_yaml: str,
        membership_yaml: str,
        *,
        created_by: str | None = None,
        activate: bool = False,
    ) -> StrategyRow:
        h = rules_hash(rules_yaml, membership_yaml)
        dup = next((s for s in self.st.strategies if s.rules_hash == h), None)
        if dup is not None:
            raise StrategyConflictError(dup.id, dup.name, dup.version)
        version = max((s.version for s in self.st.strategies if s.name == name), default=0) + 1
        row = StrategyRow(
            id=self.st.next_id("strategy"),
            name=name,
            version=version,
            rules_yaml=rules_yaml,
            membership_yaml=membership_yaml,
            rules_hash=h,
            created_by=created_by,
            created_at_ns=self.st.now_ns,
            is_active=False,
        )
        self.st.strategies.append(row)
        return await self.activate(row.id) if activate else row

    async def activate(self, strategy_id: int) -> StrategyRow:
        target = await self.get(strategy_id)
        if target is None:
            raise LookupError(f"strategy {strategy_id} not found")
        self.st.strategies = [
            replace(s, is_active=s.id == strategy_id) if s.name == target.name else s
            for s in self.st.strategies
        ]
        got = await self.get(strategy_id)
        assert got is not None
        return got

    async def get(self, strategy_id: int) -> StrategyRow | None:
        return next((s for s in self.st.strategies if s.id == strategy_id), None)

    async def get_version(self, name: str, version: int) -> StrategyRow | None:
        return next((s for s in self.st.strategies if s.name == name and s.version == version), None)

    async def latest(self, name: str) -> StrategyRow | None:
        rows = await self.list_versions(name)
        return rows[-1] if rows else None

    async def get_active(self, name: str | None = None) -> StrategyRow | None:
        rows = [s for s in self.st.strategies if s.is_active and (name is None or s.name == name)]
        return max(rows, key=lambda s: s.id) if rows else None

    async def list_versions(self, name: str) -> list[StrategyRow]:
        return sorted((s for s in self.st.strategies if s.name == name), key=lambda s: s.version)

    async def list_names(self) -> list[str]:
        return sorted({s.name for s in self.st.strategies})


class MemRuns:
    def __init__(self, st: MemoryState) -> None:
        self.st = st

    async def get(self, run_id: UUID) -> RunRow | None:
        return self.st.runs.get(run_id)

    async def list(
        self, *, kind: RunKind | str | None = None, status: RunStatus | str | None = None, limit: int = 100
    ) -> list[RunRow]:
        rows = [
            r
            for r in self.st.runs.values()
            if (kind is None or r.kind == RunKind(kind).value)
            and (status is None or r.status == RunStatus(status).value)
        ]
        rows.sort(key=lambda r: r.started_at_ns or 0, reverse=True)
        return rows[:limit]

    async def get_metrics(self, run_id: UUID) -> dict[str, float | None]:
        return dict(sorted(self.st.metrics.get(run_id, {}).items()))


class MemEquity:
    def __init__(self, st: MemoryState) -> None:
        self.st = st

    async def curve(
        self, run_id: UUID, ts_from_ns: int | None = None, ts_to_ns: int | None = None
    ) -> list[EquityRow]:
        return [
            p
            for p in self.st.equity.get(run_id, [])
            if (ts_from_ns is None or p.ts_ns >= ts_from_ns) and (ts_to_ns is None or p.ts_ns < ts_to_ns)
        ]


class MemDecisions:
    def __init__(self, st: MemoryState) -> None:
        self.st = st

    async def insert(self, rec: DecisionRecord) -> int:
        did = self.st.next_id("decision")
        self.st.decisions[did] = DecisionRow(
            id=did,
            run_id=rec.run_id,
            instrument_id=rec.instrument_id,
            open_time_ns=rec.open_time_ns,
            t_in=numeric(rec.t_in, 5),
            r_in=numeric(rec.r_in, 5),
            v_in=numeric(rec.v_in, 5),
            agreement=numeric(rec.agreement, 4),
            kappa=numeric(rec.kappa, 4),
            u_raw=numeric(rec.u_raw, 5),
            u_final=numeric(rec.u_final, 5),
            detector_outputs=jsonb(rec.detector_outputs),
            memberships=jsonb(rec.memberships),
            fired_rules=jsonb(rec.fired_rules),
            target_side=rec.target_side,
            target_qty=rec.target_qty,
            binding_constraint=rec.binding_constraint,
            stop_price=rec.stop_price,
            tp_price=rec.tp_price,
            liq_price=rec.liq_price,
            sizing=jsonb(rec.sizing),
            risk=jsonb(rec.risk),
            narrative=rec.narrative,
        )
        return did

    async def get(self, decision_id: int) -> DecisionRow | None:
        return self.st.decisions.get(decision_id)


class MemCandles:
    def __init__(self, st: MemoryState) -> None:
        self.st = st

    def add(self, rows: Iterable[CandleRow]) -> None:
        for r in rows:
            self.st.candles.setdefault((r.instrument_id, r.tf), {})[r.open_time_ns] = r

    async def range(
        self,
        instrument_id: int,
        tf: str,
        ts_from_ns: int | None = None,
        ts_to_ns: int | None = None,
        *,
        after_ns: int | None = None,
        limit: int = 1000,
        closed_only: bool = False,
        descending: bool = False,
    ) -> CandlePage:
        rows = sorted(
            self.st.candles.get((instrument_id, tf), {}).values(),
            key=lambda r: r.open_time_ns,
            reverse=descending,
        )
        rows = [
            r
            for r in rows
            if (ts_from_ns is None or r.open_time_ns >= ts_from_ns)
            and (ts_to_ns is None or r.open_time_ns < ts_to_ns)
            and (not closed_only or r.is_closed)
            and (after_ns is None or (r.open_time_ns < after_ns if descending else r.open_time_ns > after_ns))
        ]
        more = len(rows) > limit
        items = rows[:limit]
        return CandlePage(items=items, next_after_ns=items[-1].open_time_ns if more else None)

    async def latest_open_time_ns(
        self, instrument_id: int, tf: str, *, closed_only: bool = True
    ) -> int | None:
        rows = [
            r.open_time_ns
            for r in self.st.candles.get((instrument_id, tf), {}).values()
            if r.is_closed or not closed_only
        ]
        return max(rows) if rows else None


class MemInstruments:
    def __init__(self, st: MemoryState) -> None:
        self.st = st

    async def get(self, instrument_id: int) -> InstrumentRow | None:
        return self.st.instruments.get(instrument_id)

    async def get_by_canon(self, symbol_canon: str) -> InstrumentRow | None:
        return next((i for i in self.st.instruments.values() if i.symbol_canon == symbol_canon), None)

    async def list(self, *, active_only: bool = False) -> list[InstrumentRow]:
        return [
            i for i in sorted(self.st.instruments.values(), key=lambda i: i.id) if i.active or not active_only
        ]


class MemDq:
    def __init__(self, st: MemoryState) -> None:
        self.st = st

    async def upsert(self, row: DqRow) -> None:
        self.st.dq[(row.instrument_id, row.hour_start_ns)] = row

    async def range(
        self, instrument_id: int, ts_from_ns: int | None = None, ts_to_ns: int | None = None
    ) -> list[DqRow]:
        return [
            r
            for (iid, h), r in sorted(self.st.dq.items())
            if iid == instrument_id
            and (ts_from_ns is None or h >= ts_from_ns)
            and (ts_to_ns is None or h < ts_to_ns)
        ]

    async def latest(self, instrument_id: int) -> DqRow | None:
        rows = await self.range(instrument_id)
        return rows[-1] if rows else None


class MemGaps:
    def __init__(self, st: MemoryState) -> None:
        self.st = st

    async def open(
        self,
        instrument_id: int,
        stream: str,
        ts_lo_ns: int,
        ts_hi_ns: int,
        *,
        expected_count: int | None = None,
        detector: str = "time",
        detected_at_ns: int | None = None,
    ) -> int:
        gid = self.st.next_id("gap")
        self.st.gaps[gid] = GapRow(
            id=gid,
            instrument_id=instrument_id,
            stream=stream,
            ts_lo_ns=ts_lo_ns,
            ts_hi_ns=ts_hi_ns,
            expected_count=expected_count,
            filled_rows=0,
            detector=detector,
            status=GapStatus.OPEN.value,
            attempts=0,
            detected_at_ns=detected_at_ns or self.st.now_ns,
            closed_at_ns=None,
        )
        return gid

    async def update_status(
        self,
        gap_id: int,
        status: GapStatus | str,
        *,
        filled_rows: int | None = None,
        count_attempt: bool = True,
        at_ns: int | None = None,
    ) -> GapRow:
        g = self.st.gaps.get(gap_id)
        if g is None:
            raise LookupError(f"ingest_gap {gap_id} not found")
        st = GapStatus(status)
        terminal = st in (GapStatus.FILLED, GapStatus.PARTIAL, GapStatus.UNFILLABLE)
        g = replace(
            g,
            status=st.value,
            filled_rows=g.filled_rows if filled_rows is None else filled_rows,
            attempts=(g.attempts or 0) + (1 if count_attempt else 0),
            closed_at_ns=(at_ns if at_ns is not None else self.st.now_ns) if terminal else None,
        )
        self.st.gaps[gap_id] = g
        return g

    async def get(self, gap_id: int) -> GapRow | None:
        return self.st.gaps.get(gap_id)

    async def list_open(self, instrument_id: int | None = None, *, limit: int = 1_000) -> list[GapRow]:
        rows = [
            g
            for g in self.st.gaps.values()
            if g.status in ("OPEN", "FILLING") and (instrument_id is None or g.instrument_id == instrument_id)
        ]
        return sorted(rows, key=lambda g: g.detected_at_ns or 0, reverse=True)[:limit]

    async def list_by_status(
        self,
        statuses: Iterable[GapStatus | str],
        *,
        instrument_id: int | None = None,
        max_attempts: int | None = None,
        limit: int = 1_000,
    ) -> list[GapRow]:
        wanted = {GapStatus(s).value for s in statuses}
        rows = [
            g
            for g in self.st.gaps.values()
            if g.status in wanted
            and (instrument_id is None or g.instrument_id == instrument_id)
            and (max_attempts is None or (g.attempts or 0) < max_attempts)
        ]
        return sorted(rows, key=lambda g: (g.detected_at_ns or 0, g.id))[:limit]

    async def list_overlapping(
        self, instrument_id: int, ts_lo_ns: int, ts_hi_ns: int, *, stream: str | None = None
    ) -> list[GapRow]:
        return sorted(
            (
                g
                for g in self.st.gaps.values()
                if g.instrument_id == instrument_id
                and (g.ts_lo_ns or 0) < ts_hi_ns
                and (g.ts_hi_ns or 0) >= ts_lo_ns
                and (stream is None or g.stream == stream)
            ),
            key=lambda g: (g.ts_lo_ns or 0, g.id),
        )

    async def stats(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for g in self.st.gaps.values():
            out[str(g.status)] = out.get(str(g.status), 0) + 1
        return out


class MemRisk:
    def __init__(self, st: MemoryState) -> None:
        self.st = st

    def add(
        self,
        *,
        run_id: UUID,
        ts_ns: int,
        rule: str,
        verdict: VerdictKind | None = None,
        factor: Decimal | None = None,
        observed: Decimal | None = None,
        limit_value: Decimal | None = None,
        state_from: str | None = None,
        state_to: str | None = None,
        dwell_bars: int | None = None,
        payload: Mapping[str, Any] | None = None,
        instrument_id: int | None = 1,
    ) -> RiskEventRow:
        row = RiskEventRow(
            id=self.st.next_id("risk"),
            run_id=run_id,
            ts_ns=ts_ns,
            instrument_id=instrument_id,
            rule=rule,
            verdict=None if verdict is None else verdict.value,
            factor=numeric(factor, 4),
            observed=observed,
            limit_value=limit_value,
            state_from=state_from,
            state_to=state_to,
            dwell_bars=dwell_bars,
            actor=None,
            payload=jsonb(exact_factor_payload(factor, payload or {})),
        )
        self.st.risk_events.append(row)
        return row

    async def list_for_run(
        self, run_id: UUID, *, since_ns: int | None = None, limit: int = 500, rule: str | None = None
    ) -> list[RiskEventRow]:
        rows = [
            r
            for r in self.st.risk_events
            if r.run_id == run_id
            and (since_ns is None or (r.ts_ns or 0) >= since_ns)
            and (rule is None or r.rule == rule)
        ]
        return sorted(rows, key=lambda r: (r.ts_ns or 0, r.id), reverse=True)[:limit]

    async def page_for_run(
        self,
        run_id: UUID,
        *,
        since_ns: int | None = None,
        until_ns: int | None = None,
        rule: str | None = None,
        verdict: VerdictKind | str | None = None,
        before: tuple[int, int] | None = None,
        limit: int = 500,
    ) -> list[RiskEventRow]:
        """Семантика RiskEventRepo.page_for_run: (ts, id) DESC, ts ∈ [since, until), (ts, id) < before."""
        want = None if verdict is None else getattr(verdict, "value", verdict)
        rows = [
            r
            for r in self.st.risk_events
            if r.run_id == run_id
            and r.ts_ns is not None
            and (since_ns is None or r.ts_ns >= since_ns)
            and (until_ns is None or r.ts_ns < until_ns)
            and (rule is None or r.rule == rule)
            and (want is None or r.verdict == want)
            and (before is None or (r.ts_ns, r.id) < before)
        ]
        return sorted(rows, key=lambda r: (r.ts_ns or 0, r.id), reverse=True)[:limit]

    async def vetoes(self, run_id: UUID, *, limit: int = 500) -> list[RiskEventRow]:
        return [r for r in await self.list_for_run(run_id, limit=10**9) if r.verdict == "VETO"][:limit]

    async def transitions(self, run_id: UUID) -> list[RiskEventRow]:
        rows = [r for r in self.st.risk_events if r.run_id == run_id and r.rule == "risk_state"]
        return sorted(rows, key=lambda r: (r.ts_ns or 0, r.id))


class MemoryRepos:
    def __init__(self, st: MemoryState) -> None:
        self.st = st
        self.users = MemUsers(st)
        self.audit = MemAudit(st)
        self.strategies = MemStrategies(st)
        self.runs = MemRuns(st)
        self.equity = MemEquity(st)
        self.decisions = MemDecisions(st)
        self.candles = MemCandles(st)
        self.instruments = MemInstruments(st)
        self.dq = MemDq(st)
        self.gaps = MemGaps(st)
        self.risk = MemRisk(st)
        self.pending: list[tuple[str, str, dict[str, Any]]] = []

    async def notify(self, channel: str, kind: str, payload: Mapping[str, Any]) -> None:
        self.pending.append((channel, kind, jsonb(dict(payload))))


class MemoryDb:
    """Стан + транзакційна одиниця роботи (знімок/відкат, доставка подій після «COMMIT»)."""

    def __init__(self, hub: LiveHub | None = None) -> None:
        self.state = MemoryState()
        self.hub = hub
        self.commits = 0
        self.fail_next_commit = False

    @contextlib.asynccontextmanager
    async def uow(self) -> AsyncIterator[MemoryRepos]:
        snapshot = copy.deepcopy(self.state)
        repos = MemoryRepos(self.state)
        try:
            yield repos
            if self.fail_next_commit:
                self.fail_next_commit = False
                raise OSError("simulated COMMIT failure")
        except BaseException:
            self.state.__dict__.update(snapshot.__dict__)
            raise
        self.commits += 1
        for channel, kind, payload in repos.pending:
            self.state.control.append((channel, kind, payload))
            if channel == LIVE_CHANNEL and self.hub is not None:
                self.hub.publish(kind, payload)

    def repos(self) -> MemoryRepos:
        """Прямий доступ для наповнення фікстур (поза транзакцією)."""
        return MemoryRepos(self.state)


class FakeBacktests:
    """BacktestService без рушія: фіксує постановки в чергу, стан задачі задає тест."""

    def __init__(self) -> None:
        self.jobs: dict[UUID, BacktestJob] = {}

    async def submit(self, run_id: UUID, spec: Mapping[str, Any], *, actor: str) -> BacktestJob:
        job = BacktestJob(run_id=run_id, spec=dict(spec), actor=actor, status=JobStatus.PENDING)
        self.jobs[run_id] = job
        return job

    def job(self, run_id: UUID) -> BacktestJob | None:
        return self.jobs.get(run_id)

    async def aclose(self) -> None:
        return None


def memory_services(
    tmp_path: Path, *, config_dir: Path, clock: ManualClock | None = None
) -> tuple[ApiServices, MemoryDb]:
    """ApiServices над MemoryDb і копією config/risk_limits.yaml у tmp_path (оригінал не змінюється)."""
    hub = LiveHub()
    db = MemoryDb(hub)
    limits_path = tmp_path / "risk_limits.yaml"
    limits_path.write_text((config_dir / "risk_limits.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    clk = clock or ManualClock(T0_NS)
    settings = Settings(jwt_secret=JWT_SECRET, _env_file=None)  # type: ignore[call-arg]
    services = ApiServices(
        uow=db.uow,
        limits=FileLimitsStore(limits_path),
        backtests=FakeBacktests(),
        live=hub,
        settings=settings,
        clock=clk,
        ids=SeededIdGenerator(7, b"api-test"),
        login_limiter=LoginRateLimiter(now_s=lambda: clk.now_ns() / 1e9),
        password_context=FAST_PWD,
    )
    return services, db
