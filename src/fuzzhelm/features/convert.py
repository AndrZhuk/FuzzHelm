"""Точка конвертації №1: Decimal → float (вхід у float-домен індикаторів і нечіткого ядра).

Найменування: features/convert.py
Автор: Андрій Жук, 2026.

Межа типів: Decimal — ціни/обсяги/гроші; float — індикатори, нечітке ядро, статистика.
Жоден інший модуль (крім sizing/convert.py) не викликає float(...) чи Decimal(...) з
нелітеральним аргументом — перевіряє tests/arch/test_decimal_float_boundary.py.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from fuzzhelm.core.dto import Candle


def to_float(value: Decimal, scale: Decimal | None = None) -> float:
    """float(value / scale); scale=None — без масштабування."""
    if scale is not None:
        value = value / scale
    return float(value)


def to_float_opt(value: Decimal | None) -> float | None:
    return None if value is None else float(value)


@dataclass(frozen=True, slots=True)
class Bar:
    """Float-двійник закритої свічки для конвеєра ознак (гарячий шлях: slots, без валідації)."""

    t_ns: int          # open_time_ns
    o: float
    h: float
    l: float
    c: float
    v: float           # обсяг у базовому активі
    qv: float = 0.0    # обсяг у котирувальному активі
    n: int = 0         # кількість угод


def bar_from_candle(c: Candle) -> Bar:
    return Bar(
        t_ns=c.open_time_ns,
        o=float(c.o), h=float(c.h), l=float(c.l), c=float(c.c),
        v=float(c.volume), qv=float(c.quote_volume), n=c.trades_count,
    )
