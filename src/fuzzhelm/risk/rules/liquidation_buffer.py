"""Ліміт LiquidationBufferGuard: DTL = |P − P_liq|/ATR ≥ 6; при порушенні — ЗМЕНШИТИ плече, а не відхилити.

Найменування: risk/rules/liquidation_buffer.py
Автор: Андрій Жук, 2026.

P_liq рахується для пост-трейдової позиції |q_tgt| на крос-маржі: власні кошти W = E − N_інших·mmr
(капітал мінус підтримувана маржа інших позицій), вхід — поточна ціна (точна тотожність, див. margin.py).
DTL береться ЗНАКОВИЙ (margin.signed_dtl_atr): для позиції з плечем > 1/mmr ціна ліквідації лежить по
інший бік від P (лонг: P_liq > P), і модуль |P − P_liq| показав би фіктивний запас замість миттєвої
ліквідації; знакова відстань тоді від'ємна ⇒ порушення ⇒ стискання.

Ескалація (§5.10): DTL ≥ 6 → ALLOW; інакше плече зменшується до
    L_cap = min(L'_max, L_DTL),  L'_max = 1/(Δ_stop/(P_e(1−b)) + mmr),  L_DTL — плече, за якого DTL = 6 рівно,
і приріст стискається SHRINK-ом до q_cap = L_cap·W/P. VETO — лише коли навіть без приросту (незмінна
частина позиції) місця немає. Чому мінімум із двох: L'_max гарантує, що стоп спрацює раніше за ліквідацію
із запасом b, але сам по собі не повертає DTL ≥ 6, коли Δ_stop < 6·(1−b)·ATR (у сайзері Δ_stop = 2·ATR),
тобто заявлений ліміт після «ремонту» лишався б порушеним (docs/deviations.d/risk.md).
"""

from __future__ import annotations

from decimal import Decimal

from fuzzhelm.core.enums import Side
from fuzzhelm.risk.config import LiquidationBufferCfg
from fuzzhelm.risk.context import RiskContext, cap_increase
from fuzzhelm.risk.margin import max_qty_for_dtl, reduced_max_leverage, side_liq_price, signed_dtl_atr
from fuzzhelm.risk.verdict import ALLOW, VETO, RuleVerdict


class LiquidationBufferGuard:
    name = "liquidation_buffer"

    def __init__(self, min_dtl_atr: Decimal = Decimal("6.0"), b: Decimal = Decimal("0.20")) -> None:
        if min_dtl_atr <= 0 or not Decimal(0) <= b < Decimal(1):
            raise ValueError("min_dtl_atr must be > 0 and b in [0, 1)")
        self.limit = min_dtl_atr
        self.b = b

    @classmethod
    def from_config(cls, cfg: LiquidationBufferCfg) -> LiquidationBufferGuard:
        return cls(cfg.min_dtl_atr, cfg.b)

    def check(self, ctx: RiskContext) -> RuleVerdict:
        inc = ctx.increase_qty
        payload: dict[str, object] = {"increase_qty": inc, "b": self.b}
        if ctx.post_qty == 0:
            payload["reason"] = "no_position_after_order"
            return RuleVerdict(self.name, ALLOW, None, self.limit, payload)
        side = Side.LONG if ctx.target_side > 0 else Side.SHORT
        wallet = ctx.equity - ctx.gross_notional_other * ctx.mmr
        if wallet <= 0 or ctx.atr <= 0:
            payload["reason"] = "non_positive_wallet" if wallet <= 0 else "atr_unavailable"
            return RuleVerdict(self.name, VETO if inc > 0 else ALLOW, None, self.limit, payload)
        price = ctx.price
        p_liq = side_liq_price(side, ctx.post_qty, price, wallet, ctx.mmr, maint_amount=ctx.maint_amount)
        lev_post = ctx.post_notional / wallet
        payload.update({"liq_price": p_liq, "wallet": wallet, "leverage_post": lev_post})
        if side is Side.LONG and p_liq <= 0:
            payload["reason"] = "liquidation_unreachable"
            return RuleVerdict(self.name, ALLOW, None, self.limit, payload)
        # знакова відстань: P_liq по «неправильний» бік ціни (плече > 1/mmr) — це миттєва ліквідація,
        # а не запас; |P − P_liq| із §5.10 для такої позиції дав би фіктивне DTL ≥ 6 і ALLOW
        dtl = signed_dtl_atr(side, price, p_liq, ctx.atr)
        if dtl >= self.limit:
            return RuleVerdict(self.name, ALLOW, dtl, self.limit, payload)
        # порушення: не відхиляємо, а зменшуємо плече до L_cap = min(L'_max, L_DTL)
        l_red = reduced_max_leverage(ctx.stop_distance, price, ctx.mmr, self.b)
        q_red = l_red * wallet / price
        q_dtl = max_qty_for_dtl(side, wallet=wallet, price=price, atr=ctx.atr, min_dtl=self.limit,
                                mmr=ctx.mmr, maint_amount=ctx.maint_amount)
        q_cap = min(q_red, q_dtl)
        payload.update({
            "reduced_max_leverage": l_red,
            "dtl_max_leverage": q_dtl * price / wallet,
            "q_cap_post": q_cap,
            "leverage_cap": q_cap * price / wallet,
            "cap_binding": "reduced_max_leverage" if q_red <= q_dtl else "dtl_limit",
        })
        return RuleVerdict(self.name, cap_increase(ctx, q_cap), dtl, self.limit, payload)
