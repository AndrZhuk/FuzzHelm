"""E. Індикатори: golden-тести Уайлдера, EMA, Велфорд, Паркінсон, OLS, прогрів.

Найменування: tests/unit/test_indicators.py
Автор: Андрій Жук, 2026.
"""

from __future__ import annotations

import csv
import json
import math
from fractions import Fraction
from itertools import pairwise
from pathlib import Path

import numpy as np
import pytest

from fuzzhelm.features.convert import Bar
from fuzzhelm.features.indicators import (
    EMA,
    SMA,
    MonotonicDequeMax,
    MonotonicDequeMin,
    Parkinson,
    PercentileRank,
    RollingOLS,
    RollingWelford,
    WilderATR,
    WilderRSI,
)
from fuzzhelm.features.pipeline import FEATURE_NAMES, FeatureParams, FeaturePipeline

GOLDEN = Path(__file__).resolve().parents[2] / "fixtures" / "golden"


def _source_rows() -> list[list[str]]:
    doc = json.loads((GOLDEN / "source_klines_btcusdt_1m.json").read_text(encoding="utf-8"))
    return doc["klines"]


def _golden(name: str) -> list[dict[str, str]]:
    with (GOLDEN / name).open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def _bars() -> list[Bar]:
    return [Bar(t_ns=int(r[0]) * 1_000_000, o=float(r[1]), h=float(r[2]), l=float(r[3]),
                c=float(r[4]), v=float(r[5])) for r in _source_rows()]


# ---------------------------------------------------------------- golden Уайлдера


def test_rsi14_matches_wilder_golden_csv() -> None:
    rows = _source_rows()
    gold = _golden("rsi14.csv")
    assert len(rows) == len(gold) == 2000
    rsi = WilderRSI(14)
    checked = 0
    for r, g in zip(rows, gold, strict=True):
        assert int(g["open_time_ms"]) == int(r[0])
        got = rsi.update(float(r[4]))
        if g["rsi14"] == "":
            assert got is None
        else:
            assert got is not None
            assert got == pytest.approx(float(g["rsi14"]), abs=1e-9, rel=0)
            checked += 1
    assert checked == 2000 - 14


def test_atr14_matches_wilder_golden_csv() -> None:
    rows = _source_rows()
    gold = _golden("atr14.csv")
    atr = WilderATR(14)
    checked = 0
    for r, g in zip(rows, gold, strict=True):
        assert int(g["open_time_ms"]) == int(r[0])
        got = atr.update(float(r[2]), float(r[3]), float(r[4]))
        if g["atr14"] == "":
            assert got is None
        else:
            assert got is not None
            assert got == pytest.approx(float(g["atr14"]), abs=1e-9, rel=0)
            checked += 1
    assert checked == 2000 - 14


def test_wilder_golden_csv_matches_exact_rational_recurrence() -> None:
    # Третій, незалежний від float шлях: ті самі означення Уайлдера в точній раціональній арифметиці
    # (Fraction над десятковими рядками біржі). Golden-CSV і інкрементальні класи мусять бути від
    # точного значення на рівні округлення float (~1e−12 на цих числах), що далеко нижче atol 1e−9.
    rows = _source_rows()
    n = 14
    c = [Fraction(r[4]) for r in rows]
    h = [Fraction(r[2]) for r in rows]
    lo = [Fraction(r[3]) for r in rows]
    gains = [max(c[i] - c[i - 1], Fraction(0)) for i in range(1, len(c))]
    losses = [max(c[i - 1] - c[i], Fraction(0)) for i in range(1, len(c))]
    tr = [max(h[i] - lo[i], abs(h[i] - c[i - 1]), abs(lo[i] - c[i - 1])) for i in range(1, len(c))]
    ag, al, atr = sum(gains[:n]) / n, sum(losses[:n]) / n, sum(tr[:n]) / n
    rsi_exact = {n: 100 * ag / (ag + al)}
    atr_exact = {n: atr}
    for i in range(n + 1, len(c)):
        ag += (gains[i - 1] - ag) / n
        al += (losses[i - 1] - al) / n
        atr += (tr[i - 1] - atr) / n
        rsi_exact[i] = 100 * ag / (ag + al)
        atr_exact[i] = atr
    gold_rsi = _golden("rsi14.csv")
    gold_atr = _golden("atr14.csv")
    for i in range(n, len(c)):
        assert float(gold_rsi[i]["rsi14"]) == pytest.approx(float(rsi_exact[i]), abs=1e-10, rel=0)
        assert float(gold_atr[i]["atr14"]) == pytest.approx(float(atr_exact[i]), abs=1e-10, rel=0)


