"""Property-тести ризик-контуру: алгебра вердиктів (§5.11), ланцюг лімітів, CVaR ≥ VaR (§5.13).

Найменування: tests/property/test_risk_property.py
Автор: Андрій Жук, 2026.
"""

from __future__ import annotations

from decimal import Decimal
from fractions import Fraction

import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from fuzzhelm.core.enums import Side, VerdictKind
from fuzzhelm.core.money import setup_decimal_context
from fuzzhelm.risk.config import load_risk_config
from fuzzhelm.risk.context import RiskContext
from fuzzhelm.risk.guard import RiskGuard, default_rules
from fuzzhelm.risk.journal import RiskJournal
from fuzzhelm.risk.margin import (
    dtl_atr,
    max_qty_for_dtl,
    reduced_max_leverage,
    side_liq_price,
    signed_dtl_atr,
)
from fuzzhelm.risk.rules.liquidation_buffer import LiquidationBufferGuard
from fuzzhelm.risk.var import historical_var_cvar, tail_count
from fuzzhelm.risk.verdict import ALLOW, VETO, Verdict, compose, exposure, shrink

pytestmark = pytest.mark.property

D = Decimal

factors_dec = st.decimals(min_value=D("0.0001"), max_value=D("0.9999"), places=4).map(shrink)
factors_frac = st.fractions(min_value=Fraction(1, 10**9), max_value=1 - Fraction(1, 10**9),
                            max_denominator=10**9).filter(lambda f: 0 < f < 1).map(shrink)
verdicts = st.one_of(st.just(ALLOW), st.just(VETO), factors_dec, factors_frac)
chains = st.lists(verdicts, max_size=20)
requested = st.decimals(min_value=D(0), max_value=D("1e9"), places=8)


@settings(max_examples=300)
@given(chain=chains, extra=verdicts, req=requested)
def test_risk_chain_never_increases_exposure(chain: list[Verdict], extra: Verdict, req: Decimal) -> None:
    setup_decimal_context()
    combined = exposure(compose(chain), req)
    each = [exposure(v, req) for v in chain]
    assert combined <= min(each, default=req) <= req
    assert combined >= 0
    # додавання будь-якого нового правила не може збільшити експозицію
    assert exposure(compose([*chain, extra]), req) <= combined


@settings(max_examples=300)
@given(chain=chains, data=st.data())
def test_compose_is_order_independent(chain: list[Verdict], data: st.DataObject) -> None:
    perm = data.draw(st.permutations(chain))
    assert compose(perm) == compose(chain)                      # точна рівність, не з допуском
    cut = data.draw(st.integers(min_value=0, max_value=len(chain)))
    # асоціативність: згорнути частини окремо, потім разом — те саме
    assert compose([compose(chain[:cut]), compose(chain[cut:])]) == compose(chain)
    assert compose([ALLOW, *chain]) == compose(chain)           # ALLOW — нейтральний елемент


@settings(max_examples=300)
@given(chain=chains, pos=st.integers(min_value=0, max_value=20), req=requested)
def test_veto_absorbs_everything(chain: list[Verdict], pos: int, req: Decimal) -> None:
    with_veto = [*chain[:pos], VETO, *chain[pos:]]
    v = compose(with_veto)
    assert v is VETO or (v.kind is VerdictKind.VETO and v.factor == 0)
    assert exposure(v, req) == 0


@settings(max_examples=200)
@given(st.lists(st.floats(min_value=-1.0, max_value=1.0, allow_nan=False, width=64),
                min_size=1, max_size=600),
       st.sampled_from([None, 500, 20]),
       st.sampled_from([0.05, 0.01, 0.1]))
def test_cvar_ge_var_always(returns: list[float], window: int | None, alpha: float) -> None:
    res = historical_var_cvar(returns, alpha, window)
    assert res.cvar >= res.var                                  # у float, без допуску
    w = np.asarray(returns[-window:] if window else returns)
    m = tail_count(w.size, alpha)
    srt = np.sort(w)
    assert res.m == m and res.n == w.size
    assert res.var == -srt[m - 1]                               # незалежний шлях: повне сортування
    scale = max(1.0, float(np.max(np.abs(srt[:m]))))
    assert abs(res.cvar - (-srt[:m].mean())) <= 1e-12 * scale


# ---------------------------------------------------------------- ланцюг лімітів як ціле

_CFG = load_risk_config()


