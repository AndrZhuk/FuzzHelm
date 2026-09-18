"""E. Індикатори — property-тести (hypothesis): інкрементальне = наївне на довільних рядах.

Найменування: tests/property/test_features_property.py
Автор: Андрій Жук, 2026.
"""

from __future__ import annotations

from bisect import bisect_right

import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from fuzzhelm.features.indicators import (
    MonotonicDequeMax,
    MonotonicDequeMin,
    PercentileRank,
    RollingOLS,
    RollingWelford,
)

pytestmark = pytest.mark.property

finite = st.floats(min_value=-1e12, max_value=1e12, allow_nan=False, allow_infinity=False)


@settings(max_examples=200)
@given(xs=st.lists(finite, min_size=1, max_size=200), n=st.integers(min_value=1, max_value=40))
def test_donchian_deque_matches_naive_max(xs: list[float], n: int) -> None:
    dmax = MonotonicDequeMax(n)
    dmin = MonotonicDequeMin(n)
    for i, x in enumerate(xs):
        got_max = dmax.update(x)
        got_min = dmin.update(x)
        if i < n - 1:
            assert got_max is None and got_min is None
        else:
            win = xs[i - n + 1:i + 1]
            assert got_max == max(win)
            assert got_min == min(win)
            assert dmax.value == got_max and dmin.value == got_min


@settings(max_examples=100)
@given(xs=st.lists(st.floats(min_value=-1e6, max_value=1e6, allow_nan=False), min_size=1, max_size=150),
       n=st.integers(min_value=1, max_value=30))
def test_percentile_rank_matches_naive_count(xs: list[float], n: int) -> None:
    pr = PercentileRank(n)
    for i, x in enumerate(xs):
        got = pr.update(x)
        if i < n - 1:
            assert got is None
        else:
            win = xs[i - n + 1:i + 1]
            assert got == sum(1 for v in win if v <= x) / n
            assert got == bisect_right(sorted(win), x) / n


@settings(max_examples=100)
@given(level=st.floats(min_value=-1e6, max_value=1e6), scale=st.floats(min_value=1e-3, max_value=1e3),
       seed=st.integers(0, 2**32 - 1), n=st.integers(min_value=2, max_value=40))
def test_rolling_welford_matches_numpy(level: float, scale: float, seed: int, n: int) -> None:
    rng = np.random.default_rng(seed)
    xs = level + scale * rng.standard_normal(3 * n + 17)
    w = RollingWelford(n, resync_every=7)
    for i, x in enumerate(xs):
        m = w.update(float(x))
        if i >= n - 1:
            win = xs[i - n + 1:i + 1]
            assert m == pytest.approx(float(np.mean(win)), abs=1e-9 * (abs(level) + scale))
            assert w.var == pytest.approx(float(np.var(win)), rel=1e-6, abs=1e-12 * (abs(level) + scale) ** 2)


@settings(max_examples=100)
@given(level=st.floats(min_value=-1e5, max_value=1e5), slope=st.floats(min_value=-10, max_value=10),
       seed=st.integers(0, 2**32 - 1), n=st.integers(min_value=3, max_value=30))
def test_rolling_ols_matches_polyfit(level: float, slope: float, seed: int, n: int) -> None:
    rng = np.random.default_rng(seed)
    t = np.arange(2 * n + 11, dtype=float)
    ys = level + slope * t + rng.standard_normal(t.size)
    ols = RollingOLS(n, resync_every=5)
    x = np.arange(n, dtype=float)
    for i, y in enumerate(ys):
        got = ols.update(float(y))
        if i >= n - 1:
            win = ys[i - n + 1:i + 1]
            b, a = np.polyfit(x, win, 1)
            sst = float(np.sum((win - win.mean()) ** 2))
            r2 = 1.0 - float(np.sum((win - (a + b * x)) ** 2)) / sst
            assert got == pytest.approx(b, abs=1e-7 * (1 + abs(level)))
            assert ols.r2 == pytest.approx(r2, abs=1e-6)
