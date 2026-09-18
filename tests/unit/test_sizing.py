"""Група I брифінгу (§10): сайзинг — ATR-ризик, vol-target, фільтр 1-го порядку, мінімум трьох, гістерезис.

Найменування: tests/unit/test_sizing.py
Автор: Андрій Жук, 2026.
"""

from __future__ import annotations

import math
from decimal import Decimal

import numpy as np
import pytest

from fuzzhelm.config import load_yaml
from fuzzhelm.core.enums import RejectCode, Side
from fuzzhelm.risk.config import load_risk_config
from fuzzhelm.sizing.atr_risk import q_atr, risk_money
from fuzzhelm.sizing.convert import float_to_decimal_exact
from fuzzhelm.sizing.hysteresis import HysteresisGate, churn_cost_per_day
from fuzzhelm.sizing.sizer import BindingConstraint, PositionSizer, SizingInput, SizingParams
from fuzzhelm.sizing.vol_target import (
    VolTarget,
    backward_euler_time_constant_bars,
    filter_step_response,
    filter_time_constant_bars,
)

ONE_MINUS_INV_E = 1.0 - math.exp(-1.0)        # 63.2% — рівень «сталої часу»


def _inp(**kw: object) -> SizingInput:
    base: dict[str, object] = {
        "u_final": 1.0, "equity": Decimal(10_000), "price": Decimal(100), "atr": 1.0,
        "s_t": 1.0, "kappa_mode": 1.0, "step_size": Decimal("0.001"), "min_notional": Decimal(5),
    }
    base.update(kw)
    return SizingInput(**base)  # type: ignore[arg-type]


def _converge(vt: VolTarget, r: float, n: int = 400) -> None:
    for i in range(n):
        vt.update(r if i % 2 == 0 else -r)


# ---------------------------------------------------------------- ATR-ризик


def test_position_size_inverse_to_atr() -> None:
    """20 значень ATR: q·χ·ATR (гроші під ризиком до стопу) = ρ_base·|u|·E = const; q ∝ 1/ATR."""
    sizer = PositionSizer(SizingParams(rho_base=0.005, chi=2.0, max_leverage=3.0))
    equity, u = 100_000.0, 0.8
    expected_risk = 0.005 * u * equity                         # 400 USDT
    atrs = np.geomspace(100.0, 2000.0, 20)
    qs = []
    for atr in atrs:
        qa = q_atr(u, equity, float(atr), 0.005, 2.0)
        assert risk_money(qa, float(atr), 2.0) == pytest.approx(expected_risk, rel=1e-12)
        res = sizer.size(_inp(u_final=u, equity=Decimal(100_000), price=Decimal(50_000), atr=float(atr),
                              s_t=3.0, step_size=Decimal("0.000001")))
        assert res.binding_constraint is BindingConstraint.ATR_RISK
        # після квантування ВНИЗ ризик не перевищує бюджет і недобирає менше одного кроку
        realized = float(res.qty) * 2.0 * float(atr)
        assert realized <= expected_risk + 1e-9
        assert expected_risk - realized < 2.0 * float(atr) * 1e-6 + 1e-9
        qs.append(qa)
    q = np.array(qs)
    np.testing.assert_allclose(q * atrs, q[0] * atrs[0], rtol=1e-12)   # обернена пропорційність


def test_atr_non_positive_means_no_position() -> None:
    assert q_atr(1.0, 10_000.0, 0.0) == 0.0
    assert q_atr(1.0, 10_000.0, math.nan) == 0.0
    res = PositionSizer().size(_inp(atr=0.0))
    assert res.qty == 0 and res.reject_code is RejectCode.BELOW_MIN_NOTIONAL


# ---------------------------------------------------------------- vol-target


