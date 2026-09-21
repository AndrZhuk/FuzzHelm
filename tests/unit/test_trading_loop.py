"""TradingLoop — один код рішень для бектесту і live/replay: виконання t → t+1, стопи/TP, гістерезис,
ризик-ланцюг і автомат станів, flatten-all у HALTED, фандинг, трасування рішень, конфігурація.

Автор: Андрій Жук, 2026. Сценарії з заскриптованим наміром ядра (tests/helpers/engine_scripted.py) над
реальними барами fixtures/rest/binance_klines.json.gz: детерміновані і не залежать від того, що саме видасть
нечітке ядро.
"""

from __future__ import annotations

import gzip
import json
import statistics
from decimal import Decimal

import pytest
from tests.helpers.engine_scripted import base_config, fixture, run_loop, scripted_loop

from fuzzhelm.backtest.dataset import DEFAULT_KLINES, Dataset, load_exchange_instrument
from fuzzhelm.backtest.engine import BacktestConfig, EngineInvariantError, TradingLoop, run_backtest
from fuzzhelm.core.clock import NS_PER_DAY
from fuzzhelm.core.enums import ExitReason, OrderType, RiskState, Role, Side
from fuzzhelm.core.errors import ConfigValidationError, PermissionDeniedError
from fuzzhelm.decision.narrative_uk import narrate
from fuzzhelm.ingest.normalize import normalize_rest_klines
from fuzzhelm.sizing.vol_target import VolTarget

NS_PER_HOUR = 3_600_000_000_000


