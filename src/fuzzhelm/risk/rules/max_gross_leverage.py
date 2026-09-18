"""Ліміт MaxGrossLeverage: Σ|номінал| / E ≤ 3.0 після заявки (дія — SHRINK).

Найменування: risk/rules/max_gross_leverage.py
Автор: Андрій Жук, 2026.

gross_post = N_інших + |q_post|·P;  q_max = (L_max·E − N_інших)/P. Якщо місця для приросту немає
зовсім, SHRINK(f → 0) вироджується у VETO (f ∈ (0; 1) строго).
"""

from __future__ import annotations

from decimal import Decimal

from fuzzhelm.risk.config import MaxGrossLeverageCfg
from fuzzhelm.risk.context import RiskContext, cap_increase, ratio
from fuzzhelm.risk.verdict import ALLOW, VETO, RuleVerdict


class MaxGrossLeverage:
    name = "max_gross_leverage"

    def __init__(self, max_leverage: Decimal = Decimal("3.0")) -> None:
        if max_leverage <= 0:
            raise ValueError("max_leverage must be > 0")
        self.limit = max_leverage

    @classmethod
    def from_config(cls, cfg: MaxGrossLeverageCfg) -> MaxGrossLeverage:
        return cls(cfg.value)

    def check(self, ctx: RiskContext) -> RuleVerdict:
        gross_post = ctx.gross_notional_other + ctx.post_notional
        observed = ratio(gross_post, ctx.equity)
        payload: dict[str, object] = {
            "gross_notional_post": gross_post,
            "gross_notional_other": ctx.gross_notional_other,
            "increase_qty": ctx.increase_qty,
        }
        if ctx.equity <= 0:
            payload["reason"] = "non_positive_equity"
            verdict = VETO if ctx.increase_qty > 0 else ALLOW
            return RuleVerdict(self.name, verdict, observed, self.limit, payload)
        q_max = (self.limit * ctx.equity - ctx.gross_notional_other) / ctx.price
        payload["q_max_post"] = q_max
        return RuleVerdict(self.name, cap_increase(ctx, q_max), observed, self.limit, payload)
