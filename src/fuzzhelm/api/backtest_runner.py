"""Типовий виконавець POST /backtests: свічки з PostgreSQL → рушій бектесту → результати назад у БД.

Найменування: api/backtest_runner.py
Призначення: зв'язати асинхронну чергу API (api/backtests.py) зі справжнім рушієм
`fuzzhelm.backtest.engine.run_backtest(dataset, cfg, seed, *, run_id, ...)`, який сам по собі чистий
(масиви в пам'яті → BacktestResult) і нічого не знає про БД. Тут: (1) підготовка — інструмент, закриті
свічки вікна (`CandleRepo.load_arrays`), ставки фінансування з `data/funding_<SYMBOL>.json`, конфігурація
рушія з профілю `backtest` + дерева правил/МФ версії стратегії + перекриття параметрів;
(2) паспорт `run` зі статусом RUNNING ДО запуску (GET /runs/{id} одразу показує хеші); (3) рушій у
окремому потоці; (4) запис результатів однією транзакцією: рішення з трасуванням (+ narrative_uk),
ордери з виконаннями, позиції, крива капіталу, записи ризик-контуру, метрики; `finish(DONE)` з
journal_head_hash і equity_hash. Будь-яка помилка → `finish(FAILED, error)`.
Автор: Андрій Жук, 2026.

Рушій імпортується ліниво (`api.backtests.load_engine_module`): API стартує й без нього, а відсутній
рушій дає задачу FAILED із поясненням. Відображення BacktestResult → рядки БД (`plan_persistence`) —
чиста функція, тестується офлайн на справжньому рушії; запис — інтеграційно.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any
from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from fuzzhelm.api import backtests as backtests_mod
from fuzzhelm.api.validation import validate_strategy_texts
from fuzzhelm.core.dto import Fill, Instrument
from fuzzhelm.core.enums import OrderStatus, RunKind, RunStatus
from fuzzhelm.core.errors import FuzzHelmError
from fuzzhelm.core.money import dec
from fuzzhelm.core.ports import Clock
from fuzzhelm.decision.narrative_uk import narrate
from fuzzhelm.infra.wallclock import SystemClock
from fuzzhelm.storage.repositories import (
    CandleRepo,
    DecisionRecord,
    DecisionRepo,
    EquityPoint,
    EquityRepo,
    InstrumentRepo,
    OrderRepo,
    PositionRepo,
    RiskEventRepo,
    RunRepo,
    StrategyRepo,
)
from fuzzhelm.storage.repositories.common import SQLSTATE_UNIQUE_VIOLATION, sqlstate
from fuzzhelm.storage.session import session_scope

BACKTEST_PROFILE = "backtest"

# Перекриття параметрів, які дозволено передати в POST /backtests (поля BacktestConfig рушія).
PARAM_KEYS: frozenset[str] = frozenset(
    {"n_atr", "chi", "u_enter", "u_exit", "rho_base", "lam", "tp_multiple", "cost_mode", "initial_equity"}
)


class DuplicateRunError(FuzzHelmError):
    """Ідентичний прогін (config_hash, dataset_hash, seed, engine, git_sha) уже є в БД (ux_run_identity)."""

    def __init__(self, existing_id: UUID) -> None:
        super().__init__(
            f"identical run already stored as {existing_id} "
            "(same config_hash, dataset_hash, seed, engine, git_sha)"
        )
        self.existing_id = existing_id


class EmptyWindowError(FuzzHelmError):
    """У вікні немає закритих свічок — рушію нема на чому працювати."""


# ------------------------------------------------------------------ підготовка


@dataclass(frozen=True, slots=True)
class PreparedBacktest:
    instrument_id: int
    symbol: str
    tf: str
    ts_from_ns: int
    ts_to_ns: int
    seed: int
    engine: str
    strategy_id: int | None
    dataset: Any  # fuzzhelm.backtest.dataset.Dataset
    config: Any  # fuzzhelm.backtest.engine.BacktestConfig


def strategy_trees(rules_yaml: str, membership_yaml: str) -> dict[str, Any]:
    """Дерева правил і МФ версії стратегії — тим самим валідатором, що й POST/PUT /strategies."""
    v = validate_strategy_texts(rules_yaml, membership_yaml)
    return {"membership": dict(v.membership_tree), "rules": dict(v.rules)}


def build_config(
    engine_mod: ModuleType, spec: Mapping[str, Any], trees: Mapping[str, Any] | None = None
) -> Any:
    """BacktestConfig: профіль `backtest` + рушій із запиту + перекриття параметрів + дерева стратегії.

    Невідомий параметр → ValueError (схема запиту їх уже відсікає; тут — захист для прямих викликів).
    """
    params = {k: v for k, v in dict(spec.get("params") or {}).items() if v is not None}
    unknown = set(params) - PARAM_KEYS
    if unknown:
        raise ValueError(f"unknown backtest parameters: {sorted(unknown)}")
    if "initial_equity" in params:
        params["initial_equity"] = dec(str(params["initial_equity"]))
    if "n_atr" in params:
        params["n_atr"] = int(params["n_atr"])
    return engine_mod.BacktestConfig.from_profile(
        BACKTEST_PROFILE, engine=str(spec.get("engine") or "mamdani"), trees=dict(trees or {}), **params
    )


def funding_rates(path: Path, instrument: Instrument) -> Sequence[Any] | None:
    """Усі ставки фінансування з data/funding_<SYMBOL>.json (`FundingRate`); файлу немає → None
    (рушій тоді бере funding.fallback_rate з config/engine.yaml, EXE-05).

    Обрізання до вікна свідомо НЕ робиться тут: його робить `Dataset.from_candle_arrays` за правилом
    рушія [t₀ − 1 доба, t_last + 1 хв] (`Dataset.with_funding`). Тоді той самий відрізок свічок дає
    той самий dataset_hash і через API, і через CLI/сітку/walk-forward (`backtest.runner.load_db_window`);
    власне обрізання [ts_from, ts_to) давало інший хеш для будь-якого під-вікна (deviations API-15).
    """
    if not path.is_file():
        return None
    from fuzzhelm.ingest.funding import load_funding_json  # noqa: PLC0415

    return load_funding_json(path, instrument).rates


async def prepare_backtest(
    session: AsyncSession, spec: Mapping[str, Any], engine_mod: ModuleType, *, data_dir: Path
) -> PreparedBacktest:
    """Прочитати все потрібне рушію однією транзакцією (лише читання)."""
    from fuzzhelm.backtest.dataset import Dataset  # noqa: PLC0415 — пакет backtest імпортується ліниво

    symbol = str(spec.get("symbol") or "BTC-USDT-PERP")
    tf = str(spec.get("tf") or "1m")
    ts_from, ts_to = int(spec["ts_from_ns"]), int(spec["ts_to_ns"])
    inst_row = await InstrumentRepo(session).get_by_canon(symbol)
    if inst_row is None:
        raise LookupError(f"instrument {symbol!r} not found")
    strategy_id = spec.get("strategy_id")
    trees = None
    if strategy_id is not None:
        srow = await StrategyRepo(session).get(int(strategy_id))
        if srow is None:
            raise LookupError(f"strategy {strategy_id} not found")
        trees = strategy_trees(srow.rules_yaml, srow.membership_yaml)
    arrays = await CandleRepo(session).load_arrays(inst_row.id, tf, ts_from, ts_to, closed_only=True)
    if arrays.t_ns.size == 0:
        raise EmptyWindowError(f"no closed {tf} candles of {symbol} in [{ts_from}, {ts_to})")
    instrument = inst_row.to_dto()
    rates = funding_rates(data_dir / f"funding_{instrument.symbol_venue}.json", instrument)
    # канонічний шлях рушія (той самий, що в backtest.runner.load_db_window): ідентичні дані → ідентичний хеш
    dataset = Dataset.from_candle_arrays(
        arrays, instrument, tf=tf, funding=rates, source=f"db:{symbol}:{tf}:[{ts_from},{ts_to})"
    )
    cfg = build_config(engine_mod, spec, trees)
    return PreparedBacktest(
        instrument_id=inst_row.id,
        symbol=symbol,
        tf=tf,
        ts_from_ns=ts_from,
        ts_to_ns=ts_to,
        seed=int(spec["seed"]),
        engine=str(cfg.engine),
        strategy_id=None if strategy_id is None else int(strategy_id),
        dataset=dataset,
        config=cfg,
    )


def run_config_json(cfg: Any) -> dict[str, Any]:
    """Що саме запускалося (run.config JSONB): identity_dict рушія (скаляри + перекриті дерева, з
    яких рахується config_hash). З нього /explain відтворює МФ, правила і параметри κ прогону."""
    out: dict[str, Any] = cfg.identity_dict()
    return out


# ------------------------------------------------------------------ BacktestResult → рядки БД


@dataclass
class PersistPlan:
    """Що буде записано (чисте відображення результату рушія; без БД)."""

    decisions: list[DecisionRecord] = field(default_factory=list)
    decision_keys: list[int] = field(default_factory=list)  # open_time_ns кожного рішення (FK ордерів)
    decisions_skipped: int = 0  # рішення без трасування (record_traces=none) — у decision не пишуться
    orders: list[tuple[Any, int]] = field(default_factory=list)  # (OrderRecord, open_time_ns рішення)
    orders_skipped: int = 0  # ордери без рішення-джерела (sim_order.decision_id NOT NULL, ST-01)
    fills: dict[UUID, list[Fill]] = field(default_factory=dict)
    positions: list[Any] = field(default_factory=list)
    equity: list[EquityPoint] = field(default_factory=list)
    risk_events: list[Any] = field(default_factory=list)
    metrics: dict[str, float | None] = field(default_factory=dict)


def plan_persistence(result: Any, *, run_id: UUID, instrument_id: int) -> PersistPlan:
    plan = PersistPlan()
    for d in result.decisions:
        if d.trace is None:
            plan.decisions_skipped += 1
            continue
        plan.decisions.append(
            DecisionRecord.from_trace(
                d.trace,
                run_id=run_id,
                instrument_id=instrument_id,
                target_side=int(d.target_side),
                target_qty=d.target_qty,
                binding_constraint=d.binding_constraint,
                stop_price=d.stop_price,
                tp_price=d.tp_price,
                liq_price=d.liq_price,
                narrative=narrate(d.trace),
            )
        )
        plan.decision_keys.append(int(d.open_time_ns))
    known = set(plan.decision_keys)
    for o in result.orders:
        if o.decision_ns is None or o.decision_ns not in known:
            plan.orders_skipped += 1
            continue
        plan.orders.append((o, int(o.decision_ns)))
    fills: dict[UUID, list[Fill]] = defaultdict(list)
    for f in result.fills:
        fills[f.client_order_id].append(f)
    plan.fills = dict(fills)
    plan.positions = list(result.positions)
    plan.equity = [
        EquityPoint(
            ts_ns=p.ts_ns,
            equity=p.equity,
            cash=p.cash,
            unrealized=p.unrealized,
            gross_exposure=p.gross_exposure,
            leverage=p.leverage,
            drawdown=p.drawdown,
            risk_state=p.risk_state,
            kappa=p.kappa,
            var95=p.var95,
            cvar95=p.cvar95,
        )
        for p in result.equity_points
    ]
    plan.risk_events = list(result.risk_events)
    plan.metrics = {**dict(result.metrics), **dict(result.extras)}
    return plan


@dataclass(frozen=True, slots=True)
class PersistCounts:
    decisions: int
    decisions_skipped: int
    orders: int
    orders_skipped: int
    fills: int
    positions: int
    equity_points: int
    risk_events: int
    metrics: int

    def as_dict(self) -> dict[str, int]:
        return {k: getattr(self, k) for k in self.__slots__}


async def persist_backtest_result(
    session: AsyncSession, run_id: UUID, result: Any, *, instrument_id: int, symbol: str
) -> PersistCounts:
    """Записати результат рушія в межах транзакції викликача (репозиторії storage не комітять)."""
    plan = plan_persistence(result, run_id=run_id, instrument_id=instrument_id)
    ids = await DecisionRepo(session).insert_many(plan.decisions)
    decision_id = dict(zip(plan.decision_keys, ids, strict=True))

    orders = OrderRepo(session)
    n_fills = 0
    for o, key in plan.orders:
        own_fills = plan.fills.get(o.request.client_order_id, [])
        initial = OrderStatus.NEW if own_fills else OrderStatus(o.status)
        await orders.create(
            o.request,
            decision_id=decision_id[key],
            run_id=run_id,
            instrument_id=instrument_id,
            status=initial,
            reject_code=o.reject_code,
        )
        for f in own_fills:
            await orders.apply_fill(f)
            n_fills += 1
        if own_fills and o.status is OrderStatus.CANCELED:
            await orders.cancel(o.request.client_order_id)  # частково виконаний і знятий

    positions = PositionRepo(session)
    for p in plan.positions:
        pid = await positions.open(
            run_id=run_id,
            instrument_id=instrument_id,
            side=p.side,
            qty=p.qty,
            avg_entry=p.avg_entry,
            opened_at_ns=p.opened_at_ns,
            leverage=p.leverage,
            allocated_margin=p.allocated_margin,
            stop_price=p.stop_price,
            tp_price=p.tp_price,
            liq_price=p.liq_price,
        )
        if p.max_adverse_excursion is not None:
            await positions.update(pid, max_adverse_excursion=p.max_adverse_excursion)
        if p.closed_at_ns is not None and p.exit_reason is not None:
            await positions.close(
                pid,
                closed_at_ns=p.closed_at_ns,
                exit_reason=p.exit_reason,
                realized_pnl=p.realized_pnl,
                funding_paid=p.funding_paid,
            )

    n_eq = await EquityRepo(session).insert_many(run_id, plan.equity) if plan.equity else 0
    n_risk = (
        await RiskEventRepo(session).insert_many(
            plan.risk_events, run_id=run_id, instrument_ids={symbol: instrument_id}
        )
        if plan.risk_events
        else 0
    )
    n_metrics = await RunRepo(session).put_metrics(run_id, plan.metrics)
    return PersistCounts(
        decisions=len(ids),
        decisions_skipped=plan.decisions_skipped,
        orders=len(plan.orders),
        orders_skipped=plan.orders_skipped,
        fills=n_fills,
        positions=len(plan.positions),
        equity_points=int(n_eq),
        risk_events=int(n_risk),
        metrics=int(n_metrics),
    )


# ------------------------------------------------------------------ виконавець


class DbBacktestRunner:
    """`Runner` для EngineBacktestService: (run_id, spec) → прогін рушія з записом у PostgreSQL."""

    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        *,
        data_dir: Path,
        clock: Clock | None = None,
        git: bool = True,
    ) -> None:
        self.factory = factory
        self.data_dir = Path(data_dir)
        self.clock = clock or SystemClock()
        self.git = git

    async def _git(self) -> tuple[str | None, bool | None]:
        """(git_sha, dirty): SHA — HEAD; dirty — незакомічені зміни в дереві (run_metric.git_dirty, W-11)."""
        if not self.git:
            return None, None
        from fuzzhelm.backtest.manifest import read_git_sha  # noqa: PLC0415

        return await asyncio.to_thread(read_git_sha)

    async def _fail(self, run_id: UUID, error: str) -> None:
        async with session_scope(self.factory) as s:
            await RunRepo(s).finish(
                run_id, RunStatus.FAILED, error=error[: backtests_mod.ERROR_MAX_CHARS],
                finished_at_ns=self.clock.now_ns(),
            )

    async def __call__(self, run_id: UUID, spec: Mapping[str, Any]) -> dict[str, Any]:
        engine_mod = backtests_mod.load_engine_module()  # до БД: без рушія — зрозуміла помилка
        async with session_scope(self.factory) as s:
            prep = await prepare_backtest(s, spec, engine_mod, data_dir=self.data_dir)
        cfg, ds = prep.config, prep.dataset
        # хеш датасету (BLAKE2b над стовпчиками) і git — не в event loop
        ds_hash: str = await asyncio.to_thread(lambda: ds.dataset_hash)
        git_sha, git_dirty = await self._git()
        async with session_scope(self.factory) as s:
            runs = RunRepo(s)
            dup = await runs.find_by_identity(
                config_hash=cfg.config_hash, dataset_hash=ds_hash, seed=prep.seed, engine=prep.engine,
                git_sha=git_sha,
            )
            if dup is not None:
                raise DuplicateRunError(dup.id)
            try:
                await runs.create(
                    run_id,
                    kind=RunKind.BACKTEST,
                    config=run_config_json(cfg),
                    config_hash=cfg.config_hash,
                    dataset_hash=ds_hash,
                    seed=prep.seed,
                    engine=prep.engine,
                    git_sha=git_sha,
                    strategy_id=prep.strategy_id,
                    instrument_id=prep.instrument_id,
                    tf=prep.tf,
                    ts_from_ns=prep.ts_from_ns,
                    ts_to_ns=prep.ts_to_ns,
                    started_at_ns=self.clock.now_ns(),
                )
                if git_dirty is not None:
                    from fuzzhelm.workers.persist import GIT_DIRTY_METRIC  # noqa: PLC0415

                    await runs.put_metrics(run_id, {GIT_DIRTY_METRIC: 1.0 if git_dirty else 0.0})
            except IntegrityError as e:  # паралельний ідентичний запит встиг першим
                if sqlstate(e) == SQLSTATE_UNIQUE_VIOLATION:
                    raise FuzzHelmError("identical run is being stored concurrently") from e
                raise
        try:
            # хеш-ланцюг журналу прогону збирається під час роботи рушія і пишеться в event_journal разом із
            # рештою результатів (workers.persist, пакетно; W-08): голова ланцюга = run.journal_head_hash
            journal: list[Any] = []
            result = await asyncio.to_thread(
                engine_mod.run_backtest, ds, cfg, prep.seed, run_id=run_id, git=False, kind=RunKind.BACKTEST,
                journal_sink=journal.append,
            )
            m = result.manifest
            if (m.config_hash, m.dataset_hash) != (cfg.config_hash, ds_hash):
                raise FuzzHelmError("engine manifest does not match the stored passport")
            from fuzzhelm.workers.persist import persist_backtest  # noqa: PLC0415

            async with session_scope(self.factory) as s:
                counts = await persist_backtest(
                    s, run_id, result, instrument_id=prep.instrument_id, symbol=prep.symbol, journal=journal
                )
                await RunRepo(s).finish(
                    run_id,
                    RunStatus.DONE,
                    journal_head_hash=m.journal_head_hash,
                    equity_hash=m.equity_hash,
                    finished_at_ns=self.clock.now_ns(),
                )
        except asyncio.CancelledError:
            with contextlib.suppress(Exception):
                await asyncio.shield(self._fail(run_id, "cancelled on shutdown"))
            raise
        except Exception as e:
            with contextlib.suppress(Exception):
                await self._fail(run_id, backtests_mod.public_error(e))
            raise
        return {
            "run_id": str(run_id),
            "bars": len(ds),
            "config_hash": cfg.config_hash,
            "dataset_hash": ds_hash,
            "equity_hash": m.equity_hash,
            **counts.as_dict(),
        }


__all__: Sequence[str] = (
    "PARAM_KEYS",
    "DbBacktestRunner",
    "DuplicateRunError",
    "EmptyWindowError",
    "PersistCounts",
    "PersistPlan",
    "PreparedBacktest",
    "build_config",
    "funding_rates",
    "persist_backtest_result",
    "plan_persistence",
    "prepare_backtest",
    "run_config_json",
    "strategy_trees",
)
