"""Ціна ліквідації і коефіцієнт маржі.

Найменування: tests/unit/test_margin.py
Автор: Андрій Жук, 2026.
"""

from __future__ import annotations

import math
from decimal import ROUND_HALF_EVEN, Decimal

import pytest

from fuzzhelm.core.enums import Side
from fuzzhelm.risk.margin import (
    dtl_atr,
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


# ---------------------------------------------------------------- VaR / CVaR (§5.13)