def test_vol_target_halves_notional_when_vol_doubles() -> None:
    r = 2e-4                                                    # σ_ann = r·√525600 ≈ 0.145 → s* ≈ 1.38
    a, b = VolTarget(), VolTarget()
    _converge(a, r)
    _converge(b, 2 * r)
    assert b.sigma_ann / a.sigma_ann == pytest.approx(2.0, rel=1e-12)
    assert 0.25 < b.s_star < a.s_star < 3.0                     # обидва всередині меж clip
    assert b.s_t / a.s_t == pytest.approx(0.5, rel=1e-12)
    sizer = PositionSizer()
    kw = {"atr": 1e-6, "step_size": Decimal("0.000001"), "equity": Decimal(100_000), "price": Decimal(100)}
    ra = sizer.size(_inp(s_t=a.s_t, **kw))
    rb = sizer.size(_inp(s_t=b.s_t, **kw))
    assert ra.binding_constraint is rb.binding_constraint is BindingConstraint.VOL_TARGET
    assert ra.q_vt * 100 == pytest.approx(a.s_t * 100_000, rel=1e-12)       # номінал = s_t·E
    assert float(rb.notional) / float(ra.notional) == pytest.approx(0.5, abs=1e-8)


def test_vol_target_clipped_at_bounds() -> None:
    calm, wild, dead = VolTarget(), VolTarget(), VolTarget()
    for i in range(300):
        _, s_star_c, s_c = calm.update(1e-7 if i % 2 else -1e-7)   # σ_ann ≈ 7e-5 → σ_target/σ_ann ≫ 3
        _, s_star_w, s_w = wild.update(0.05 if i % 2 else -0.05)   # σ_ann ≈ 36 → σ_target/σ_ann ≪ 0.25
        _, s_star_d, s_d = dead.update(0.0)                        # σ_ann = 0 → межа clip від +∞
        assert s_star_c == 3.0 and s_star_w == 0.25 and s_star_d == 3.0
        assert 0.25 <= s_w <= s_c <= 3.0 and s_d <= 3.0
    assert calm.s_t == pytest.approx(3.0, abs=1e-12)
    assert wild.s_t == pytest.approx(0.25, abs=1e-12)


def test_first_order_filter_reaches_63pct_in_T() -> None:
    """Перевіряємо, що ПРАВДА про сталу часу фільтра s_t = s_{t−1} + γ(s* − s_{t−1}), γ = 0.2.

    Брифінг: «γ = 0.2 ⇒ T = 4Δt». Насправді перехідна характеристика 1 − (1−γ)^n дає 59.04% після 4 барів
    (< 63.2%) і 67.23% після 5 (≥ 63.2%); точна стала часу −Δt/ln(1−γ) ≈ 4.48Δt. Число 4 — стала
    неперервного прототипу при зворотному Ейлері (1−γ)/γ. Розходження — docs/deviations.d/risk.md.
    """
    gamma = 0.2
    sigma_target = 0.20
    r = 0.16 / math.sqrt(525_600)                               # σ_ann = 0.16 → s* = 1.25 (усередині меж)
    vt = VolTarget(gamma=gamma, sigma_target=sigma_target, s0=0.25)
    s0 = vt.s_t
    fractions = []
    for i in range(8):
        _, s_star, s = vt.update(r if i % 2 == 0 else -r)
        assert s_star == pytest.approx(1.25, rel=1e-12)
        fractions.append((s - s0) / (1.25 - s0))
    for n, frac in enumerate(fractions, start=1):
        assert frac == pytest.approx(filter_step_response(gamma, n), rel=1e-12)
    assert fractions[3] == pytest.approx(0.5904, abs=1e-12)     # n = 4
    assert fractions[4] == pytest.approx(0.67232, abs=1e-12)    # n = 5
    assert fractions[3] < ONE_MINUS_INV_E <= fractions[4]
    tau = filter_time_constant_bars(gamma)
    assert tau == pytest.approx(-1.0 / math.log(0.8), rel=1e-15)
    assert 4.48 < tau < 4.49
    assert 1.0 - (1.0 - gamma) ** tau == pytest.approx(ONE_MINUS_INV_E, rel=1e-12)
    assert backward_euler_time_constant_bars(gamma) == pytest.approx(4.0)   # звідки «4Δt» у брифінгу
    assert 1.0 / gamma == pytest.approx(5.0)                                # прямий Ейлер


