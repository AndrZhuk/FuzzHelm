"""Єдиний контракт детекторів патернів.

Найменування: detectors/base.py
Автор: Андрій Жук, 2026.

Детектор — чиста функція від вікна барів (BarWindow з LookaheadGuard): однаковий вхід →
однаковий вихід, без побічних ефектів. Вихід: сила s ∈ [−1;1] (напрям) і довіра c ∈ [0;1],
обчислена з властивостей самих даних (R², ширина каналу, bandwidth-перцентиль, ...).
До прогріву детектор повертає s = 0, c = 0 (нульовий внесок у консенсус, а не NaN).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from fuzzhelm.features.window import BarWindow


class DetectorGroup(StrEnum):
    TREND = "trend"          # множина A: EmaSlope, Donchian → T
    REVERSION = "reversion"  # множина B: RsiExhaustion, BollingerZ, CandleGeometry → R
    CONTEXT = "context"      # VolRegime → V (не входить у T/R)


@dataclass(frozen=True, slots=True)
class DetectorOutput:
    name: str
    s: float                                   # [−1; 1]
    c: float                                   # [0; 1]
    features: Mapping[str, float] = field(default_factory=dict)  # діагностика для /explain
    group: DetectorGroup = DetectorGroup.TREND
    weight: float = 1.0                        # ω_k з detectors.yaml

    def __post_init__(self) -> None:
        if not (-1.0 <= self.s <= 1.0) or self.s != self.s:
            raise ValueError(f"{self.name}: s={self.s} outside [-1,1]")
        if not (0.0 <= self.c <= 1.0) or self.c != self.c:
            raise ValueError(f"{self.name}: c={self.c} outside [0,1]")
        # від'ємна чи нескінченна ω зламала б опуклість консенсусу (DEC-04)
        if not (0.0 <= self.weight < float("inf")):
            raise ValueError(f"{self.name}: weight={self.weight} must be finite and >= 0")


@runtime_checkable
class Detector(Protocol):
    name: str
    group: DetectorGroup
    weight: float
    warmup: int                                # скільки барів потрібно до першого ненульового c

    def compute(self, window: BarWindow) -> DetectorOutput: ...
