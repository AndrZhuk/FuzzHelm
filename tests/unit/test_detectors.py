"""F. Детектори: чистота, реєстр, знаки/довіра на еталонних сценаріях, пін-бар.

Найменування: tests/unit/test_detectors.py
Автор: Андрій Жук, 2026.
"""

from __future__ import annotations

import copy
import json
import math
from itertools import pairwise
from pathlib import Path

import numpy as np
import pytest

from fuzzhelm.config import load_yaml
from fuzzhelm.core.errors import ConfigValidationError
from fuzzhelm.detectors.base import Detector, DetectorGroup, DetectorOutput
from fuzzhelm.detectors.bollinger_z import BollingerZ
from fuzzhelm.detectors.candle_geometry import (
    CandleGeometry,
    engulfing_scores,
    pin_bar_scores,
    sigma_s,
)
from fuzzhelm.detectors.donchian import Donchian
from fuzzhelm.detectors.ema_slope import EmaSlope
from fuzzhelm.detectors.registry import DETECTOR_NAMES, MIN_WINDOW_CAPACITY, build_detectors
from fuzzhelm.detectors.rsi_exhaustion import RsiExhaustion, rsi_strength
from fuzzhelm.detectors.vol_regime import V_UNKNOWN, VolRegime
from fuzzhelm.features.convert import Bar
from fuzzhelm.features.pipeline import FeatureParams, FeaturePipeline, Features
from fuzzhelm.features.window import BarWindow

GOLDEN = Path(__file__).resolve().parents[2] / "fixtures" / "golden"


def _golden_bars() -> list[Bar]:
    doc = json.loads((GOLDEN / "source_klines_btcusdt_1m.json").read_text(encoding="utf-8"))
    return [Bar(t_ns=int(r[0]) * 1_000_000, o=float(r[1]), h=float(r[2]), l=float(r[3]),
                c=float(r[4]), v=float(r[5])) for r in doc["klines"]]


def _run(bars: list[Bar], params: FeatureParams | None = None, capacity: int = 64
         ) -> tuple[BarWindow, list[Features]]:
    pipe = FeaturePipeline(params)
    w = BarWindow(capacity)
    feats = []
    for b in bars:
        f = pipe.update(b)
        w.append(b, f)
        feats.append(f)
    return w, feats


def _window_with(feats: Features, bar: Bar | None = None, prev: Bar | None = None) -> BarWindow:
    w = BarWindow(4)
    if prev is not None:
        w.append(prev, Features())
    w.append(bar or Bar(t_ns=0, o=100.0, h=101.0, l=99.0, c=100.0, v=1.0), feats)
    return w


def _snapshot(w: BarWindow) -> tuple[object, ...]:
    return (w.t, len(w), tuple(w.bar(k) for k in range(len(w))), tuple(w.feats(k) for k in range(len(w))))


# ---------------------------------------------------------------- контракт і реєстр


def test_all_detectors_registered() -> None:
    dets = build_detectors()
    assert tuple(d.name for d in dets) == DETECTOR_NAMES
    assert len(DETECTOR_NAMES) == 6
    yaml_cfg = load_yaml("detectors")["detectors"]
    groups = {
        "ema_slope": DetectorGroup.TREND, "donchian": DetectorGroup.TREND,
        "rsi_exhaustion": DetectorGroup.REVERSION, "bollinger_z": DetectorGroup.REVERSION,
        "candle_geometry": DetectorGroup.REVERSION, "vol_regime": DetectorGroup.CONTEXT,
    }
    for d in dets:
        assert isinstance(d, Detector)
        assert d.weight == yaml_cfg[d.name]["weight"]
        assert d.group is groups[d.name]
        assert isinstance(d.warmup, int) and d.warmup >= 2
    assert max(d.warmup for d in dets) == FeatureParams.from_config(load_yaml("detectors")).max_lookback

    bad = copy.deepcopy(load_yaml("detectors"))
    bad["detectors"]["ema_slope"]["weight"] = -1.0
    with pytest.raises(ConfigValidationError) as ei:
        build_detectors(bad)
    assert ei.value.path == "detectors.ema_slope.weight"
    del bad["detectors"]["vol_regime"]
    with pytest.raises(ConfigValidationError):
        build_detectors(bad)