# ---------------------------------------------------------------- мінімум трьох обмежень

SCENARIOS = {
    # u, atr, s_t, gross_other → очікуване вузьке місце; E=10 000, p=100
    "atr": (0.5, 5.0, 1.0, Decimal(0), BindingConstraint.ATR_RISK),          # 2.5 | 100 | 300
    "vol": (1.0, 0.01, 0.5, Decimal(0), BindingConstraint.VOL_TARGET),       # 2500 | 50 | 300
    "lev": (1.0, 0.01, 3.0, Decimal(25_000), BindingConstraint.LEVERAGE),    # 2500 | 300 | 50
}


@pytest.mark.parametrize("case", sorted(SCENARIOS))
def test_final_qty_is_min_of_three_constraints(case: str) -> None:
    u, atr, s_t, gross, _ = SCENARIOS[case]
    kappa = 0.5
    res = PositionSizer().size(_inp(u_final=u, atr=atr, s_t=s_t, gross_notional=gross, kappa_mode=kappa))
    e, p = 10_000.0, 100.0
    qa = 0.005 * abs(u) * e / (2.0 * atr)
    qv = s_t * e / p
    ql = (e * 3.0 - float(gross)) / p
    assert (res.q_atr, res.q_vt, res.q_lev) == pytest.approx((qa, qv, ql), rel=1e-12)
    expected = Decimal(math.floor(kappa * min(qa, qv, ql) * 1000)) / 1000
    assert res.qty == expected
    assert float(res.qty) <= kappa * min(qa, qv, ql) + 1e-12
    assert res.reject_code is None and res.side is Side.LONG


@pytest.mark.parametrize("case", sorted(SCENARIOS))
def test_sizer_reports_binding_constraint(case: str) -> None:
    u, atr, s_t, gross, binding = SCENARIOS[case]
    res = PositionSizer().size(_inp(u_final=u, atr=atr, s_t=s_t, gross_notional=gross))
    assert res.binding_constraint is binding
    named = {BindingConstraint.ATR_RISK: res.q_atr, BindingConstraint.VOL_TARGET: res.q_vt,
             BindingConstraint.LEVERAGE: res.q_lev}
    assert named[binding] == min(named.values())
    assert sum(v == min(named.values()) for v in named.values()) == 1     # вузьке місце однозначне
    assert res.to_dict()["binding_constraint"] == binding.value


def test_short_signal_sizes_by_absolute_value() -> None:
    long_ = PositionSizer().size(_inp(u_final=0.6, atr=5.0))
    short = PositionSizer().size(_inp(u_final=-0.6, atr=5.0))
    assert short.side is Side.SHORT and long_.side is Side.LONG
    assert short.qty == long_.qty > 0 and short.signed_qty == -long_.qty


def test_below_min_notional_rejected_never_rounded_up() -> None:
    # q_atr = 0.005·0.1·10 000/(2·50) = 0.05 → номінал 5.0 < min_notional 6 → відмова, а не 0.06
    res = PositionSizer().size(_inp(u_final=0.1, atr=50.0, min_notional=Decimal(6)))
    assert res.qty == 0 and res.reject_code is RejectCode.BELOW_MIN_NOTIONAL
    ok = PositionSizer().size(_inp(u_final=0.1, atr=50.0, min_notional=Decimal(5)))
    assert ok.qty == Decimal("0.050") and ok.reject_code is None


