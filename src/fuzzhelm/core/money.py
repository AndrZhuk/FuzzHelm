"""Decimal-дисципліна: контекст, квантування, гроші.

Найменування: core/money.py
Призначення: усе, що входить у хеш стану та в БД (NUMERIC(38,18)), — лише Decimal.
Автор: Андрій Жук, 2026.

Правила:
  * ціни квантуються до tick_size з ROUND_HALF_EVEN (банківське округлення, без систематичного зсуву);
  * кількості — до step_size з ROUND_DOWN (ніколи не округляти вгору: ризик не може зрости від округлення);
  * серіалізація — лише format(d, "f") (без експоненти: Decimal('1E+2') → '100', після quantize_money → '100.00').
"""

from __future__ import annotations

import decimal
from decimal import ROUND_DOWN, ROUND_HALF_EVEN, Context, Decimal

D0 = Decimal(0)
D1 = Decimal(1)
QUANTUM_MONEY = Decimal("0.01")          # звітні суми в USDT
QUANTUM_INTERNAL = Decimal("1E-18")      # внутрішній облік = масштаб NUMERIC(38,18)

DECIMAL_CONTEXT = Context(
    prec=38,
    rounding=ROUND_HALF_EVEN,
    traps=[decimal.InvalidOperation, decimal.DivisionByZero, decimal.Overflow],
)


def setup_decimal_context() -> None:
    """Виставити контекст Decimal у поточному потоці/процесі (викликається initializer-ом воркерів)."""
    decimal.setcontext(DECIMAL_CONTEXT.copy())


def quantize_step(x: Decimal, step: Decimal, rounding: str = ROUND_DOWN) -> Decimal:
    """Кратне `step`, округлене за `rounding`. Працює для будь-якого кроку (0.001, 0.10, 0.5 ...)."""
    if step <= 0:
        raise ValueError(f"step must be > 0, got {step}")
    n = (x / step).to_integral_value(rounding=rounding)
    return (n * step).quantize(step)


def quantize_price(x: Decimal, tick: Decimal) -> Decimal:
    return quantize_step(x, tick, ROUND_HALF_EVEN)


def floor_qty(x: Decimal, step: Decimal) -> Decimal:
    return quantize_step(x, step, ROUND_DOWN)


def quantize_money(x: Decimal) -> Decimal:
    return x.quantize(QUANTUM_MONEY, rounding=ROUND_HALF_EVEN)


def quantize_internal(x: Decimal) -> Decimal:
    return x.quantize(QUANTUM_INTERNAL, rounding=ROUND_HALF_EVEN)


def dec_str(x: Decimal) -> str:
    """Канонічний рядок Decimal без експоненти."""
    return format(x, "f")


def dec(x: int | str | Decimal) -> Decimal:
    """Єдиний дозволений конструктор Decimal з рантайм-значення поза sizing/convert.py.
    float відкидається: float → Decimal лише через sizing.convert.to_decimal (з квантуванням)."""
    if isinstance(x, bool) or isinstance(x, float):
        raise TypeError(f"dec() refuses {type(x).__name__}; use sizing.convert.to_decimal for floats")
    if isinstance(x, Decimal):
        return x
    if isinstance(x, int | str):
        return Decimal(x)
    raise TypeError(f"dec() cannot convert {type(x).__name__}")
