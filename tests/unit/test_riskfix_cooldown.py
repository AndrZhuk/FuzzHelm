"""RF-01 (ENG-14): політика COOLDOWN — scaled_entries (типово, рішення автора) проти reduce_only (§5.12).

Найменування: tests/unit/test_riskfix_cooldown.py
Автор: Андрій Жук, 2026.

(а) scaled_entries: пласка книга в COOLDOWN відкриває позицію розміром κ_mode(COOLDOWN) = 0.25 від NORMAL
    (з точністю до квантування кроком); (б) позицію, відкриту до COOLDOWN, у COOLDOWN не збільшити за жодної
    політики; (в) reduce_only відтворює поглинаючий COOLDOWN пласкої книги (регресія ENG-14); автомат
    (таблиця, пороги, витримка) від політики не залежить. Сценарії циклу — заскриптований намір над
    реальними барами.
"""

from __future__ import annotations

import copy
import itertools
from decimal import Decimal
from typing import Any

import pytest
from tests.helpers.engine_scripted import base_config, fixture, scripted_loop

from fuzzhelm.backtest.engine import BacktestConfig, TradingLoop
from fuzzhelm.core.enums import RiskState, Role, VerdictKind
from fuzzhelm.core.errors import ConfigValidationError
from fuzzhelm.core.money import D0
from fuzzhelm.risk.config import CooldownPolicy, StateMachineCfg, load_risk_config
from fuzzhelm.risk.context import RiskContext
from fuzzhelm.risk.guard import RiskGuard, default_rules
from fuzzhelm.risk.state import TRANSITIONS, RiskObservation, RiskStateMachine
from fuzzhelm.sizing.convert import to_decimal
from fuzzhelm.sizing.sizer import PositionSizer, SizingInput, SizingParams
from fuzzhelm.sizing.vol_target import VolTarget

D = Decimal
POLICIES = [CooldownPolicy.SCALED_ENTRIES, CooldownPolicy.REDUCE_ONLY]
T0 = 1_789_516_800_000_000_000          # 2026-09-18 00:00 UTC


def _ctx(**kw: Any) -> RiskContext:
    base: dict[str, Any] = {
        "ts_ns": T0, "instrument": "BTC-USDT-PERP", "price": D(100), "equity": D(10_000),
        "current_qty": D(0), "target_qty": D(10), "atr": D("0.5"), "stop_distance": D(1),
        "risk_state": RiskState.COOLDOWN, "step_size": D("0.001"),
    }
    base.update(kw)
    return RiskContext(**base)


def _mode(res: Any) -> Any:
    return next(r for r in res.records if r.rule == "risk_mode")


# ---------------------------------------------------------------- конфігурація і перемикач


def test_cooldown_policy_is_a_config_switch_defaulting_to_scaled_entries() -> None:
    cfg = load_risk_config()                                       # config/risk_limits.yaml
    assert cfg.state_machine.cooldown_policy is CooldownPolicy.SCALED_ENTRIES
    assert StateMachineCfg().cooldown_policy is CooldownPolicy.SCALED_ENTRIES
    tree = copy.deepcopy(dict(base_config().trees["risk_limits"]))
    tree["state_machine"] = {**tree["state_machine"], "cooldown_policy": "reduce_only"}
    assert load_risk_config(tree).state_machine.cooldown_policy is CooldownPolicy.REDUCE_ONLY
    tree["state_machine"]["cooldown_policy"] = "no_such_policy"
    with pytest.raises(ConfigValidationError) as e:
        load_risk_config(tree)
    assert e.value.path == "state_machine.cooldown_policy"
    assert RiskGuard.from_config(cfg).cooldown_policy is CooldownPolicy.SCALED_ENTRIES