def test_size_zero_in_halted_regardless_of_signal() -> None:
    sizer = PositionSizer()
    for u in np.linspace(-1.0, 1.0, 41):
        res = sizer.size(_inp(u_final=float(u), kappa_mode=0.0, atr=0.5, s_t=3.0))
        assert res.qty == 0
        assert res.q_raw == 0.0
        assert res.reject_code is RejectCode.HALTED
        assert res.notional == 0


def test_sizing_params_and_voltarget_read_risk_limits_yaml() -> None:
    cfg = load_risk_config().sizing
    p = SizingParams.from_config(cfg)
    vt = VolTarget.from_config(cfg)
    assert (p.rho_base, p.chi, p.max_leverage) == (0.005, 2.0, 3.0)
    got = (vt.lam, vt.A, vt.sigma_target, vt.s_min, vt.s_max, vt.gamma)
    assert got == (0.94, 525_600, 0.2, 0.25, 3.0, 0.2)


def test_ewma_recursion_matches_definition() -> None:
    rs = [0.001, -0.002, 0.0005, 0.003, -0.001]
    vt = VolTarget()
    s2 = rs[0] ** 2
    for i, r in enumerate(rs):
        if i:
            s2 = 0.94 * s2 + 0.06 * r * r
        sigma_ann, _, _ = vt.update(r)
        assert sigma_ann == pytest.approx(math.sqrt(s2) * math.sqrt(525_600), rel=1e-12)


# ---------------------------------------------------------------- гістерезис


def test_hysteresis_prevents_flip_flop() -> None:
    gate = HysteresisGate(enter=0.25, exit=0.12)
    seq = [0.30, 0.20, 0.15, 0.11]
    assert [gate.update(u) for u in seq] == [Side.LONG, Side.LONG, Side.LONG, Side.FLAT]
    assert gate.flips == 2                                      # один вхід, один вихід
    # без гістерезису (компаратор enter = exit = 0.25) позиція закрилась би вже на 0.20
    plain = HysteresisGate(enter=0.25, exit=0.25)
    assert [plain.update(u) for u in seq] == [Side.LONG, Side.FLAT, Side.FLAT, Side.FLAT]
    # сигнал, що тремтить біля порогу входу: тригер Шмітта входить один раз, компаратор — щобару
    schmitt, comparator = HysteresisGate(), HysteresisGate(enter=0.25, exit=0.25)
    for i in range(200):
        u = 0.26 if i % 2 == 0 else 0.24
        schmitt.update(u)
        comparator.update(u)
    assert schmitt.flips == 1 and schmitt.side is Side.LONG
    assert comparator.flips == 200


def test_hysteresis_exit_is_signed_and_reversal_needs_enter_level() -> None:
    gate = HysteresisGate()
    assert gate.update(-0.30) is Side.SHORT
    assert gate.update(-0.13) is Side.SHORT        # |u| ≥ 0.12 у напрямку позиції — тримаємо
    assert gate.update(0.20) is Side.FLAT          # сигнал проти позиції, слабший за вхід, — лише вихід
    assert gate.update(0.30) is Side.LONG
    assert gate.update(-0.25) is Side.SHORT        # розворот на рівні входу


