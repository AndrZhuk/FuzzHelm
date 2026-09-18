"""K (property). Тотожність обліку капіталу на КОЖНОМУ кроці — для довільних послідовностей подій."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from fuzzhelm.core.clock import NS_PER_MIN, ManualClock, SeededIdGenerator
from fuzzhelm.core.dto import Fill, Instrument, OrderRequest
from fuzzhelm.core.enums import ContractType, Liquidity, OrderType, Side, Venue
from fuzzhelm.core.money import floor_qty
from fuzzhelm.execution.cost_model import CostMode, CostModel, FundingCharge
from fuzzhelm.execution.paper_broker import DecBar, PaperBroker
from fuzzhelm.execution.portfolio import Portfolio

pytestmark = pytest.mark.property

D = Decimal
TOL = D("1E-18")                  # жорсткіше за 1e−9 з брифінгу: масштаб NUMERIC(38,18)
INSTS = ("BTC-USDT-PERP", "ETH-USDT-PERP")
W0 = D("10000")

qty_st = st.integers(1, 5000).map(lambda n: D(n) * D("0.001"))
price_st = st.integers(10, 1_000_000).map(lambda n: D(n) * D("0.1"))
fee_rate_st = st.sampled_from([D(0), D("0.0002"), D("0.0004")])
rate_st = st.integers(-100, 100).map(lambda n: D(n) * D("0.00001"))
inst_st = st.sampled_from(INSTS)
side_st = st.sampled_from([Side.LONG, Side.SHORT])

op_st = st.one_of(
    st.tuples(st.just("fill"), inst_st, side_st, qty_st, price_st, fee_rate_st),
    st.tuples(st.just("funding"), inst_st, rate_st),
    st.tuples(st.just("mark"), inst_st, price_st),
)


@settings(max_examples=150)
@given(ops=st.lists(op_st, min_size=1, max_size=60))
def test_equity_accounting_identity(ops: list[tuple]) -> None:
    pf = Portfolio(W0)
    # незалежний еталон: лише сирі грошові потоки, без середніх цін і реалізованого PnL
    ref_cash, ref_fees = W0, D(0)
    ref_q = dict.fromkeys(INSTS, D(0))
    ref_mark: dict[str, Decimal] = {}
    # другий еталон — реалізований PnL за СЕРЕДНЬОЮ ЦІНОЮ (зберігається ціна, а не cost_basis, як у коді):
    # сама тотожність не бачить помилок розподілу собівартості при частковому закритті, цей — бачить
    ref_avg: dict[str, Decimal] = {}
    ref_realized = D(0)
    for n, op in enumerate(ops):
        kind, inst = op[0], op[1]
        if kind == "fill":
            _, _, side, qty, price, fee_rate = op
            fee = qty * price * fee_rate
            pf.apply_fill(Fill(client_order_id=UUID(int=n + 1), instrument=inst, side=side, qty=qty,
                               price=price, fee=fee, liquidity=Liquidity.TAKER, ts_fill_ns=n))
            ref_cash -= int(side) * qty * price
            ref_fees += fee
            q_old, sgn = ref_q[inst], int(side)
            if q_old == 0 or (q_old > 0) == (sgn > 0):
                old_notional = D(0) if q_old == 0 else ref_avg[inst] * abs(q_old)
                ref_avg[inst] = (old_notional + price * qty) / (abs(q_old) + qty)
            else:
                closed = min(qty, abs(q_old))
                ref_realized += closed * (price - ref_avg[inst]) * (1 if q_old > 0 else -1)
                if qty > abs(q_old):
                    ref_avg[inst] = price            # розворот: залишок відкрито за ціною виконання
            ref_q[inst] += sgn * qty
            ref_mark.setdefault(inst, price)
        elif kind == "funding":
            if inst not in ref_mark:
                continue
            rate = op[2]
            amount = ref_q[inst] * ref_mark[inst] * rate
            pf.apply_funding(FundingCharge(inst, n, ref_q[inst], ref_mark[inst], rate, amount))
            ref_cash -= amount
        else:
            pf.mark({inst: op[2]})
            ref_mark[inst] = op[2]

        # --- інваріанти після КОЖНОГО кроку
        assert abs(pf.identity_residual()) <= TOL                       # cash + Σq·p − Σfees == equity
        ref_equity = ref_cash + sum((q * ref_mark[i] for i, q in ref_q.items() if q != 0), D(0)) - ref_fees
        assert abs(pf.equity - ref_equity) <= TOL
        assert all(pf.position_qty(i) == ref_q[i] for i in INSTS)
        assert pf.fees_paid == ref_fees
        assert abs(pf.realized_pnl - ref_realized) <= TOL
        for i in INSTS:
            avg = pf.positions[i].avg_entry if i in pf.positions else None
            assert (avg is None) == (ref_q[i] == 0)
            assert avg is None or abs(avg - ref_avg[i]) <= TOL
    # коли все закрито, капітал = W₀ + Σ чистого PnL угод (комісії розворотів діляться пропорційно)
    if all(q == 0 for q in ref_q.values()):
        assert abs(pf.equity - W0 - sum((t.pnl for t in pf.closed_trades), D(0))) <= TOL


SYM = "BTC-USDT-PERP"
INST = Instrument(
    venue=Venue.PAPER, symbol_venue="BTCUSDT", symbol_canon=SYM, base_asset="BTC", quote_asset="USDT",
    contract_type=ContractType.PERP, tick_size=D("0.1"), step_size=D("0.001"), min_notional=D("5"),
)
T0 = int(datetime(2026, 9, 18, 7, 0, tzinfo=UTC).timestamp()) * 1_000_000_000

action_st = st.one_of(
    st.none(),
    st.tuples(st.just("mkt"), side_st, st.integers(1, 300), st.booleans()),
    st.tuples(st.just("stop"), side_st, st.integers(1, 300), st.integers(-40, 40)),
    st.tuples(st.just("tp"), st.integers(-40, 40)),
    st.tuples(st.just("liq"), st.integers(-60, 60)),
)


@settings(max_examples=60)
@given(
    seed=st.integers(0, 2**31),
    steps=st.lists(st.tuples(st.integers(-30, 30), st.integers(0, 20), st.integers(0, 20), action_st),
                   min_size=5, max_size=80),
)
def test_broker_portfolio_identity_every_bar(seed: int, steps: list[tuple]) -> None:
    """Повний контур PaperBroker → Portfolio з моделлю full (+шум): тотожність після кожного бару."""
    clock = ManualClock(T0)
    broker = PaperBroker(CostModel(CostMode.FULL, noise_bps=D("0.5"), seed=seed), [INST],
                         SeededIdGenerator(seed, b"b"), clock)
    ids = SeededIdGenerator(seed, b"o")
    pf = Portfolio(W0)
    px = D("30000.0")
    for i, (drift, up, down, action) in enumerate(steps):
        t = T0 + i * 60 * NS_PER_MIN            # годинні бари: фандинг о 08:00 і 16:00 потрапляє в прогін
        o = px
        c = max(o + D(drift) * D("5"), D("100"))
        h = max(o, c) + D(up) * D("5")
        low = max(min(o, c) - D(down) * D("5"), D("50"))
        clock.set(t + 60 * NS_PER_MIN - 1)
        pos_before = broker.position(SYM)
        fills = broker.on_bar(DecBar(SYM, t, o, h, low, c, D("25")))
        for ch in broker.drain_funding():
            pf.apply_funding(ch)
        for f in fills:
            assert f.price == f.price.quantize(INST.tick_size)                 # ціна кратна tick
            assert f.qty == floor_qty(f.qty, INST.step_size)                 # кількість кратна step
            ack = broker.order_ack(f.client_order_id)
            assert ack is not None
            pf.apply_fill(f, broker.exit_reason(f.client_order_id))
        pf.mark({SYM: c})
        assert pf.position_qty(SYM) == broker.position(SYM)                 # брокер і облік узгоджені
        assert abs(pf.identity_residual()) <= TOL
        if fills and all(broker.exit_reason(f.client_order_id) is not None for f in fills):
            # виходи STOP/TP/LIQUIDATION ніколи не збільшують |позицію|
            assert abs(broker.position(SYM)) <= abs(pos_before) or pos_before == 0
        px = c
        if action is None:
            continue
        kind = action[0]
        if kind == "mkt":
            _, side, q, ro = action
            broker.submit(OrderRequest(client_order_id=ids.next_uuid(), instrument=SYM, side=side,
                                       otype=OrderType.MARKET, qty=D(q) * D("0.001"), reduce_only=ro,
                                       ts_created_ns=clock.now_ns()))
        elif kind == "stop":
            _, side, q, off = action
            stop = max(c + D(off) * D("10"), D("10"))
            broker.submit(OrderRequest(client_order_id=ids.next_uuid(), instrument=SYM, side=side,
                                       otype=OrderType.STOP_MARKET, qty=D(q) * D("0.001"), stop_price=stop,
                                       reduce_only=True, ts_created_ns=clock.now_ns()))
        elif kind == "tp":
            broker.set_take_profit(SYM, max(c + D(action[1]) * D("10"), D("10")))
        else:
            broker.set_liquidation_price(SYM, max(c + D(action[1]) * D("10"), D("10")))