def test_cooldown_policy_enters_config_hash_and_is_written_explicitly() -> None:
    default = base_config()
    literal = default.with_params(cooldown_policy="reduce_only")
    assert default.cooldown_policy == "scaled_entries"
    assert default.resolved_trees()["risk_limits"]["state_machine"]["cooldown_policy"] == "scaled_entries"
    assert literal.risk_config().state_machine.cooldown_policy is CooldownPolicy.REDUCE_ONLY
    assert literal.config_hash != default.config_hash            # інша поведінка — інший прогін
    assert BacktestConfig.from_dict(literal.to_dict()) == literal
    # дерево без ключа (напр. старий run.config) = типова політика, записана в дерево явно
    tree = copy.deepcopy(dict(default.trees["risk_limits"]))
    tree["state_machine"] = {k: v for k, v in tree["state_machine"].items() if k != "cooldown_policy"}
    implicit = BacktestConfig(trees={**default.trees, "risk_limits": tree})
    assert implicit.config_hash == default.config_hash
    with pytest.raises(ValueError, match="cooldown_policy"):
        default.with_params(cooldown_policy="absorbing")


def test_hot_swapped_limits_without_policy_keep_the_run_policy() -> None:
    loop = TradingLoop(fixture().instrument, base_config().with_params(cooldown_policy="reduce_only"), seed=1)
    tree = copy.deepcopy(dict(base_config().trees["risk_limits"]))
    tree["state_machine"] = {k: v for k, v in tree["state_machine"].items() if k != "cooldown_policy"}
    new = loop.apply_risk_limits(tree)
    assert new.state_machine.cooldown_policy is CooldownPolicy.REDUCE_ONLY
    assert loop.guard.cooldown_policy is CooldownPolicy.REDUCE_ONLY
    tree["state_machine"]["cooldown_policy"] = "scaled_entries"      # явна заміна — діє
    assert loop.apply_risk_limits(tree).state_machine.cooldown_policy is CooldownPolicy.SCALED_ENTRIES
    with pytest.raises(ConfigValidationError):                        # невалідне дерево — стан не змінюється
        loop.apply_risk_limits({**tree, "state_machine": "not a mapping"})
    assert loop.guard.cooldown_policy is CooldownPolicy.SCALED_ENTRIES


# ---------------------------------------------------------------- автомат від політики не залежить


@pytest.mark.parametrize("policy", POLICIES)
def test_state_machine_path_is_independent_of_cooldown_policy(policy: CooldownPolicy) -> None:
    """Таблиця, пороги, витримка — ті самі; політика лише каже, чи дозволено НОВИЙ вхід у COOLDOWN."""
    ref = RiskStateMachine(StateMachineCfg())
    fsm = RiskStateMachine(StateMachineCfg(cooldown_policy=policy))
    dds = ["0", "0.05", "0.085", *(["0.06"] * 40), *(["0.04"] * 40), *(["0.02"] * 20), "0.13", "0.13"]
    for k, dd in enumerate(dds):
        obs = RiskObservation(T0 + (k + 1) * 60 * 10**9, D(100) * (1 - D(dd)))
        a, b = ref.update(obs), fsm.update(obs)
        assert (a is None) == (b is None) and fsm.state is ref.state and fsm.dwell_bars == ref.dwell_bars
        assert fsm.reduce_only is (fsm.state in (RiskState.COOLDOWN, RiskState.HALTED))
        expect = fsm.state in (RiskState.NORMAL, RiskState.WARNING) or (
            fsm.state is RiskState.COOLDOWN and policy is CooldownPolicy.SCALED_ENTRIES)
        assert fsm.entries_allowed is expect
    assert [t.state_to for t in fsm.transitions] == [t.state_to for t in ref.transitions]
    assert fsm.state is RiskState.HALTED and not fsm.entries_allowed      # засувка — за будь-якої політики
    assert len(TRANSITIONS) == 24


# ---------------------------------------------------------------- (а) вхід у COOLDOWN з κ = 0.25


