"""Тест Купця (POF — proportion of failures) для валідації моделі VaR.

Найменування: risk/kupiec.py
Автор: Андрій Жук, 2026.

    LR = −2 ln[ ((1−p)^{W−x} · p^x) / ((1−x/W)^{W−x} · (x/W)^x) ],   p = 0.05
    LR > χ²₁(0.95) ⇒ модель VaR відкидається (частота пробоїв статистично відрізняється від p).

Межі: x = 0 і x = W — члени виду 0·ln 0 дорівнюють 0 (границя t·ln t → 0), тому
    x = 0:  LR = −2·W·ln(1 − p);   x = W:  LR = −2·W·ln p.
Критичне значення χ²₁(0.95) = z²_{0.975} = 3.8414588… (у брифінгу округлено до 3.841); обчислюється
через statistics.NormalDist, а не вписується константою.
LR — логарифм відношення правдоподібностей з ОМП у знаменнику, тому LR ≥ 0 математично; від'ємні
значення порядку −1e−15 — похибка float, обрізаються до 0.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from statistics import NormalDist

CHI2_1_95: float = NormalDist().inv_cdf(0.975) ** 2


def _xlogy(x: float, y: float) -> float:
    """x·ln y з угодою 0·ln 0 = 0."""
    return 0.0 if x == 0 else x * math.log(y)


@dataclass(frozen=True, slots=True)
class KupiecResult:
    lr: float
    reject: bool
    breaches: int
    n: int
    p: float
    p_hat: float
    critical: float


def kupiec_pof(breaches: int, W: int, p: float = 0.05, critical: float = CHI2_1_95) -> KupiecResult:
    """LR-статистика Купця для x = breaches пробоїв на W спостереженнях при номінальній частоті p."""
    if W <= 0:
        raise ValueError(f"W must be > 0, got {W}")
    if not 0 <= breaches <= W:
        raise ValueError(f"breaches must be in [0, W], got {breaches} of {W}")
    if not 0.0 < p < 1.0:
        raise ValueError(f"p must be in (0, 1), got {p}")
    x = breaches
    p_hat = x / W
    log_l0 = _xlogy(W - x, 1.0 - p) + _xlogy(x, p)
    log_l1 = _xlogy(W - x, 1.0 - p_hat) + _xlogy(x, p_hat)
    lr = max(0.0, -2.0 * (log_l0 - log_l1))
    return KupiecResult(lr=lr, reject=lr > critical, breaches=x, n=W, p=p, p_hat=p_hat, critical=critical)


def acceptance_region(W: int, p: float = 0.05, critical: float = CHI2_1_95) -> tuple[int, int]:
    """[x_lo, x_hi] — діапазон кількостей пробоїв, за яких модель НЕ відкидається."""
    ok = [x for x in range(W + 1) if not kupiec_pof(x, W, p, critical).reject]
    if not ok:
        raise ValueError("empty acceptance region")
    return ok[0], ok[-1]
