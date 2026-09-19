"""Воркери без БД: VaR/CVaR для equity_point, план запису бектесту, гаряча заміна лімітів, профілі, паспорт.

Найменування: tests/unit/test_workers.py
Автор: Андрій Жук, 2026.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import numpy as np
import pytest
import yaml

from fuzzhelm.backtest.dataset import load_fixture_dataset
from fuzzhelm.backtest.engine import BacktestConfig, TradingLoop, run_backtest
from fuzzhelm.config import CONFIG_DIR
from fuzzhelm.core.enums import RunKind
from fuzzhelm.core.errors import ConfigValidationError
from fuzzhelm.core.journal import JournalEntry, verify_chain
from fuzzhelm.risk.var import historical_var_cvar, returns_from_equity
from fuzzhelm.workers.ingest_worker import pump
from fuzzhelm.workers.persist import RollingVar, plan_backtest, var_cvar_money
from fuzzhelm.workers.trading_worker import (
    TradingSession,
    WorkerError,
    WorkerProfile,
    header_line,
    scenario_path,
    session_dataset_hash,
)

RUN_ID = UUID(int=42)


def _curve(n: int, seed: int = 7) -> list[Decimal]:
    rng = np.random.default_rng(seed)
    e, out = Decimal("10000"), []
    for z in rng.standard_normal(n):
        e = (e * (Decimal(1) + Decimal(f"{z * 0.002:.12f}"))).quantize(Decimal("1E-9"))
        out.append(e)
    return out


def test_equity_point_var_cvar_is_rolling_historical_estimate_in_money() -> None:
    """VaR₉₅/CVaR₉₅ точки t = E_t · оцінка risk.var на останніх min(t, W) дохідностях; пакетний
    (векторизований) і покроковий (live) шляхи дають ті самі числа; CVaR ≥ VaR; NULL до 20 дохідностей."""
    eq = _curve(560)
    w, min_obs = 500, 20
    var, cvar = var_cvar_money(eq, window=w, min_obs=min_obs)
    rv = RollingVar(window=w, min_obs=min_obs)
    live = [rv.update(e) for e in eq]
    r = returns_from_equity(eq)
    for t, e in enumerate(eq):
        if t < min_obs:
            assert var[t] is None and cvar[t] is None and live[t] == (None, None)
            continue
        ref = historical_var_cvar(r[max(0, t - w):t], 0.05, window=None)
        v, c = var[t], cvar[t]
        assert v is not None and c is not None
        assert abs(float(v / e) - ref.var) < 1e-12 and abs(float(c / e) - ref.cvar) < 1e-12
        lv, lc = live[t]
        assert lv is not None and lc is not None
        assert abs(lv - v) <= abs(e) * Decimal("1e-12") and abs(lc - c) <= abs(e) * Decimal("1e-12")
        assert c >= v                                     # тотожність CVaR ≥ VaR (risk.var)


def test_plan_backtest_maps_engine_result_and_journal_chain() -> None:
    ds = load_fixture_dataset().slice(0, 1500)
    cfg = BacktestConfig.from_profile("backtest")
    entries: list[JournalEntry] = []
    res = run_backtest(ds, cfg, seed=20260918, run_id=RUN_ID, journal_sink=entries.append)
    plan = plan_backtest(res, run_id=RUN_ID, instrument_id=3)
    # журнал, що пише воркер, — той самий ланцюг, голова якого в паспорті прогону
    assert entries and verify_chain(entries) is None
    assert entries[-1].hash.hex() == res.manifest.journal_head_hash
    # рішення з трасуванням → рядки decision; кожен записаний ордер має рішення-джерело (ST-01)
    assert len(plan.decisions) == len(res.decisions) and plan.decisions_skipped == 0
    assert plan.orders and plan.orders_skipped == 0
    assert {k for _, k in plan.orders} <= set(plan.decision_keys)
    assert all(d.fired_rules and d.narrative for d in plan.decisions)
    # крива: точка на бар, VaR/CVaR з 21-ї точки
    assert len(plan.equity) == len(ds)
    assert all(p.var95 is None for p in plan.equity[:20])
    assert all(p.var95 is not None for p in plan.equity[20:])
    assert all(p.cvar95 >= p.var95 for p in plan.equity[20:])  # type: ignore[operator]
    assert set(res.metrics) | set(res.extras) == set(plan.metrics) and "psr" in plan.metrics


def _risk_tree(**sm: float) -> dict[str, object]:
    tree = yaml.safe_load((CONFIG_DIR / "risk_limits.yaml").read_text(encoding="utf-8"))
    tree["state_machine"].update(sm)
    return tree


def test_trading_loop_hot_reloads_limits_and_state_machine_only() -> None:
    ds = load_fixture_dataset()
    loop = TradingLoop(ds.instrument, BacktestConfig.from_profile("replay", u_enter=0.2), seed=1)
    before = loop.risk_cfg
    tree = _risk_tree(halt_daily_loss=0.025)
    tree["limits"]["max_daily_loss"]["value"] = 0.015
    tree["hysteresis"] = {"enter": 0.9, "exit": 0.5}           # параметр стратегії: посеред прогону не діє
    new = loop.apply_risk_limits(tree)
    assert loop.fsm.cfg.halt_daily_loss == Decimal("0.025")
    assert new.limits.max_daily_loss.value == Decimal("0.015")
    daily = next(r for r in loop.guard.rules if r.name == "max_daily_loss")
    assert daily.limit == Decimal("-0.015")                  # type: ignore[attr-defined]
    assert new.hysteresis == before.hysteresis                 # u_enter прогону (0.20) лишився
    bad = _risk_tree(warn_enter=2.0)
    with pytest.raises(ConfigValidationError):
        loop.apply_risk_limits(bad)
    assert loop.risk_cfg is new                                # невалідне дерево стан не змінює


def test_session_reloads_changed_limits_file_and_journals_it(tmp_path: Path) -> None:
    ds = load_fixture_dataset()
    limits = tmp_path / "risk_limits.yaml"
    limits.write_text((CONFIG_DIR / "risk_limits.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    cfg = BacktestConfig.from_profile("replay", u_enter=0.2)
    s = TradingSession(ds.instrument, cfg, seed=1, run_id=RUN_ID, kind=RunKind.REPLAY, feed="REPLAY",
                       warmup_bars=cfg.resolved_warmup(), limits_path=limits)
    assert s.reload_limits_if_changed() is False                # файл не змінювався
    tree = _risk_tree(cool_daily_loss=0.015)
    limits.write_text(yaml.safe_dump(tree), encoding="utf-8")
    assert s.reload_limits_if_changed() is True
    assert s.loop.fsm.cfg.cool_daily_loss == Decimal("0.015")
    limits.write_text("limits: {broken", encoding="utf-8")
    assert s.reload_limits_if_changed() is False                # відхилено, ліміти лишились
    assert s.loop.fsm.cfg.cool_daily_loss == Decimal("0.015")
    kinds = [e.kind for e in s._jbuf]
    assert kinds == ["control.risk_limits", "control.risk_limits_rejected"]


def test_live_profiles_and_scenarios() -> None:
    rep = WorkerProfile.load("replay")
    assert rep.kind is RunKind.REPLAY and rep.session is not None and rep.session.is_file()
    assert rep.streams == ("kline", "markPrice") and rep.speed == 30
    assert rep.backtest_config().u_enter == 0.2 and rep.backtest_config().record_traces == "all"
    pap = WorkerProfile.load("paper")
    assert pap.kind is RunKind.PAPER and pap.session is None and pap.backtest_config().u_enter == 0.25
    with pytest.raises(ConfigValidationError):
        WorkerProfile.load("backtest")                        # не live-профіль
    assert scenario_path("flash_crash").is_file()
    with pytest.raises(WorkerError):
        scenario_path("no_such_scenario")
    assert header_line(feed="REPLAY", seed=20260918, q=0.968) == (
        "MODE: PAPER · FEED: REPLAY · NO MAINNET KEYS · SEED 20260918 · Q=0.97")
    assert header_line(feed="LIVE", seed=1, q=None).endswith("Q=—")


def test_session_dataset_hash_identifies_source_warmup_and_session_start() -> None:
    base = {"feed": "REPLAY", "source": "fixtures/ws/x.jsonl.gz", "source_sha256": "ab", "scenario": None,
            "streams": ("kline", "markPrice"), "warmup_dataset_hash": "cd", "started_ns": 1}
    h = session_dataset_hash(**base)  # type: ignore[arg-type]
    assert len(h) == 64 and h == session_dataset_hash(**{**base, "streams": ("markPrice", "kline")})  # type: ignore[arg-type]
    for k, v in (("source_sha256", "ff"), ("scenario", "flash_crash"), ("warmup_dataset_hash", "ee"),
                 ("started_ns", 2)):
        assert session_dataset_hash(**{**base, k: v}) != h  # type: ignore[arg-type]


def test_warmup_must_cover_feature_lookback() -> None:
    ds = load_fixture_dataset()
    cfg = BacktestConfig.from_profile("replay")
    with pytest.raises(WorkerError):
        TradingSession(ds.instrument, cfg, seed=1, run_id=RUN_ID, kind=RunKind.REPLAY, feed="REPLAY",
                       warmup_bars=100)
    s = TradingSession(ds.instrument, cfg, seed=1, run_id=RUN_ID, kind=RunKind.REPLAY, feed="REPLAY",
                       warmup_bars=cfg.resolved_warmup())
    with pytest.raises(WorkerError):
        s.warm_up(ds.slice(0, 600))                          # довжина має дорівнювати оголошеній
    asyncio.run(s.begin({"k": "v"}))
    assert s.started and s.journal.next_seq == 1


def test_persisted_runs_with_same_seed_get_distinct_client_order_ids() -> None:
    """client_order_id UNIQUE у sim_order: два збережені прогони з тим самим seed не колізують (W-10), а той
    самий (seed, run_id) відтворює ті самі id; без run_id — як раніше (лише seed)."""
    ds = load_fixture_dataset().slice(0, 1200)
    cfg = BacktestConfig.from_profile("backtest")

    def ids(run_id: UUID | None) -> list[UUID]:
        res = run_backtest(ds, cfg, seed=11, run_id=run_id)
        return [o.request.client_order_id for o in res.orders]

    a, b, a2 = ids(UUID(int=1)), ids(UUID(int=2)), ids(UUID(int=1))
    assert a and a == a2 and not set(a) & set(b)
    assert ids(None) == ids(None)


def test_ingest_pump_stop_never_cancels_a_record_in_progress() -> None:
    """Зупинка ingest-воркера (SIGINT/дедлайн) не рве обробку запису посередині: інакше транзакція «свічка +
    пакет журналу» відкотилася б, вийняті з буфера записи журналу загубились, і ланцюг у БД мав би дірку."""

    async def scenario() -> tuple[str, list[int], bool]:
        stop, halted, entered, release = asyncio.Event(), asyncio.Event(), asyncio.Event(), asyncio.Event()
        handled: list[int] = []
        finished = False

        async def source():  # type: ignore[no-untyped-def]
            for i in range(3):
                yield i

        async def handle(x: int) -> None:
            nonlocal finished
            entered.set()
            await release.wait()                  # «транзакція БД» у процесі
            handled.append(x)
            finished = True

        task = asyncio.create_task(pump(source(), handle, stop=stop, halted=halted))  # type: ignore[arg-type]
        await entered.wait()
        stop.set()
        await halted.wait()                       # рішення зупинитись прийнято, поки запис обробляється
        release.set()
        return await task, handled, finished

    reason, handled, finished = asyncio.run(scenario())
    assert reason == "stop" and handled == [0] and finished     # дообробив поточний і не взяв наступний


def test_ingest_pump_cancels_only_the_wait_for_the_next_record() -> None:
    async def scenario(deadline: float | None) -> tuple[str, list[int]]:
        stop, never = asyncio.Event(), asyncio.Event()
        handled: list[int] = []

        async def source():  # type: ignore[no-untyped-def]
            yield 0
            await never.wait()                    # живий WS мовчить
            yield 1

        async def handle(x: int) -> None:
            handled.append(x)
            if deadline is None:
                stop.set()

        return await pump(source(), handle, stop=stop, deadline_s=deadline), handled  # type: ignore[arg-type]

    assert asyncio.run(scenario(None)) == ("stop", [0])
    assert asyncio.run(scenario(0.001)) == ("deadline", [0])

    async def finite() -> str:
        async def source():  # type: ignore[no-untyped-def]
            for i in range(4):
                yield i

        got: list[int] = []

        async def handle(x: int) -> None:
            got.append(x)

        reason = await pump(source(), handle, stop=asyncio.Event())  # type: ignore[arg-type]
        assert got == [0, 1, 2, 3]
        return reason

    assert asyncio.run(finite()) == "eof"

    async def failing() -> None:
        async def source():  # type: ignore[no-untyped-def]
            yield 0

        async def handle(x: int) -> None:
            raise RuntimeError("db down")

        await pump(source(), handle, stop=asyncio.Event())  # type: ignore[arg-type]

    with pytest.raises(RuntimeError, match="db down"):
        asyncio.run(failing())