def test_scaled_entries_flat_book_in_cooldown_opens_quarter_of_normal_size() -> None:
    cfg = load_risk_config()
    fsm = RiskStateMachine(cfg.state_machine)
    # повільна просадка по −1…1.5% на добу: денні ліміти не спрацьовують, DD = 8.2% → COOLDOWN
    for day, e in enumerate([10_000, 9_850, 9_700, 9_560, 9_420, 9_280, 9_180]):
        fsm.update(RiskObservation(T0 + day * 86_400 * 10**9, D(e)))
    assert fsm.state is RiskState.COOLDOWN and fsm.kappa_mode == D("0.25") and fsm.entries_allowed
    assert fsm.snapshot is not None and fsm.snapshot.day_return > -D("0.02")
    sizer = PositionSizer(SizingParams.from_config(cfg.sizing))
    step, price, equity = D("0.001"), D("63497.2"), D("9180")
    common: dict[str, Any] = {"u_final": 0.6, "equity": equity, "price": price, "atr": 35.0, "s_t": 0.5,
                              "step_size": step, "min_notional": D(100)}
    normal = sizer.size(SizingInput(kappa_mode=1.0, **common))
    cool = sizer.size(SizingInput(kappa_mode=fsm.kappa_mode_float, **common))
    assert normal.qty > 0 and cool.qty > 0
    assert cool.q_raw == 0.25 * normal.q_raw                           # κ — точний множник у float
    assert cool.qty == to_decimal(0.25 * normal.q_raw, step)           # квантування ВНИЗ після множення
    assert abs(cool.qty - normal.qty / 4) < step                       # 0.25× NORMAL з точністю до кроку
    guard = RiskGuard.from_config(cfg, killswitch=fsm.killswitch)
    ctx = RiskContext.from_snapshot(fsm.snapshot, instrument="BTC-USDT-PERP", price=price, current_qty=D0,
                                    target_qty=cool.qty, atr=D(35), stop_distance=D(70),
                                    risk_state=fsm.state, step_size=step)
    res = guard.evaluate(ctx)
    gate = _mode(res)
    assert res.verdict.kind is VerdictKind.ALLOW and res.approved_qty == cool.qty
    assert gate.verdict.kind is VerdictKind.ALLOW and (gate.observed, gate.limit) == (D(2), D(2))
    assert gate.payload["new_entry"] is True and gate.payload["cooldown_policy"] == "scaled_entries"
    literal = RiskGuard(default_rules(cfg), cooldown_policy="reduce_only").evaluate(ctx)
    assert literal.verdict.kind is VerdictKind.VETO and literal.approved_qty == 0
    assert (_mode(literal).observed, _mode(literal).limit) == (D(2), D(1))


# ---------------------------------------------------------------- (б) позицію до COOLDOWN не збільшити


@pytest.mark.parametrize("policy", POLICIES)
def test_position_opened_before_cooldown_cannot_be_increased_under_either_policy(
        policy: CooldownPolicy) -> None:
    guard = RiskGuard(default_rules(load_risk_config()), cooldown_policy=policy)
    res = guard.evaluate(_ctx(current_qty=D(10), target_qty=D(20)))
    gate = _mode(res)
    assert gate.verdict.kind is VerdictKind.VETO and gate.payload["new_entry"] is False
    assert (gate.observed, gate.limit) == (D(2), D(1))           # приріст наявної позиції — лише ≤ WARNING
    assert res.approved_qty == D(10) and res.increase_approved == 0 and res.order_qty == 0
    short = guard.evaluate(_ctx(current_qty=D(-10), target_qty=D(-15)))
    assert short.approved_qty == D(-10) and _mode(short).verdict.kind is VerdictKind.VETO
    # зменшення і закриття — завжди
    assert guard.evaluate(_ctx(current_qty=D(10), target_qty=D(4))).approved_qty == D(4)
    assert guard.evaluate(_ctx(current_qty=D(10), target_qty=D(0))).approved_qty == 0
    # розворот: стару позицію закрито (редукція), новий бік — новий вхід (лише за scaled_entries)
    flip = guard.evaluate(_ctx(current_qty=D(10), target_qty=D(-3)))
    assert flip.approved_qty == (D(-3) if policy is CooldownPolicy.SCALED_ENTRIES else D0)
    # у WARNING наявну позицію збільшувати можна (κ уже в сайзері) — політика стосується лише COOLDOWN
    warn = guard.evaluate(_ctx(current_qty=D(10), target_qty=D(20), risk_state=RiskState.WARNING))
    assert warn.approved_qty == D(20)


@pytest.mark.parametrize("policy", POLICIES)
def test_halted_and_killswitch_veto_every_increase_under_either_policy(policy: CooldownPolicy) -> None:
    guard = RiskGuard(default_rules(load_risk_config()), cooldown_policy=policy)
    for cur, tgt in itertools.product([D(0), D(5), D(-5)], [D(10), D(-10)]):
        res = guard.evaluate(_ctx(current_qty=cur, target_qty=tgt, risk_state=RiskState.HALTED))
        assert res.flatten_all and res.approved_qty == 0
        assert _mode(res).verdict.kind is VerdictKind.VETO


