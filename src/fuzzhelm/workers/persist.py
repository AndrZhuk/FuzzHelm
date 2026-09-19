"""Запис торгового циклу в PostgreSQL: паспорт прогону, рішення, ордери, позиції, ризик, капітал, журнал.

Найменування: workers/persist.py
Призначення: адаптер «рушій → сховище». Рушій (backtest.engine) чистий і БД не знає: кожен крок дає
    StepResult, прогін — BacktestResult, записи вже мають форму рядків DDL §6. Тут їх пишуть репозиторії
    storage:
      * паспорт `run` (config_hash, dataset_hash, git_sha, seed, engine → journal_head_hash, equity_hash);
      * `decision` з повним трасуванням (+ колонки ревізії 0004: sizing, risk, narrative);
      * `sim_order` (decision_id NOT NULL: жоден ордер без рішення ядра), `position`, `risk_event`
        (точний множник у payload.factor_exact пише RiskEventRepo), `equity_point` з ковзними
        VaR₉₅/CVaR₉₅, `run_metric`, `event_journal` (хеш-ланцюг; голова = run.journal_head_hash).
    Два режими: пакетний (готовий BacktestResult — executemany/COPY однією транзакцією) і покроковий
    (live/replay-воркер: LivePersister пише кожен бар у транзакції воркера разом із NOTIFY).
Автор: Андрій Жук, 2026.

VaR/CVaR у equity_point (§5.13 — звітна метрика, ордери не блокує; RF-03). Колонки NUMERIC(38,18) стоять
поруч із капіталом, тож вони в ГРОШАХ (USDT): VaR_t = E_t · VaR̂₉₅(r), де VaR̂ — історична оцінка
risk.var (нижній емпіричний квантиль r_(m), m = ⌊0.05·W⌋ = 25) на вікні останніх W = 500 бар-дохідностей
r_τ = ΔE_τ/E_{τ−1}, τ ≤ t (вікно закінчується на t включно: це «ризик кривої станом на t», а не прогноз для
тесту Купця — той робить risk.var.rolling_var_breaches строго на минулому). Поки дохідностей менше за
W = 500 (VAR_MIN_OBS = VAR_WINDOW: лише повні вікна §5.13), колонки NULL. Пакетний шлях бере числа, які вже
порахував рушій (BacktestResult.equity_points, risk.var.var_cvar_money), покроковий — risk.var.RollingVarCvar
(те саме ядро — ті самі числа). Значення не обрізаються до 0 (R-04: VaR < 0 на вікні майже з самих виграшів).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any
from uuid import UUID

import numpy as np
import numpy.typing as npt
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from fuzzhelm.core.dto import Candle, Fill
from fuzzhelm.core.enums import OrderStatus, RunKind, RunStatus
from fuzzhelm.core.journal import JournalEntry
from fuzzhelm.decision.narrative_uk import narrate
from fuzzhelm.risk.journal import AuditRecord, RiskEventRecord
from fuzzhelm.risk.var import RollingVarCvar, rolling_var_cvar
from fuzzhelm.risk.var import var_cvar_money as _var_cvar_money
from fuzzhelm.storage.repositories import (
    AuditRepo,
    CandleRepo,
    DecisionRecord,
    DecisionRepo,
    EquityPoint,
    EquityRepo,
    JournalRepo,
    OrderRepo,
    PositionRepo,
    RiskEventRepo,
    RunRepo,
    RunRow,
)
from fuzzhelm.storage.repositories.order import order_values
from fuzzhelm.storage.repositories.position import position_values

VAR_WINDOW = 500
VAR_ALPHA = 0.05
VAR_MIN_OBS = VAR_WINDOW                # §5.13: лише повні вікна W = 500 (до того — NULL)
GIT_DIRTY_METRIC = "git_dirty"          # 1.0 — прогін зроблено з незакоміченого дерева (sha — лише HEAD)

FloatArray = npt.NDArray[np.float64]


# ====================================================================== VaR / CVaR (звітна метрика)


def var_cvar_fractions(returns: Sequence[float] | FloatArray, *, window: int = VAR_WINDOW,
                       alpha: float = VAR_ALPHA, min_obs: int = VAR_MIN_OBS,
                       chunk: int = 4096) -> tuple[FloatArray, FloatArray]:
    """VaR/CVaR (частки капіталу) для кожної точки кривої: N = len(returns) + 1 точок, NaN до min_obs
    дохідностей. Обгортка risk.var.rolling_var_cvar (векторизовано блоками, без матриці N×W у пам'яті)."""
    return rolling_var_cvar(returns, window, alpha, min_obs=min_obs, chunk=chunk)


def var_cvar_money(equity: Sequence[Decimal], *, window: int = VAR_WINDOW, alpha: float = VAR_ALPHA,
                   min_obs: int = VAR_MIN_OBS) -> tuple[list[Decimal | None], list[Decimal | None]]:
    """VaR₉₅/CVaR₉₅ у грошах (E_t · частка) для кожної точки кривої капіталу (risk.var.var_cvar_money)."""
    return _var_cvar_money(equity, window, alpha, min_obs=min_obs)


class RollingVar(RollingVarCvar):
    """Покрокова версія var_cvar_money для live-воркера (ті самі числа на тій самій кривій)."""

    def __init__(self, window: int = VAR_WINDOW, alpha: float = VAR_ALPHA,
                 min_obs: int = VAR_MIN_OBS) -> None:
        super().__init__(window, alpha, min_obs=min_obs)


# ====================================================================== паспорт прогону


@dataclass(frozen=True, slots=True)
class Passport:
    """Рядок `run` на старті (RUNNING). Хеші — hex; git_dirty пишеться метрикою run_metric.git_dirty."""

    run_id: UUID
    kind: RunKind
    config: Mapping[str, Any]
    config_hash: str
    dataset_hash: str
    seed: int
    engine: str
    git_sha: str | None = None
    git_dirty: bool | None = None
    instrument_id: int | None = None
    tf: str | None = None
    ts_from_ns: int | None = None
    ts_to_ns: int | None = None
    strategy_id: int | None = None
    started_at_ns: int | None = None


async def create_run(session: AsyncSession, p: Passport) -> RunRow:
    row = await RunRepo(session).create(
        p.run_id, kind=p.kind, config=p.config, config_hash=p.config_hash, dataset_hash=p.dataset_hash,
        seed=p.seed, engine=p.engine, git_sha=p.git_sha, strategy_id=p.strategy_id,
        instrument_id=p.instrument_id, tf=p.tf, ts_from_ns=p.ts_from_ns, ts_to_ns=p.ts_to_ns,
        started_at_ns=p.started_at_ns,
    )
    if p.git_dirty is not None:
        await RunRepo(session).put_metrics(p.run_id, {GIT_DIRTY_METRIC: 1.0 if p.git_dirty else 0.0})
    return row


async def finish_run(session: AsyncSession, run_id: UUID, status: RunStatus = RunStatus.DONE, *,
                     journal_head_hash: bytes | str | None = None, equity_hash: str | None = None,
                     error: str | None = None, finished_at_ns: int | None = None,
                     metrics: Mapping[str, float | int | None] | None = None,
                     ts_to_ns: int | None = None) -> RunRow:
    if metrics:
        await RunRepo(session).put_metrics(run_id, metrics)
    return await RunRepo(session).finish(run_id, status, journal_head_hash=journal_head_hash,
                                         equity_hash=equity_hash, error=error, finished_at_ns=finished_at_ns,
                                         ts_to_ns=ts_to_ns)


# ====================================================================== відображення записів рушія


def decision_record(d: Any, *, run_id: UUID, instrument_id: int) -> DecisionRecord:
    """DecisionEntry рушія (з trace) → рядок decision з україномовним трасуванням (колонка 0004)."""
    return DecisionRecord.from_trace(
        d.trace, run_id=run_id, instrument_id=instrument_id, target_side=int(d.target_side),
        target_qty=d.target_qty, binding_constraint=d.binding_constraint, stop_price=d.stop_price,
        tp_price=d.tp_price, liq_price=d.liq_price, narrative=narrate(d.trace),
    )


def order_row(o: Any, *, decision_id: int, run_id: UUID, instrument_id: int) -> dict[str, Any]:
    """OrderRecord рушія (накопичений стан виконань) → рядок sim_order."""
    return order_values(
        o.request, decision_id=decision_id, run_id=run_id, instrument_id=instrument_id,
        status=OrderStatus(o.status), reject_code=o.reject_code, venue_order_id=o.venue_order_id,
        filled_qty=o.filled_qty, avg_fill_price=o.avg_fill_price, fee=o.fee, slippage_bps=o.slippage_bps,
        liquidity=o.liquidity, ts_filled_ns=o.ts_filled_ns,
    )


def position_row(p: Any, *, run_id: UUID, instrument_id: int) -> dict[str, Any]:
    """PositionRecord рушія → рядок position (відкрита позиція — без closed_at/exit_reason)."""
    closed = p.closed_at_ns is not None and p.exit_reason is not None
    return position_values(
        run_id=run_id, instrument_id=instrument_id, side=p.side, qty=p.qty, avg_entry=p.avg_entry,
        opened_at_ns=p.opened_at_ns, leverage=p.leverage, allocated_margin=p.allocated_margin,
        stop_price=p.stop_price, tp_price=p.tp_price, liq_price=p.liq_price,
        closed_at_ns=p.closed_at_ns if closed else None, exit_reason=p.exit_reason if closed else None,
        realized_pnl=p.realized_pnl if closed else None, funding_paid=p.funding_paid if closed else None,
        max_adverse_excursion=p.max_adverse_excursion,
    )


def equity_point(p: Any, var95: Decimal | None, cvar95: Decimal | None) -> EquityPoint:
    return EquityPoint(ts_ns=p.ts_ns, equity=p.equity, cash=p.cash, unrealized=p.unrealized,
                       gross_exposure=p.gross_exposure, leverage=p.leverage, drawdown=p.drawdown,
                       risk_state=p.risk_state, kappa=p.kappa, var95=var95, cvar95=cvar95)


# ====================================================================== пакетний запис (бектест)


@dataclass
class BacktestPlan:
    """Що буде записано з BacktestResult (чисте відображення, без БД — тестується офлайн)."""

    decisions: list[DecisionRecord] = field(default_factory=list)
    decision_keys: list[int] = field(default_factory=list)       # open_time_ns кожного рішення (FK ордерів)
    decisions_skipped: int = 0                                     # без трасування (record_traces="none")
    orders: list[tuple[Any, int]] = field(default_factory=list)   # (OrderRecord, open_time_ns рішення)
    orders_skipped: int = 0                                        # без рішення-джерела (ST-01) — не пишуться
    fills: int = 0
    positions: list[Any] = field(default_factory=list)
    equity: list[EquityPoint] = field(default_factory=list)
    risk_events: list[Any] = field(default_factory=list)       # RiskEventRecord ≅ RiskRecordLike
    metrics: dict[str, float | None] = field(default_factory=dict)


def plan_backtest(result: Any, *, run_id: UUID, instrument_id: int, var_window: int = VAR_WINDOW,
                  var_min_obs: int = VAR_MIN_OBS) -> BacktestPlan:
    plan = BacktestPlan()
    for d in result.decisions:
        if d.trace is None:
            plan.decisions_skipped += 1
            continue
        plan.decisions.append(decision_record(d, run_id=run_id, instrument_id=instrument_id))
        plan.decision_keys.append(int(d.open_time_ns))
    known = set(plan.decision_keys)
    for o in result.orders:
        if o.decision_ns is None or o.decision_ns not in known:
            plan.orders_skipped += 1
            continue
        plan.orders.append((o, int(o.decision_ns)))
    plan.fills = len(result.fills)
    plan.positions = list(result.positions)
    points = list(result.equity_points)
    if (var_window, var_min_obs) == (VAR_WINDOW, VAR_MIN_OBS) and _engine_filled_var(points, var_min_obs):
        # рушій уже порахував ту саму оцінку (run_backtest → risk.var.var_cvar_money з тими самими W, α)
        plan.equity = [equity_point(p, p.var95, p.cvar95) for p in points]
    else:
        var, cvar = var_cvar_money([p.equity for p in points], window=var_window, min_obs=var_min_obs)
        plan.equity = [equity_point(p, v, c) for p, v, c in zip(points, var, cvar, strict=True)]
    plan.risk_events = list(result.risk_events)
    plan.metrics = {**dict(result.metrics), **dict(result.extras)}
    return plan


def _engine_filled_var(points: Sequence[Any], min_obs: int) -> bool:
    """Чи несуть точки VaR/CVaR рушія: крива коротша за min_obs + 1 точок (усі NULL — і так, і так) або
    точка min_obs має значення (рушій заповнює всі точки від min_obs)."""
    if len(points) <= min_obs:
        return True
    p = points[min_obs]
    return getattr(p, "var95", None) is not None and getattr(p, "cvar95", None) is not None


@dataclass(frozen=True, slots=True)
class PersistCounts:
    decisions: int = 0
    decisions_skipped: int = 0
    orders: int = 0
    orders_skipped: int = 0
    fills: int = 0
    positions: int = 0
    equity_points: int = 0
    risk_events: int = 0
    metrics: int = 0
    journal_entries: int = 0
    candles: int = 0

    def as_dict(self) -> dict[str, int]:
        return {k: getattr(self, k) for k in self.__slots__}


async def write_plan(session: AsyncSession, run_id: UUID, plan: BacktestPlan, *, instrument_id: int,
                     symbol: str, journal: Sequence[JournalEntry] = ()) -> PersistCounts:
    """Записати план у транзакції викликача: рішення (id у порядку входу) → ордери з FK → позиції → капітал
    (COPY) → ризик → метрики → журнал. Пакетно: executemany частинами, капітал — бінарним COPY."""
    ids = await DecisionRepo(session).insert_many(plan.decisions)
    decision_id = dict(zip(plan.decision_keys, ids, strict=True))
    await OrderRepo(session).insert_many(
        [order_row(o, decision_id=decision_id[key], run_id=run_id, instrument_id=instrument_id)
         for o, key in plan.orders])
    await PositionRepo(session).insert_many(
        [position_row(p, run_id=run_id, instrument_id=instrument_id) for p in plan.positions])
    n_eq = await EquityRepo(session).insert_many(run_id, plan.equity) if plan.equity else 0
    n_risk = (await RiskEventRepo(session).insert_many(plan.risk_events, run_id=run_id,
                                                       instrument_ids={symbol: instrument_id})
              if plan.risk_events else 0)
    n_metrics = await RunRepo(session).put_metrics(run_id, plan.metrics)
    n_journal = await JournalRepo(session).append_many(list(journal)) if journal else 0
    return PersistCounts(
        decisions=len(ids), decisions_skipped=plan.decisions_skipped, orders=len(plan.orders),
        orders_skipped=plan.orders_skipped, fills=plan.fills, positions=len(plan.positions),
        equity_points=int(n_eq), risk_events=int(n_risk), metrics=int(n_metrics),
        journal_entries=int(n_journal),
    )


async def persist_backtest(session: AsyncSession, run_id: UUID, result: Any, *, instrument_id: int,
                           symbol: str, journal: Sequence[JournalEntry] = ()) -> PersistCounts:
    """BacktestResult → усі таблиці прогону (паспорт створює/завершує викликач: create_run / finish_run)."""
    plan = plan_backtest(result, run_id=run_id, instrument_id=instrument_id)
    return await write_plan(session, run_id, plan, instrument_id=instrument_id, symbol=symbol,
                            journal=journal)


# ====================================================================== покроковий запис (live / replay)


async def pg_notify(session: AsyncSession, channel: str, payload: str) -> None:
    """NOTIFY у транзакції викликача: браузер (SSE) отримає подію лише після COMMIT (API-07)."""
    await session.execute(text("SELECT pg_notify(:ch, :p)"), {"ch": channel, "p": payload})


@dataclass
class StepWrite:
    """Що записано за один крок (для NOTIFY і лічильників воркера)."""

    decision_id: int | None = None
    orders: int = 0
    fills: int = 0
    positions_opened: int = 0
    positions_closed: int = 0
    risk_events: int = 0
    equity_points: int = 0
    journal_entries: int = 0
    candles: int = 0
    var95: Decimal | None = None
    cvar95: Decimal | None = None


class LivePersister:
    """Покроковий запис прогону live/replay. Стан між кроками: id рішень (FK ордерів, зокрема синтетичних
    TP/ліквідації, що посилаються на рішення-відкриття), створені ордери, id відкритих позицій."""

    def __init__(self, run_id: UUID, instrument_id: int, symbol: str, *,
                 var: RollingVar | None = None) -> None:
        self.run_id = run_id
        self.instrument_id = instrument_id
        self.symbol = symbol
        self.var = var or RollingVar()
        self._decision_ids: dict[int, int] = {}
        self._orders: set[UUID] = set()
        self._positions: dict[int, int] = {}          # opened_at_ns → position.id (одна позиція за раз)
        self.orders_skipped = 0
        self.totals: dict[str, int] = defaultdict(int)

    def decision_id(self, open_time_ns: int) -> int | None:
        return self._decision_ids.get(open_time_ns)

    async def write_step(self, session: AsyncSession, sr: Any, *, candle: Candle | None = None,
                         journal: Sequence[JournalEntry] = (),
                         extra_risk: Sequence[RiskEventRecord] = ()) -> StepWrite:
        out = StepWrite()
        run_id, iid = self.run_id, self.instrument_id
        if candle is not None:
            res = await CandleRepo(session).upsert([candle], iid)
            out.candles = res.inserted + res.updated
        d = sr.decision
        if d is not None and d.trace is not None:
            out.decision_id = await DecisionRepo(session).insert(
                decision_record(d, run_id=run_id, instrument_id=iid))
            self._decision_ids[int(d.open_time_ns)] = out.decision_id
        new_rows: list[dict[str, Any]] = []
        for o in sr.orders:
            did = None if o.decision_ns is None else self._decision_ids.get(int(o.decision_ns))
            if did is None:
                self.orders_skipped += 1
                continue
            # стан «до виконань цього кроку»: виконання нижче накопичить СУБД (OrderRepo.apply_fill)
            new_rows.append(order_values(o.request, decision_id=did, run_id=run_id, instrument_id=iid,
                                         status=OrderStatus.NEW if _has_fill(sr.fills, o) else o.status,
                                         reject_code=o.reject_code, venue_order_id=o.venue_order_id))
            self._orders.add(o.request.client_order_id)
        if new_rows:
            out.orders = await OrderRepo(session).insert_many(new_rows)
        orders = OrderRepo(session)
        for f in sr.fills:
            if f.client_order_id in self._orders:
                await orders.apply_fill(f)
                out.fills += 1
        for o in sr.order_updates:
            coid = o.request.client_order_id
            if coid in self._orders and OrderStatus(o.status) in (OrderStatus.CANCELED, OrderStatus.REJECTED):
                if OrderStatus(o.status) is OrderStatus.CANCELED:
                    await orders.cancel(coid, at_ns=sr.close_time_ns)
                else:
                    await orders.apply_ack(_ack(o))
        positions = PositionRepo(session)
        for p in sr.opened_positions:
            self._positions[int(p.opened_at_ns)] = await positions.open(
                run_id=run_id, instrument_id=iid, side=p.side, qty=p.qty, avg_entry=p.avg_entry,
                opened_at_ns=p.opened_at_ns, leverage=p.leverage, allocated_margin=p.allocated_margin,
                stop_price=p.stop_price, tp_price=p.tp_price, liq_price=p.liq_price)
            out.positions_opened += 1
        for p in sr.closed_positions:
            pid = self._positions.pop(int(p.opened_at_ns), None)
            if pid is None:
                continue
            await positions.update(pid, qty=p.qty, avg_entry=p.avg_entry, liq_price=p.liq_price,
                                   max_adverse_excursion=p.max_adverse_excursion)
            await positions.close(pid, closed_at_ns=p.closed_at_ns, exit_reason=p.exit_reason,
                                  realized_pnl=p.realized_pnl, funding_paid=p.funding_paid)
            out.positions_closed += 1
        risk: list[Any] = [*extra_risk, *sr.risk_events]
        if risk:
            out.risk_events = await RiskEventRepo(session).insert_many(
                risk, run_id=run_id, instrument_ids={self.symbol: iid})
        if sr.equity_point is not None:
            out.var95, out.cvar95 = self.var.update(sr.equity_point.equity)
            out.equity_points = await EquityRepo(session).insert_many(
                run_id, [equity_point(sr.equity_point, out.var95, out.cvar95)])
        if journal:
            out.journal_entries = await JournalRepo(session).append_many(list(journal))
        for k in ("orders", "fills", "positions_opened", "positions_closed", "risk_events", "equity_points",
                  "journal_entries", "candles"):
            self.totals[k] += getattr(out, k)
        self.totals["decisions"] += int(out.decision_id is not None)
        return out

    async def write_out_of_band(self, session: AsyncSession, *, risk: Sequence[RiskEventRecord] = (),
                                audits: Iterable[AuditRecord] = (), journal: Sequence[JournalEntry] = (),
                                user_id: int | None = None) -> int:
        """Події між барами (зняття HALTED адміністратором): risk_event переходу, audit_log (автор — user_id
        запиту API), журнал."""
        n = 0
        if risk:
            rows: list[Any] = list(risk)
            n += await RiskEventRepo(session).insert_many(rows, run_id=self.run_id,
                                                          instrument_ids={self.symbol: self.instrument_id})
            self.totals["risk_events"] += len(risk)
        for a in audits:
            rec: Any = a                                  # frozen AuditRecord — структурно AuditRecordLike
            await AuditRepo(session).append_record(rec, user_id=user_id)
            n += 1
        if journal:
            n += await JournalRepo(session).append_many(list(journal))
            self.totals["journal_entries"] += len(journal)
        return n


def _has_fill(fills: Sequence[Fill], o: Any) -> bool:
    coid = o.request.client_order_id
    return any(f.client_order_id == coid for f in fills)


def _ack(o: Any) -> Any:
    from fuzzhelm.core.dto import OrderAck  # noqa: PLC0415 — лише для рідкісного шляху REJECTED

    return OrderAck(client_order_id=o.request.client_order_id, venue_order_id=o.venue_order_id,
                    status=OrderStatus(o.status), reject_code=o.reject_code, ts_ns=o.ts_created_ns)


__all__: Sequence[str] = (
    "GIT_DIRTY_METRIC",
    "VAR_ALPHA",
    "VAR_MIN_OBS",
    "VAR_WINDOW",
    "BacktestPlan",
    "LivePersister",
    "Passport",
    "PersistCounts",
    "RollingVar",
    "StepWrite",
    "create_run",
    "decision_record",
    "equity_point",
    "finish_run",
    "order_row",
    "persist_backtest",
    "pg_notify",
    "plan_backtest",
    "position_row",
    "var_cvar_fractions",
    "var_cvar_money",
    "write_plan",
)
