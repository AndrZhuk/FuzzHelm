"""Property-тести рушія: для БУДЬ-ЯКОЇ послідовності намірів ядра і збоїв якості даних цикл тримає
інваріанти обліку й ризику (за обох політик COOLDOWN, RF-01); рішення до бару t не залежать від жодних
барів після t.

Автор: Андрій Жук, 2026. Реальні бари BTCUSDT 1m (fixtures/rest), короткий прогрів — щоб приклад був дешевим.
"""

from __future__ import annotations

import copy
import dataclasses
from decimal import Decimal
from typing import Any

import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from tests.helpers.engine_scripted import base_config, fixture, scripted_loop

from fuzzhelm.backtest.dataset import Dataset
from fuzzhelm.backtest.engine import DecisionEntry, StepResult, run_backtest
from fuzzhelm.core.enums import RiskState
from fuzzhelm.features.convert import to_float
from fuzzhelm.sizing.convert import to_decimal
from fuzzhelm.sizing.sizer import PositionSizer, SizingInput, SizingResult

pytestmark = pytest.mark.property

WARMUP = 30
N_SIGNALS = 60
U_VALUES = st.one_of(
    st.sampled_from([-0.9, -0.5, -0.3, -0.25, -0.2, -0.12, -0.1, 0.0, 0.1, 0.12, 0.2, 0.25, 0.3, 0.5, 0.9]),
    st.floats(min_value=-1.0, max_value=1.0, allow_nan=False),
)
POLICIES = st.sampled_from(["scaled_entries", "reduce_only"])


def _side(x: Decimal) -> int:
    return (x > 0) - (x < 0)


def _new_entry(d: DecisionEntry) -> bool:
    """Рішення відкриває НОВУ позицію: з пласкої книги або новий бік розвороту (не приріст наявної)."""
    return d.target_qty > 0 and d.target_side != _side(d.current_qty)


def _increase_allowed_in(state: RiskState, policy: str, d: DecisionEntry) -> bool:
    """Де ризик-контур дозволяє приріст: NORMAL/WARNING — так; COOLDOWN — лише новий вхід (scaled_entries)."""
    if state in (RiskState.NORMAL, RiskState.WARNING):
        return True
    return state is RiskState.COOLDOWN and policy == "scaled_entries" and _new_entry(d)


@settings(max_examples=40, deadline=None)
@given(us=st.lists(U_VALUES, min_size=N_SIGNALS, max_size=N_SIGNALS),
       bad_dq=st.sets(st.integers(min_value=0, max_value=N_SIGNALS - 1), max_size=10),
       trip_at=st.none() | st.integers(min_value=0, max_value=N_SIGNALS - 1),
       seed=st.integers(min_value=0, max_value=2**31), policy=POLICIES)
def test_engine_invariants_hold_for_any_signal_sequence(us: list[float], bad_dq: set[int],
                                                        trip_at: int | None, seed: int, policy: str) -> None:
    start = WARMUP - 1                                       # перше рішення — на барі прогріву
    script = {start + i: u for i, u in enumerate(us)}
    loop = scripted_loop(script, warmup=WARMUP, seed=seed,   # check_invariants=True: облік щобару
                         cooldown_policy=policy)
    ds = fixture().slice(0, start + N_SIGNALS + 2)
    sym = ds.instrument.symbol_canon
    cap = Decimal("0.30") * Decimal(ds.instrument.max_leverage)   # max_position_notional: маржа ≤ 0.3·E
    halted = False
    prev: StepResult | None = None
    for i, (bar, dbar, close_ns) in enumerate(ds.feed()):
        j = i - start
        if trip_at is not None and j == trip_at:
            loop.fsm.killswitch.trip("property", close_ns)
        before = abs(loop.portfolio.position_qty(sym))
        # засувка перед баром (HALTED або kill-switch, спрацьований між барами): жодних приростів на ньому
        latched = loop.fsm.state is RiskState.HALTED or loop.fsm.killswitch.is_tripped
        sr = loop.step(bar, close_ns, dbar=dbar, dq_score=Decimal("0.5") if j in bad_dq else Decimal(1))
        after = abs(sr.position_qty)
        if latched:
            assert after <= before and not [f for f in sr.fills if f.client_order_id in {
                o.request.client_order_id for o in loop.orders if o.role in ("enter", "flip")}]
        if after > before:
            # приріст можливий лише як виконання рішення попереднього бару, що пройшло ризик-ланцюг
            assert prev is not None and prev.decision is not None
            assert prev.decision.verdict in ("ALLOW", "SHRINK")
            assert _increase_allowed_in(prev.risk_state, policy, prev.decision)
            assert not halted
        d = sr.decision
        if d is not None and d.requested_qty is not None:
            # ризик-ланцюг ніколи не збільшує запитану ціль
            assert d.target_qty <= abs(d.requested_qty) or d.target_qty <= abs(d.current_qty)
            cur_side = (d.current_qty > 0) - (d.current_qty < 0)
            if d.target_qty > 0 and d.target_side != cur_side:          # новий бік = приріст експозиції
                assert d.target_qty * dbar.c <= cap * sr.equity           # маржа позиції ≤ 0.30·E
                assert _increase_allowed_in(sr.risk_state, policy, d)
                assert j not in bad_dq, "increase approved on bad data quality (Q < 0.90)"
        halted = halted or sr.risk_state is RiskState.HALTED
        if halted:
            assert sr.kappa_mode == 0 and not [o for o in sr.orders if o.role in ("enter", "flip")]
        prev = sr
    assert abs(loop.portfolio.identity_residual()) <= Decimal("1E-18")


