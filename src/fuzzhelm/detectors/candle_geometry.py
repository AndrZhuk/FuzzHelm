"""Детектор геометрії свічок (реверсійна група B): пін-бари і поглинання без порогових if.

Найменування: detectors/candle_geometry.py
Призначення: s = clip(Σ_j sign_j·v_j·score_j / Σ_j v_j, −1, 1);  c = max_j score_j·exp(−dist_j/(2·ATR))
             (брифінг §5.2). Усі скори — добутки гладких сигмоїд σ_s(x) = 1/(1+e^{−3x}).
Автор: Андрій Жук, 2026.

Патерни j (у брифінгу задано лише пін-бар; решта — див. docs/deviations.d/features.md):
  pin_bull (+1): π⁺ = σ_s(W_l/max(B,ε) − 2)·σ_s(1 − W_u/max(B,ε))·min(1, Rg/ATR)   — формула брифінгу
  pin_bear (−1): π⁻ = σ_s(W_u/max(B,ε) − 2)·σ_s(1 − W_l/max(B,ε))·min(1, Rg/ATR)   — дзеркало
  engulf_bull (+1): E⁺ = σ_s(B_t/max(B_{t−1},ε) − 1)·σ_s(4δ_t − 1)·σ_s(−4δ_{t−1} − 1)·min(1, Rg/ATR)
  engulf_bear (−1): E⁻ = σ_s(B_t/max(B_{t−1},ε) − 1)·σ_s(−4δ_t − 1)·σ_s(4δ_{t−1} − 1)·min(1, Rg/ATR)
де B — тіло, W_u/W_l — верхня/нижня тінь, Rg — діапазон, δ = (c − o)/Rg — знакова частка тіла.
Голос патерну v_j = w_j·score_j (апріорна вага × присутність): тоді s — середнє знаків, зважене
присутністю, і |s| = score, коли спрацював один патерн (при сталих v_j сигнал стискався б у 4 рази).
dist_j — відстань від екстремуму патерну до «його» рівня: для бичачих — |low − L| (підтримка),
для ведмежих — |high − U| (опір), де U, L — канал Дончіана попередніх n барів.
"""

from __future__ import annotations

import math

from fuzzhelm.detectors.base import DetectorGroup, DetectorOutput
from fuzzhelm.features.convert import Bar
from fuzzhelm.features.pipeline import REL_FLOOR, FeatureParams
from fuzzhelm.features.window import BarWindow

_TINY = 1e-300


def sigma_s(x: float) -> float:
    """σ_s(x) = 1/(1+e^{−3x}) без переповнення exp для великих |x|."""
    if x >= 0.0:
        return 1.0 / (1.0 + math.exp(-3.0 * x))
    e = math.exp(3.0 * x)
    return e / (1.0 + e)


def _geometry(b: Bar) -> tuple[float, float, float, float, float, float]:
    """(hi, lo, B, W_u, W_l, Rg) з ефективними екстремумами max/min(o,h,l,c) — стійко до будь-якого OHLC."""
    o, c = b.o, b.c
    top = o if o > c else c
    bot = c if o > c else o
    hi = b.h if b.h > top else top
    lo = b.l if b.l < bot else bot
    return hi, lo, top - bot, hi - top, bot - lo, hi - lo


def pin_bar_scores(bar: Bar, atr: float) -> tuple[float, float]:
    """(π⁺, π⁻) — бичачий і ведмежий пін-бар за формулою брифінгу §5.2."""
    _, _, body, w_u, w_l, rg = _geometry(bar)
    eps = REL_FLOOR * abs(bar.c) + _TINY
    bd = body if body > eps else eps
    a = atr if atr > eps else eps
    m = min(1.0, rg / a)
    pin_bull = sigma_s(w_l / bd - 2.0) * sigma_s(1.0 - w_u / bd) * m
    pin_bear = sigma_s(w_u / bd - 2.0) * sigma_s(1.0 - w_l / bd) * m
    return pin_bull, pin_bear


def engulfing_scores(bar: Bar, prev: Bar, atr: float) -> tuple[float, float]:
    """(E⁺, E⁻) — бичаче і ведмеже поглинання (гладко, без if)."""
    _, _, body, _, _, rg = _geometry(bar)
    _, _, body_p, _, _, rg_p = _geometry(prev)
    eps = REL_FLOOR * abs(bar.c) + _TINY
    a = atr if atr > eps else eps
    m = min(1.0, rg / a)
    dt = (bar.c - bar.o) / (rg if rg > eps else eps)
    dp = (prev.c - prev.o) / (rg_p if rg_p > eps else eps)
    size = sigma_s(body / (body_p if body_p > eps else eps) - 1.0)
    # добутки кольорів записано так, щоб при dt = dp отримати побітово однакові E⁺ і E⁻
    col_bull = sigma_s(4.0 * dt - 1.0) * sigma_s(-4.0 * dp - 1.0)
    col_bear = sigma_s(-4.0 * dt - 1.0) * sigma_s(4.0 * dp - 1.0)
    return size * col_bull * m, size * col_bear * m


class CandleGeometry:
    __slots__ = ("group", "name", "w_engulf", "w_pin", "warmup", "weight")

    def __init__(self, weight: float = 0.6, *, w_pin: float = 1.0, w_engulf: float = 1.0,
                 params: FeatureParams | None = None) -> None:
        p = params or FeatureParams()
        self.name = "candle_geometry"
        self.group = DetectorGroup.REVERSION
        self.weight = weight
        self.w_pin = w_pin
        self.w_engulf = w_engulf
        w = p.warmup()
        self.warmup = max(w["atr"], w["donch_hi"], 2)

    def compute(self, window: BarWindow) -> DetectorOutput:
        f = window.feats()
        atr, u, lvl_lo = f.atr, f.donch_hi, f.donch_lo
        if atr is None or u is None or lvl_lo is None or len(window) < 2:
            return DetectorOutput(self.name, 0.0, 0.0, {}, self.group, self.weight)
        bar = window.bar(0)
        prev = window.bar(1)
        hi, lo, *_ = _geometry(bar)
        a = max(atr, REL_FLOOR * abs(bar.c)) + _TINY
        pin_b, pin_s = pin_bar_scores(bar, atr)
        eng_b, eng_s = engulfing_scores(bar, prev, atr)
        wp, we = self.w_pin, self.w_engulf
        num = wp * (pin_b * pin_b - pin_s * pin_s) + we * (eng_b * eng_b - eng_s * eng_s)
        den = wp * (pin_b + pin_s) + we * (eng_b + eng_s)
        s = num / den if den > 1e-12 else 0.0
        s = min(1.0, max(-1.0, s)) if math.isfinite(s) else 0.0
        prox_bull = math.exp(-abs(lo - lvl_lo) / (2.0 * a))
        prox_bear = math.exp(-abs(hi - u) / (2.0 * a))
        c = max(pin_b * prox_bull, eng_b * prox_bull, pin_s * prox_bear, eng_s * prox_bear)
        c = min(1.0, max(0.0, c)) if math.isfinite(c) else 0.0
        return DetectorOutput(
            self.name, s, c,
            {"pin_bull": pin_b, "pin_bear": pin_s, "engulf_bull": eng_b, "engulf_bear": eng_s,
             "prox_support": prox_bull, "prox_resistance": prox_bear},
            self.group, self.weight,
        )