def _bar_index(ds: Dataset, t_ns: int) -> int:
    return int((t_ns - int(ds.t_ns[0])) // (int(ds.t_ns[1]) - int(ds.t_ns[0])))


# ---------------------------------------------------------------- один шлях для live і бектесту


def test_live_candle_path_equals_backtest_path() -> None:
    inst = load_exchange_instrument("BTCUSDT")
    rows = json.loads(gzip.decompress(DEFAULT_KLINES.read_bytes()))[:1100]
    candles = normalize_rest_klines(rows, inst, ts_ingest_ns=rows[-1][6] * 1_000_000 + 10**9)
    cfg = base_config().with_params(record_traces="trades", check_invariants=True)
    live = TradingLoop(inst, cfg, seed=7)
    for cd in candles:                                   # live/replay: DTO-свічки по одній
        live.on_candle(cd)
    bt = run_backtest(Dataset.from_candles(candles, inst), cfg, seed=7)
    assert len(bt.fills) > 0
    assert live.equity == bt.equity
    assert [f.model_dump() for f in live.fills] == [f.model_dump() for f in bt.fills]
    assert live.decisions == bt.decisions
    with pytest.raises(ValueError, match="closed"):
        live.on_candle(candles[-1].model_copy(update={"is_closed": False}))
    with pytest.raises(ValueError, match="trades"):
        live.on_candle(candles[-1].model_copy(update={"instrument": "ETH-USDT-PERP"}))


# ---------------------------------------------------------------- виконання і рівні позиції


def test_entry_fills_at_next_open_with_protective_stop_and_tp() -> None:
    ds = fixture()
    k = next(i for i in range(40, 400) if ds.o[i + 1] != ds.c[i])  # бар, де open_{t+1} ≠ close_t
    loop = scripted_loop({k: 0.3, k + 1: 0.2}, cost_mode="zero")
    run_loop(loop, ds, stop=k + 2)
    entry = [o for o in loop.orders if o.role == "enter"]
    stop = [o for o in loop.orders if o.role == "stop"]
    assert len(entry) == len(stop) == 1
    e, s = entry[0], stop[0]
    assert e.ts_created_ns == ds.close_time_ns(k) and e.decision_ns == int(ds.t_ns[k])
    [fill] = loop.fills
    assert fill.ts_fill_ns == int(ds.t_ns[k + 1])                    # open t+1, не close t
    assert fill.price == ds.dec_bar(k + 1).o != ds.dec_bar(k).c
    assert fill.side is Side.LONG and fill.qty == e.request.qty
    dec = next(d for d in loop.decisions if d.order_ids)
    ref_c = ds.dec_bar(k).c
    assert s.request.otype is OrderType.STOP_MARKET and s.request.reduce_only
    assert s.request.side is Side.SHORT and s.request.qty == fill.qty
    assert s.request.stop_price == dec.stop_price < ref_c < dec.tp_price
    dist = ref_c - dec.stop_price
    m = Decimal(str(base_config().tp_multiple))
    assert abs((dec.tp_price - ref_c) - m * dist) <= 2 * ds.instrument.tick_size
    pos = loop.open_position
    assert pos is not None and pos.side == 1 and pos.opening_decision_ns == int(ds.t_ns[k])
    assert pos.stop_price == dec.stop_price and pos.tp_price == dec.tp_price
    assert pos.liq_price is None                        # лонг із плечем ≤ 3: P_liq ≤ 0 — ліквідація недосяжна
    assert pos.leverage is not None and pos.leverage <= 3


def test_hysteresis_exit_and_flip_are_signal_exits() -> None:
    script = {40: 0.3, 41: 0.2, 42: 0.15, 43: 0.13, 44: 0.13, 45: 0.1,
              50: -0.3, 51: -0.2, 52: -0.2, 53: -0.2, 54: -0.2, 55: 0.3}
    loop = scripted_loop(script, cost_mode="zero")
    run_loop(loop, fixture(), stop=70)
    ds = fixture()
    assert [(p.side, p.exit_reason) for p in loop.positions] == [
        (1, ExitReason.SIGNAL), (-1, ExitReason.SIGNAL), (1, ExitReason.SIGNAL)]
    assert [_bar_index(ds, p.opened_at_ns) for p in loop.positions] == [41, 51, 56]
    assert [_bar_index(ds, p.closed_at_ns or 0) for p in loop.positions] == [46, 56, 57]   # 0.12 > u = 0.1
    flip = next(o for o in loop.orders if o.role == "flip")
    assert flip.request.qty == 2 * loop.positions[1].qty and not flip.request.reduce_only
    # вихід — reduce-only; стоп старої позиції скасовано розворотом (не лишився «сиротою»)
    assert all(o.request.reduce_only for o in loop.orders if o.role == "exit")
    loop.refresh_order_statuses()
    assert all(o.status.value == "CANCELED" for o in loop.orders if o.role == "stop")


def test_take_profit_fill_gets_a_sim_order_row_bound_to_the_opening_decision() -> None:
    # TP — рівень позиції в брокері (EXE-03): його виконання — синтетична заявка, якої рушій не подавав;
    # для рядка sim_order (decision_id NOT NULL) вона прив'язується до рішення, що відкрило позицію
    loop = scripted_loop(lambda t: 0.3 if t >= 40 else 0.0, cost_mode="zero", chi=2.5, tp_multiple=0.05)
    run_loop(loop, fixture(), stop=400)
    tp_trades = [p for p in loop.positions if p.exit_reason is ExitReason.TP]
    assert tp_trades, "a TP at 0.05·stop distance must be hit within 360 bars"
    tp_orders = [o for o in loop.orders if o.role == "tp"]
    assert len(tp_orders) == len(tp_trades)
    for o, pos in zip(tp_orders, tp_trades, strict=True):
        assert o.decision_ns == pos.opening_decision_ns and o.status.value == "FILLED"
        assert o.request.reduce_only and o.request.qty == o.filled_qty == pos.qty
        assert o.avg_fill_price == pos.exit_price


@pytest.mark.parametrize("engine", ["mamdani"])
def test_fast_intent_path_equals_traced_decision(engine: str) -> None:
    # бектест без трасування йде через DecisionCore.intent (infer_u); числа мусять бути ТІ САМІ
    loop = TradingLoop(fixture().instrument, base_config().with_params(engine=engine, record_traces="none",
                                                                       warmup_bars=60), seed=1)
    compared = 0
    for i, (bar, dbar, close_ns) in enumerate(fixture().slice(0, 400).feed()):
        loop.step(bar, close_ns, dbar=dbar)
        if i >= loop.decide_from:
            tr = loop.core.decide(loop.window, bar.t_ns)
            assert loop.core.intent(loop.window) == (tr.u_raw, tr.kappa, tr.u_final)
            compared += 1
    assert compared == 400 - loop.decide_from


def test_gate_resyncs_after_stop_exit() -> None:
    # тісний стоп (χ = 0.1·ATR) спрацьовує; далі u = 0.2 між порогами — повторного входу бути не може
    script = {i: (0.3 if i == 40 else 0.2) for i in range(40, 120)}
    loop = scripted_loop(script, cost_mode="zero", chi=0.1, tp_multiple=1000.0)
    run_loop(loop, fixture(), stop=120)
    assert loop.positions and loop.positions[0].exit_reason is ExitReason.STOP
    assert len(loop.positions) == 1 and loop.open_position is None
    assert sum(1 for o in loop.orders if o.role == "enter") == 1


def test_step_reports_order_status_updates_for_live_persistence() -> None:
    # live-воркер пише sim_order по кроках: нова заявка — у StepResult.orders, її подальші зміни
    # (виконання, скасування стопа закритої позиції) — у StepResult.order_updates рівно раз
    loop = scripted_loop({40: 0.3, 41: 0.2, 42: 0.2, 43: 0.0}, cost_mode="zero", chi=2.5, tp_multiple=50.0)
    created: dict[int, tuple[object, ...]] = {}
    updated: dict[int, tuple[object, ...]] = {}
    for i, (bar, dbar, close_ns) in enumerate(fixture().slice(0, 50).feed()):
        sr = loop.step(bar, close_ns, dbar=dbar)
        created[i], updated[i] = sr.orders, sr.order_updates
    entry, stop, exit_ = (next(o for o in loop.orders if o.role == r) for r in ("enter", "stop", "exit"))
    assert [p.exit_reason for p in loop.positions] == [ExitReason.SIGNAL]
    assert entry in created[40] and stop in created[40] and exit_ in created[43]
    assert updated[41] == (entry,) and entry.status.value == "FILLED"
    assert {id(o) for o in updated[44]} == {id(exit_), id(stop)}
    assert exit_.status.value == "FILLED" and stop.status.value == "CANCELED"
    assert sum(len(u) for u in updated.values()) == 3                 # кожна зміна — один раз


def test_sizer_rejection_is_not_sent_to_the_risk_chain() -> None:
    # E = 10 USDT: q·p < minNotional ⇒ сайзер відмовляє (BELOW_MIN_NOTIONAL); оцінювати ризик нічого
    loop = scripted_loop({40: 0.3}, initial_equity=Decimal("10"))
    steps = [loop.step(bar, c, dbar=d) for bar, d, c in fixture().slice(0, 45).feed()]
    d = steps[40].decision
    assert d is not None and d.gate_side == 1 and d.requested_qty is None
    assert d.action == "none" and d.verdict is None and d.order_ids == ()
    assert loop.orders == [] and not [e for e in loop.risk_events if e.rule != "risk_state"]
    # те саме з повним трасуванням і реальним ядром: причина відмови — у trace.risk
    cfg = base_config().with_params(warmup_bars=60, record_traces="all", initial_equity=Decimal("10"))
    res = run_backtest(fixture().slice(0, 400), cfg, seed=3)
    intents = [x for x in res.decisions if x.gate_side != 0]
    assert intents and res.orders == []
    for x in intents:
        assert x.trace is not None and x.trace.risk is not None and x.trace.risk["evaluated"] is False
        assert x.trace.risk["note"] == "BELOW_MIN_NOTIONAL" and x.requested_qty is None


# ---------------------------------------------------------------- ризик-контур у циклі


def test_halted_means_zero_new_exposure_and_flatten_all() -> None:
    ds = fixture()
    loop = scripted_loop(lambda t: 0.3 if t >= 40 else 0.0)
    run_loop(loop, ds, stop=60)
    assert loop.portfolio.position_qty(ds.instrument.symbol_canon) > 0
    loop.fsm.killswitch.trip("test: operator-independent latch", ds.close_time_ns(59))
    feed = list(ds.slice(0, 120).feed())
    for bar, dbar, close_ns in feed[60:]:
        before = abs(loop.portfolio.position_qty(ds.instrument.symbol_canon))
        sr = loop.step(bar, close_ns, dbar=dbar)
        assert abs(sr.position_qty) <= before                     # HALTED ⇒ жодного приросту
        assert sr.risk_state is RiskState.HALTED and sr.kappa_mode == 0
    last = loop.positions[-1]
    assert last.exit_reason is ExitReason.HALT and _bar_index(ds, last.closed_at_ns or 0) == 61
    flatten = [o for o in loop.orders if o.role == "flatten"]
    assert len(flatten) == 1 and flatten[0].request.reduce_only
    assert flatten[0].intent_reason is ExitReason.HALT
    assert loop.portfolio.position_qty(ds.instrument.symbol_canon) == 0
    late = ds.close_time_ns(59)
    assert not [o for o in loop.orders if o.role in ("enter", "flip") and o.ts_created_ns > late]
    assert loop.halted_at == 60


def test_killswitch_tripped_between_bars_cancels_queued_increase_but_keeps_open_stop() -> None:
    # оператор/API спрацьовує засувку ПІСЛЯ рішення на закритті t, але ДО open_{t+1}: заявка на вхід
    # (або розворот), що чекає open_{t+1}, не має виконатися — HALTED ⇒ жодної нової експозиції (ENG-20)
    ds = fixture()
    sym = ds.instrument.symbol_canon
    feed = list(ds.slice(0, 60).feed())
    flat = scripted_loop({40: 0.3})
    for i, (bar, dbar, close_ns) in enumerate(feed[:45]):
        if i == 41:
            flat.fsm.killswitch.trip("operator", feed[40][2])
        sr = flat.step(bar, close_ns, dbar=dbar)       # check_invariants=True: засувка ловиться і там
        if i == 41:
            assert sr.fills == () and sr.position_qty == 0 and sr.risk_state is RiskState.HALTED
            assert {o.role for o in sr.order_updates} == {"enter", "stop"}
            assert all(o.status.value == "CANCELED" for o in sr.order_updates)
    assert flat.fills == [] and flat.positions == [] and flat.open_position is None

    # розворот у черзі: скасовано лише його (і стоп нової позиції); стоп відкритого лонга живе до flatten
    long_ = scripted_loop({40: 0.3, 41: 0.2, 42: 0.2, 43: -0.3})
    for i, (bar, dbar, close_ns) in enumerate(feed[:50]):
        if i == 44:
            long_.fsm.killswitch.trip("operator", feed[43][2])
        sr = long_.step(bar, close_ns, dbar=dbar)
        if i == 44:
            assert sr.position_qty > 0                  # розворот не виконано, лонг лишився
            old_stop = next(o for o in long_.orders if o.role == "stop" and o.decision_ns == int(ds.t_ns[40]))
            assert old_stop.status.value == "NEW"        # захист відкритої позиції не знято
            assert sr.decision is not None and sr.decision.action == "flatten"
    flip = next(o for o in long_.orders if o.role == "flip")
    assert flip.status.value == "CANCELED" and flip.filled_qty == 0
    [pos] = long_.positions
    assert pos.side == 1 and pos.exit_reason is ExitReason.HALT
    assert long_.portfolio.position_qty(sym) == 0


def test_halt_release_requires_admin_then_cooldown_is_reduce_only() -> None:
    # буквальна політика §5.12 (cooldown_policy=reduce_only); типову scaled_entries перевіряє
    # tests/unit/test_riskfix_cooldown.py (вхід у COOLDOWN з κ = 0.25)
    ds = fixture()
    loop = scripted_loop(lambda t: 0.3 if t >= 40 else 0.0, record_traces="none",
                         cooldown_policy="reduce_only")
    run_loop(loop, ds, stop=60)
    loop.fsm.killswitch.trip("test", ds.close_time_ns(59))
    feed = list(ds.slice(0, 200).feed())
    for bar, dbar, close_ns in feed[60:70]:
        loop.step(bar, close_ns, dbar=dbar)
    with pytest.raises(PermissionDeniedError):
        loop.release_halt(Role.ANALYST)
    assert loop.fsm.state is RiskState.HALTED
    tr = loop.release_halt(Role.ADMIN, actor="admin@test")
    assert tr is not None and tr.state_to is RiskState.COOLDOWN and tr.actor == "admin@test"
    sym = ds.instrument.symbol_canon
    cooldown_bars = 0
    for bar, dbar, close_ns in feed[70:]:
        sr = loop.step(bar, close_ns, dbar=dbar)
        if sr.risk_state is RiskState.COOLDOWN:              # COOLDOWN: reduce-only — кожен намір входу VETO
            cooldown_bars += 1
            assert sr.position_qty == 0 and sr.decision is not None and sr.decision.verdict == "VETO"
            assert any(e.rule == "risk_mode" and e.verdict == "VETO" for e in sr.risk_events)
    assert cooldown_bars == loop.risk_cfg.state_machine.cool_dwell - 1     # вихід на барі з dwell = 30
    path = [(t.state_from, t.state_to) for t in loop.transitions]
    assert (RiskState.COOLDOWN, RiskState.WARNING) in path    # після витримки → WARNING (κ = 0.5)
    assert path[-1][1] is not RiskState.HALTED
    assert loop.portfolio.position_qty(sym) > 0              # вхід знову дозволено


def test_vetoed_flip_closes_with_risk_veto() -> None:
    ds = fixture()
    loop = scripted_loop({40: 0.3, 41: 0.2, 42: 0.2, 43: -0.3}, cost_mode="zero")
    for i, (bar, dbar, close_ns) in enumerate(ds.slice(0, 50).feed()):
        # поганий потік даних (Q < 0.90) на барі розвороту: StaleDataGuard відхиляє новий бік
        loop.step(bar, close_ns, dbar=dbar, dq_score=Decimal("0.5") if i == 43 else Decimal(1))
    d = next(x for x in loop.decisions if x.open_time_ns == int(ds.t_ns[43]))
    assert d.action == "exit" and d.verdict == "VETO" and d.target_qty == 0
    assert d.requested_qty is not None and d.requested_qty < 0
    [pos] = loop.positions
    assert pos.side == 1 and pos.exit_reason is ExitReason.RISK_VETO
    stale = [e for e in loop.risk_events if e.rule == "stale_data" and e.ts_ns == ds.close_time_ns(43)]
    assert stale and stale[0].verdict == "VETO" and stale[0].observed == Decimal("0.5")
    assert stale[0].limit_value == Decimal("0.9")
    assert loop.open_position is None                        # розворот не відбувся


def test_every_intent_is_journaled_by_all_rules_with_observed_and_limit() -> None:
    loop = scripted_loop({40: 0.3, 45: 0.0})
    run_loop(loop, fixture(), stop=50)
    by_ts: dict[int, list[str]] = {}
    for e in loop.risk_events:
        if e.rule != "risk_state":
            by_ts.setdefault(e.ts_ns, []).append(e.rule)
            assert e.limit_value is not None and e.verdict is not None
    assert len(by_ts) == 2                                   # вхід і вихід — два наміри
    for rules in by_ts.values():
        assert rules == ["stale_data", "max_position_notional", "max_gross_leverage", "max_daily_loss",
                         "max_drawdown_halt", "liquidation_buffer", "risk_mode"]


# ---------------------------------------------------------------- фандинг


def _funding_moments(ds: Dataset) -> list[int]:
    t0, t1 = int(ds.t_ns[0]), int(ds.t_ns[-1])
    day = t0 - t0 % NS_PER_DAY
    out = []
    while day <= t1:
        out += [day + h * NS_PER_HOUR for h in (0, 8, 16) if t0 < day + h * NS_PER_HOUR <= t1]
        day += NS_PER_DAY
    return out


def test_funding_series_rates_applied_at_funding_moments() -> None:
    ds = fixture()
    moments = _funding_moments(ds)
    assert len(moments) >= 5
    rates = [0.0001 * (j + 1) for j in range(len(moments))]
    series_t = [m + 4_000_000 for m in moments]              # Binance: fundingTime з мілісекундним «хвостом»
    hold = lambda t: 0.3 if t >= 40 else 0.0  # noqa: E731
    wide = {"chi": 2.5, "tp_multiple": 50.0, "cost_mode": "full"}
    with_series = scripted_loop(hold, **wide)
    with_series.set_funding_series(series_t, rates)
    run_loop(with_series, ds)
    fallback = scripted_loop(hold, **wide)
    run_loop(fallback, ds)
    charges = with_series.funding
    assert charges, "position must span at least one funding moment"
    for ch in charges:
        j = moments.index(ch.ts_ns)
        assert ch.rate == Decimal(str(rates[j]))
        assert ch.amount == ch.position_qty * ch.mark_price * ch.rate
        assert ch.mark_price == ds.dec_bar(_bar_index(ds, ch.ts_ns)).o
    assert all(ch.rate == Decimal("0.0001") for ch in fallback.funding)
    assert sum(ch.amount for ch in charges) == with_series.portfolio.funding_paid


# ---------------------------------------------------------------- трасування, прогрів, конфіг


def test_decision_trace_has_sizing_and_risk_attached() -> None:
    res = run_backtest(fixture().slice(0, 800), base_config().with_params(record_traces="all"), seed=3)
    assert res.decisions
    for d in res.decisions:
        tr = d.trace
        assert tr is not None and tr.open_time_ns == d.open_time_ns and tr.u_final == d.u_final
        sz = tr.sizing
        assert sz is not None
        assert {"q_atr", "q_vt", "q_lev", "s_t", "sigma_ann", "binding_constraint", "kappa_mode",
                "qty"} <= set(sz)
        rk = tr.risk
        assert rk is not None and rk["state"] in {s.value for s in RiskState}
        assert rk["evaluated"] is bool(d.requested_qty is not None)
        if rk["evaluated"]:
            assert len(rk["records"]) == 7 and rk["verdict"] == d.verdict
    evaluated = [d for d in res.decisions if d.trace is not None and d.trace.risk["evaluated"]]
    assert evaluated
    text = narrate(evaluated[0].trace)
    assert "Спрацювало правило" in text and "u_final" in text


def test_warmup_blocks_decisions_and_sigma_base_is_warmup_median() -> None:
    ds = fixture().slice(0, 600)
    loop = TradingLoop(ds.instrument, base_config().with_params(record_traces="none"), seed=1)
    assert loop.warmup_bars == 523 and loop.decide_from == 522
    decided = []
    for bar, dbar, close_ns in ds.feed():
        decided.append(loop.step(bar, close_ns, dbar=dbar).decided)
    assert not any(decided[:522]) and all(decided[522:])
    vt = VolTarget.from_config(loop.risk_cfg.sizing)
    sig = [vt.update(ds.c[i].item() / ds.c[i - 1].item() - 1.0)[0] for i in range(1, 522)]
    assert loop.sigma_base == statistics.median(sig)


def test_config_roundtrip_identity_and_profiles() -> None:
    cfg = base_config()
    assert BacktestConfig.from_dict(cfg.to_dict()) == cfg
    assert (cfg.engine, cfg.cost_mode, cfg.n_atr, cfg.chi, cfg.u_enter, cfg.rho_base, cfg.lam) == (
        "mamdani", "full", 14, 2.0, 0.25, 0.005, 0.94)
    cell = cfg.with_params(n_atr=21, chi=1.5, u_enter=0.3, rho_base=0.0025, lam=0.97)
    assert cell.config_hash != cfg.config_hash
    trees = cell.resolved_trees()
    assert trees["detectors"]["features"]["n_atr"] == 21
    assert trees["risk_limits"]["sizing"]["chi_atr"] == 1.5
    assert trees["risk_limits"]["hysteresis"]["enter"] == 0.3
    assert cfg.with_params(record_traces="all", check_invariants=True).config_hash == cfg.config_hash
    assert BacktestConfig.from_profile("grid").record_traces == "none"
    assert BacktestConfig.from_profile("backtest").record_traces == "trades"
    with pytest.raises(ValueError):
        cfg.with_params(detectors=("no_such_detector",))
    with pytest.raises(ValueError):
        cfg.with_params(engine="sugeno")
    for bad, path in (({"take_profit": {"multiple_of_stop": 0}}, "engine.take_profit.multiple_of_stop"),
                      ({"sigma_base": {"method": "ewma"}}, "engine.sigma_base.method"),
                      ({"no_such_key": 1}, "engine.no_such_key")):
        trees = {**cfg.trees, "engine": {**cfg.trees["engine"], **bad}}
        with pytest.raises(ConfigValidationError) as ei:
            BacktestConfig(trees=trees)
        assert ei.value.path == path
    sub = cfg.with_params(detectors=("ema_slope", "donchian", "vol_regime"))
    loop = TradingLoop(fixture().instrument, sub, seed=1)
    assert [d.name for d in loop.core.detectors] == ["ema_slope", "donchian", "vol_regime"]


def test_invariant_violation_is_reported() -> None:
    loop = scripted_loop({40: 0.3})
    run_loop(loop, fixture(), stop=45)
    loop.portfolio._cash += Decimal("0.01")                  # зіпсований облік: тотожність мусить впасти
    bar, dbar, close_ns = list(fixture().slice(0, 46).feed())[45]
    with pytest.raises(EngineInvariantError, match="accounting identity"):
        loop.step(bar, close_ns, dbar=dbar)
