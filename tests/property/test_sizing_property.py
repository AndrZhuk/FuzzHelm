"""Property-тести сайзера: квантування лише вниз (§5.8, крок 7–8) і межі коефіцієнта vol-target.

Найменування: tests/property/test_sizing_property.py
Автор: Андрій Жук, 2026.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from fuzzhelm.core.enums import RejectCode, Side
from fuzzhelm.core.money import setup_decimal_context
from fuzzhelm.sizing.convert import float_to_decimal_exact
from fuzzhelm.sizing.sizer import PositionSizer, SizingInput
from fuzzhelm.sizing.vol_target import VolTarget

pytestmark = pytest.mark.property

D = Decimal


@settings(max_examples=400)
@given(
    u=st.floats(min_value=-1.0, max_value=1.0, allow_nan=False),
    equity=st.decimals(min_value=D(10), max_value=D(10_000_000), places=2),
    price=st.decimals(min_value=D("0.01"), max_value=D(100_000), places=2),
    atr=st.floats(min_value=1e-6, max_value=1e4),
    s_t=st.floats(min_value=0.25, max_value=3.0),
    kappa=st.sampled_from([1.0, 0.5, 0.25]),
    step=st.sampled_from([D("0.001"), D("0.000001"), D("0.1"), D(1), D("0.5")]),
    min_notional=st.sampled_from([D(0), D(5), D(100)]),
    gross=st.decimals(min_value=D(0), max_value=D(40_000_000), places=2),
)
def test_qty_floored_never_rounded_up(u: float, equity: Decimal, price: Decimal, atr: float, s_t: float,
                                      kappa: float, step: Decimal, min_notional: Decimal,
                                      gross: Decimal) -> None:
    setup_decimal_context()
    res = PositionSizer().size(SizingInput(u_final=u, equity=equity, price=price, atr=atr, s_t=s_t,
                                           kappa_mode=kappa, step_size=step, min_notional=min_notional,
                                           gross_notional=gross))
    exact = float_to_decimal_exact(res.q_raw)            # точне значення float-результату
    assert res.qty >= 0
    assert res.qty % step == 0                            # кратне кроку
    assert res.qty <= exact                               # ніколи не вгору
    assert res.q_raw <= kappa * min(res.q_atr, res.q_vt, res.q_lev) * (1 + 1e-15)
    if res.reject_code is None and res.side is not Side.FLAT:
        assert exact - res.qty < step                     # і недобір менший за один крок
        assert res.qty * price >= min_notional
    elif res.side is not Side.FLAT:
        assert res.qty == 0
        assert res.reject_code in (RejectCode.BELOW_MIN_NOTIONAL, RejectCode.ZERO_QTY)
        # відхилення лише тоді, коли навіть округлена вниз кількість не дотягує до мінімуму
        floored = (exact / step).to_integral_value(rounding="ROUND_DOWN") * step
        assert floored * price < min_notional or floored == 0


@settings(max_examples=200)
@given(st.lists(st.floats(min_value=-0.2, max_value=0.2, allow_nan=False), min_size=1, max_size=300))
def test_vol_target_scale_always_within_bounds(returns: list[float]) -> None:
    vt = VolTarget()
    for r in returns:
        sigma_ann, s_star, s_t = vt.update(r)
        assert sigma_ann >= 0
        assert 0.25 <= s_star <= 3.0
        assert 0.25 - 1e-12 <= s_t <= 3.0 + 1e-12           # опукла комбінація значень з [0.25; 3]
