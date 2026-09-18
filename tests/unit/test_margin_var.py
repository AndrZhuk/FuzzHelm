"""Група J брифінгу (§10): ціна ліквідації, коефіцієнт маржі, VaR/CVaR, тест Купця, оцінка швидкості просадки.

Найменування: tests/unit/test_margin_var.py
Автор: Андрій Жук, 2026.
"""

from __future__ import annotations

import math
from decimal import ROUND_HALF_EVEN, Decimal
from statistics import NormalDist

import numpy as np
import pytest

from fuzzhelm.core.enums import Side
from fuzzhelm.risk.kupiec import CHI2_1_95, acceptance_region, kupiec_pof
from fuzzhelm.risk.margin import (
    dtl_atr,
    leverage,
    liq_price,
    liq_price_long,
    liq_price_short,
    margin_ratio,
    max_qty_for_dtl,
    min_bars_to_drawdown,
    per_bar_loss_bound,
    reduced_max_leverage,
    signed_dtl_atr,
)
from fuzzhelm.risk.var import (
    historical_var_cvar,
    parametric_var,
    returns_from_equity,
    rolling_var_breaches,
    tail_count,
)

D = Decimal
MMR = D("0.005")
CHI2_1_95_TABLE = 3.841459          # табличне значення χ²₁(0.95) (незалежне від NormalDist у коді)


def _binom_logpmf(x: int, n: int, p: float) -> float:
    """ln Binom(x; n, p) через lgamma — інший обчислювальний шлях, ніж формула POF у kupiec.py."""
    log_c = math.lgamma(n + 1) - math.lgamma(x + 1) - math.lgamma(n - x + 1)
    return log_c + (x * math.log(p) if x else 0.0) + ((n - x) * math.log1p(-p) if n - x else 0.0)


# ---------------------------------------------------------------- ціна ліквідації (§5.10)


def test_liq_price_long_10x_equals_90_4523() -> None:
    p = liq_price_long(q=D(1), entry=D(100), wallet=D(10), mmr=MMR, maint_amount=D(0))
    assert p.quantize(D("0.0001"), rounding=ROUND_HALF_EVEN) == D("90.4523")
    assert p == D(90) / D("0.995")
    assert p > D(90)                                     # вище наївних 90 саме на підтримуючу маржу
    assert liq_price(D(1), D(100), D(10), MMR) == p      # єдина формула для позиції зі знаком


def test_liq_price_short_symmetry() -> None:
    q, pe, w = D(1), D(100), D(10)
    # без підтримуючої маржі ліквідація симетрична відносно входу: P_e ∓ W/q
    assert liq_price_long(q, pe, w, D(0)) == D(90)
    assert liq_price_short(q, pe, w, D(0)) == D(110)
    # з mmr > 0 обидві ціни зсуваються ДО входу (ліквідація настає раніше), кожна на свою сторону
    lo, hi = liq_price_long(q, pe, w, MMR), liq_price_short(q, pe, w, MMR)
    assert D(90) < lo < pe < hi < D(110)
    assert hi == (w + q * pe) / (q * (1 + MMR))
    # дзеркало: знакова формула з q < 0 дає шортову ціну; maintenance amount зсуває обидві від входу
    assert liq_price(-q, pe, w, MMR) == hi
    ma = D("0.5")
    assert liq_price_long(q, pe, w, MMR, ma) < lo and liq_price_short(q, pe, w, MMR, ma) > hi


@pytest.mark.parametrize(("q", "entry", "wallet", "mmr", "ma"), [
    (D(1), D(100), D(10), D("0.005"), D(0)),
    (D("0.37"), D("64123.5"), D(2500), D("0.004"), D(0)),
    (D(3), D(1850), D(1200), D("0.01"), D(5)),
])
def test_margin_ratio_is_one_at_liq_price(q: Decimal, entry: Decimal, wallet: Decimal, mmr: Decimal,
                                          ma: Decimal) -> None:
    for signed in (q, -q):
        p_liq = liq_price(signed, entry, wallet, mmr, ma)
        mr = margin_ratio(signed, entry, p_liq, wallet, mmr, maint_amount=ma)
        assert abs(mr - 1) < D("1e-30")                  # Equity = MM рівно (до точності prec=38)
        safer = p_liq * (D("1.01") if signed > 0 else D("0.99"))
        worse = p_liq * (D("0.999") if signed > 0 else D("1.001"))
        assert margin_ratio(signed, entry, safer, wallet, mmr, maint_amount=ma) < 1
        assert margin_ratio(signed, entry, worse, wallet, mmr, maint_amount=ma) > 1