def test_rsi14_matches_stockcharts_published_example() -> None:
    # Зовнішній опублікований приклад: StockCharts ChartSchool, «Relative Strength Index (RSI)»,
    # таблиця-приклад (cs-rsi.xls) — 33 ціни закриття з 4 знаками і 19 значень RSI(14), округлених до 0.01.
    # Відтворено з пам'яті; достовірність: усі 19 опублікованих значень збігаються до 0.01 — випадково
    # (з хибними цінами) такий збіг неможливий. З цінами, округленими до 0.01, вийшло б 70.46 замість 70.53
    # (саме тому перша спроба не відтворилась, див. deviations F-06).
    closes = [44.3389, 44.0902, 44.1497, 43.6124, 44.3278, 44.8264, 45.0955, 45.4245, 45.8433, 46.0826,
              45.8931, 46.0328, 45.6140, 46.2820, 46.2820, 46.0028, 46.0328, 46.4116, 46.2222, 45.6439,
              46.2122, 46.2521, 45.7137, 46.4515, 45.7835, 45.3548, 44.0288, 44.1783, 44.2181, 44.5672,
              43.4205, 42.6628, 43.1314]
    published = [70.53, 66.32, 66.55, 69.41, 66.36, 57.97, 62.93, 63.26, 56.06, 62.38, 54.71, 50.42,
                 39.99, 41.46, 41.87, 45.46, 37.30, 33.08, 37.77]
    rsi = WilderRSI(14)
    got = [v for x in closes if (v := rsi.update(x)) is not None]
    assert len(got) == len(published) == len(closes) - 14
    for g, p in zip(got, published, strict=True):
        assert abs(g - p) <= 0.005 + 1e-9                  # опубліковано з округленням до 0.01
    # контроль: EMA-згладжування (α = 2/(n+1)) з тим самим стартом цей приклад не відтворює
    a = 2.0 / 15
    ag = sum(max(y - x, 0.0) for x, y in pairwise(closes[:15])) / 14
    al = sum(max(x - y, 0.0) for x, y in pairwise(closes[:15])) / 14
    ema_style = []
    for x, y in pairwise(closes[14:]):
        ag += a * (max(y - x, 0.0) - ag)
        al += a * (max(x - y, 0.0) - al)
        ema_style.append(100.0 * ag / (ag + al))
    assert max(abs(e - p) for e, p in zip(ema_style, published[1:], strict=True)) > 1.0


