"""Недомінований (Парето) фронт замість одновимірного argmax Sharpe.

Найменування: backtest/pareto.py
Призначення: вибір робочої точки сітки за кількома критеріями одночасно
(SR_OOS ↑, MaxDD_OOS ↓, Turnover ↓ — брифінг §5.16).
Автор: Андрій Жук, 2026.

Точка a домінує b, якщо a не гірша за b за всіма критеріями і строго краща хоча б за одним.
Однакові точки одна одну не домінують (обидві лишаються на фронті). NaN вважається найгіршим
можливим значенням критерію (точка з NaN не може домінувати за цим критерієм).
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Literal

Sense = Literal["max", "min"]
DEFAULT_SENSES: tuple[Sense, Sense, Sense] = ("max", "min", "min")   # (SR, MaxDD, Turnover)


def _oriented(point: Sequence[float], senses: Sequence[Sense]) -> tuple[float, ...]:
    """Звести всі критерії до «більше — краще»; NaN → −inf."""
    out: list[float] = []
    for v, s in zip(point, senses, strict=True):
        if s not in ("max", "min"):
            raise ValueError(f"sense must be 'max' or 'min', got {s!r}")
        x = v + 0.0
        if math.isnan(x):
            out.append(-math.inf)
        else:
            out.append(x if s == "max" else -x)
    return tuple(out)


def dominates(a: Sequence[float], b: Sequence[float], senses: Sequence[Sense] = DEFAULT_SENSES) -> bool:
    oa, ob = _oriented(a, senses), _oriented(b, senses)
    pairs = list(zip(oa, ob, strict=True))
    return all(x >= y for x, y in pairs) and any(x > y for x, y in pairs)


def pareto_front(points: Sequence[Sequence[float]], senses: Sequence[Sense] = DEFAULT_SENSES) -> list[int]:
    """Індекси недомінованих точок у порядку вхідного списку. O(n²·m) — для 108 клітинок достатньо."""
    oriented = [_oriented(p, senses) for p in points]
    front: list[int] = []
    for i, a in enumerate(oriented):
        dominated = False
        for j, b in enumerate(oriented):
            if i != j and all(x >= y for x, y in zip(b, a, strict=True)) and any(
                x > y for x, y in zip(b, a, strict=True)
            ):
                dominated = True
                break
        if not dominated:
            front.append(i)
    return front