# ---------------------------------------------------------------- цикл: зняття HALTED → COOLDOWN → вхід


def _feed_until_cooldown(policy: str) -> tuple[TradingLoop, list[Any]]:
    ds = fixture()
    loop = scripted_loop(lambda t: 0.3 if t >= 40 else 0.0, cooldown_policy=policy)
    feed = list(ds.slice(0, 120).feed())
    for bar, dbar, close_ns in feed[:60]:
        loop.step(bar, close_ns, dbar=dbar)
    loop.fsm.killswitch.trip("test", feed[59][2])
    for bar, dbar, close_ns in feed[60:70]:
        loop.step(bar, close_ns, dbar=dbar)
    tr = loop.release_halt(Role.ADMIN, actor="admin@test")
    assert tr is not None and tr.state_to is RiskState.COOLDOWN
    return loop, feed


def test_scaled_entries_loop_enters_from_flat_book_in_cooldown_with_quarter_kappa() -> None:
    loop, feed = _feed_until_cooldown("scaled_entries")
    ds, sym = fixture(), fixture().instrument.symbol_canon
    bar, dbar, close_ns = feed[70]
    assert loop.portfolio.position_qty(sym) == 0
    sr = loop.step(bar, close_ns, dbar=dbar)
    assert sr.risk_state is RiskState.COOLDOWN and sr.kappa_mode == D("0.25")
    d = sr.decision
    assert d is not None and d.action == "enter" and d.verdict == "ALLOW"
    [entry] = [o for o in sr.orders if o.role == "enter"]
    gate = next(e for e in sr.risk_events if e.rule == "risk_mode")
    assert gate.verdict is VerdictKind.ALLOW and (gate.observed, gate.limit_value) == (D(2), D(2))
    # незалежний перерахунок входів сайзера на барі 70: σ-оцінка з цін закриття, ATR з вікна циклу
    vt = VolTarget.from_config(loop.risk_cfg.sizing)
    for i in range(1, 71):
        vt.update(ds.c[i].item() / ds.c[i - 1].item() - 1.0)
    atr = loop.window.feats(0).atr
    assert atr is not None
    sizer = PositionSizer(SizingParams.from_config(loop.risk_cfg.sizing))
    inst = ds.instrument
    common: dict[str, Any] = {"u_final": 0.3, "equity": sr.equity, "price": dbar.c, "atr": atr, "s_t": vt.s_t,
                              "step_size": inst.step_size, "min_notional": inst.min_notional,
                              "sigma_ann": vt.sigma_ann}
    cool = sizer.size(SizingInput(kappa_mode=0.25, **common))
    normal = sizer.size(SizingInput(kappa_mode=1.0, **common))
    assert entry.request.qty == cool.qty == d.target_qty == d.requested_qty
    assert cool.qty == to_decimal(0.25 * normal.q_raw, inst.step_size)
    assert abs(cool.qty - normal.qty / 4) < inst.step_size
    bar, dbar, close_ns = feed[71]
    nxt = loop.step(bar, close_ns, dbar=dbar)
    assert nxt.position_qty == cool.qty                                # виконано за open₇₁


def test_reduce_only_loop_vetoes_the_same_entry_after_release() -> None:
    loop, feed = _feed_until_cooldown("reduce_only")
    bar, dbar, close_ns = feed[70]
    sr = loop.step(bar, close_ns, dbar=dbar)
    assert sr.risk_state is RiskState.COOLDOWN and sr.decision is not None
    assert sr.decision.verdict == "VETO" and sr.decision.order_ids == ()
    gate = next(e for e in sr.risk_events if e.rule == "risk_mode")
    assert gate.verdict is VerdictKind.VETO and (gate.observed, gate.limit_value) == (D(2), D(1))


# ---------------------------------------------------------------- (в) регресія ENG-14: поглинаючий COOLDOWN