def test_wilder_alpha_is_one_over_n_not_two_over_n_plus_one() -> None:
    n = 14
    # ATR: 14 барів з TR = 1 → ATR = 1; потім TR = 15. Уайлдер: 1 + (15−1)/14 = 2 (точно),
    # EMA-згладжування дало б 1 + 14·2/15 ≈ 2.867.
    atr = WilderATR(n)
    for _ in range(n + 1):
        atr.update(100.5, 99.5, 100.0)
    assert atr.value == 1.0
    got = atr.update(107.5, 92.5, 100.0)
    assert got == 2.0
    assert got != pytest.approx(1.0 + 14.0 * 2.0 / (n + 1))
    assert atr.alpha == 1.0 / n

    # RSI: 7 приростів і 7 падінь по 1 → Ḡ = L̄ = 0.5 (RSI 50); далі приріст 2.
    rsi = WilderRSI(n)
    px = 100.0
    rsi.update(px)
    for i in range(n):
        px += 1.0 if i % 2 == 0 else -1.0
        rsi.update(px)
    assert rsi.value == 50.0
    got = rsi.update(px + 2.0)
    g, lo = 0.5 + (2.0 - 0.5) / n, 0.5 - 0.5 / n
    assert got == pytest.approx(100.0 * g / (g + lo), abs=1e-12)        # 56.667
    a = 2.0 / (n + 1)
    g2, l2 = 0.5 + a * 1.5, 0.5 * (1 - a)
    assert abs(got - 100.0 * g2 / (g2 + l2)) > 4.0                         # 61.76 — інше число

    # На реальних даних: EMA-згладжений TR (α = 2/(n+1)) розходиться з golden ATR на долари.
    gold = [float(g["atr14"]) for g in _golden("atr14.csv") if g["atr14"]]
    bars = _bars()
    tr = [max(b.h - b.l, abs(b.h - p.c), abs(b.l - p.c)) for p, b in pairwise(bars)]
    ema_style = [sum(tr[:n]) / n]
    for x in tr[n:]:
        ema_style.append(ema_style[-1] + a * (x - ema_style[-1]))
    assert max(abs(e - w) for e, w in zip(ema_style, gold, strict=True)) > 1.0


# ---------------------------------------------------------------- EMA


def _ema_batch(x: np.ndarray, n: int) -> np.ndarray:
    """Замкнена форма E_t = (1−α)^m·S + Σ_{j<m} α(1−α)^j·x_{t−j}, m = t−(n−1), S = mean(x_0..x_{n−1}).

    Згортка з ядром α(1−α)^j (обрізаним там, де (1−α)^j < 1e−18) — інший шлях обчислення, ніж рекурсія.
    """
    a = 2.0 / (n + 1)
    size = len(x)
    out = np.full(size, np.nan)
    seed = float(np.mean(x[:n]))
    k = math.ceil(math.log(1e-18) / math.log(1.0 - a))
    kernel = a * (1.0 - a) ** np.arange(k)
    q = x.copy()
    q[:n] = 0.0
    conv = np.convolve(q, kernel)[:size]
    m = np.arange(size) - (n - 1)
    out[n - 1:] = (1.0 - a) ** m[n - 1:] * seed + conv[n - 1:]
    return out


def test_ema_incremental_equals_batch() -> None:
    rng = np.random.default_rng(20260918)
    x = 100.0 + np.cumsum(rng.normal(0.0, 0.5, size=10_000))
    batch = _ema_batch(x, 21)
    ema = EMA(21)
    inc = np.array([np.nan if (v := ema.update(float(p))) is None else v for p in x])
    assert np.all(np.isnan(inc[:20])) and np.all(np.isnan(batch[:20]))
    np.testing.assert_allclose(inc[20:], batch[20:], atol=1e-10, rtol=0)
    assert ema.alpha == 2.0 / 22


# ---------------------------------------------------------------- Велфорд


def test_welford_stable_on_1e9_offset_series() -> None:
    rng = np.random.default_rng(1)
    y = rng.integers(-8192, 8192, size=5000) / 1024.0   # діадичні ∈ [−8, 8): 1e9 + y представимі точно
    x = 1e9 + y
    n = 20
    w = RollingWelford(n)
    welford_err = 0.0
    naive_err = 0.0
    naive_negative = 0
    for i, xi in enumerate(x):
        w.update(float(xi))
        if i < n - 1:
            continue
        ref = float(np.var(y[i - n + 1:i + 1]))       # еталон без зсуву — точний
        assert w.var is not None
        welford_err = max(welford_err, abs(w.var - ref) / ref)
        win = [float(v) for v in x[i - n + 1:i + 1]]
        s1 = 0.0
        s2 = 0.0
        for v in win:
            s1 += v
            s2 += v * v
        naive = (s2 - s1 * s1 / n) / n                  # наївна Σx² − (Σx)²/n
        naive_err = max(naive_err, abs(naive - ref) / ref)
        naive_negative += naive < 0
    # Очікуваний рівень похибки Велфорда: округлення середнього на рівні 1e9 — ulp/2 ≈ 6e−8 за крок,
    # відносно σ ≈ 4.6 і з накопиченням до 1024 кроків між ресинхронізаціями → ~1e−6 (виміряно 1.03e−6).
    assert welford_err < 1e-5
    assert naive_err > 1.0                              # наївна формула: похибка > 100 %
    assert naive_negative > 0                            # і навіть від'ємна «дисперсія»


