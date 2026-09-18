"""ATR-ризик: розмір позиції, за якого стоп на відстані χ·ATR коштує ρ_base·|u|·E.

Найменування: sizing/atr_risk.py
Автор: Андрій Жук, 2026.

    q_atr = ρ_base·|u_final|·E_t / (χ·ATR_t),   ρ_base = 0.005, χ = 2.0

Грошовий ризик угоди q_atr·χ·ATR = ρ_base·|u|·E не залежить від ATR: розмір обернено пропорційний шуму.
ATR ≤ 0 або не скінченний — «ризик невідомий»: повертається 0 (консервативно, без ділення на нуль).
"""

from __future__ import annotations

import math


def stop_distance(atr: float, chi: float = 2.0) -> float:
    """Δ_stop = χ·ATR — відстань захисного стопа в одиницях ціни."""
    return chi * atr


def q_atr(u_final: float, equity: float, atr: float, rho_base: float = 0.005, chi: float = 2.0) -> float:
    if not (math.isfinite(atr) and atr > 0.0) or equity <= 0.0:
        return 0.0
    if rho_base <= 0.0 or chi <= 0.0:
        raise ValueError("rho_base and chi must be > 0")
    return rho_base * abs(u_final) * equity / (chi * atr)


def risk_money(qty: float, atr: float, chi: float = 2.0) -> float:
    """Грошовий ризик позиції qty зі стопом χ·ATR."""
    return qty * chi * atr