@settings(max_examples=200)
@given(
    equity=st.decimals(min_value=D(100), max_value=D(1_000_000), places=2),
    price=st.decimals(min_value=D(1), max_value=D(100_000), places=2),
    cur_frac=st.decimals(min_value=D(-4), max_value=D(4), places=3),
    tgt_frac=st.decimals(min_value=D(-5), max_value=D(5), places=3),
    atr_frac=st.decimals(min_value=D("0.0001"), max_value=D("0.08"), places=4),
    other_frac=st.decimals(min_value=D(0), max_value=D(3), places=2),
    lev_set=st.sampled_from([D(1), D(3), D(10), D(20)]),
)
def test_guard_approved_position_satisfies_every_limit(
        equity: Decimal, price: Decimal, cur_frac: Decimal, tgt_frac: Decimal, atr_frac: Decimal,
        other_frac: Decimal, lev_set: Decimal) -> None:
    """Добуток SHRINK-множників задовольняє кожен ліміт одночасно: для прийнятої позиції
    IM ≤ 0.30·E, Σ|номінал| ≤ 3·E, і (якщо приріст прийнято) DTL ≥ 6 та L ≤ L'_max; |ціль| не зростає."""
    setup_decimal_context()
    atr = price * atr_frac
    ctx = RiskContext(
        ts_ns=0, instrument="X", price=price, equity=equity,
        current_qty=(equity * cur_frac / price).quantize(D("0.000001")),
        target_qty=(equity * tgt_frac / price).quantize(D("0.000001")),
        atr=atr, stop_distance=2 * atr, gross_notional_other=equity * other_frac,
        leverage_setting=lev_set, step_size=D("0.000001"),
    )
    guard = RiskGuard(default_rules(_CFG), RiskJournal(keep=False))
    res = guard.evaluate(ctx)
    assert res.increase_approved <= res.increase_requested
    assert abs(res.approved_qty) <= max(abs(ctx.target_qty), D(0))
    if res.increase_approved == 0:
        return
    post = abs(res.approved_qty)
    tol = D("1e-20") * equity
    assert post * price / lev_set <= D("0.30") * equity + tol
    assert ctx.gross_notional_other + post * price <= D(3) * equity + tol
    side = Side.LONG if res.approved_qty > 0 else Side.SHORT
    wallet = equity - ctx.gross_notional_other * ctx.mmr
    liq = side_liq_price(side, post, price, wallet, ctx.mmr)
    if not (side is Side.LONG and liq <= 0):
        assert dtl_atr(price, liq, atr) >= D(6) - D("1e-20")
        # щойно спрацювало зменшення плеча, пост-позиція вміщається в обидві межі
        if ctx.post_qty > max_qty_for_dtl(side, wallet=wallet, price=price, atr=atr, min_dtl=D(6),
                                          mmr=ctx.mmr):
            assert post * price / wallet <= reduced_max_leverage(2 * atr, price, ctx.mmr, D("0.20")) + tol


@settings(max_examples=200)
@given(
    lev=st.decimals(min_value=D("0.01"), max_value=D(2000), places=2),
    atr_frac=st.decimals(min_value=D("0.00001"), max_value=D("0.08"), places=5),
    short=st.booleans(),
)
def test_liquidation_guard_alone_never_approves_dtl_below_limit(lev: Decimal, atr_frac: Decimal,
                                                                  short: bool) -> None:
    """Правило саме по собі (без MaxGrossLeverage) на будь-якому плечі до 2000× — зокрема > 1/mmr = 200×, де
    P_liq лежить по інший бік від ціни: прийнятий приріст завжди має знакову DTL ≥ 6."""
    setup_decimal_context()
    equity, price = D(10_000), D(100)
    atr = price * atr_frac
    qty = (equity * lev / price).quantize(D("0.000001"))
    side = Side.SHORT if short else Side.LONG
    ctx = RiskContext(ts_ns=0, instrument="X", price=price, equity=equity, current_qty=D(0),
                      target_qty=-qty if short else qty, atr=atr, stop_distance=2 * atr)
    rv = LiquidationBufferGuard().check(ctx)
    post = exposure(rv.verdict, ctx.increase_qty)
    if post == 0:
        return
    liq = side_liq_price(side, post, price, equity, ctx.mmr)
    if side is Side.LONG and liq <= 0:
        return                                                  # ліквідація недосяжна
    assert signed_dtl_atr(side, price, liq, atr) >= D(6) - D("1e-20")
