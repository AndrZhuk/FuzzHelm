"""Сітка параметрів Θ = n_ATR × χ × u_enter × ρ_base × λ (3·3·3·2·2 = 108 конфігурацій).

Найменування: backtest/grid.py
Призначення: детермінований перелік клітинок для паралельного grid-пошуку (брифінг §5.16).
Автор: Андрій Жук, 2026.

Значення — симетрично навколо дефолтів брифінгу/конфігурації:
  n_atr    — період ATR Уайлдера (дефолт 14, §5.1): {7, 14, 21};
  chi      — множник ATR у ризику на одиницю q_atr = ρ·|u|·E/(χ·ATR) (дефолт 2.0, §5.8): {1.5, 2.0, 2.5};
  u_enter  — поріг входу тригера Шмітта (дефолт 0.25, §5.9; вихід 0.12 < усіх значень): {0.20, 0.25, 0.30};
  rho_base — базовий ризик на угоду (дефолт 0.005): {0.0025, 0.005};
  lam      — λ EWMA-волатильності vol-target (дефолт 0.94 — RiskMetrics для денних даних;
             0.97 — RiskMetrics для місячних): {0.94, 0.97}.
Порядок — лексикографічний добуток осей у порядку ключів (остання вісь змінюється найшвидше).
"""

from __future__ import annotations

import itertools
from collections.abc import Mapping, Sequence
from typing import Any

from fuzzhelm.config import load_yaml

DEFAULT_SPACE: dict[str, tuple[Any, ...]] = {
    "n_atr": (7, 14, 21),
    "chi": (1.5, 2.0, 2.5),
    "u_enter": (0.20, 0.25, 0.30),
    "rho_base": (0.0025, 0.005),
    "lam": (0.94, 0.97),
}


def make_grid(space: Mapping[str, Sequence[Any]] | None = None) -> list[dict[str, Any]]:
    """Декартів добуток осей (порядок осей = порядок ключів). Дефолт — 108 клітинок."""
    sp = DEFAULT_SPACE if space is None else space
    if not sp:
        raise ValueError("grid space is empty")
    keys = list(sp)
    axes = [tuple(sp[k]) for k in keys]
    for k, ax in zip(keys, axes, strict=True):
        if not ax:
            raise ValueError(f"grid axis {k!r} is empty")
        if len(set(map(repr, ax))) != len(ax):
            raise ValueError(f"grid axis {k!r} has duplicate values")
    return [dict(zip(keys, combo, strict=True)) for combo in itertools.product(*axes)]


def load_grid_space(profile: Mapping[str, Any] | None = None) -> dict[str, tuple[Any, ...]]:
    """Простір із config/profiles/grid.yaml (ключ `space`), якщо він там є; інакше DEFAULT_SPACE."""
    prof = profile if profile is not None else load_yaml("profiles/grid")
    space = prof.get("space")
    if not space:
        return dict(DEFAULT_SPACE)
    return {str(k): tuple(v) for k, v in space.items()}
