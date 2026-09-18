"""Квантування до сітки біржі та перевірки фільтрів інструмента (tick / step / minNotional).

Найменування: ingest/quantize.py
Призначення: ціна → кратне tick_size (ROUND_HALF_EVEN), кількість → кратне step_size (ROUND_DOWN),
відмова з кодом RejectCode.BELOW_MIN_NOTIONAL, якщо qty·price < minNotional.
Автор: Андрій Жук, 2026.

Саме округлення делеговано core.money (одне джерело правил); тут — предикати та семантика відмов.
Чому HALF_EVEN для ціни: банківське округлення не має систематичного зсуву на половинках тику.
Чому DOWN для кількості: округлення ніколи не збільшує позицію, тобто ризик.
Межа minNotional включна (Binance: «notional must be no smaller than minNotional»).
"""

from __future__ import annotations

from decimal import Decimal

from fuzzhelm.core.dto import Instrument
from fuzzhelm.core.enums import RejectCode
from fuzzhelm.core.money import D0, floor_qty, quantize_price


def quantize_to_tick(price: Decimal, tick: Decimal) -> Decimal:
    return quantize_price(price, tick)


def floor_to_step(qty: Decimal, step: Decimal) -> Decimal:
    return floor_qty(qty, step)


def _is_multiple(x: Decimal, step: Decimal) -> bool:
    if step <= 0:
        raise ValueError(f"step must be > 0, got {step}")
    q = x / step
    return q == q.to_integral_value()


def check_tick(price: Decimal, tick: Decimal) -> bool:
    """True, якщо ціна лежить точно на сітці tick_size."""
    return _is_multiple(price, tick)


def check_step(qty: Decimal, step: Decimal) -> bool:
    """True, якщо кількість лежить точно на сітці step_size."""
    return _is_multiple(qty, step)


def notional(qty: Decimal, price: Decimal) -> Decimal:
    return qty * price


def reject_below_min_notional(qty: Decimal, price: Decimal, instrument: Instrument) -> RejectCode | None:
    """RejectCode.BELOW_MIN_NOTIONAL, якщо |qty|·price < min_notional; інакше None."""
    if abs(qty) * price < instrument.min_notional:
        return RejectCode.BELOW_MIN_NOTIONAL
    return None


def prepare_order_qty(qty_raw: Decimal, price: Decimal,
                      instrument: Instrument) -> tuple[Decimal, RejectCode | None]:
    """Повний шлях кількості до ордера: floor до step → ZERO_QTY → BELOW_MIN_NOTIONAL.

    qty_raw — модуль кількості (напрям задає Side ордера). Від'ємне значення — помилка викликача, а не
    «нульова кількість»: ROUND_DOWN округлив би −5 до −5 і відмова ZERO_QTY приховала б хибний знак.
    """
    if qty_raw < D0:
        raise ValueError(f"qty_raw must be >= 0 (direction is the order side), got {qty_raw}")
    qty = floor_to_step(qty_raw, instrument.step_size)
    if qty <= D0:
        return qty, RejectCode.ZERO_QTY
    return qty, reject_below_min_notional(qty, price, instrument)
