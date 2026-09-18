"""PositionSizer: мінімум трьох обмежень (ATR-ризик, vol-target, плече) × κ_mode, квантування ВНИЗ.

Найменування: sizing/sizer.py
Призначення: перетворити намір ядра u_final ∈ [−1; 1] на цільову кількість (§5.8, кроки 4–8).
Автор: Андрій Жук, 2026.

    q_atr = ρ_base·|u|·E/(χ·ATR)          — грошовий ризик стопу = ρ_base·|u|·E
    q_vt  = s_t·E/p                       — номінал s_t·E (таргетування волатильності)
    q_lev = (E·L_max − N_gross)/p         — залишок бюджету плеча (N_gross — номінал ІНШИХ позицій)
    q     = floor_to_step(κ_mode·min(q_atr, q_vt, q_lev), step_size)
    q·p < min_notional ⇒ відхилити з RejectCode.BELOW_MIN_NOTIONAL (НЕ округляти вгору)

Мінімум трьох — перетин допустимих множин керування; binding_constraint називає вузьке місце
(при рівності — перший у порядку ATR_RISK, VOL_TARGET, LEVERAGE). κ_mode = 0 (HALTED) ⇒ qty = 0 за
будь-якого сигналу (RejectCode.HALTED). Математика — float; Decimal лише на виході через
sizing.convert.to_decimal (ROUND_DOWN), тож округлення ніколи не збільшує ризик.
q — ЦІЛЬОВА позиція за модулем (не приріст), тому N_gross у q_lev — номінал лише інших інструментів.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from fuzzhelm.core.enums import RejectCode, Side
from fuzzhelm.core.money import D0
from fuzzhelm.features.convert import to_float
from fuzzhelm.sizing.atr_risk import q_atr, stop_distance
from fuzzhelm.sizing.convert import to_decimal

if TYPE_CHECKING:
    from fuzzhelm.risk.config import SizingCfg


class BindingConstraint(StrEnum):
    ATR_RISK = "ATR_RISK"
    VOL_TARGET = "VOL_TARGET"
    LEVERAGE = "LEVERAGE"


@dataclass(frozen=True, slots=True)
class SizingParams:
    rho_base: float = 0.005
    chi: float = 2.0
    max_leverage: float = 3.0

    def __post_init__(self) -> None:
        if self.rho_base <= 0 or self.chi <= 0 or self.max_leverage <= 0:
            raise ValueError("rho_base, chi and max_leverage must be > 0")

    @classmethod
    def from_config(cls, cfg: SizingCfg) -> SizingParams:
        return cls(cfg.rho_base, cfg.chi_atr, cfg.max_leverage)


@dataclass(frozen=True, slots=True)
class SizingInput:
    u_final: float              # намір ядра ∈ [−1; 1]
    equity: Decimal             # E_t
    price: Decimal              # p_t (ціна, за якою оцінюється номінал)
    atr: float                  # ATR_t в одиницях ціни (float-домен індикаторів)
    s_t: float                  # згладжений коефіцієнт vol-target (VolTarget.s_t)
    kappa_mode: float           # множник режиму ризику ∈ [0; 1] (RiskStateMachine.kappa_mode_float)
    step_size: Decimal
    min_notional: Decimal
    gross_notional: Decimal = D0   # Σ|номінал| інших позицій
    sigma_ann: float = math.nan    # лише для трасування


@dataclass(frozen=True, slots=True)
class SizingResult:
    qty: Decimal                              # цільова кількість за модулем, кратна step_size (0 — відхилено)
    side: Side
    binding_constraint: BindingConstraint
    q_atr: float
    q_vt: float
    q_lev: float
    s_t: float
    sigma_ann: float
    kappa_mode: float
    reject_code: RejectCode | None
    q_raw: float                              # κ_mode·min(...) до квантування
    notional: Decimal                         # qty·price
    stop_distance: float                      # χ·ATR

    @property
    def signed_qty(self) -> Decimal:
        return self.qty * int(self.side)

    def to_dict(self) -> dict[str, Any]:
        """JSON-сумісна розкладка для DecisionTrace.sizing / ExplainView (float тут допустимі)."""
        return {
            "qty": format(self.qty, "f"), "side": int(self.side),
            "binding_constraint": self.binding_constraint.value,
            "q_atr": self.q_atr, "q_vt": self.q_vt, "q_lev": self.q_lev, "q_raw": self.q_raw,
            "s_t": self.s_t, "sigma_ann": None if math.isnan(self.sigma_ann) else self.sigma_ann,
            "kappa_mode": self.kappa_mode,
            # ATR = NaN на прогріві дає NaN-стоп; у JSON — null, а не невалідний NaN
            "stop_distance": self.stop_distance if math.isfinite(self.stop_distance) else None,
            "notional": format(self.notional, "f"),
            "reject_code": None if self.reject_code is None else self.reject_code.value,
        }


def _side(u: float) -> Side:
    if u > 0.0:
        return Side.LONG
    if u < 0.0:
        return Side.SHORT
    return Side.FLAT


class PositionSizer:
    __slots__ = ("params",)

    def __init__(self, params: SizingParams | None = None) -> None:
        self.params = params or SizingParams()

    def size(self, inp: SizingInput) -> SizingResult:
        prm = self.params
        if not (math.isfinite(inp.u_final) and -1.0 <= inp.u_final <= 1.0):
            raise ValueError(f"u_final must be in [-1, 1], got {inp.u_final}")
        if not 0.0 <= inp.kappa_mode <= 1.0:
            raise ValueError(f"kappa_mode must be in [0, 1], got {inp.kappa_mode}")
        if inp.price <= 0:
            raise ValueError(f"price must be > 0, got {inp.price}")
        eq = to_float(inp.equity)
        p = to_float(inp.price)
        n_gross = to_float(inp.gross_notional)
        qa = q_atr(inp.u_final, eq, inp.atr, prm.rho_base, prm.chi)
        qv = max(0.0, inp.s_t * eq / p) if eq > 0.0 else 0.0
        ql = max(0.0, (eq * prm.max_leverage - n_gross) / p)
        q_min = qa
        binding = BindingConstraint.ATR_RISK
        if qv < q_min:
            q_min, binding = qv, BindingConstraint.VOL_TARGET
        if ql < q_min:
            q_min, binding = ql, BindingConstraint.LEVERAGE
        q_raw = inp.kappa_mode * q_min
        side = _side(inp.u_final)
        stop = stop_distance(inp.atr, prm.chi)

        reject: RejectCode | None = None
        qty = D0
        if inp.kappa_mode == 0.0:
            reject = RejectCode.HALTED                    # HALTED: нуль за будь-якого сигналу
        elif side is not Side.FLAT:
            qty = to_decimal(q_raw, inp.step_size)        # ROUND_DOWN: ніколи не вгору
            if qty <= 0 or qty * inp.price < inp.min_notional:
                reject = RejectCode.BELOW_MIN_NOTIONAL if inp.min_notional > 0 else RejectCode.ZERO_QTY
                qty = D0
        return SizingResult(
            qty=qty, side=side, binding_constraint=binding, q_atr=qa, q_vt=qv, q_lev=ql,
            s_t=inp.s_t, sigma_ann=inp.sigma_ann, kappa_mode=inp.kappa_mode, reject_code=reject,
            q_raw=q_raw, notional=qty * inp.price, stop_distance=stop,
        )
