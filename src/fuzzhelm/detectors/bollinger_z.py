"""Детектор відхилення від смуги Боллінджера (реверсійна група B).

Найменування: detectors/bollinger_z.py
Призначення: s = −tanh(z_t/2.0), z = (p − SMA₂₀)/σ₂₀;  c = 1 − ρ_t, ρ — перцентиль bandwidth
             за 200 барів (брифінг §5.2): коли смуга розширюється (ρ → 1), реверсії не довіряємо.
Автор: Андрій Жук, 2026.
"""

from __future__ import annotations

import math

from fuzzhelm.detectors.base import DetectorGroup, DetectorOutput
from fuzzhelm.features.pipeline import REL_FLOOR, FeatureParams
from fuzzhelm.features.window import BarWindow


class BollingerZ:
    __slots__ = ("group", "name", "warmup", "weight", "z_scale")

    def __init__(self, weight: float = 0.8, *, z_scale: float = 2.0,
                 params: FeatureParams | None = None) -> None:
        p = params or FeatureParams()
        self.name = "bollinger_z"
        self.group = DetectorGroup.REVERSION
        self.weight = weight
        self.z_scale = z_scale
        self.warmup = p.warmup()["bb_bw_rank"]

    def compute(self, window: BarWindow) -> DetectorOutput:
        f = window.feats()
        mu, sigma, rho = f.sma, f.sigma, f.bb_bw_rank
        if mu is None or sigma is None or rho is None:
            return DetectorOutput(self.name, 0.0, 0.0, {}, self.group, self.weight)
        p = window.bar().c
        # σ на рівні шуму округлення (пласке вікно) — не ділимо шум на шум
        sd = max(sigma, REL_FLOOR * abs(mu))
        z = (p - mu) / sd if sd > 0.0 else 0.0
        s = -math.tanh(z / self.z_scale)
        if not math.isfinite(s):
            s = 0.0
        c = 1.0 - min(1.0, max(0.0, rho))
        return DetectorOutput(self.name, s, c, {"z": z, "bw_rank": rho}, self.group, self.weight)