def test_welford_add_phase_matches_numpy() -> None:
    xs = [3.0, 1.0, 4.0, 1.0, 5.0, 9.0, 2.0, 6.0]
    w = RollingWelford(len(xs))
    for v in xs:
        w.update(v)
    assert w.mean == pytest.approx(float(np.mean(xs)), abs=1e-15)
    assert w.var == pytest.approx(float(np.var(xs)), abs=1e-13)
    assert w.sample_var == pytest.approx(float(np.var(xs, ddof=1)), abs=1e-13)


def test_rolling_resync_bounds_drift_over_long_run() -> None:
    rng = np.random.default_rng(5)
    x = 1e5 + np.cumsum(rng.normal(0.0, 10.0, size=100_000))
    n = 20
    w = RollingWelford(n)
    s = SMA(n)
    for v in x:
        w.update(float(v))
        s.update(float(v))
    tail = x[-n:]
    assert w.mean == pytest.approx(float(np.mean(tail)), abs=1e-8)
    assert s.value == pytest.approx(float(np.mean(tail)), abs=1e-8)
    assert w.var == pytest.approx(float(np.var(tail)), rel=1e-8)


# ---------------------------------------------------------------- Паркінсон


def test_parkinson_vol_exact_on_constant_range() -> None:
    w = 24
    ratio = 1.004
    expected = math.log(ratio) / math.sqrt(4.0 * math.log(2.0))
    pk = Parkinson(w)
    rng = np.random.default_rng(3)
    lows = 50_000.0 * np.exp(np.cumsum(rng.normal(0.0, 0.01, size=300)))
    got: list[float] = []
    for lo in lows:
        v = pk.update(float(lo) * ratio, float(lo))
        if v is not None:
            got.append(v)
    assert len(got) == 300 - w + 1
    assert max(abs(g - expected) / expected for g in got) < 1e-12

    flat = Parkinson(3)
    for _ in range(5):
        v = flat.update(100.0, 100.0)
    assert v == 0.0


def test_parkinson_matches_direct_formula() -> None:
    bars = _bars()[:200]
    w = 24
    pk = Parkinson(w)
    for i, b in enumerate(bars):
        v = pk.update(b.h, b.l)
        if i >= w - 1:
            window = bars[i - w + 1:i + 1]
            ref = math.sqrt(math.fsum(math.log(x.h / x.l) ** 2 for x in window) / (4 * math.log(2) * w))
            assert v == pytest.approx(ref, rel=1e-12)


# ---------------------------------------------------------------- OLS


def test_ols_r2_near_one_on_clean_trend() -> None:
    rng = np.random.default_rng(11)
    t = np.arange(400, dtype=float)
    y = 5.0 + 0.3 * t + rng.normal(0.0, 0.01, size=t.size)
    ols = RollingOLS(20)
    r2s: list[float] = []
    for i, v in enumerate(y):
        slope = ols.update(float(v))
        if i >= 19:
            assert slope == pytest.approx(0.3, abs=2e-3)
            assert ols.r2 is not None
            r2s.append(ols.r2)
    assert min(r2s) > 0.999
    # контроль: на чистому шумі R² далекий від одиниці
    noise = RollingOLS(20)
    for v in rng.normal(0.0, 1.0, size=100):
        noise.update(float(v))
    assert noise.r2 is not None and noise.r2 < 0.5


