"""L. Метрики бектесту: еталонні ряди, що рахуються вручну; PSR/DSR — проти замкнених формул
і опублікованого прикладу."""

from __future__ import annotations

import math
from decimal import Decimal

import numpy as np
import pytest

from fuzzhelm.backtest.metrics import (
    METRIC_NAMES,
    compute_metrics,
    drawdown_series,
    max_drawdown,
    moments,
    returns_from_equity,
    sharpe_ratio,
    sortino_ratio,
    ulcer_index,
)

D = Decimal


def _equity_from_returns(r: list[float], e0: float = 100.0) -> np.ndarray:
    return e0 * np.cumprod(np.concatenate([[1.0], 1.0 + np.asarray(r)]))


# ============================================================ брифінг, група L


def test_sharpe_matches_reference_series() -> None:
    # r = [1%, 3%, −1%, 1%]: mean = 0.01, Σ(r−mean)² = 0.0008, std(ddof=1) = √(0.0008/3)
    # SR_period = 0.01/√(0.0008/3) = √0.375;  SR_ann(P=252) = √(252·0.375) = √94.5
    equity = [D("100"), D("101"), D("104.03"), D("102.9897"), D("104.019597")]   # 100·Π(1+r), точно
    m = compute_metrics(equity, [], periods_per_year=252)
    assert m["sharpe"] == pytest.approx(math.sqrt(94.5), rel=1e-9)
    assert m["ann_vol"] == pytest.approx(math.sqrt(0.0008 / 3 * 252), rel=1e-9)
    assert m["total_return"] == pytest.approx(0.04019597, rel=1e-12)
    r = returns_from_equity(equity)
    assert r == pytest.approx([0.01, 0.03, -0.01, 0.01], abs=1e-15)
    # безризикова ставка за період 0.5 % удвічі зменшує надлишкову доходність
    assert sharpe_ratio(r, 252, rf=0.005) == pytest.approx(math.sqrt(94.5) / 2, rel=1e-9)


def test_sortino_penalizes_only_downside() -> None:
    # однакове середнє (1 %) і однакові збитки (−1 %), різна «висхідна» волатильність
    a = [0.02, 0.02, -0.01, 0.01]
    b = [0.04, 0.00, -0.01, 0.01]
    ra, rb = np.asarray(a), np.asarray(b)
    # нижнє відхилення = √(0.01²/4) = 0.005 ⇒ Sortino за період = 0.01/0.005 = 2
    assert sortino_ratio(ra, 1) == pytest.approx(2.0, rel=1e-12)
    assert sortino_ratio(rb, 1) == pytest.approx(2.0, rel=1e-12)
    assert sharpe_ratio(rb, 1) < sharpe_ratio(ra, 1)          # Шарп карає і за «добру» волатильність
    # а глибший збиток Sortino помічає: −2 % ⇒ нижнє відхилення 0.01 ⇒ Sortino = 1
    assert sortino_ratio(np.asarray([0.02, 0.02, -0.02, 0.02]), 1) == pytest.approx(1.0, rel=1e-12)
    m = compute_metrics(_equity_from_returns(b), [], periods_per_year=365)
    assert m["sortino"] == pytest.approx(2.0 * math.sqrt(365), rel=1e-9)


def test_max_drawdown_on_known_curve() -> None:
    curve = [D(100), D(120), D(90), D(110), D(80), D(130), D(120)]
    # піки: 100,120,120,120,120,130,130 ⇒ DD: 0, 0, 1/4, 1/12, 1/3, 0, 1/13
    assert max_drawdown(curve) == pytest.approx(1 / 3, rel=1e-12)
    m = compute_metrics(curve, [], periods_per_year=6)
    assert m["max_drawdown"] == pytest.approx(1 / 3, rel=1e-12)
    # CAGR за 6 періодів при P = 6 — рівно рік: 120/100 − 1 = 0.2 ⇒ Calmar = 0.2/(1/3) = 0.6
    assert m["cagr"] == pytest.approx(0.2, rel=1e-12)
    assert m["calmar"] == pytest.approx(0.6, rel=1e-12)


def test_ulcer_zero_on_monotone_curve() -> None:
    assert ulcer_index([D(100), D(100), D(101), D("101.5"), D(200)]) == 0.0
    assert max_drawdown([1.0, 1.0, 2.0, 3.0]) == 0.0
    # і ненульовий на відомій кривій: √(mean DD²), DD = 0, 0, 1/4, 1/12, 1/3, 0, 1/13
    dd = [0, 0, 1 / 4, 1 / 12, 1 / 3, 0, 1 / 13]
    expected = math.sqrt(sum(x * x for x in dd) / 7)
    assert ulcer_index([100, 120, 90, 110, 80, 130, 120]) == pytest.approx(expected, rel=1e-12)


