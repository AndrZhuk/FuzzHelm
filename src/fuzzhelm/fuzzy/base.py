"""Контракт рушія виведення (MamdaniEngine).

Найменування: fuzzy/base.py
Автор: Андрій Жук, 2026.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

import numpy as np
import numpy.typing as npt


@dataclass(frozen=True, slots=True)
class FiredRule:
    rule_id: str
    alpha: float                    # ступінь активації α_r = min(μ_A(T), μ_B(R), μ_C(V)) · (w_r)
    consequent: str                 # терм вихідної змінної U
    antecedent: Mapping[str, str]   # {"T": "STRONG_UP", "R": "NO_PRESSURE", "V": "MID"}


@dataclass(frozen=True, slots=True)
class FuzzyResult:
    u_raw: float                                              # ∈ [−1; 1]; 0.0 при порожній активації
    inputs: Mapping[str, float]                               # {"T": .., "R": .., "V": ..}
    memberships: Mapping[str, Mapping[str, float]]            # {"T": {"STRONG_UP": 0.71, ...}, ...}
    fired: tuple[FiredRule, ...]                              # лише α > 0, за спаданням α, далі за rule_id
    grid: npt.NDArray[np.float64] | None = None               # вузли u_j (для графіка μ_agg)
    mu_agg: npt.NDArray[np.float64] | None = None             # μ_agg(u_j)
    engine: str = "mamdani"
    extras: Mapping[str, float] = field(default_factory=dict)


@runtime_checkable
class InferenceEngine(Protocol):
    name: str

    def infer(self, T: float, R: float, V: float) -> FuzzyResult: ...