def test_ols_matches_numpy_polyfit_on_price_level_data() -> None:
    closes = [b.c for b in _bars()[:300]]
    n = 5
    ols = RollingOLS(n, resync_every=10_000)       # без ресинхронізації — перевірка самої рекурентності
    for i, v in enumerate(closes):
        ols.update(v)
        if i >= n - 1:
            win = np.array(closes[i - n + 1:i + 1])
            x = np.arange(n, dtype=float)
            b, a = np.polyfit(x, win, 1)
            resid = win - (a + b * x)
            sst = float(np.sum((win - win.mean()) ** 2))
            r2 = 1.0 - float(np.sum(resid ** 2)) / sst if sst > 0 else 0.0
            assert ols.slope == pytest.approx(b, abs=1e-6)
            assert ols.r2 == pytest.approx(r2, abs=1e-6)


def test_ols_r2_zero_on_flat_series() -> None:
    ols = RollingOLS(5)
    for _ in range(10):
        ols.update(75_000.0)
    assert ols.slope == 0.0 and ols.r2 == 0.0


# ---------------------------------------------------------------- прогрів


def test_indicators_return_none_before_warmup() -> None:
    def warm_index(make, feed) -> int:
        ind = make()
        for i in range(1, 1000):
            if feed(ind, i) is not None:
                return i
        raise AssertionError("never warmed up")

    px = lambda i: 100.0 + math.sin(i) * 3.0 + i * 0.01  # noqa: E731
    cases = {
        "EMA(21)": (lambda: EMA(21), lambda o, i: o.update(px(i)), 21),
        "SMA(20)": (lambda: SMA(20), lambda o, i: o.update(px(i)), 20),
        "RollingWelford(20)": (lambda: RollingWelford(20), lambda o, i: o.update(px(i)), 20),
        "WilderATR(14)": (lambda: WilderATR(14), lambda o, i: o.update(px(i) + 1, px(i) - 1, px(i)), 15),
        "WilderRSI(14)": (lambda: WilderRSI(14), lambda o, i: o.update(px(i)), 15),
        "DequeMax(20)": (lambda: MonotonicDequeMax(20), lambda o, i: o.update(px(i)), 20),
        "DequeMin(20)": (lambda: MonotonicDequeMin(20), lambda o, i: o.update(px(i)), 20),
        "Parkinson(24)": (lambda: Parkinson(24), lambda o, i: o.update(px(i) + 1, px(i) - 1), 24),
        "PercentileRank(500)": (lambda: PercentileRank(500), lambda o, i: o.update(px(i)), 500),
        "RollingOLS(5)": (lambda: RollingOLS(5), lambda o, i: o.update(px(i)), 5),
    }
    for name, (make, feed, expected) in cases.items():
        assert warm_index(make, feed) == expected, name
        fresh = make()
        assert fresh.value is None, name

    # конвеєр: кожне поле Features стає не-None рівно на барі params.warmup()[field]
    params = FeatureParams()
    warm = params.warmup()
    pipe = FeaturePipeline(params)
    first_seen: dict[str, int] = {}
    for i, bar in enumerate(_bars()[:600], start=1):
        f = pipe.update(bar)
        for name in FEATURE_NAMES:
            if name not in first_seen and getattr(f, name) is not None:
                first_seen[name] = i
    for name, bars_needed in warm.items():
        assert first_seen[name] == bars_needed, name
    assert pipe.max_lookback == 523 == max(warm.values())


def test_rsi_flat_is_neutral_and_monotone_up_is_100() -> None:
    flat = WilderRSI(14)
    for _ in range(20):
        v = flat.update(100.0)
    assert v == 50.0
    up = WilderRSI(14)
    for i in range(20):
        v = up.update(100.0 + i)
    assert v == 100.0


def test_percentile_rank_counts_ties_and_self() -> None:
    pr = PercentileRank(4)
    for v in (1.0, 2.0, 2.0):
        assert pr.update(v) is None
    assert pr.update(2.0) == 1.0          # {1,2,2,2}: усі ≤ 2
    assert pr.update(0.5) == 0.25         # {2,2,2,0.5}: лише сам
    with pytest.raises(ValueError):
        pr.update(float("nan"))