# ============================================================ PSR/DSR: опублікований приклад


def test_non_finite_equity_rejected() -> None:
    for bad in (math.nan, math.inf):
        curve = np.array([100.0, 101.0, bad, 102.0])
        with pytest.raises(ValueError, match="NaN or inf"):
            compute_metrics(curve, [], periods_per_year=1)
        with pytest.raises(ValueError, match="NaN or inf"):
            max_drawdown(curve)
    with pytest.raises(ValueError):
        ulcer_index([0.0, 1.0])                           # DD при нульовому початковому капіталі не визначена


# ============================================================ 17 метрик


def test_compute_metrics_returns_exactly_17_named_metrics() -> None:
    m = compute_metrics([D(100), D(101), D(99), D(102)], [D(3), D(-1)], periods_per_year=525600,
                        positions=[0, 1, 1, 0], traded_notional=D(400))
    assert tuple(m) == METRIC_NAMES and len(m) == 17
    assert all(isinstance(v, float) for v in m.values())
    assert m["exposure"] == 0.5


def test_trade_metrics_hand_computed() -> None:
    m = compute_metrics([D(1000)] * 11, [D(10), D(-5), D(20), D(-5)], periods_per_year=10,
                        traded_notional=D(5000))
    assert m["n_trades"] == 4 and m["win_rate"] == 0.5
    assert m["avg_win"] == 15.0 and m["avg_loss"] == 5.0
    assert m["profit_factor"] == 3.0                        # 30/10
    assert m["expectancy"] == 5.0                           # 0.5·15 − 0.5·5 = середній PnL угоди
    assert m["turnover"] == 5.0                             # 5000/1000 · (P/n = 10/10)


def test_flat_equity_is_well_defined() -> None:
    m = compute_metrics([D("10000")] * 50, [], periods_per_year=525600, positions=[0] * 50)
    for k in METRIC_NAMES:
        assert m[k] == 0.0, k                               # жодних NaN/inf на нульовому сигналі
    # без позицій exposure не обчислюється — чесний NaN, а не вигаданий нуль
    assert math.isnan(compute_metrics([D(1), D(1)], [], periods_per_year=1)["exposure"])


# ============================================================ вироджені криві (межі визначеності метрик)


def test_degenerate_curves_have_defined_metrics_or_explicit_errors() -> None:
    assert returns_from_equity([D(100)]).size == 0 and drawdown_series([]).size == 0
    assert sortino_ratio(np.empty(0), 525600) == 0.0 and moments(np.array([0.01])) == (0.0, 3.0)
    # одна точка: доходностей немає — CAGR і хвостове відношення 0; оборот на 0 періодах не визначений (NaN)
    one = compute_metrics([D(100)], [], periods_per_year=365, traded_notional=D(0))
    assert (one["total_return"], one["cagr"], one["tail_ratio"]) == (0.0, 0.0, 0.0)
    assert math.isnan(one["turnover"])
    # повна втрата капіталу: CAGR рівно −100 % (логарифм нуля не береться)
    ruin = compute_metrics([D(100), D(50), D(0)], [], periods_per_year=365)
    assert ruin["cagr"] == -1.0 and ruin["total_return"] == -1.0 and ruin["max_drawdown"] == 1.0
    # доходність після нульового капіталу і просадка від непозитивного старту не визначені
    with pytest.raises(ValueError, match="stay > 0"):
        returns_from_equity([D(100), D(0), D(10)])
    with pytest.raises(ValueError, match="initial equity"):
        drawdown_series([0.0, 1.0])
    for bad in ({"equity": [], "trades": [], "periods_per_year": 1},
                {"equity": [D(1)], "trades": [], "periods_per_year": 0}):
        with pytest.raises(ValueError):
            compute_metrics(**bad)                                   # type: ignore[arg-type]


def test_turnover_is_nan_when_trades_carry_no_notional() -> None:
    # PnL-числа без номіналу: оборот невідомий — NaN, а не нуль (нуль означав би «не торгував»)
    m = compute_metrics([D(100), D(101), D(102)], [D(1), D(1)], periods_per_year=365)
    assert m["n_trades"] == 2 and math.isnan(m["turnover"])


