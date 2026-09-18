"""Ентропійна узгодженість детекторів і коефіцієнт довіри κ.

Найменування: decision/agreement.py
Призначення: оцінити, наскільки трендові й реверсійні детектори погоджуються щодо напряму, і
перетворити це на множник κ ∈ [κ_min; 1], що гасить вихід нечіткого ядра (брифінг §5.4).
Автор: Андрій Жук, 2026.

    Z  = Σ ω_k c_k                         (k ∈ TREND ∪ REVERSION)
    p₊ = Σ ω_k c_k · max(s_k, 0)  / Z
    p₋ = Σ ω_k c_k · max(−s_k, 0) / Z
    p₀ = Σ ω_k c_k · (1 − |s_k|)  / Z      незадіяна маса свідчень
    H  = −Σ p_i ln p_i / ln 3,  0·ln0 = 0;  A_g = 1 − H;  κ = κ_min + (1 − κ_min)·A_g^ν

Тотожність max(s,0) + max(−s,0) + (1 − |s|) = 1 дає p₊ + p₋ + p₀ ≡ 1, тому нормування на ln 3
(максимум ентропії трьох категорій) коректне.

Регуляризація малої маси свідчень (Z < ε, ε — та сама, що в консенсусі). Формула брифінгу не
визначена при Z = 0 і чисельно нестійка при субнормальних Z. Бракуючу масу ε − Z розподіляємо
рівномірно — апріорний розподіл максимальної ентропії:

    p_i = (Σ ω_k c_k m_ik + (ε − Z)⁺ / 3) / max(ε, Z)

При Z ≥ ε це дослівно формула брифінгу; при Z = 0 — p = (⅓, ⅓, ⅓), H = 1, κ = κ_min
(«немає свідчень ⇒ максимальне гасіння»); на межі Z = ε вираз неперервний.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

from fuzzhelm.decision.aggregator import EPS
from fuzzhelm.detectors.base import DetectorGroup, DetectorOutput

KAPPA_MIN = 0.35
NU = 1.0
_LN3 = math.log(3.0)
_VOTING = (DetectorGroup.TREND, DetectorGroup.REVERSION)


@dataclass(frozen=True, slots=True)
class Agreement:
    p_plus: float
    p_minus: float
    p_zero: float
    H: float            # нормована ентропія ∈ [0; 1]
    A_g: float          # узгодженість = 1 − H
    kappa: float        # ∈ [κ_min; 1]
    mass: float = 0.0   # Z = Σ ω_k c_k (сумарна маса свідчень)
    kappa_min: float = KAPPA_MIN
    nu: float = NU
    eps: float = EPS

    @property
    def has_evidence(self) -> bool:
        """Чи маса свідчень досягла ε (інакше p доповнено рівномірним апріорним розподілом)."""
        return self.mass >= self.eps

    def to_dict(self) -> dict[str, object]:
        return {
            "p_plus": self.p_plus, "p_minus": self.p_minus, "p_zero": self.p_zero,
            "H": self.H, "A_g": self.A_g, "kappa": self.kappa, "mass": self.mass,
            "kappa_min": self.kappa_min, "nu": self.nu, "has_evidence": self.has_evidence,
        }


def validate_params(kappa_min: float, nu: float) -> None:
    if not 0.0 <= kappa_min <= 1.0:
        raise ValueError(f"kappa_min={kappa_min} must lie in [0, 1]")
    if not 0.0 < nu < math.inf:
        raise ValueError(f"nu={nu} must be finite and > 0")


def normalized_entropy(p_plus: float, p_minus: float, p_zero: float) -> float:
    """H = −Σ p ln p / ln 3 з домовленістю 0·ln0 = 0; результат обрізано до [0; 1] від похибки округлення."""
    h = 0.0
    for p in (p_plus, p_minus, p_zero):
        if p > 0.0:
            h -= p * math.log(p)
    h /= _LN3
    return min(1.0, max(0.0, h))


def kappa_from_agreement(a_g: float, kappa_min: float = KAPPA_MIN, nu: float = NU) -> float:
    """κ = κ_min + (1 − κ_min)·A_g^ν — монотонно неспадна за A_g при ν > 0."""
    a = min(1.0, max(0.0, a_g))
    return kappa_min + (1.0 - kappa_min) * math.pow(a, nu)


def agreement(
    outputs: Sequence[DetectorOutput],
    kappa_min: float = KAPPA_MIN,
    nu: float = NU,
    eps: float = EPS,
) -> Agreement:
    """Ентропійна узгодженість лише за групами TREND і REVERSION (CONTEXT не голосує за напрям)."""
    validate_params(kappa_min, nu)
    if not eps > 0.0:
        raise ValueError(f"eps={eps} must be > 0")
    z = 0.0
    n_plus = 0.0
    n_minus = 0.0
    n_zero = 0.0
    for o in outputs:
        if o.group not in _VOTING:
            continue
        w = o.weight
        if not 0.0 <= w < math.inf:
            raise ValueError(f"{o.name}: weight={w} must be finite and >= 0")
        e = w * o.c
        s = o.s
        z += e
        if s > 0.0:
            n_plus += e * s
            n_zero += e * (1.0 - s)
        elif s < 0.0:
            n_minus += e * -s
            n_zero += e * (1.0 + s)
        else:
            n_zero += e
    if z < eps:
        prior = (eps - z) / 3.0
        n_plus += prior
        n_minus += prior
        n_zero += prior
    den = max(eps, z)
    p_plus = n_plus / den
    p_minus = n_minus / den
    p_zero = n_zero / den
    h = normalized_entropy(p_plus, p_minus, p_zero)
    a_g = 1.0 - h
    return Agreement(
        p_plus=p_plus, p_minus=p_minus, p_zero=p_zero, H=h, A_g=a_g,
        kappa=kappa_from_agreement(a_g, kappa_min, nu), mass=z, kappa_min=kappa_min, nu=nu, eps=eps,
    )