def test_dtl_and_reduced_leverage_formulas() -> None:
    assert dtl_atr(D(100), D("90.4523"), D(2)) == D("4.77385")
    l_red = reduced_max_leverage(D(4), D(100), MMR, D("0.20"))    # Δ_stop = 4 ⇒ 1/(4/80 + 0.005)
    assert l_red == D(1) / D("0.055")
    # за L = L'_max відстань до ліквідації свіжої позиції ≥ Δ_stop/(1−b): стоп спрацює раніше
    q = l_red * D(1000) / D(100)
    dist = D(100) - liq_price_long(q, D(100), D(1000), MMR)
    assert dist >= D(4) / D("0.8")
    # max_qty_for_dtl: на межі DTL рівно заданий, трохи більше — вже менший
    q_k = max_qty_for_dtl(Side.LONG, wallet=D(1000), price=D(100), atr=D(1), min_dtl=D(6), mmr=MMR)
    assert abs(dtl_atr(D(100), liq_price_long(q_k, D(100), D(1000), MMR), D(1)) - 6) < D("1e-30")
    assert dtl_atr(D(100), liq_price_long(q_k * D("1.001"), D(100), D(1000), MMR), D(1)) < 6
    q_s = max_qty_for_dtl(Side.SHORT, wallet=D(1000), price=D(100), atr=D(1), min_dtl=D(6), mmr=MMR)
    assert abs(dtl_atr(D(100), liq_price_short(q_s, D(100), D(1000), MMR), D(1)) - 6) < D("1e-30")
    # знакова відстань: збігається з |P − P_liq| по правильний бік і від'ємна за межею 1/mmr = 200×
    liq_ok = liq_price_long(D(10), D(100), D(1000), MMR)                   # 1×
    assert signed_dtl_atr(Side.LONG, D(100), liq_ok, D(1)) == dtl_atr(D(100), liq_ok, D(1))
    liq_bad = liq_price_long(D(3000), D(100), D(1000), MMR)                # 300× > 1/mmr
    assert liq_bad > D(100) and signed_dtl_atr(Side.LONG, D(100), liq_bad, D(1)) < 0
    liq_bad_s = liq_price_short(D(3000), D(100), D(1000), MMR)
    assert liq_bad_s < D(100) and signed_dtl_atr(Side.SHORT, D(100), liq_bad_s, D(1)) < 0


def test_worst_case_drawdown_speed_is_a_bound_not_a_proof() -> None:
    v = per_bar_loss_bound(D("0.25"), D("0.005"), D("0.5"))       # COOLDOWN, ρ = 0.5%, ковзання 50% стопу
    assert v == D("0.001875")
    b = min_bars_to_drawdown(D("0.08"), D("0.12"), v)
    assert b.n_min == D("0.04") / D("0.001875")                  # ≈ 21.33 бару
    # гірший допустимий шлях (кожен бар втрачає рівно v_max від поточного капіталу) не швидший за межу
    e, peak, n = D(92), D(100), 0
    while D(1) - e / peak < D("0.12"):
        e -= e * v
        n += 1
    assert n >= b.n_min
    assert min_bars_to_drawdown(D("0.08"), D("0.12"), D(0)).n_min.is_infinite()
    with pytest.raises(ValueError):
        min_bars_to_drawdown(D("0.2"), D("0.12"), v)


# ---------------------------------------------------------------- тест Купця (§5.13)


def test_kupiec_lr_zero_when_breaches_equal_expected() -> None:
    res = kupiec_pof(25, 500, 0.05)                        # x = p·W = 25 рівно
    assert res.lr == 0.0 and not res.reject
    assert res.p_hat == 0.05


def test_kupiec_rejects_at_20_breaches_of_500() -> None:
    """Назва — з брифінгу; перевіряється ПРАВДА: 20 пробоїв із 500 при p = 0.05 НЕ відкидаються.

    LR(20; 500, 0.05) = 1.1267 < χ²₁(0.95) = 3.8415. Область неприйняття на W = 500 — x ≤ 16 або x ≥ 36
    (LR(16) = 3.888, LR(17) = 3.022, LR(35) = 3.765, LR(36) = 4.511). Розходження — docs/deviations.d/risk.md.
    """
    res = kupiec_pof(20, 500, 0.05)
    # незалежний шлях: відношення біноміальних правдоподібностей (C(W,x) скорочується)
    ref = -2.0 * (_binom_logpmf(20, 500, 0.05) - _binom_logpmf(20, 500, 20 / 500))
    assert res.lr == pytest.approx(ref, rel=1e-10)
    assert res.lr == pytest.approx(1.1267, abs=1e-4)
    assert res.reject is False                           # чесний результат, не підгонка
    assert acceptance_region(500, 0.05) == (17, 35)
    assert kupiec_pof(16, 500).reject and not kupiec_pof(17, 500).reject
    assert not kupiec_pof(35, 500).reject and kupiec_pof(36, 500).reject


def test_kupiec_limits_and_critical_value() -> None:
    assert abs(CHI2_1_95 - CHI2_1_95_TABLE) < 5e-7
    assert round(CHI2_1_95, 3) == 3.841
    assert kupiec_pof(0, 500).lr == pytest.approx(-2 * 500 * math.log(0.95), rel=1e-12)
    assert kupiec_pof(500, 500).lr == pytest.approx(-2 * 500 * math.log(0.05), rel=1e-12)
    for x in (0, 1, 7, 25, 60, 499, 500):
        assert kupiec_pof(x, 500).lr >= 0.0
    with pytest.raises(ValueError):
        kupiec_pof(501, 500)


