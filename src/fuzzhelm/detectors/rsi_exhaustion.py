"""Детектор виснаження за RSI (реверсійна група B).

Найменування: detectors/rsi_exhaustion.py
Призначення: s = sign(z)·|z|^1.6, z = (50 − RSI)/50;  c = min(1, 0.6·|z|^0.8 + 0.4·div_score)
             (брифінг §5.2). Опукле відображення: біля 50 сигнал слабкий, біля 0/100 — сильний.
Автор: Андрій Жук, 2026.

div_score (у брифінгу не визначено; див. docs/deviations.d/features.md) — неперервна міра
дивергенції ціна↔RSI за k барів без порогових if:
    a = tanh( (c_t − c_{t−k}) / (ATR_t·√k) )     нормований рух ціни (√k — дифузійний масштаб)
    b = tanh( (RSI_t − RSI_{t−k}) / 10 )           рух RSI у «10 пунктах»
    ζ = tanh(z / 0.1)                              гладкий знак очікуваного розвороту
    div_score = max(0, −ζ·a) · max(0, ζ·b)  ∈ [0, 1]
Бичача дивергенція (перепроданість z > 0): ціна вниз (a < 0), RSI вгору (b > 0); ведмежа — дзеркально.
"""

from __future__ import annotations

import math

from fuzzhelm.detectors.base import DetectorGroup, DetectorOutput
from fuzzhelm.features.pipeline import REL_FLOOR, FeatureParams
from fuzzhelm.features.window import BarWindow


def rsi_strength(rsi: float, power: float = 1.6) -> float:
    """s(RSI) = sign(z)·|z|^power, z = (50 − RSI)/50, з обмеженням z ∈ [−1, 1]."""
    z = (50.0 - rsi) / 50.0
    z = min(1.0, max(-1.0, z))
    return math.copysign(abs(z) ** power, z) if z != 0.0 else 0.0


class RsiExhaustion:
    __slots__ = ("base_w", "conf_power", "div_lookback", "div_w", "group", "name", "power",
                 "rsi_scale", "warmup", "weight", "z0")

    def __init__(self, weight: float = 0.8, *, power: float = 1.6, conf_power: float = 0.8,
                 base_w: float = 0.6, div_w: float = 0.4, rsi_scale: float = 10.0, z0: float = 0.1,
                 params: FeatureParams | None = None) -> None:
        p = params or FeatureParams()
        self.name = "rsi_exhaustion"
        self.group = DetectorGroup.REVERSION
        self.weight = weight
        self.power = power
        self.conf_power = conf_power
        self.base_w = base_w
        self.div_w = div_w
        self.rsi_scale = rsi_scale
        self.z0 = z0
        self.div_lookback = p.rsi_div_lookback
        w = p.warmup()
        self.warmup = max(w["rsi_delta"], w["close_delta"], w["atr"])

    def compute(self, window: BarWindow) -> DetectorOutput:
        f = window.feats()
        rsi, d_rsi, d_px, atr = f.rsi, f.rsi_delta, f.close_delta, f.atr
        if rsi is None or d_rsi is None or d_px is None or atr is None or not math.isfinite(rsi):
            return DetectorOutput(self.name, 0.0, 0.0, {}, self.group, self.weight)
        z = min(1.0, max(-1.0, (50.0 - rsi) / 50.0))
        az = abs(z)
        s = math.copysign(az ** self.power, z) if az > 0.0 else 0.0
        a_den = max(atr, REL_FLOOR * abs(window.bar().c)) * math.sqrt(self.div_lookback)
        a = math.tanh(d_px / a_den) if a_den > 0.0 else 0.0
        b = math.tanh(d_rsi / self.rsi_scale)
        zeta = math.tanh(z / self.z0)
        div = max(0.0, -zeta * a) * max(0.0, zeta * b)
        if not math.isfinite(div):
            div = 0.0
        c = min(1.0, self.base_w * az ** self.conf_power + self.div_w * div)
        return DetectorOutput(self.name, s, c, {"rsi": rsi, "z": z, "div_score": div},
                              self.group, self.weight)
