"""F. Детектори — property: s ∈ [−1;1], c ∈ [0;1], без NaN для будь-якого OHLCV.

Найменування: tests/property/test_detectors_property.py
Автор: Андрій Жук, 2026.

Домен «будь-якого OHLCV»: ціни — скінченні додатні (домен Candle: PosDec) у діапазоні 1e−6…1e9
(15 порядків, стрибки між сусідніми барами до ×1e15), обсяг — скінченний ≥ 0 (включно з нулем),
бари з нульовим діапазоном, пласкі серії та «сирі» бари, де o/h/l/c узгоджені довільно (h < l тощо).
Щоб прогрів не з'їдав приклади, у hypothesis-тесті вікна скорочено (структура формул та сама);
окремий тест проганяє виробничі параметри на довгій змішаній серії.
"""

from __future__ import annotations

import math

import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from fuzzhelm.detectors.base import Detector, DetectorOutput
from fuzzhelm.detectors.bollinger_z import BollingerZ
from fuzzhelm.detectors.candle_geometry import CandleGeometry
from fuzzhelm.detectors.donchian import Donchian
from fuzzhelm.detectors.ema_slope import EmaSlope
from fuzzhelm.detectors.registry import build_detectors
from fuzzhelm.detectors.rsi_exhaustion import RsiExhaustion
from fuzzhelm.detectors.vol_regime import VolRegime
from fuzzhelm.features.convert import Bar
from fuzzhelm.features.pipeline import FeatureParams, FeaturePipeline
from fuzzhelm.features.window import BarWindow

pytestmark = pytest.mark.property

PMIN, PMAX = 1e-6, 1e9
SMALL = FeatureParams(n_ema=3, ema_horizon=2, ema_r2_points=3, n_atr=3, n_rsi=3, rsi_div_lookback=2,
                      bb_n=3, bb_rank_window=4, donchian_n=3, park_window=2, vol_rank_window=4,
                      volume_window=3, resync_every=5)


def _small_detectors() -> list[Detector]:
    p = SMALL
    return [EmaSlope(horizon=p.ema_horizon, params=p), Donchian(params=p), RsiExhaustion(params=p),
            BollingerZ(params=p), CandleGeometry(params=p), VolRegime(params=p)]


def _clamp(x: float) -> float:
    return min(PMAX, max(PMIN, x))


@st.composite
def ohlcv_sequences(draw: st.DrawFn) -> list[Bar]:
    n = draw(st.integers(min_value=1, max_value=40))
    px = draw(st.floats(min_value=PMIN, max_value=PMAX))
    unit = st.floats(min_value=0.0, max_value=1.0)
    bars: list[Bar] = []
    for i in range(n):
        kind = draw(st.sampled_from(("walk", "flat", "zero_range", "jump", "raw")))
        vol = draw(st.one_of(st.just(0.0), st.floats(min_value=0.0, max_value=1e12)))
        if kind == "raw":
            o, h, lo, c = (draw(st.floats(min_value=PMIN, max_value=PMAX)) for _ in range(4))
        elif kind == "flat":
            o = h = lo = c = px
        else:
            if kind == "jump":
                px = _clamp(px * 10.0 ** draw(st.floats(min_value=-15.0, max_value=15.0)))
            else:
                px = _clamp(px * math.exp(draw(st.floats(min_value=-0.5, max_value=0.5))))
            if kind == "zero_range":
                o = h = lo = c = px
            else:
                rng = px * draw(st.floats(min_value=0.0, max_value=2.0))
                lo = _clamp(px - rng * draw(unit))
                h = _clamp(lo + rng)
                o = _clamp(lo + (h - lo) * draw(unit))
                c = _clamp(lo + (h - lo) * draw(unit))
                px = c
        bars.append(Bar(t_ns=i, o=o, h=h, l=lo, c=c, v=vol))
    return bars


def _check(out: DetectorOutput) -> None:
    assert -1.0 <= out.s <= 1.0 and math.isfinite(out.s), out
    assert 0.0 <= out.c <= 1.0 and math.isfinite(out.c), out
    for k, v in out.features.items():
        assert math.isfinite(v), (out.name, k, v)


@settings(max_examples=200)
@given(bars=ohlcv_sequences())
def test_all_detectors_bounded_and_no_nan_on_any_ohlcv(bars: list[Bar]) -> None:
    dets = _small_detectors()
    pipe = FeaturePipeline(SMALL)
    w = BarWindow(8)
    for b in bars:
        w.append(b, pipe.update(b))
        for d in dets:                       # кожен із 6 детекторів на кожному з 200 прикладів
            _check(d.compute(w))


def test_detectors_bounded_on_long_adversarial_series_with_production_params() -> None:
    rng = np.random.default_rng(20260918)
    dets = build_detectors()
    pipe = FeaturePipeline()
    w = BarWindow(8)
    px = 100.0
    active = {d.name: 0 for d in dets}
    for i in range(1500):
        regime = (i // 150) % 5
        if regime == 0:                                     # звичайне блукання
            px *= math.exp(rng.normal(0, 0.002))
            r = px * abs(rng.normal(0, 0.002))
            o, c = px * (1 + rng.normal(0, 5e-4)), px
            h, lo = max(o, c) + r, min(o, c) - r
        elif regime == 1:                                   # пласко, нульовий діапазон і обсяг
            o = h = lo = c = px
        elif regime == 2:                                   # стрибки на порядки
            px = _clamp(px * 10.0 ** rng.uniform(-3, 3))
            o, c = px, _clamp(px * rng.uniform(0.5, 2))
            h, lo = max(o, c) * 1.5, min(o, c) / 1.5
        elif regime == 3:                                   # довільні неузгоджені OHLC
            o, h, lo, c = (_clamp(px * 10.0 ** rng.uniform(-1, 1)) for _ in range(4))
        else:                                               # сильний тренд
            px *= 1.01
            o, h, lo, c = px / 1.01, px * 1.001, px / 1.012, px
        v = 0.0 if regime == 1 else float(rng.exponential(10.0))
        bar = Bar(t_ns=i, o=o, h=h, l=lo, c=c, v=v)
        w.append(bar, pipe.update(bar))
        for d in dets:
            out = d.compute(w)
            _check(out)
            active[d.name] += out.c > 0
    assert all(n > 0 for n in active.values()), active     # кожен детектор реально працював
