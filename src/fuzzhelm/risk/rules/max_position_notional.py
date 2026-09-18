"""Ліміт MaxPositionNotional: початкова маржа позиції ≤ 0.30 капіталу (дія — SHRINK).

Найменування: risk/rules/max_position_notional.py
Автор: Андрій Жук, 2026.

Інтерпретація `value: 0.30, unit: equity_fraction` (зафіксовано в docs/deviations.d/risk.md):
частка капіталу, яку позиція ЗАЙМАЄ як початкову маржу, IM = |q|·P / L_set ≤ 0.30·E, тобто
номінал ≤ 0.30·E·L_set (L_set — біржове плече позиції). Чому не «номінал ≤ 0.30·E»: тоді
одна позиція не могла б перевищити 0.3× капіталу, і ліміт MaxGrossLeverage = 3.0 став би
недосяжним декором, а обмеження vol-target сайзера (номінал s_t·E, s_t ∈ [0.25; 3]) — перекритим щоразу,
коли s_t > 0.3, тобто при σ_ann < σ_target/0.3. Маржинальне прочитання лишає інші ліміти змістовними.
Застереження: межею найгіршого збитку позиції (втрата її IM) це було б лише за ІЗОЛЬОВАНОЇ маржі; ризик-модель
цього пакета — крос-маржа (LiquidationBufferGuard бере W = E), тож тут це межа частки капіталу, зарезервованої
під позицію, а не межа збитку: збиток до стопу обмежує ρ_base сайзера, запас до ліквідації — DTL ≥ 6.
"""

from __future__ import annotations

from decimal import Decimal

from fuzzhelm.risk.config import MaxPositionNotionalCfg
from fuzzhelm.risk.context import RiskContext, cap_increase, ratio
from fuzzhelm.risk.verdict import ALLOW, VETO, RuleVerdict


class MaxPositionNotional:
    name = "max_position_notional"

    def __init__(self, max_margin_fraction: Decimal = Decimal("0.30")) -> None:
        if not Decimal(0) < max_margin_fraction <= Decimal(1):
            raise ValueError("max_margin_fraction must be in (0, 1]")
        self.limit = max_margin_fraction

    @classmethod
    def from_config(cls, cfg: MaxPositionNotionalCfg) -> MaxPositionNotional:
        return cls(cfg.value)

    def check(self, ctx: RiskContext) -> RuleVerdict:
        margin_post = ctx.post_notional / ctx.leverage_setting
        observed = ratio(margin_post, ctx.equity)
        payload: dict[str, object] = {
            "interpretation": "initial_margin_over_equity",
            "margin_post": margin_post,
            "leverage_setting": ctx.leverage_setting,
            "increase_qty": ctx.increase_qty,
        }
        if ctx.equity <= 0:
            payload["reason"] = "non_positive_equity"
            verdict = VETO if ctx.increase_qty > 0 else ALLOW
            return RuleVerdict(self.name, verdict, observed, self.limit, payload)
        q_max = self.limit * ctx.equity * ctx.leverage_setting / ctx.price
        payload["q_max_post"] = q_max
        return RuleVerdict(self.name, cap_increase(ctx, q_max), observed, self.limit, payload)
