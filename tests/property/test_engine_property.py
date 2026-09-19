"""Property-тести рушія: для БУДЬ-ЯКОЇ послідовності намірів ядра і збоїв якості даних цикл тримає
інваріанти обліку й ризику; рішення до бару t не залежать від жодних барів після t.

Автор: Андрій Жук, 2026. Реальні бари BTCUSDT 1m (fixtures/rest), короткий прогрів — щоб приклад був дешевим.
"""

from __future__ import annotations

from decimal import Decimal

import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from tests.helpers.engine_scripted import base_config, fixture, scripted_loop

from fuzzhelm.backtest.dataset import Dataset
from fuzzhelm.backtest.engine import StepResult, run_backtest
from fuzzhelm.core.enums import RiskState

pytestmark = pytest.mark.property

WARMUP = 30
N_SIGNALS = 60
U_VALUES = st.one_of(
    st.sampled_from([-0.9, -0.5, -0.3, -0.25, -0.2, -0.12, -0.1, 0.0, 0.1, 0.12, 0.2, 0.25, 0.3, 0.5, 0.9]),
    st.floats(min_value=-1.0, max_value=1.0, allow_nan=False),
)


@settings(max_examples=40, deadline=None)
@given(us=st.lists(U_VALUES, min_size=N_SIGNALS, max_size=N_SIGNALS),
       bad_dq=st.sets(st.integers(min_value=0, max_value=N_SIGNALS - 1), max_size=10),
       trip_at=st.none() | st.integers(min_value=0, max_value=N_SIGNALS - 1),
       seed=st.integers(min_value=0, max_value=2**31))
def test_engine_invariants_hold_for_any_signal_sequence(us: list[float], bad_dq: set[int],
                                                        trip_at: int | None, seed: int) -> None:
    start = WARMUP - 1                                       # перше рішення — на барі прогріву
    script = {start + i: u for i, u in enumerate(us)}
    loop = scripted_loop(script, warmup=WARMUP, seed=seed)  # check_invariants=True: облік щобару
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
            assert prev.risk_state in (RiskState.NORMAL, RiskState.WARNING)
            assert not halted
        d = sr.decision
        if d is not None and d.requested_qty is not None:
            # ризик-ланцюг ніколи не збільшує запитану ціль
            assert d.target_qty <= abs(d.requested_qty) or d.target_qty <= abs(d.current_qty)
            cur_side = (d.current_qty > 0) - (d.current_qty < 0)
            if d.target_qty > 0 and d.target_side != cur_side:          # новий бік = приріст експозиції
                assert d.target_qty * dbar.c <= cap * sr.equity           # маржа позиції ≤ 0.30·E
                assert sr.risk_state in (RiskState.NORMAL, RiskState.WARNING)
                assert j not in bad_dq, "increase approved on bad data quality (Q < 0.90)"
        halted = halted or sr.risk_state is RiskState.HALTED
        if halted:
            assert sr.kappa_mode == 0 and not [o for o in sr.orders if o.role in ("enter", "flip")]
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
