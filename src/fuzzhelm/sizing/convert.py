"""Точка конвертації №2: float → Decimal (вихід із float-домену в облік).

Найменування: sizing/convert.py
Автор: Андрій Жук, 2026.

Кількість завжди квантується ROUND_DOWN до step_size: округлення ніколи не збільшує ризик.
Ціни (напр. відновлення ціни з float-масиву бектесту) — ROUND_HALF_EVEN до tick_size.
"""

from __future__ import annotations

import math
from decimal import ROUND_DOWN, ROUND_HALF_EVEN, Decimal

from fuzzhelm.core.money import quantize_step


def to_decimal(x: float, step: Decimal, rounding: str = ROUND_DOWN) -> Decimal:
    """Найбільше (для ROUND_DOWN — за модулем не більше) кратне `step` від x. repr(x) — найкоротше
    точне десяткове представлення float, тому 0.1 → Decimal('0.1'), а не 0.1000000000000000055…"""
    if not math.isfinite(x):
        raise ValueError(f"cannot convert non-finite float {x!r} to Decimal")
    return quantize_step(Decimal(repr(x)), step, rounding)


def price_to_decimal(x: float, tick: Decimal) -> Decimal:
    return to_decimal(x, tick, ROUND_HALF_EVEN)


def float_to_decimal_exact(x: float) -> Decimal:
    """Без квантування (для звітних метрик, що зберігаються як Decimal)."""
    if not math.isfinite(x):
        raise ValueError(f"cannot convert non-finite float {x!r} to Decimal")
    return Decimal(repr(x))
