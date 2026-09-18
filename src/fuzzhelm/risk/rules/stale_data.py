"""Ліміт StaleDataGuard: лаг даних > 5 с АБО скор якості Q < 0.90 ⇒ VETO на приріст.

Найменування: risk/rules/stale_data.py
Автор: Андрій Жук, 2026.

Лаг = ctx.ts_ns − ctx.last_data_ns (обидві мітки — час подій/ін'єктованого Clock, не настінний).
observed/limit — метрика, що порушена (лаг у секундах або Q); якщо не порушено нічого — лаг.
Скор якості — перший вхід ланцюга ризик-перевірок: на поганих даних рішення не збільшує ризик.
"""

from __future__ import annotations

from decimal import Decimal

from fuzzhelm.risk.config import StaleDataCfg
from fuzzhelm.risk.context import RiskContext, dec_ns_to_s
from fuzzhelm.risk.verdict import ALLOW, VETO, RuleVerdict


class StaleDataGuard:
    name = "stale_data"

    def __init__(self, max_lag_s: Decimal = Decimal("5.0"), min_dq: Decimal = Decimal("0.90")) -> None:
        if max_lag_s <= 0 or not Decimal(0) <= min_dq <= Decimal(1):
            raise ValueError("max_lag_s must be > 0 and min_dq in [0, 1]")
        self.max_lag_s = max_lag_s
        self.min_dq = min_dq

    @classmethod
    def from_config(cls, cfg: StaleDataCfg) -> StaleDataGuard:
        return cls(cfg.max_lag_s, cfg.min_dq)

    def check(self, ctx: RiskContext) -> RuleVerdict:
        lag_s = dec_ns_to_s(ctx.lag_ns)
        lag_bad = lag_s > self.max_lag_s
        dq_bad = ctx.dq_score < self.min_dq
        payload: dict[str, object] = {
            "lag_s": lag_s, "max_lag_s": self.max_lag_s,
            "dq_score": ctx.dq_score, "min_dq": self.min_dq,
            "lag_breached": lag_bad, "dq_breached": dq_bad,
            "increase_qty": ctx.increase_qty,
        }
        if dq_bad and not lag_bad:
            observed, limit = ctx.dq_score, self.min_dq
        else:
            observed, limit = lag_s, self.max_lag_s
        verdict = VETO if (lag_bad or dq_bad) and ctx.increase_qty > 0 else ALLOW
        return RuleVerdict(self.name, verdict, observed, limit, payload)