def test_all_detectors_are_pure() -> None:
    bars = _golden_bars()
    dets = build_detectors()
    pipe = FeaturePipeline(FeatureParams.from_config(load_yaml("detectors")))
    w = BarWindow(32)
    checkpoints = {25, 300, 700, 1200, 1999}
    for i, b in enumerate(bars):
        w.append(b, pipe.update(b))
        if i not in checkpoints:
            continue
        snap = _snapshot(w)
        state = [tuple(getattr(d, s) for s in type(d).__slots__) for d in dets]
        first = [d.compute(w) for d in dets]
        second = [d.compute(w) for d in reversed(dets)][::-1]
        third = [d.compute(w) for d in dets]
        assert first == second == third
        assert _snapshot(w) == snap                                   # вікно не змінено
        assert [tuple(getattr(d, s) for s in type(d).__slots__) for d in dets] == state
    # той самий вміст вікна, зібраний іншим екземпляром конвеєра й мінімальної ємності → той самий вихід
    w2, _ = _run(bars, FeatureParams.from_config(load_yaml("detectors")), capacity=MIN_WINDOW_CAPACITY)
    assert [d.compute(w2) for d in dets] == [d.compute(w) for d in dets]


def test_detectors_silent_until_their_inputs_are_warm() -> None:
    bars = _golden_bars()[:600]
    dets = build_detectors()
    pipe = FeaturePipeline()
    w = BarWindow(8)
    for i, b in enumerate(bars, start=1):
        w.append(b, pipe.update(b))
        for d in dets:
            out = d.compute(w)
            assert isinstance(out, DetectorOutput) and out.group is d.group and out.weight == d.weight
            if i < d.warmup:
                assert (out.s, out.c) == (0.0, 0.0), (d.name, i)
    last = {d.name: d.compute(w) for d in dets}
    assert last["vol_regime"].c == 1.0 and last["ema_slope"].c > 0.0


# ---------------------------------------------------------------- EmaSlope


def test_ema_slope_positive_on_linear_uptrend() -> None:
    bars = [Bar(t_ns=i, o=100.0 + 0.1 * i - 0.05, h=100.0 + 0.1 * i + 0.05, l=100.0 + 0.1 * i - 0.05,
                c=100.0 + 0.1 * i, v=1.0) for i in range(60)]
    w, _ = _run(bars)
    out = EmaSlope().compute(w)
    # EMA лінійного ряду після прогріву — лінійна з тим самим нахилом 0.1: g = 0.5/(5·ATR), ATR = 0.15
    assert out.features["g"] == pytest.approx(0.5 / (5 * 0.15), rel=1e-6)
    assert out.s > 0.99 and out.c > 0.999
    down = [Bar(t_ns=b.t_ns, o=200.0 - b.o, h=200.0 - b.l, l=200.0 - b.h, c=200.0 - b.c, v=1.0) for b in bars]
    wd, _ = _run(down)
    assert EmaSlope().compute(wd).s == pytest.approx(-out.s, abs=1e-9)


# ---------------------------------------------------------------- Donchian


def test_donchian_confidence_decays_with_staleness() -> None:
    det = Donchian()
    bar = Bar(t_ns=0, o=105.5, h=106.2, l=105.4, c=106.0, v=1.0)
    cs = []
    for age in range(12):
        f = Features(donch_hi=110.0, donch_lo=90.0, atr=1.0, donch_ref=105.0, donch_dir=1.0,
                     bars_since_breakout=float(age))
        out = det.compute(_window_with(f, bar))
        assert out.s == pytest.approx(math.tanh(1.0 / 0.5), abs=1e-15)   # d = (106 − 105)/1
        cs.append(out.c)
    # ширина 20 ≥ 6·ATR → множник ширини = 1; c = exp(−0.3·вік) точно
    for age, c in enumerate(cs):
        assert c == pytest.approx(math.exp(-0.3 * age), rel=1e-15)
    assert all(b < a for a, b in pairwise(cs))

    # наскрізно через конвеєр: бокові коливання → пробій угору → бари всередині нового каналу
    rng = np.random.default_rng(2)
    series = []
    for i in range(40):
        c = 100.0 + rng.uniform(-1, 1)
        series.append(Bar(t_ns=i, o=c, h=c + 0.3, l=c - 0.3, c=c, v=1.0))
    series.append(Bar(t_ns=40, o=100.5, h=104.2, l=100.4, c=104.0, v=1.0))        # пробій
    for i in range(41, 51):
        series.append(Bar(t_ns=i, o=103.0, h=103.2, l=102.8, c=103.0, v=1.0))
    pipe = FeaturePipeline()
    w = BarWindow(4)
    trail = []
    for b in series:
        f = pipe.update(b)
        w.append(b, f)
        trail.append((f.bars_since_breakout, det.compute(w)))
    post = trail[40:]
    assert [age for age, _ in post] == [float(k) for k in range(11)]
    assert post[0][1].s > 0.0                                   # над пробитим рівнем — бичачий
    # На барі 0 ширина — канал ДО пробою; з бару 1 сам пробійний бар розширює канал (U = його high),
    # тому порівнюємо з віку 1, коли ширина вже стала: далі довіра строго згасає з віком пробою.
    aged = post[1:]
    assert all(o2.c < o1.c for (_, o1), (_, o2) in pairwise(aged))
    assert aged[-1][1].c < 0.1 * aged[0][1].c