# ---------------------------------------------------------------- VaR / CVaR (§5.13)


def test_historical_var_matches_lower_quantile_on_500_window() -> None:
    rng = np.random.default_rng(20260918)
    r = rng.standard_t(df=3, size=800) * 0.001
    res = historical_var_cvar(r, 0.05, window=500)
    w = r[-500:]
    assert res.n == 500 and res.m == 25
    assert res.var == pytest.approx(-np.quantile(w, 0.05, method="lower"), rel=0, abs=0)
    assert res.cvar == pytest.approx(-np.sort(w)[:25].mean(), rel=1e-12)
    assert res.cvar >= res.var > 0


def test_var_can_be_negative_on_all_gain_window() -> None:
    """Брифінг: «CVaR ≥ VaR ≥ 0». Друга нерівність не тотожність: на вікні лише з додатними дохідностями
    5%-квантиль додатний, і VaR = −Q < 0 (як і CVaR). Тотожністю є лише CVaR ≥ VaR."""
    res = historical_var_cvar(np.linspace(0.001, 0.002, 500), window=500)
    assert res.var < 0 and res.cvar >= res.var


def test_parametric_var_uses_normal_quantile() -> None:
    z = NormalDist().inv_cdf(0.95)
    assert z == pytest.approx(1.6448536, abs=1e-7)
    assert parametric_var(0.01, equity=10_000.0, h=4.0) == pytest.approx(z * 0.01 * 2.0 * 10_000.0)


def test_returns_from_equity_and_tail_count() -> None:
    r = returns_from_equity([D(100), D(110), D(99)])
    np.testing.assert_allclose(r, [0.1, -0.1], rtol=1e-15)
    assert tail_count(500, 0.05) == 25 and tail_count(100, 0.57) == 57 and tail_count(10, 0.05) == 1


def test_rolling_var_uses_only_past_window() -> None:
    rng = np.random.default_rng(7)
    r = rng.normal(0, 0.001, 1200)
    bt = rolling_var_breaches(r, window=500)
    assert bt.n == 700 and bt.var_series.shape == (700,)
    for t in (0, 123, 699):
        expect = historical_var_cvar(r[t:t + 500], window=500).var
        assert bt.var_series[t] == expect
    r2 = r.copy()
    r2[900:] = -0.5                                     # майбутнє змінено різко
    bt2 = rolling_var_breaches(r2, window=500)
    np.testing.assert_array_equal(bt2.var_series[:400], bt.var_series[:400])
    assert bt.breaches == int(np.sum(r[500:] < -bt.var_series))


def test_margin_var_kupiec_reject_invalid_inputs() -> None:
    bad_calls = [
        lambda: liq_price_long(D(0), D(100), D(10), MMR),
        lambda: liq_price_short(D(-1), D(100), D(10), MMR),
        lambda: liq_price(D(0), D(100), D(10), MMR),
        lambda: margin_ratio(D(1), D(100), D(80), D(10), MMR),          # equity позиції ≤ 0
        lambda: dtl_atr(D(100), D(90), D(0)),
        lambda: reduced_max_leverage(D(1), D(100), MMR, D(1)),
        lambda: reduced_max_leverage(D(1), D(0), MMR, D("0.2")),
        lambda: max_qty_for_dtl(Side.FLAT, wallet=D(1), price=D(1), atr=D(1), min_dtl=D(6), mmr=MMR),
        lambda: per_bar_loss_bound(D(-1), D("0.005"), D(0)),
        lambda: min_bars_to_drawdown(D(0), D("0.1"), D(-1)),
        lambda: kupiec_pof(1, 0),
        lambda: kupiec_pof(1, 10, 1.0),
        lambda: historical_var_cvar([0.1], alpha=0.0),
        lambda: historical_var_cvar([0.1], window=0),
        lambda: historical_var_cvar([[0.1]]),                            # type: ignore[list-item]
        lambda: historical_var_cvar([float("nan")]),
        lambda: tail_count(0, 0.05),
        lambda: parametric_var(-0.1),
        lambda: returns_from_equity([D(0), D(1)]),
        lambda: rolling_var_breaches([0.1] * 600, window=500, alpha=0.0),
        lambda: rolling_var_breaches([0.1] * 600, window=500, alpha=1.5),
        lambda: rolling_var_breaches([0.1] * 600, window=0),
        lambda: signed_dtl_atr(Side.LONG, D(100), D(90), D(0)),
        lambda: signed_dtl_atr(Side.FLAT, D(100), D(90), D(1)),
    ]
    for call in bad_calls:
        with pytest.raises(ValueError):
            call()
    assert max_qty_for_dtl(Side.LONG, wallet=D(-5), price=D(1), atr=D(1), min_dtl=D(6), mmr=MMR) == 0
    assert rolling_var_breaches([0.1] * 10, window=500).n == 0
    assert leverage(D(3), D(100), D(100)) == D(3)
    with pytest.raises(ValueError):
        leverage(D(3), D(100), D(0))
