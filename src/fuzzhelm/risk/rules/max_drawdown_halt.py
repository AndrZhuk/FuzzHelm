"""Ліміт MaxDrawdownHalt: DD ≥ 0.12 ⇒ VETO на приріст + сигнал HALT (закрити все, засувка).

Найменування: risk/rules/max_drawdown_halt.py
Автор: Андрій Жук, 2026.

DD_t = 1 − E_t / max_{τ≤t} E_τ (running peak, див. EquityTracker). Сигнал halt піднімається
незалежно від напряму заявки: навіть заявка на закриття повідомляє рушію, що книга має бути пласкою.
"""

from __future__ import annotations

from decimal import Decimal

from fuzzhelm.risk.config import MaxDrawdownHaltCfg
from fuzzhelm.risk.context import RiskContext
from fuzzhelm.risk.verdict import ALLOW, VETO, RuleVerdict


class MaxDrawdownHalt:
    name = "max_drawdown_halt"

    def __init__(self, max_drawdown: Decimal = Decimal("0.12")) -> None:
        if not Decimal(0) < max_drawdown < Decimal(1):
            raise ValueError("max_drawdown must be in (0, 1)")
        self.limit = max_drawdown

    @classmethod
    def from_config(cls, cfg: MaxDrawdownHaltCfg) -> MaxDrawdownHalt:
        return cls(cfg.value)

    def check(self, ctx: RiskContext) -> RuleVerdict:
        breached = ctx.drawdown >= self.limit
        payload: dict[str, object] = {"breached": breached, "increase_qty": ctx.increase_qty}
        verdict = VETO if breached and ctx.increase_qty > 0 else ALLOW
        return RuleVerdict(self.name, verdict, ctx.drawdown, self.limit, payload, halt=breached)
