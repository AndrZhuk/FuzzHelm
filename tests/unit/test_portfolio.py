"""K. Облік перп-рахунку: реалізований PnL за середньою ціною, розворот через нуль, фандинг, угоди."""

from __future__ import annotations

import itertools
from decimal import Decimal
from uuid import UUID

import pytest

from fuzzhelm.core.dto import Fill
from fuzzhelm.core.enums import ExitReason, Liquidity, Side
from fuzzhelm.execution.cost_model import FundingCharge
from fuzzhelm.execution.portfolio import Portfolio

D = Decimal
SYM = "BTC-USDT-PERP"
_COUNTER = itertools.count(1)


def fill(side: Side, qty: str, price: str, fee: str = "0", ts: int = 0) -> Fill:
    return Fill(client_order_id=UUID(int=next(_COUNTER)), instrument=SYM, side=side, qty=D(qty),
                price=D(price), fee=D(fee), liquidity=Liquidity.TAKER, ts_fill_ns=ts)


def test_long_partial_close_then_flip_hand_computed() -> None:
    pf = Portfolio(D("1000"))
    pf.apply_fill(fill(Side.LONG, "2", "100", fee="0.08"))
    assert pf.positions[SYM].avg_entry == D(100)
    pf.apply_fill(fill(Side.LONG, "2", "110", fee="0.088"))
    assert pf.positions[SYM].avg_entry == D(105)            # (2·100 + 2·110)/4
    closed = pf.apply_fill(fill(Side.SHORT, "1", "115", fee="0.046"))
    assert closed == []
    assert pf.realized_pnl == D(10)                         # 1·(115 − 105)
    # продаж 5 при позиції 3: закриває 3 (+3·(90−105) = −45) і відкриває шорт 2 @ 90
    (trade,) = pf.apply_fill(fill(Side.SHORT, "5", "90", fee="0.18"), exit_reason=ExitReason.SIGNAL)
    assert pf.realized_pnl == D(-35)
    pos = pf.positions[SYM]
    assert pos.qty == D(-2) and pos.avg_entry == D(90)
    assert trade.side == Side.LONG and trade.qty == D(4) and trade.gross_pnl == D(-35)
    assert trade.entry_price == D(105) and trade.exit_price == D("96.25")    # VWAP зменшень: (115 + 3·90)/4
    # комісія розворотного виконання ділиться пропорційно: 3/5 закриттю, 2/5 новій угоді
    assert trade.fees == D("0.08") + D("0.088") + D("0.046") + D("0.108")
    assert trade.pnl == trade.gross_pnl - trade.fees
    assert trade.exit_reason == ExitReason.SIGNAL
    pf.mark({SYM: D(80)})
    assert pf.unrealized_pnl == D(20)                        # шорт 2: (90 − 80)·2
    assert pf.fees_paid == D("0.394")
    assert pf.equity == D(1000) - D(35) - D("0.394") + D(20)
    assert pf.identity_residual() == 0


def test_short_round_trip_and_wallet_vs_cash_forms() -> None:
    pf = Portfolio(D("500"))
    pf.apply_fill(fill(Side.SHORT, "3", "200", fee="0.24"))
    pf.mark({SYM: D(190)})
    assert pf.unrealized_pnl == D(30)
    assert pf.cash == D(500) + D(600)                        # спот-еквівалент: продаж приніс номінал
    assert pf.wallet_balance == D(500) - D("0.24")           # гаманець перпа номіналу не отримує
    assert pf.equity == pf.equity_cash_form == D("529.76")
    (t,) = pf.apply_fill(fill(Side.LONG, "3", "190", fee="0.228"), exit_reason=ExitReason.TP)
    assert t.gross_pnl == D(30) and t.pnl == D(30) - D("0.468")
    assert pf.positions[SYM].qty == 0 and pf.unrealized_pnl == 0
    # без позиції: гаманець = капітал = спот-еквівалентні гроші мінус комісії
    assert pf.cash - pf.fees_paid == pf.wallet_balance == pf.equity == D("529.532")


def test_funding_sign_long_pays_short_receives() -> None:
    pf = Portfolio(D("1000"))
    pf.apply_fill(fill(Side.LONG, "1", "100"))
    pf.mark({SYM: D(100)})
    pf.apply_funding(FundingCharge(SYM, 0, D(1), D(100), D("0.0001"), D("0.0100")))
    assert pf.funding_paid == D("0.01") and pf.equity == D("999.99")
    pf.apply_fill(fill(Side.SHORT, "2", "100"))
    pf.apply_funding(FundingCharge(SYM, 1, D(-1), D(100), D("0.0001"), D("-0.0100")))
    assert pf.funding_paid == 0 and pf.equity == D(1000)
    assert pf.identity_residual() == 0
    (t,) = [tr for tr in pf.closed_trades if tr.side == Side.LONG]
    assert t.funding == D("0.01") and t.pnl == D("-0.01")


def test_exposure_and_leverage() -> None:
    pf = Portfolio(D("1000"))
    pf.apply_fill(fill(Side.SHORT, "10", "150"))
    pf.mark({SYM: D(150)})
    assert pf.gross_exposure == D(1500) and pf.net_exposure == D(-1500)
    assert pf.leverage == D("1.5")
    snap = pf.snapshot(123)
    assert snap.equity == D(1000) and snap.leverage == D("1.5") and snap.ts_ns == 123


def test_max_adverse_excursion_tracked_by_marks() -> None:
    pf = Portfolio(D("1000"))
    pf.apply_fill(fill(Side.LONG, "1", "100"))
    for p in ("98", "95", "103", "97"):
        pf.mark({SYM: D(p)})
    (t,) = pf.apply_fill(fill(Side.SHORT, "1", "104"))
    assert t.max_adverse_excursion == D(-5)


def test_equity_requires_mark_price() -> None:
    pf = Portfolio(D("100"))
    pf.apply_funding(FundingCharge("ETH-USDT-PERP", 0, D(0), D(1), D(0), D(0)))
    pf.positions["ETH-USDT-PERP"].qty = D(1)          # штучно: позиція без жодної ціни
    with pytest.raises(KeyError):
        _ = pf.equity
    with pytest.raises(ValueError):
        pf.mark({SYM: D(0)})
