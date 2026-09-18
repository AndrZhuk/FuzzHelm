"""Ліміт MaxDailyLoss: PnL_day ≤ −0.02·E_open ⇒ VETO на приріст; скидання о UTC-північ.

Найменування: risk/rules/max_daily_loss.py
Автор: Андрій Жук, 2026.

observed = PnL_day / E_open, limit = −0.02 (обидва — частки капіталу, як у журналі демо:
«MaxDailyLoss · VETO · observed=−2.10% · limit=−2.00%»). Доба визначається ЛИШЕ часом події
(ctx.ts_ns від ін'єктованого Clock або часу бару): якщо PnL у контексті належить попередній
UTC-добі (ctx.day_start_ns ≠ доба ts_ns), він вважається вже скинутим — правило не читає настінний
годинник і не покладається на те, що хтось встиг оновити трекер.
"""

from __future__ import annotations

from decimal import Decimal

from fuzzhelm.core.clock import utc_day_start_ns
from fuzzhelm.core.money import D0
from fuzzhelm.risk.config import MaxDailyLossCfg
from fuzzhelm.risk.context import RiskContext, ratio
from fuzzhelm.risk.verdict import ALLOW, VETO, RuleVerdict


class MaxDailyLoss:
    name = "max_daily_loss"

    def __init__(self, max_loss_fraction: Decimal = Decimal("0.02")) -> None:
        if not Decimal(0) < max_loss_fraction < Decimal(1):
            raise ValueError("max_loss_fraction must be in (0, 1)")
        self.limit = -max_loss_fraction

    @classmethod
    def from_config(cls, cfg: MaxDailyLossCfg) -> MaxDailyLoss:
        return cls(cfg.value)

    def check(self, ctx: RiskContext) -> RuleVerdict:
        day = utc_day_start_ns(ctx.ts_ns)
        stale_day = ctx.day_start_ns is not None and ctx.day_start_ns != day
        pnl_day = D0 if stale_day else ctx.pnl_day
        e_open = ctx.equity if stale_day else ctx.effective_day_open
        observed = ratio(pnl_day, e_open)
        breached = observed is None or observed <= self.limit
        payload: dict[str, object] = {
            "pnl_day": pnl_day,
            "equity_day_open": e_open,
            "day_start_ns": day,
            "reset_applied": stale_day,
            "breached": breached,
            "increase_qty": ctx.increase_qty,
        }
        verdict = VETO if breached and ctx.increase_qty > 0 else ALLOW
        return RuleVerdict(self.name, verdict, observed, self.limit, payload)
