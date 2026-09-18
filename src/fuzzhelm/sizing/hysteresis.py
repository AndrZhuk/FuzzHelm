"""HysteresisGate — тригер Шмітта на наміри ядра: вхід при |u| ≥ 0.25, вихід лише при |u| < 0.12.

Найменування: sizing/hysteresis.py
Призначення: усунути чатеринг позиції біля порогу (§5.9). Ширина петлі enter − exit = 0.13.
Автор: Андрій Жук, 2026.

Автомат на трьох станах {FLAT, LONG, SHORT}:
    FLAT:  u ≥ enter → LONG;  u ≤ −enter → SHORT;  інакше FLAT
    LONG:  u ≤ −enter → SHORT (розворот);  u < exit → FLAT;  інакше LONG
    SHORT: u ≥ enter → LONG (розворот);    u > −exit → FLAT; інакше SHORT
Умова виходу — ЗНАКОВА: лонг тримається, доки сигнал у напрямку позиції не слабший за exit
(u ≥ 0.12); буквальне «|u| < 0.12» тримало б лонг і при u = −0.20, тобто проти сигналу.

Ціна відсутності гістерезису (перевірено в тестах): якщо позиція перевідкривається щобару (закриття +
відкриття = 2 комісії на бар), то 1440 барів/добу × 2 × 0.0004 (taker, config/cost_model.yaml) = 1.152
номіналу, тобто 115.2% (а не 1.15%, як у брифінгу, — помилка одиниць, docs/deviations.d/risk.md) на добу
при номіналі 1×E лише на комісіях; компаратор біля порогу (1 виконання на бар) — 57.6%/добу.
"""

from __future__ import annotations

import math

from fuzzhelm.core.enums import Side

BARS_PER_DAY_1M = 1440


def churn_cost_per_day(fee_rate: float, fills_per_bar: float = 2.0,
                       bars_per_day: int = BARS_PER_DAY_1M) -> float:
    """Частка номіналу, що з'їдається комісіями за добу при fills_per_bar виконаннях на бар."""
    return bars_per_day * fills_per_bar * fee_rate


class HysteresisGate:
    __slots__ = ("_side", "enter", "exit", "flips")

    def __init__(self, enter: float = 0.25, exit: float = 0.12, initial: Side = Side.FLAT) -> None:
        if not 0.0 <= exit <= enter <= 1.0:
            raise ValueError(f"need 0 <= exit <= enter <= 1, got enter={enter}, exit={exit}")
        self.enter = enter
        self.exit = exit
        self._side = initial
        self.flips = 0                     # кількість змін стану (для оцінки обороту)

    @property
    def side(self) -> Side:
        return self._side

    def reset(self, side: Side = Side.FLAT) -> None:
        self._side = side

    def update(self, u_final: float) -> Side:
        # NaN робить усі порівняння хибними, і автомат мовчки тримав би позицію — відмова, як у сайзері
        if not math.isfinite(u_final):
            raise ValueError(f"u_final must be finite, got {u_final!r}")
        s = self._side
        if s is Side.FLAT:
            if u_final >= self.enter:
                s = Side.LONG
            elif u_final <= -self.enter:
                s = Side.SHORT
        elif s is Side.LONG:
            if u_final <= -self.enter:
                s = Side.SHORT
            elif u_final < self.exit:
                s = Side.FLAT
        elif u_final >= self.enter:
            s = Side.LONG
        elif u_final > -self.exit:
            s = Side.FLAT
        if s is not self._side:
            self.flips += 1
            self._side = s
        return s
