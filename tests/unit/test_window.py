"""BarWindow і охоронець look-ahead (група K: test_lookahead_guard_raises_on_future_index).

Найменування: tests/unit/test_window.py
Автор: Андрій Жук, 2026.
"""

from __future__ import annotations

import numpy as np
import pytest

from fuzzhelm.core.errors import LookaheadError
from fuzzhelm.features.convert import Bar
from fuzzhelm.features.pipeline import Features
from fuzzhelm.features.window import BarWindow, GuardedArray, LookaheadGuard


def _bar(i: int) -> Bar:
    return Bar(t_ns=i * 60_000_000_000, o=100.0 + i, h=101.0 + i, l=99.0 + i, c=100.5 + i, v=float(i))


def test_lookahead_guard_raises_on_future_index() -> None:
    closes = np.arange(10, dtype=float) * 10.0
    g = LookaheadGuard(closes, cursor=4)
    assert g[4] == 40.0 and g[0] == 0.0
    with pytest.raises(LookaheadError):
        g[5]
    with pytest.raises(LookaheadError):
        g[-1]                       # останній елемент ПОВНОГО масиву — майбутнє
    with pytest.raises(LookaheadError):
        g[3:6]
    with pytest.raises(LookaheadError):
        g[2:]                       # «до кінця» зачіпає майбутнє
    np.testing.assert_array_equal(g[0:5], closes[:5])
    np.testing.assert_array_equal(g.visible(), closes[:5])
    g.advance()
    assert g[5] == 50.0
    with pytest.raises(LookaheadError):
        g[6]

    # вікно детекторів: від'ємний лаг — теж майбутнє
    w = BarWindow(8)
    for i in range(3):
        w.append(_bar(i), Features(ema=float(i)))
    with pytest.raises(LookaheadError):
        w.bar(-1)
    with pytest.raises(LookaheadError):
        w.feat("ema", -1)
    with pytest.raises(LookaheadError):
        w.feats(-2)


def test_guarded_array_views_are_read_only_and_2d_rows_guarded() -> None:
    arr = np.arange(20, dtype=float).reshape(10, 2)
    g = GuardedArray(arr, cursor=2)
    row = g[2]
    assert list(row) == [4.0, 5.0]
    with pytest.raises(ValueError):
        row[0] = -1.0
    with pytest.raises(ValueError):
        g.visible()[0, 0] = -1.0
    with pytest.raises(LookaheadError):
        g[3, 0]
    assert len(g) == 3
    with pytest.raises(IndexError):
        g.cursor = 10
    assert arr[2, 0] == 4.0


def test_bar_window_lags_capacity_and_series() -> None:
    w = BarWindow(4)
    assert len(w) == 0 and w.t == -1
    for i in range(6):
        w.append(_bar(i), Features(ema=float(i), atr=None if i < 3 else 1.0))
    assert w.t == 5 and len(w) == 4
    assert w.bar().c == 105.5 and w.bar(3).c == 102.5
    assert w.feat("ema") == 5.0 and w.feat("ema", 2) == 3.0
    with pytest.raises(IndexError):
        w.bar(4)                    # глибше за історію — нестача даних, не майбутнє
    np.testing.assert_array_equal(w.series("ema", 4), [2.0, 3.0, 4.0, 5.0])
    np.testing.assert_array_equal(w.series("c", 2), [104.5, 105.5])
    s = w.series("atr", 4)
    assert np.isnan(s[0]) and list(s[1:]) == [1.0, 1.0, 1.0]
    with pytest.raises(IndexError):
        w.series("ema", 5)
    with pytest.raises(LookaheadError):
        w.series("ema", -1)


def test_bar_window_series_keeps_ns_timestamps_exact() -> None:
    # 2026-09-15 00:00:00.000000001 UTC у нс: > 2^53, у float64 останні розряди загубились би
    t0 = 1_789_430_400_000_000_001
    w = BarWindow(3)
    for i in range(3):
        w.append(Bar(t_ns=t0 + i * 60_000_000_000, o=1.0, h=1.0, l=1.0, c=1.0, v=0.0, n=i), Features())
    ts = w.series("t_ns", 3)
    assert ts.dtype == np.int64
    assert ts.tolist() == [t0, t0 + 60_000_000_000, t0 + 120_000_000_000]
    assert float(t0) != t0                                   # контроль: float64 справді втрачає точність
    assert w.series("n", 3).tolist() == [0, 1, 2]