def test_hysteresis_absence_cost_matches_brief_arithmetic() -> None:
    """§5.9: «1440 барів/добу × 2 комісії × 0.04% = −1.15% капіталу на добу» — ПЕРЕВІРКА НЕ ПРОЙШЛА.

    1440 × 2 × 0.0004 = 1.152 — це частка номіналу, тобто 115.2% (а не 1.15%) на добу при номіналі 1×E:
    у брифінгу помилка одиниць на два порядки (docs/deviations.d/risk.md). Сценарій «2 виконання на бар» —
    перевідкриття щобару; компаратор на сигналі, що тремтить біля порогу (0.26/0.24), дає 1 виконання
    на бар (57.6%/добу); тригер Шмітта на тій самій послідовності — 1 виконання за добу.
    Висновок брифінгу (гістерезис необхідний) від виправлення лише посилюється.
    """
    taker = float(load_yaml("cost_model")["taker_fee"])
    assert taker == 0.0004
    daily = churn_cost_per_day(taker, fills_per_bar=2.0)
    assert daily == pytest.approx(1.152, rel=1e-12)             # 115.2% номіналу за добу
    assert daily * 100 != pytest.approx(1.15, abs=0.01)         # заявлені 1.15% — хибні
    comparator, schmitt = HysteresisGate(enter=0.25, exit=0.25), HysteresisGate()
    for i in range(1440):
        u = 0.26 if i % 2 == 0 else 0.24
        comparator.update(u)
        schmitt.update(u)
    assert comparator.flips == 1440
    assert comparator.flips * taker == pytest.approx(churn_cost_per_day(taker, fills_per_bar=1.0), rel=1e-12)
    assert comparator.flips * taker == pytest.approx(0.576, rel=1e-12)
    assert schmitt.flips == 1
    assert schmitt.flips * taker == pytest.approx(0.0004)


def test_sizing_result_decimal_qty_is_exact_multiple_of_step() -> None:
    res = PositionSizer().size(_inp(u_final=0.37, atr=3.3, step_size=Decimal("0.005")))
    assert res.qty % Decimal("0.005") == 0
    assert res.qty <= float_to_decimal_exact(res.q_raw)


def test_sizing_inputs_are_validated() -> None:
    sizer = PositionSizer()
    for bad in ({"u_final": 1.5}, {"u_final": math.nan}, {"kappa_mode": 1.2}, {"price": Decimal(0)}):
        with pytest.raises(ValueError):
            sizer.size(_inp(**bad))
    for kw in ({"lam": 1.0}, {"A": 0}, {"s_min": 0.0}, {"gamma": 0.0}, {"sigma2_0": -1.0}):
        with pytest.raises(ValueError):
            VolTarget(**kw)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        VolTarget().update(math.inf)
    with pytest.raises(ValueError):
        SizingParams(rho_base=0.0)
    with pytest.raises(ValueError):
        HysteresisGate(enter=0.1, exit=0.2)
    with pytest.raises(ValueError):
        filter_time_constant_bars(1.0)
    with pytest.raises(ValueError):
        q_atr(1.0, 100.0, 1.0, rho_base=0.0)
    vt = VolTarget(sigma2_0=0.0)
    assert math.isnan(VolTarget().sigma_ann) and vt.sigma_ann == 0.0 and vt.n == 0 and vt.sigma2 == 0.0
    flat = sizer.size(_inp(u_final=0.0))
    assert flat.side is Side.FLAT and flat.qty == 0 and flat.reject_code is None
    zero = sizer.size(_inp(u_final=0.01, atr=1e6, min_notional=Decimal(0)))
    assert zero.qty == 0 and zero.reject_code is RejectCode.ZERO_QTY
    gate = HysteresisGate(initial=Side.LONG)
    gate.reset()
    assert gate.side is Side.FLAT


def test_non_finite_inputs_are_refused_not_silently_held() -> None:
    """NaN робить усі порівняння хибними: тригер Шмітта мовчки тримав би позицію. s0 поза [s_min; s_max]
    ламав би інваріант меж s_t. NaN-стоп (ATR на прогріві) не повинен потрапляти в JSON як NaN."""
    gate = HysteresisGate()
    assert gate.update(0.30) is Side.LONG
    for bad in (math.nan, math.inf, -math.inf):
        with pytest.raises(ValueError, match="finite"):
            gate.update(bad)
    assert gate.side is Side.LONG and gate.flips == 1                # стан не змінено відмовою
    for s0 in (0.1, 3.5, math.nan):
        with pytest.raises(ValueError, match="s0"):
            VolTarget(s0=s0)
    assert VolTarget(s0=3.0).s_t == 3.0
    warm = PositionSizer().size(_inp(atr=math.nan))
    assert warm.qty == 0 and warm.to_dict()["stop_distance"] is None