def _tight_trees() -> dict[str, Any]:
    """Пороги автомата в частках відсотка: COOLDOWN досяжний за кілька десятків хвилинних барів фікстури."""
    risk = copy.deepcopy(dict(base_config().trees["risk_limits"]))
    risk["state_machine"] = {**risk["state_machine"], "warn_enter": 0.001, "warn_exit": 0.0005,
                             "warn_dwell": 3, "cool_enter": 0.002, "cool_exit": 0.0015, "cool_dwell": 5,
                             "halt_enter": 0.05, "cool_daily_loss": 0.03, "halt_daily_loss": 0.04}
    return {**base_config().trees, "risk_limits": risk}


def _oracle_run(policy: str, n_bars: int = 900) -> tuple[TradingLoop, list[Any], int]:
    """Заскриптований намір знає майбутнє (тестовий «оракул», не рушій — цикл бачить лише бари ≤ t):
    до першого COOLDOWN — проти руху бару t+1 (детермінований збиток), на першому барі COOLDOWN — вихід
    (закрити позицію, відкриту ДО COOLDOWN), далі — за рухом (прибуток за нульових витрат)."""
    ds = fixture()
    c, o = ds.c, ds.o
    state: dict[str, Any] = {}

    def script(t: int) -> float:
        if state.get("cool_at") is None and state["loop"].fsm.state is RiskState.COOLDOWN:
            state["cool_at"] = t
        move = c[t + 1].item() - o[t + 1].item()
        sgn = (move > 0) - (move < 0)
        if state.get("cool_at") is None:
            return -0.3 * sgn
        return 0.0 if t == state["cool_at"] else 0.3 * sgn

    loop = scripted_loop(script, cost_mode="zero", cooldown_policy=policy, trees=_tight_trees())
    state["loop"] = loop
    steps = [loop.step(bar, close_ns, dbar=dbar) for bar, dbar, close_ns in ds.slice(0, n_bars).feed()]
    assert state.get("cool_at") is not None, "scenario must reach COOLDOWN"
    return loop, steps, state["cool_at"]


def test_reduce_only_cooldown_is_absorbing_for_a_flat_book_eng14() -> None:
    loop, steps, k = _oracle_run("reduce_only")
    cfg = loop.risk_cfg.state_machine
    entered = next(t for t in loop.transitions if t.state_to is RiskState.COOLDOWN)
    assert entered.drawdown > cfg.cool_exit                            # вхід при DD > 0.0015 …
    assert all(sr.risk_state is RiskState.COOLDOWN for sr in steps[k:])  # … і вихід неможливий до кінця
    assert loop.transitions[-1] is entered and loop.fsm.state is RiskState.COOLDOWN
    flat_from = next(i for i in range(k, len(steps)) if steps[i].position_qty == 0)   # бар виконання виходу
    tail = steps[flat_from:]
    assert len({sr.equity for sr in tail}) == 1                        # пласка книга: капітал (і DD) сталий
    assert not [f for sr in tail[1:] for f in sr.fills]               # жодного виконання після виходу
    intents = [sr for sr in tail if sr.decision is not None and sr.decision.requested_qty not in (None, D0)]
    assert len(intents) > 100                                          # оракул хотів торгувати весь час
    for sr in intents:
        gate = next(e for e in sr.risk_events if e.rule == "risk_mode")
        assert gate.verdict is VerdictKind.VETO and sr.decision is not None and sr.decision.order_ids == ()


def test_scaled_entries_cooldown_is_not_absorbing_on_the_same_path() -> None:
    loop, steps, k = _oracle_run("scaled_entries")
    _, lit_steps, lit_k = _oracle_run("reduce_only")
    assert k == lit_k and [sr.equity for sr in steps[:k]] == [sr.equity for sr in lit_steps[:k]]
    cool = [i for i in range(k, len(steps)) if steps[i].risk_state is RiskState.COOLDOWN]
    # у COOLDOWN відкрито нові позиції (з пласкої книги, κ = 0.25) — капітал і DD змінюються
    opened = [p for i in cool for p in steps[i].opened_positions]
    assert opened and all(steps[i].kappa_mode == D("0.25") for i in cool)
    assert len({steps[i].equity for i in cool}) > 1
    rec = next(t for t in loop.transitions if t.state_from is RiskState.COOLDOWN)
    assert rec.state_to is RiskState.WARNING and rec.event.value == "RECOVERY"
    assert rec.drawdown <= loop.risk_cfg.state_machine.cool_exit and rec.dwell_bars >= 5
