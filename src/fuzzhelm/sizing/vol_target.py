"""Таргетування волатильності: EWMA-оцінка σ, коефіцієнт s* і фільтр 1-го порядку (gain scheduling).

Найменування: sizing/vol_target.py
Призначення: тримати реалізовану волатильність позиції на завданні σ_target, масштабуючи номінал
множником s_t = згладжене σ_target/σ̂ (§5.8, кроки 1–3). Математика — у float-домені.
Автор: Андрій Жук, 2026.

    σ²_t = λ·σ²_{t−1} + (1−λ)·r²_t,   λ = 0.94;     σ_ann = σ_t·√A,  A = 525 600 (1m, 24/7)
    s*_t = clip(σ_target / σ_ann, s_min, s_max) = clip(·, 0.25, 3.0)
    s_t  = s_{t−1} + γ·(s*_t − s_{t−1}),  γ = 0.2

Стала часу фільтра. Перехідна характеристика дискретного фільтра на сходинку: 1 − (1−γ)^n, тобто
59.0% після 4 барів і 67.2% після 5; рівня 1 − e^{−1} = 63.2% вона досягає при
n = T/Δt = −1/ln(1−γ) ≈ 4.48 (точний еквівалент полюса e^{−Δt/T} = 1−γ). Число «T = 4Δt» з брифінгу —
стала часу неперервного прототипу при дискретизації зворотним Ейлером, γ = Δt/(T+Δt) ⇒ T = (1−γ)/γ·Δt;
прямий Ейлер дав би T = Δt/γ = 5Δt (docs/deviations.d/risk.md).

Ініціалізація: σ²_0 = r²_1 (перше спостереження), якщо не задано sigma2_0; s_0 = s_min (консервативний
старт — експозиція нарощується через фільтр, а не стрибком). σ_ann = 0 ⇒ s* = s_max (межа clip від +∞).
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fuzzhelm.risk.config import SizingCfg

MINUTES_PER_YEAR_24_7 = 525_600


def filter_step_response(gamma: float, n: int) -> float:
    """Частка сходинки, пройдена фільтром за n барів: 1 − (1−γ)^n."""
    return 1.0 - (1.0 - gamma) ** n


def filter_time_constant_bars(gamma: float) -> float:
    """Точна стала часу дискретного фільтра в барах: T/Δt = −1 / ln(1−γ) (≈ 4.4814 при γ = 0.2)."""
    if not 0.0 < gamma < 1.0:
        raise ValueError("gamma must be in (0, 1) for a finite time constant")
    return -1.0 / math.log(1.0 - gamma)


def backward_euler_time_constant_bars(gamma: float) -> float:
    """Стала часу неперервного прототипу при дискретизації зворотним Ейлером: (1−γ)/γ (= 4 при γ = 0.2)."""
    return (1.0 - gamma) / gamma


class VolTarget:
    """Стан EWMA-волатильності і згладженого коефіцієнта s_t. update(r) — O(1) на бар."""

    __slots__ = ("A", "_n", "_s", "_s_star", "_sigma2", "_sqrt_a", "gamma", "lam", "s_max", "s_min",
                 "sigma_target")

    def __init__(self, *, lam: float = 0.94, A: int = MINUTES_PER_YEAR_24_7, sigma_target: float = 0.20,
                 s_min: float = 0.25, s_max: float = 3.0, gamma: float = 0.2,
                 s0: float | None = None, sigma2_0: float | None = None) -> None:
        if not 0.0 < lam < 1.0:
            raise ValueError("lam must be in (0, 1)")
        if A <= 0 or sigma_target <= 0:
            raise ValueError("A and sigma_target must be > 0")
        if not 0.0 < s_min <= s_max:
            raise ValueError("need 0 < s_min <= s_max")
        if not 0.0 < gamma <= 1.0:
            raise ValueError("gamma must be in (0, 1]")
        if sigma2_0 is not None and not (math.isfinite(sigma2_0) and sigma2_0 >= 0.0):
            raise ValueError("sigma2_0 must be a finite non-negative number")
        # s_t — опукла комбінація s_0 і значень s* ∈ [s_min; s_max]: межі тримаються, лише якщо s_0 теж там
        if s0 is not None and not s_min <= s0 <= s_max:
            raise ValueError(f"s0 must lie in [s_min, s_max] = [{s_min}, {s_max}], got {s0!r}")
        self.lam = lam
        self.A = A
        self.sigma_target = sigma_target
        self.s_min = s_min
        self.s_max = s_max
        self.gamma = gamma
        self._sqrt_a = math.sqrt(A)
        self._sigma2: float | None = sigma2_0
        self._s = s_min if s0 is None else s0
        self._s_star = self._s
        self._n = 0

    @classmethod
    def from_config(cls, cfg: SizingCfg) -> VolTarget:
        return cls(lam=cfg.ewma_lambda, A=cfg.annualization_bars, sigma_target=cfg.sigma_target,
                   s_min=cfg.scale_min, s_max=cfg.scale_max, gamma=cfg.filter_gamma)

    @property
    def sigma2(self) -> float | None:
        return self._sigma2

    @property
    def sigma_ann(self) -> float:
        return math.nan if self._sigma2 is None else math.sqrt(self._sigma2) * self._sqrt_a

    @property
    def s_star(self) -> float:
        return self._s_star

    @property
    def s_t(self) -> float:
        return self._s

    @property
    def n(self) -> int:
        return self._n

    def target_scale(self, sigma_ann: float) -> float:
        """s* = clip(σ_target/σ_ann, s_min, s_max)."""
        if sigma_ann <= 0.0:
            return self.s_max
        return min(self.s_max, max(self.s_min, self.sigma_target / sigma_ann))

    def update(self, r: float) -> tuple[float, float, float]:
        """Дохідність бару r → (σ_ann, s*_t, s_t)."""
        if not math.isfinite(r):
            raise ValueError(f"return must be finite, got {r!r}")
        r2 = r * r
        s2 = r2 if self._sigma2 is None else self.lam * self._sigma2 + (1.0 - self.lam) * r2
        self._sigma2 = s2
        sigma_ann = math.sqrt(s2) * self._sqrt_a
        s_star = self.target_scale(sigma_ann)
        self._s = self._s + self.gamma * (s_star - self._s)
        self._s_star = s_star
        self._n += 1
        return sigma_ann, s_star, self._s