def test_donchian_breakdown_is_mirror_and_silent_without_breakout() -> None:
    det = Donchian()
    assert det.compute(_window_with(Features(donch_hi=110.0, donch_lo=90.0, atr=1.0))) .c == 0.0
    up = Features(donch_hi=110.0, donch_lo=90.0, atr=1.0, donch_ref=110.0, donch_dir=1.0,
                  bars_since_breakout=0.0)
    dn = Features(donch_hi=110.0, donch_lo=90.0, atr=1.0, donch_ref=90.0, donch_dir=-1.0,
                  bars_since_breakout=0.0)
    o_up = det.compute(_window_with(up, Bar(t_ns=0, o=110.0, h=111.0, l=109.5, c=110.8, v=1.0)))
    o_dn = det.compute(_window_with(dn, Bar(t_ns=0, o=90.0, h=90.5, l=89.0, c=89.2, v=1.0)))
    assert o_up.s == pytest.approx(-o_dn.s, abs=1e-15) and o_up.s > 0.9
    assert o_up.c == o_dn.c == 1.0


# ---------------------------------------------------------------- RsiExhaustion


def _rsi_out(rsi: float, rsi_delta: float = 0.0, close_delta: float = 0.0) -> DetectorOutput:
    f = Features(rsi=rsi, rsi_delta=rsi_delta, close_delta=close_delta, atr=1.0)
    return RsiExhaustion().compute(_window_with(f))


def test_rsi_convex_map_weak_in_middle() -> None:
    s45, s25 = _rsi_out(45.0).s, _rsi_out(25.0).s
    assert abs(s45) < abs(s25) / 4
    assert s45 == pytest.approx(0.1 ** 1.6, rel=1e-12) and s25 == pytest.approx(0.5 ** 1.6, rel=1e-12)
    assert _rsi_out(50.0).s == 0.0
    assert _rsi_out(0.0).s == 1.0 and _rsi_out(100.0).s == -1.0
    assert _rsi_out(55.0).s == pytest.approx(-s45, abs=1e-15)
    grid = [rsi_strength(r) for r in np.linspace(0, 100, 101)]
    assert all(b <= a for a, b in pairwise(grid))     # монотонно спадає


def test_rsi_divergence_raises_confidence_only_in_reversal_direction() -> None:
    base = _rsi_out(25.0)
    bull_div = _rsi_out(25.0, rsi_delta=+8.0, close_delta=-6.0)       # ціна вниз, RSI вгору
    wrong = _rsi_out(25.0, rsi_delta=-8.0, close_delta=+6.0)          # «ведмежа» при перепроданості
    assert base.features["div_score"] == 0.0 and wrong.features["div_score"] == 0.0
    assert bull_div.features["div_score"] > 0.3
    assert bull_div.c > base.c == pytest.approx(0.6 * 0.5 ** 0.8, rel=1e-12)
    assert bull_div.s == base.s                                        # сила не змінюється


# ---------------------------------------------------------------- BollingerZ


def test_bollinger_confidence_drops_when_bandwidth_expands() -> None:
    rng = np.random.default_rng(4)
    bars = []
    px = 100.0
    for i in range(330):
        vol = 0.05 if i < 300 else 1.0                                 # розширення з бару 300
        px += rng.normal(0.0, vol)
        bars.append(Bar(t_ns=i, o=px, h=px + vol, l=px - vol, c=px, v=1.0))
    det = BollingerZ()
    pipe = FeaturePipeline()
    w = BarWindow(4)
    cs = []
    for b in bars:
        w.append(b, pipe.update(b))
        cs.append(det.compute(w).c)
    quiet = np.array(cs[250:300])
    expanding = np.array(cs[302:330])
    assert quiet.mean() > 0.3
    assert expanding.max() <= 0.1
    assert expanding.mean() < quiet.mean() / 5


def test_bollinger_sign_is_mean_reverting_and_flat_window_is_neutral() -> None:
    f = Features(sma=100.0, sigma=2.0, bb_bw_rank=0.25)
    hi = BollingerZ().compute(_window_with(f, Bar(t_ns=0, o=103, h=104.5, l=103, c=104.0, v=1)))
    assert hi.s == pytest.approx(-math.tanh(1.0), rel=1e-12) and hi.c == 0.75
    flat = Features(sma=100.0, sigma=0.0, bb_bw_rank=1.0)
    out = BollingerZ().compute(_window_with(flat, Bar(t_ns=0, o=100, h=100, l=100, c=100.0, v=1)))
    assert out.s == 0.0


