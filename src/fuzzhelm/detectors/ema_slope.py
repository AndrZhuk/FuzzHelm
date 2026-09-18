"""Детектор нахилу EMA (трендова група A).

Найменування: detectors/ema_slope.py
Призначення: s = tanh(g_t/0.15), g_t = (e_t − e_{t−5})/(5·ATR_t); c = R² OLS-апроксимації e_{t−4..t}
             за часом (брифінг §5.2). Довіра — властивість самих даних: наскільки EMA лінійна.
Автор: Андрій Жук, 2026.
"""

from __future__ import annotations

import math

from fuzzhelm.detectors.base import DetectorGroup, DetectorOutput
from fuzzhelm.features.pipeline import REL_FLOOR, FeatureParams
from fuzzhelm.features.window import BarWindow


class EmaSlope:
    __slots__ = ("group", "horizon", "name", "scale", "warmup", "weight")

    def __init__(self, weight: float = 1.0, *, horizon: int = 5, scale: float = 0.15,
                 params: FeatureParams | None = None) -> None:
        p = params or FeatureParams(ema_horizon=horizon)
        if p.ema_horizon != horizon:
            raise ValueError(f"EmaSlope.horizon={horizon} != FeatureParams.ema_horizon={p.ema_horizon}")
        self.name = "ema_slope"
        self.group = DetectorGroup.TREND
        self.weight = weight
        self.horizon = horizon
        self.scale = scale
        w = p.warmup()
        self.warmup = max(w["ema_lag"], w["ema_r2"], w["atr"])

    def compute(self, window: BarWindow) -> DetectorOutput:
        f = window.feats()
        e, e_lag, atr, r2 = f.ema, f.ema_lag, f.atr, f.ema_r2
        if e is None or e_lag is None or atr is None or r2 is None:
            return DetectorOutput(self.name, 0.0, 0.0, {}, self.group, self.weight)
        den = self.horizon * max(atr, REL_FLOOR * abs(e))
        g = (e - e_lag) / den if den > 0.0 else 0.0
        s = math.tanh(g / self.scale)
        if not math.isfinite(s):
            s = 0.0
        c = r2 if 0.0 <= r2 <= 1.0 else 0.0
        return DetectorOutput(self.name, s, c, {"g": g, "r2": r2}, self.group, self.weight)