def _tight_trees() -> dict[str, Any]:
    """Пороги автомата в частках відсотка: WARNING/COOLDOWN досяжні за кілька десятків барів фікстури."""
    risk = copy.deepcopy(dict(base_config().trees["risk_limits"]))
    risk["state_machine"] = {**risk["state_machine"], "warn_enter": 0.001, "warn_exit": 0.0005,
                             "warn_dwell": 3, "cool_enter": 0.002, "cool_exit": 0.0015, "cool_dwell": 5,
                             "halt_enter": 0.05, "cool_daily_loss": 0.03, "halt_daily_loss": 0.04}
    return {**base_config().trees, "risk_limits": risk}


_TIGHT = _tight_trees()


class _SizerSpy:
    """Обгортка сайзера циклу: запам'ятовує останній вхід, щоб тест міг НЕЗАЛЕЖНО перерахувати розмір NORMAL
    (κ = 1) для тих самих входів і перевірити, що ціль рішення ≤ floor(κ_mode(стан)·q_raw(NORMAL))."""

    def __init__(self, inner: PositionSizer) -> None:
        self.inner = inner
        self.last: SizingInput | None = None

    def size(self, inp: SizingInput) -> SizingResult:
        self.last = inp
        return self.inner.size(inp)


@settings(max_examples=30, deadline=None)
@given(us=st.lists(U_VALUES, min_size=120, max_size=120), seed=st.integers(min_value=0, max_value=2**31),
       policy=POLICIES)
def test_cooldown_never_increases_an_open_position_and_reduce_only_never_enters(
        us: list[float], seed: int, policy: str) -> None:
    """Автомат із тісними порогами часто буває в COOLDOWN: за будь-якої послідовності намірів
    * наявну позицію в COOLDOWN не збільшено (за обох політик), приріст — лише новий вхід за scaled_entries;
    * кожен новий вхід (у будь-якому стані) не перевищує κ_mode(стан)·розмір NORMAL для тих самих входів
      сайзера (перераховано незалежно, κ з конфігурації автомата), а виконана позиція — схваленої цілі;
    * новий вхід у COOLDOWN (scaled_entries) — з κ_mode = 0.25;
    * жодне рішення не дає ціль більшу за запитану (ланцюг ніколи не збільшує експозицію)."""
    start = WARMUP - 1
    loop = scripted_loop({start + i: u for i, u in enumerate(us)}, warmup=WARMUP, seed=seed,
                         cooldown_policy=policy, trees=_TIGHT)
    spy = _SizerSpy(loop._sizer)
    loop._sizer = spy  # type: ignore[assignment]
    kappa_of = loop.risk_cfg.state_machine.kappa_mode
    ds = fixture().slice(0, start + len(us) + 2)
    step = ds.instrument.step_size
    prev: StepResult | None = None
    for bar, dbar, close_ns in ds.feed():
        before = abs(loop.portfolio.position_qty(ds.instrument.symbol_canon))
        spy.last = None
        sr = loop.step(bar, close_ns, dbar=dbar)
        if abs(sr.position_qty) > before:
            assert prev is not None and prev.decision is not None
            assert _increase_allowed_in(prev.risk_state, policy, prev.decision)
            assert abs(sr.position_qty) <= prev.decision.target_qty          # виконано не більше схваленого
        d = sr.decision
        if d is not None and d.requested_qty is not None:
            assert d.target_qty <= abs(d.requested_qty) or d.target_qty <= abs(d.current_qty)
            if d.target_side == _side(d.current_qty) != 0:
                assert d.target_qty <= abs(d.current_qty)          # розмір фіксується на вході (ENG-04)
            if _new_entry(d):
                inp = spy.last
                assert inp is not None
                kappa = kappa_of[sr.risk_state]
                assert inp.kappa_mode == to_float(kappa)                        # сайзер отримав κ_mode стану
                normal = spy.inner.size(dataclasses.replace(inp, kappa_mode=1.0))
                assert d.target_qty <= to_decimal(to_float(kappa) * normal.q_raw, step)
            if sr.risk_state is RiskState.COOLDOWN and _new_entry(d):
                assert policy == "scaled_entries" and sr.kappa_mode == Decimal("0.25")
        prev = sr
    assert abs(loop.portfolio.identity_residual()) <= Decimal("1E-18")


@settings(max_examples=8, deadline=None)
@given(t=st.integers(min_value=70, max_value=150),
       scale=st.floats(min_value=0.8, max_value=1.25, allow_nan=False),
       vol_mult=st.floats(min_value=0.0, max_value=5.0, allow_nan=False))
def test_decisions_before_t_ignore_any_future_bars(t: int, scale: float, vol_mult: float) -> None:
    ds = fixture().slice(0, 160)
    tick = float(ds.instrument.tick_size)
    cols = {k: getattr(ds, k).copy() for k in ("o", "h", "l", "c", "v", "qv", "n")}
    for k in ("o", "h", "l", "c"):
        cols[k][t + 1:] = np.floor(cols[k][t + 1:] * scale / tick) * tick
    cols["v"][t + 1:] *= vol_mult
    alt_ds = Dataset.from_arrays(ds.instrument, tf=ds.tf, t_ns=ds.t_ns, **cols, source="perturbed")
    cfg = base_config().with_params(warmup_bars=40, record_traces="all")
    ref = run_backtest(ds, cfg, seed=5, hash_equity=False)
    alt = run_backtest(alt_ds, cfg, seed=5, hash_equity=False)
    cut = int(ds.t_ns[t])
    past = [d for d in ref.decisions if d.open_time_ns <= cut]
    assert past and past == [d for d in alt.decisions if d.open_time_ns <= cut]
    assert ref.equity[: t + 1] == alt.equity[: t + 1]
