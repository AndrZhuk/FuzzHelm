"""Детектор режиму волатильності (контекст, змінна V нечіткого ядра).

Найменування: detectors/vol_regime.py
Призначення: s = 0 (напрямку не несе), c = 1.0; вихід — V_t = перцентильний ранг σ_P Паркінсона
             за L = 500 барів у features["V"] (брифінг §5.2, контракт §3).
Автор: Андрій Жук, 2026.
"""

from __future__ import annotations

import math

from fuzzhelm.detectors.base import DetectorGroup, DetectorOutput
from fuzzhelm.features.pipeline import FeatureParams
from fuzzhelm.features.window import BarWindow

# До прогріву рангу V невідоме; повертаємо середину шкали (максимум невизначеності) з c = 0,
# щоб споживач завжди мав ключ "V", а прапорець warm = 0 показував, що це не вимір.
V_UNKNOWN = 0.5


class VolRegime:
    __slots__ = ("group", "name", "warmup", "weight")

    def __init__(self, weight: float = 1.0, *, params: FeatureParams | None = None) -> None:
        p = params or FeatureParams()
        self.name = "vol_regime"
        self.group = DetectorGroup.CONTEXT
        self.weight = weight
        self.warmup = p.warmup()["vol_rank"]

    def compute(self, window: BarWindow) -> DetectorOutput:
        f = window.feats()
        v = f.vol_rank
        if v is None or math.isnan(v):
            return DetectorOutput(self.name, 0.0, 0.0, {"V": V_UNKNOWN, "warm": 0.0},
                                  self.group, self.weight)
        v = min(1.0, max(0.0, v))
        park = f.park_sigma if f.park_sigma is not None else 0.0
        return DetectorOutput(self.name, 0.0, 1.0, {"V": v, "park_sigma": park, "warm": 1.0},
                              self.group, self.weight)