# ---------------------------------------------------------------- CandleGeometry


def test_pin_bar_score_zero_when_wicks_symmetric() -> None:
    # Формула брифінгу π⁺ = σ_s(W_l/B − 2)·σ_s(1 − W_u/B)·m при W_l = W_u = W (r = W/B) дає
    # σ_s(r−2)·σ_s(1−r); аргументи в сумі дають −1, тож максимум при r = 1.5: σ_s(−0.5)² ≈ 0.0333.
    # Отже π⁺ НЕ рівно нуль (див. deviations): рівно нулем є ЧИСТИЙ знаковий пін-скор π⁺ − π⁻ і сила s.
    bound = sigma_s(-0.5) ** 2
    assert bound == pytest.approx(0.03328, abs=1e-5)
    atr = 1.0
    for body, wick, bull in [(0.5, 1.0, True), (0.5, 0.75, False), (0.2, 3.0, True), (1.0, 0.25, True),
                             (0.4, 0.6, True), (1.0, 1.5, False)]:
        o, c = (100.0, 100.0 + body) if bull else (100.0 + body, 100.0)
        bar = Bar(t_ns=0, o=o, h=100.0 + body + wick, l=100.0 - wick, c=c, v=1.0)
        pin_b, pin_s = pin_bar_scores(bar, atr)
        assert pin_b == pin_s
        assert pin_b <= bound + 1e-15
        if wick == 1.5 * body:
            assert pin_b == pytest.approx(bound, rel=1e-12)             # межа досягається
        f = Features(atr=atr, donch_hi=110.0, donch_lo=90.0)
        out = CandleGeometry().compute(_window_with(f, bar, prev=bar))
        assert out.s == 0.0
        assert out.features["pin_bull"] - out.features["pin_bear"] == 0.0
    # контроль: класичний молот (нижня тінь = 3 тіла, верхньої немає) — сильний бичачий пін-бар
    hammer = Bar(t_ns=0, o=100.0, h=100.5, l=98.5, c=100.5, v=1.0)
    pb, ps = pin_bar_scores(hammer, atr)
    assert pb > 0.9 and ps < 1e-3


def test_candle_geometry_hammer_at_support_is_bullish_and_confident() -> None:
    prev = Bar(t_ns=0, o=100.6, h=100.7, l=100.0, c=100.1, v=1.0)
    hammer = Bar(t_ns=1, o=100.0, h=100.5, l=98.5, c=100.5, v=1.0)
    near = Features(atr=1.0, donch_hi=106.0, donch_lo=98.6)
    far = Features(atr=1.0, donch_hi=106.0, donch_lo=94.0)
    o_near = CandleGeometry().compute(_window_with(near, hammer, prev))
    o_far = CandleGeometry().compute(_window_with(far, hammer, prev))
    assert o_near.s > 0.5
    assert o_near.c > 0.8 > o_far.c
    assert o_near.s == o_far.s                                             # рівень впливає лише на довіру
    star = Bar(t_ns=1, o=100.5, h=102.0, l=100.0, c=100.0, v=1.0)          # падаюча зірка — дзеркало
    o_star = CandleGeometry().compute(_window_with(Features(atr=1.0, donch_hi=102.1, donch_lo=94.0),
                                                   star, prev))
    assert o_star.s < -0.5


def test_engulfing_scores_mirror() -> None:
    prev = Bar(t_ns=0, o=101.0, h=101.1, l=99.9, c=100.0, v=1.0)
    cur = Bar(t_ns=1, o=99.9, h=101.6, l=99.8, c=101.5, v=1.0)
    eb, es = engulfing_scores(cur, prev, 1.0)
    assert eb > 0.8 and es < 1e-3
    rev_prev = Bar(t_ns=0, o=100.0, h=101.1, l=99.9, c=101.0, v=1.0)
    rev_cur = Bar(t_ns=1, o=101.1, h=101.2, l=99.4, c=99.5, v=1.0)
    eb2, es2 = engulfing_scores(rev_cur, rev_prev, 1.0)
    assert es2 > 0.8 and eb2 < 1e-3


# ---------------------------------------------------------------- VolRegime


def test_vol_regime_emits_v_rank_and_neutral_before_warmup() -> None:
    det = VolRegime()
    cold = det.compute(_window_with(Features()))
    assert (cold.s, cold.c) == (0.0, 0.0) and cold.features["V"] == V_UNKNOWN
    warm = det.compute(_window_with(Features(vol_rank=0.82, park_sigma=0.001)))
    assert (warm.s, warm.c) == (0.0, 1.0) and warm.features["V"] == 0.82
    assert det.group is DetectorGroup.CONTEXT
