"""Детектор пробою каналу Дончіана (трендова група A).

Найменування: detectors/donchian.py
Призначення: s = tanh(d_t/0.5), d_t = (p_t − U*)/ATR_t;
             c = min(1, (U−L)/(6·ATR))·exp(−0.3·bars_since_breakout)   (брифінг §5.2).
Автор: Андрій Жук, 2026.

Уточнення недовизначеного місця брифінгу (див. docs/deviations.d/features.md):
  * U, L — межі каналу ПОПЕРЕДНІХ n барів (без поточного), інакше p_t ≤ h_t ≤ U_t і s ніколи не
    було б додатним;
  * пробій — подія «закриття поза каналом попередніх n барів» (визначення події, не поріг скору);
    U* — рівень, який пробито: U при пробої вгору, L при пробої вниз (дзеркально). Тому d > 0, поки
    ціна тримається над пробитим опором, і d < 0 після пробою підтримки або при хибному пробої;
  * закриття рівно на пробитому рівні дає d = 0, тож новий пробій стартує з s ≈ 0 (без стрибка від стану
    «пробою ще не було»); далі довіра згасає з віком пробою. Якщо ж був давніший пробій, новий пробій
    переносить U* на новий рівень і внесок c·s змінюється стрибком — це наслідок формули d = (p − U)/ATR.
До першого пробою детектор мовчить (s = c = 0).
"""

from __future__ import annotations

import math

from fuzzhelm.detectors.base import DetectorGroup, DetectorOutput
from fuzzhelm.features.pipeline import REL_FLOOR, FeatureParams
from fuzzhelm.features.window import BarWindow


class Donchian:
    __slots__ = ("d_scale", "decay", "group", "name", "warmup", "weight", "width_atr")

    def __init__(self, weight: float = 1.0, *, decay: float = 0.3, d_scale: float = 0.5,
                 width_atr: float = 6.0, params: FeatureParams | None = None) -> None:
        p = params or FeatureParams()
        self.name = "donchian"
        self.group = DetectorGroup.TREND
        self.weight = weight
        self.decay = decay
        self.d_scale = d_scale
        self.width_atr = width_atr
        w = p.warmup()
        self.warmup = max(w["donch_hi"], w["atr"])

    def compute(self, window: BarWindow) -> DetectorOutput:
        f = window.feats()
        u, lo, atr = f.donch_hi, f.donch_lo, f.atr
        ref, age = f.donch_ref, f.bars_since_breakout
        if u is None or lo is None or atr is None or ref is None or age is None:
            return DetectorOutput(self.name, 0.0, 0.0, {}, self.group, self.weight)
        p = window.bar().c
        a = max(atr, REL_FLOOR * abs(p))
        if not a > 0.0:
            return DetectorOutput(self.name, 0.0, 0.0, {}, self.group, self.weight)
        d = (p - ref) / a
        s = math.tanh(d / self.d_scale)
        width = (u - lo) / a
        c = min(1.0, max(0.0, width / self.width_atr)) * math.exp(-self.decay * age)
        if not math.isfinite(s):
            s = 0.0
        if not math.isfinite(c):
            c = 0.0
        return DetectorOutput(self.name, s, c,
                              {"d": d, "width_atr": width, "bars_since_breakout": age,
                               "direction": f.donch_dir or 0.0},
                              self.group, self.weight)
