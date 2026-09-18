"""K. Виконання: модель витрат, PaperBroker (t+1 open, песимізм усередині бару, фандинг), OrderRouter."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import pytest

from fuzzhelm.core.clock import NS_PER_MIN, ManualClock, SeededIdGenerator
from fuzzhelm.core.dto import Candle, Instrument, OrderAck, OrderRequest
from fuzzhelm.core.enums import (
    ContractType,
    ExitReason,
    Liquidity,
    OrderStatus,
    OrderType,
    RejectCode,
    Side,
    Src,
    Venue,
)
from fuzzhelm.execution.cost_model import CostMode, CostModel
from fuzzhelm.execution.paper_broker import DEFAULT_FUNDING_RATE, DecBar, PaperBroker
from fuzzhelm.execution.portfolio import Portfolio
from fuzzhelm.execution.router import OrderRouter

D = Decimal
SYM = "BTC-USDT-PERP"
T0 = int(datetime(2026, 9, 18, tzinfo=UTC).timestamp()) * 1_000_000_000   # 00:00 UTC
NS_PER_HOUR = 60 * NS_PER_MIN
INST = Instrument(
    venue=Venue.PAPER, symbol_venue="BTCUSDT", symbol_canon=SYM, base_asset="BTC", quote_asset="USDT",
    contract_type=ContractType.PERP, tick_size=D("0.1"), step_size=D("0.001"), min_notional=D("5"),
)


def bar(t_ns: int, o: str, h: str, l: str, c: str, v: str = "10") -> DecBar:
    return DecBar(SYM, t_ns, D(o), D(h), D(l), D(c), D(v))


class Env:
    """Брокер + годинник + лічильник id для детерміністичних сценаріїв."""

    def __init__(self, mode: CostMode | str = CostMode.ZERO, *, noise_bps: str = "0", seed: int = 7) -> None:
        self.clock = ManualClock(T0 - NS_PER_HOUR)
        self.ids = SeededIdGenerator(seed, b"test")
        self.cost = CostModel(mode, noise_bps=D(noise_bps), seed=seed)
        self.broker = PaperBroker(self.cost, [INST], SeededIdGenerator(seed, b"broker"), self.clock)
        self._n = 0

    def order(self, side: Side, qty: str, *, otype: OrderType = OrderType.MARKET, stop: str | None = None,
              reduce_only: bool = False, ts: int | None = None, coid: UUID | None = None) -> OrderRequest:
        return OrderRequest(
            client_order_id=coid or self.ids.next_uuid(), instrument=SYM, side=side, otype=otype,
            qty=D(qty), stop_price=None if stop is None else D(stop), reduce_only=reduce_only,
            ts_created_ns=self.clock.now_ns() if ts is None else ts,
        )

    def feed(self, b: DecBar) -> list:
        self.clock.set(b.open_time_ns + NS_PER_MIN - 1_000_000)     # час закриття бару
        return self.broker.on_bar(b)


# ============================================================ брифінг, група K


def test_fill_on_next_bar_open_not_current_close() -> None:
    env = Env(CostMode.ZERO)
    bar_t = bar(T0, "100.0", "101.0", "99.0", "100.5")
    assert env.feed(bar_t) == []
    # рішення на закритті бару t
    ack = env.broker.submit(env.order(Side.LONG, "0.5"))
    assert ack.status == OrderStatus.NEW
    assert env.broker.position(SYM) == 0          # submit нічого не виконує
    bar_t1 = bar(T0 + NS_PER_MIN, "103.7", "104.2", "103.1", "103.9")
    fills = env.feed(bar_t1)
    assert len(fills) == 1
    f = fills[0]
    assert f.price == D("103.7")                  # open(t+1) ...
    assert f.price != bar_t.c                     # ... а не close(t) = 100.5
    assert f.price != bar_t1.c                    # і не close(t+1)
    assert f.ts_fill_ns == bar_t1.open_time_ns
    assert env.broker.position(SYM) == D("0.5")
    # повторна подача того самого (старого) бару заборонена — інакше заявка «виконалась би в минулому»
    with pytest.raises(ValueError, match="strictly increasing"):
        env.broker.on_bar(bar_t)


def test_fill_waits_for_bar_not_older_than_decision() -> None:
    env = Env(CostMode.ZERO)
    env.feed(bar(T0, "100", "101", "99", "100"))
    # заявка з часом рішення в майбутньому відносно наступного бару не виконується на ньому
    env.broker.submit(env.order(Side.LONG, "1", ts=T0 + 2 * NS_PER_MIN))
    assert env.feed(bar(T0 + NS_PER_MIN, "105", "106", "104", "105")) == []
    fills = env.feed(bar(T0 + 2 * NS_PER_MIN, "107", "108", "106", "107"))
    assert [f.price for f in fills] == [D("107")]


def test_sqrt_impact_scales_with_sqrt_of_qty() -> None:
    cm = CostModel(CostMode.SQRT_IMPACT, noise_bps=D(0))
    p, sigma, v = D("50000"), D("0.001"), D("100")
    # k_s·σ·P·√(Q/V) = 0.5·0.001·50000·√(1/100) = 2.5
    base = cm.impact(p, D(1), sigma, v)
    assert base == D("2.5")
    for m, root in ((4, 2), (9, 3), (16, 4), (25, 5)):
        assert cm.impact(p, D(m), sigma, v) == base * root        # рівно, без допуску
    ratio = cm.impact(p, D(2), sigma, v) / base
    assert abs(ratio - D(2).sqrt()) < D("1e-30")
    # і в самій ціні виконання: (P_fill − P_ref − δ/2) масштабується як √Q з точністю до пів-тіка
    tick = D("0.1")
    for m, root in ((1, 1), (4, 2), (9, 3)):
        q = cm.quote(Side.LONG, p, D(m), sigma=sigma, v_bar=v, tick=tick)
        assert abs((q.price - p - q.half_spread) - base * root) <= tick / 2
        s = cm.quote(Side.SHORT, p, D(m), sigma=sigma, v_bar=v, tick=tick)
        assert abs((p - s.price - s.half_spread) - base * root) <= tick / 2
    # режим zero — імпакту немає взагалі
    assert CostModel(CostMode.ZERO).impact(p, D(9), sigma, v) == 0


def test_taker_fee_higher_than_maker() -> None:
    cm = CostModel(CostMode.FULL)
    n = D("10000")
    assert cm.fee(n, Liquidity.TAKER) == D("4")      # 10000·0.0004
    assert cm.fee(n, Liquidity.MAKER) == D("2")      # 10000·0.0002
    assert cm.fee(n, Liquidity.TAKER) > cm.fee(n, Liquidity.MAKER)
    # брокер виконує MARKET як taker і бере саме taker-ставку від фактичного номіналу
    env = Env(CostMode.FULL)
    env.feed(bar(T0, "100", "101", "99", "100"))
    env.broker.submit(env.order(Side.LONG, "2"))
    (f,) = env.feed(bar(T0 + NS_PER_MIN, "100", "101", "99", "100"))
    assert f.liquidity == Liquidity.TAKER
    assert f.fee == f.qty * f.price * D("0.0004")
    # у спрощених режимах комісій немає
    assert CostModel(CostMode.SQRT_IMPACT).fee(n, Liquidity.TAKER) == 0
    assert CostModel(CostMode.ZERO).fee(n, Liquidity.TAKER) == 0


def test_funding_charged_at_00_08_16_utc() -> None:
    env = Env(CostMode.FULL)
    start = T0 - 30 * NS_PER_MIN                      # 23:30 попередньої доби
    env.feed(bar(start, "100", "100", "100", "100"))
    env.broker.submit(env.order(Side.LONG, "2"))
    closes = {}
    for i in range(1, 24 * 60 + 40):                  # до 00:10 наступної доби
        t = start + i * NS_PER_MIN
        px = str(100 + (i % 7))
        env.feed(bar(t, px, px, px, px))
        closes[t] = D(px)
    charges = env.broker.drain_funding()
    hours = [datetime.fromtimestamp(c.ts_ns / 1e9, tz=UTC).strftime("%d %H:%M") for c in charges]
    assert hours == ["18 00:00", "18 08:00", "18 16:00", "19 00:00"]
    for c in charges:
        assert c.position_qty == D(2)
        assert c.mark_price == closes[c.ts_ns]          # mark = open бару, що починається в момент фандингу
        assert c.rate == DEFAULT_FUNDING_RATE
        assert c.amount == D(2) * c.mark_price * DEFAULT_FUNDING_RATE
        assert c.amount > 0                              # лонг платить додатний фандинг
    assert env.broker.drain_funding() == []              # забирається рівно один раз
    # шорт при додатній ставці отримує: у портфелі funding_paid < 0, капітал зростає на цю суму
    env2 = Env(CostMode.FULL)
    env2.feed(bar(T0 - NS_PER_MIN * 2, "100", "100", "100", "100"))
    env2.broker.submit(env2.order(Side.SHORT, "1"))
    pf = Portfolio(D("1000"))
    for f in env2.feed(bar(T0 - NS_PER_MIN, "100", "100", "100", "100")):
        pf.apply_fill(f)
    eq_before = pf.mark({SYM: D("100")})
    env2.feed(bar(T0, "100", "100", "100", "100"))
    (ch,) = env2.broker.drain_funding()
    assert ch.amount == D("-0.0100")
    pf.apply_funding(ch)
    assert pf.funding_paid == D("-0.01")
    assert pf.mark({SYM: D("100")}) - eq_before == D("0.01")


def test_funding_schedule_only_at_configured_hours() -> None:
    cm = CostModel(CostMode.FULL)
    times = cm.funding_times(T0 - 1, T0 + 3 * 24 * NS_PER_HOUR)
    assert len(times) == 10                                  # 3 доби × 3 + початок четвертої
    assert all((t - T0) % (8 * NS_PER_HOUR) == 0 for t in times)
    assert all(cm.is_funding_time(t) for t in times)
    assert not cm.is_funding_time(T0 + NS_PER_HOUR)
    assert cm.funding_times(T0, T0) == []
    assert cm.funding_times(T0, T0 + 8 * NS_PER_HOUR) == [T0 + 8 * NS_PER_HOUR]   # (from, to]
    assert cm.next_funding_time(T0) == T0 + 8 * NS_PER_HOUR
    assert cm.next_funding_time(T0 + 17 * NS_PER_HOUR) == T0 + 24 * NS_PER_HOUR
    assert CostModel(CostMode.SQRT_IMPACT).funding_payment(D(1), D(100), D("0.001")) == 0


def _open_position(env: Env, side: Side, stop: str, tp: str | None) -> None:
    env.feed(bar(T0, "100", "100", "100", "100"))
    env.broker.submit(env.order(side, "1"))
    stop_side = Side.SHORT if side == Side.LONG else Side.LONG
    env.broker.submit(env.order(stop_side, "1", otype=OrderType.STOP_MARKET, stop=stop, reduce_only=True))
    if tp is not None:
        env.broker.set_take_profit(SYM, D(tp))
    fills = env.feed(bar(T0 + NS_PER_MIN, "100", "100.5", "99.5", "100"))
    assert [f.price for f in fills] == [D("100")]


def test_intrabar_pessimism_resolves_stop_before_tp() -> None:
    # лонг 1 @ 100, стоп 95, тейк 105; наступний бар зачіпає обидва рівні (l=94, h=106)
    env = Env(CostMode.ZERO)
    _open_position(env, Side.LONG, stop="95", tp="105")
    fills = env.feed(bar(T0 + 2 * NS_PER_MIN, "100", "106", "94", "100"))
    assert len(fills) == 1                                  # тейк не виконався після стопа (OCO)
    (f,) = fills
    assert f.side == Side.SHORT and f.price == D("95")      # спершу стоп — песимістично
    assert env.broker.exit_reason(f.client_order_id) == ExitReason.STOP
    assert env.broker.position(SYM) == 0
    assert env.broker.open_orders(SYM) == []
    # дзеркально для шорта: стоп 105 вище, тейк 95 нижче → знову стоп
    env = Env(CostMode.ZERO)
    _open_position(env, Side.SHORT, stop="105", tp="95")
    (f,) = env.feed(bar(T0 + 2 * NS_PER_MIN, "100", "106", "94", "100"))
    assert f.side == Side.LONG and f.price == D("105")
    assert env.broker.exit_reason(f.client_order_id) == ExitReason.STOP
    # тейк БЛИЖЧЕ до open, ніж стоп (h − o = 3 < o − l = 6): евристика «спершу ближчий екстремум» дала б
    # TP @102, песимістичне правило — однаково стоп @95
    env = Env(CostMode.ZERO)
    _open_position(env, Side.LONG, stop="95", tp="102")
    (f,) = env.feed(bar(T0 + 2 * NS_PER_MIN, "100", "103", "94", "101"))
    assert f.price == D("95") and env.broker.exit_reason(f.client_order_id) == ExitReason.STOP


def test_take_profit_bound_to_side_survives_reversal() -> None:
    env = Env(CostMode.ZERO)
    env.feed(bar(T0, "100", "100", "100", "100"))
    env.broker.submit(env.order(Side.LONG, "1"))
    env.broker.set_take_profit(SYM, D("110"))                 # TP лонга (без прив'язки до боку)
    env.feed(bar(T0 + NS_PER_MIN, "100", "100", "100", "100"))
    # рішення: розворот у шорт; тейк нової позиції задається ДО виконання розвороту
    env.broker.submit(env.order(Side.SHORT, "2"))
    env.broker.set_take_profit(SYM, D("95"), side=Side.SHORT)
    (rev,) = env.feed(bar(T0 + 2 * NS_PER_MIN, "100", "100", "100", "100"))
    assert rev.side == Side.SHORT and env.broker.position(SYM) == D(-1)
    (f,) = env.feed(bar(T0 + 3 * NS_PER_MIN, "100", "100", "94", "96"))
    assert f.side == Side.LONG and f.price == D("95")
    assert env.broker.exit_reason(f.client_order_id) == ExitReason.TP and env.broker.position(SYM) == 0
    # TP без прив'язки скидається розворотом: інакше старий «лонговий» TP 110 для шорта спрацював би
    # одразу за open (100 ≤ 110) як випадковий вихід
    env = Env(CostMode.ZERO)
    env.feed(bar(T0, "100", "100", "100", "100"))
    env.broker.submit(env.order(Side.LONG, "1"))
    env.broker.set_take_profit(SYM, D("110"))
    env.feed(bar(T0 + NS_PER_MIN, "100", "100", "100", "100"))
    env.broker.submit(env.order(Side.SHORT, "2"))
    env.feed(bar(T0 + 2 * NS_PER_MIN, "100", "100", "100", "100"))
    assert env.feed(bar(T0 + 3 * NS_PER_MIN, "100", "101", "99", "100")) == []
    assert env.broker.position(SYM) == D(-1)
    # TP, прив'язаний до іншого боку, на поточну позицію не діє
    env.broker.set_take_profit(SYM, D("99"), side=Side.LONG)
    assert env.feed(bar(T0 + 4 * NS_PER_MIN, "100", "101", "98", "100")) == []
    with pytest.raises(ValueError):
        env.broker.set_take_profit(SYM, D("99"), side=Side.FLAT)


def test_stop_ignores_range_of_bar_older_than_its_creation() -> None:
    env = Env(CostMode.ZERO)
    env.feed(bar(T0, "100", "100", "100", "100"))
    env.broker.submit(env.order(Side.LONG, "1"))
    env.feed(bar(T0 + NS_PER_MIN, "100", "100", "100", "100"))
    # стоп «з майбутнього» (після початку наступного бару) не може спрацювати від діапазону цього бару
    stop = env.order(Side.SHORT, "1", otype=OrderType.STOP_MARKET, stop="95", reduce_only=True,
                     ts=T0 + 2 * NS_PER_MIN + 1)
    env.broker.submit(stop)
    assert env.feed(bar(T0 + 2 * NS_PER_MIN, "100", "100", "90", "99")) == []
    (f,) = env.feed(bar(T0 + 3 * NS_PER_MIN, "99", "99", "94", "96"))
    assert f.client_order_id == stop.client_order_id and f.price == D("95")


def test_take_profit_fills_when_stop_untouched() -> None:
    env = Env(CostMode.ZERO)
    _open_position(env, Side.LONG, stop="95", tp="105")
    (f,) = env.feed(bar(T0 + 2 * NS_PER_MIN, "100", "106", "99", "104"))
    assert f.price == D("105")
    assert env.broker.exit_reason(f.client_order_id) == ExitReason.TP
    # reduce-only стоп старої позиції скасовано, а не залишено «висіти» на майбутнє
    assert env.broker.open_orders(SYM) == []


def test_gap_through_stop_fills_at_open() -> None:
    env = Env(CostMode.ZERO)
    _open_position(env, Side.LONG, stop="95", tp="105")
    (f,) = env.feed(bar(T0 + 2 * NS_PER_MIN, "93", "94", "90", "92"))
    assert f.price == D("93")                                # за open, а не за рівнем стопа 95
    assert env.broker.exit_reason(f.client_order_id) == ExitReason.STOP


def test_liquidation_exit_reason() -> None:
    env = Env(CostMode.ZERO)
    env.feed(bar(T0, "100", "100", "100", "100"))
    env.broker.submit(env.order(Side.LONG, "1"))
    env.feed(bar(T0 + NS_PER_MIN, "100", "100", "100", "100"))
    env.broker.set_liquidation_price(SYM, D("90.4523"))
    (f,) = env.feed(bar(T0 + 2 * NS_PER_MIN, "99", "99", "89", "95"))
    assert env.broker.exit_reason(f.client_order_id) == ExitReason.LIQUIDATION
    assert f.price == D("90.5")                              # рівень, квантований до tick (HALF_EVEN)
    assert env.broker.position(SYM) == 0


def test_stop_nearer_than_liquidation_fires_first() -> None:
    env = Env(CostMode.ZERO)
    _open_position(env, Side.LONG, stop="95", tp=None)
    env.broker.set_liquidation_price(SYM, D("90"))
    (f,) = env.feed(bar(T0 + 2 * NS_PER_MIN, "100", "100", "85", "88"))
    assert f.price == D("95")
    assert env.broker.exit_reason(f.client_order_id) == ExitReason.STOP


def test_duplicate_client_order_id_rejected_without_double_fill() -> None:
    env = Env(CostMode.ZERO)
    env.feed(bar(T0, "100", "100", "100", "100"))
    req = env.order(Side.LONG, "1")
    first = env.broker.submit(req)
    dup = env.broker.submit(req)
    assert first.status == OrderStatus.NEW
    assert dup.status == OrderStatus.REJECTED and dup.reject_code == RejectCode.DUPLICATE_CLIENT_ID
    fills = env.feed(bar(T0 + NS_PER_MIN, "101", "101", "101", "101"))
    assert len(fills) == 1 and env.broker.position(SYM) == D(1)
    ack = env.broker.order_ack(req.client_order_id)
    assert ack is not None and ack.status == OrderStatus.FILLED   # оригінал не зіпсовано дублем


def test_reduce_only_never_increases_position() -> None:
    env = Env(CostMode.ZERO)
    env.feed(bar(T0, "100", "100", "100", "100"))
    env.broker.submit(env.order(Side.LONG, "1"))
    env.feed(bar(T0 + NS_PER_MIN, "100", "100", "100", "100"))
    env.broker.submit(env.order(Side.SHORT, "5", reduce_only=True))   # більше за позицію
    (f,) = env.feed(bar(T0 + 2 * NS_PER_MIN, "100", "100", "100", "100"))
    assert f.qty == D(1) and env.broker.position(SYM) == 0
    cancelled = env.order(Side.SHORT, "1", reduce_only=True)
    env.broker.submit(cancelled)
    assert env.feed(bar(T0 + 3 * NS_PER_MIN, "100", "100", "100", "100")) == []
    ack = env.broker.order_ack(cancelled.client_order_id)
    assert ack is not None and ack.status == OrderStatus.CANCELED
    assert ack.reject_code == RejectCode.REDUCE_ONLY


def test_orphan_reduce_only_stops_expire_but_bracket_of_pending_entry_survives() -> None:
    env = Env(CostMode.ZERO)
    env.feed(bar(T0, "100", "100", "100", "100"))
    # вхід відкладено на бар T0+2хв (час рішення), тож на барі T0+1хв позиції ще немає
    env.broker.submit(env.order(Side.LONG, "1", ts=T0 + 2 * NS_PER_MIN))
    guard = env.order(Side.SHORT, "1", otype=OrderType.STOP_MARKET, stop="90", reduce_only=True)
    orphan = env.order(Side.LONG, "1", otype=OrderType.STOP_MARKET, stop="120", reduce_only=True)
    env.broker.submit(guard)
    env.broker.submit(orphan)
    assert env.feed(bar(T0 + NS_PER_MIN, "100", "101", "99", "100")) == []
    g, o = env.broker.order_ack(guard.client_order_id), env.broker.order_ack(orphan.client_order_id)
    assert g is not None and g.status == OrderStatus.NEW          # захищає позицію, яку відкриє черга
    assert o is not None and o.status == OrderStatus.CANCELED     # захищав би шорт, якого не буде
    assert o.reject_code == RejectCode.REDUCE_ONLY
    env.feed(bar(T0 + 2 * NS_PER_MIN, "100", "101", "99", "100"))
    assert env.broker.position(SYM) == 1
    assert [r.client_order_id for r in env.broker.open_orders(SYM)] == [guard.client_order_id]


def test_submit_rejects_zero_qty_and_below_min_notional() -> None:
    env = Env(CostMode.ZERO)
    env.feed(bar(T0, "100", "100", "100", "100"))
    zero = env.broker.submit(env.order(Side.LONG, "0.0009"))          # < step 0.001
    assert zero.reject_code == RejectCode.ZERO_QTY
    small = env.broker.submit(env.order(Side.LONG, "0.01"))           # 0.01·100 = 1 < 5
    assert small.reject_code == RejectCode.BELOW_MIN_NOTIONAL
    unknown = env.broker.submit(env.order(Side.LONG, "1").model_copy(update={"instrument": "ETH-USDT-PERP"}))
    assert unknown.reject_code == RejectCode.VENUE_ERROR


def test_cancel_all_cancels_pending_orders() -> None:
    env = Env(CostMode.ZERO)
    env.feed(bar(T0, "100", "100", "100", "100"))
    req = env.order(Side.LONG, "1")
    env.broker.submit(req)
    env.broker.cancel_all(SYM)
    assert env.feed(bar(T0 + NS_PER_MIN, "100", "100", "100", "100")) == []
    ack = env.broker.order_ack(req.client_order_id)
    assert ack is not None and ack.status == OrderStatus.CANCELED


def test_accepts_candle_dto_through_port() -> None:
    env = Env(CostMode.ZERO)

    def candle(t: int, px: str) -> Candle:
        p = D(px)
        return Candle(instrument=SYM, venue=Venue.BINANCE_USDM, tf="1m", open_time_ns=t,
                      close_time_ns=t + NS_PER_MIN - 1_000_000, o=p, h=p, l=p, c=p, volume=D(3),
                      is_closed=True, src=Src.REST, ts_event_ns=t, ts_ingest_ns=t, event_uid="x" * 32)

    env.broker.on_bar(candle(T0, "100"))
    env.clock.set(T0 + NS_PER_MIN - 1)
    env.broker.submit(env.order(Side.LONG, "1"))
    (f,) = env.broker.on_bar(candle(T0 + NS_PER_MIN, "101"))
    assert f.price == D("101")


# ============================================================ модель витрат: режими і seed


def _run_prices(seed: int, noise_bps: str) -> list[Decimal]:
    env = Env(CostMode.FULL, noise_bps=noise_bps, seed=seed)
    out: list[Decimal] = []
    t = T0
    env.feed(bar(t, "60000", "60010", "59990", "60000", "50"))
    for i in range(1, 30):
        t = T0 + i * NS_PER_MIN
        side = Side.LONG if i % 2 else Side.SHORT
        env.broker.submit(env.order(side, "0.1", ts=t - 1))
        out.extend(f.price for f in env.feed(bar(t, str(60000 + i), str(60010 + i), str(59990 + i),
                                                 str(60000 + i), "50")))
    return out


def test_seeded_noise_reproducible_and_seed_dependent() -> None:
    a1, a2, b = _run_prices(1, "0.5"), _run_prices(1, "0.5"), _run_prices(2, "0.5")
    assert a1 == a2                       # той самий seed → ті самі ціни до біта
    assert a1 != b                        # інший seed → інша траєкторія (джерело для негативного контролю)
    # без шуму seed не впливає нічого
    assert _run_prices(1, "0") == _run_prices(2, "0")


def test_cost_modes_order_execution_quality() -> None:
    kw = {"sigma": D("0.001"), "v_bar": D("10"), "tick": D("0.1")}
    zero = CostModel(CostMode.ZERO).quote(Side.LONG, D("100"), D("1"), **kw)
    imp = CostModel(CostMode.SQRT_IMPACT).quote(Side.LONG, D("100"), D("1"), **kw)
    assert zero.price == D("100") and zero.slippage_bps == 0
    assert imp.price > zero.price and imp.slippage_bps > 0
    sell = CostModel(CostMode.SQRT_IMPACT).quote(Side.SHORT, D("100"), D("1"), **kw)
    assert sell.price < D("100") and sell.slippage_bps > 0      # ковзання завжди проти нас


def test_cost_model_from_config_yaml() -> None:
    cm = CostModel.from_config(seed=1)
    assert cm.mode == CostMode.FULL
    assert cm.k_s == D("0.5") and cm.taker == D("0.0004") and cm.maker == D("0.0002")
    assert cm.funding_hours_utc == (0, 8, 16)
    assert CostModel.from_config(seed=1, mode="zero").mode == CostMode.ZERO
    # позиційний порядок з contracts.md §12: CostModel(mode, k_s, taker, maker, noise_bps, seed)
    pos = CostModel("full", D("0.4"), D("0.0005"), D("0.0001"), D("0.5"), 9)
    assert (pos.k_s, pos.taker, pos.maker, pos.noise_bps, pos.seed) == (D("0.4"), D("0.0005"), D("0.0001"),
                                                                         D("0.5"), 9)


# ============================================================ OrderRouter


class _CountingVenue:
    name = "counting"

    def __init__(self, fail: bool = False, known: OrderAck | None = None) -> None:
        self.calls = 0
        self.fail = fail
        self.known = known

    def submit(self, req: OrderRequest) -> OrderAck:
        self.calls += 1
        if self.fail:
            raise ConnectionError("network down")
        return OrderAck(client_order_id=req.client_order_id, venue_order_id="V1", status=OrderStatus.NEW,
                        ts_ns=req.ts_created_ns)

    def on_bar(self, bar: object) -> list:
        return []

    def cancel_all(self, instrument: str) -> None:
        return None

    def query_order(self, client_order_id: UUID, instrument: str) -> OrderAck | None:
        return self.known


def test_router_idempotent_retry_returns_same_ack_without_second_venue_call() -> None:
    env = Env(CostMode.ZERO)
    env.feed(bar(T0, "100", "100", "100", "100"))
    router = OrderRouter(env.broker)
    req = env.order(Side.LONG, "1")
    a1, a2 = router.submit(req), router.submit(req)
    assert a1 == a2 and a1.status == OrderStatus.NEW
    assert router.venue_calls == 1
    assert len(router.on_bar(bar(T0 + NS_PER_MIN, "101", "101", "101", "101"))) == 1   # рівно одне виконання
    changed = req.model_copy(update={"qty": D("2")})
    dup = router.submit(changed)
    assert dup.status == OrderStatus.REJECTED and dup.reject_code == RejectCode.DUPLICATE_CLIENT_ID


def test_router_reconciles_after_venue_failure() -> None:
    env = Env(CostMode.ZERO)
    req = env.order(Side.LONG, "1")
    placed = OrderAck(client_order_id=req.client_order_id, venue_order_id="V9", status=OrderStatus.FILLED,
                      ts_ns=1)
    router = OrderRouter(_CountingVenue(fail=True, known=placed))
    assert router.submit(req) == placed                  # стан відновлено звірянням, а не вгадано
    lost = OrderRouter(_CountingVenue(fail=True, known=None))
    ack = lost.submit(req)
    assert ack.status == OrderStatus.REJECTED and ack.reject_code == RejectCode.VENUE_ERROR
    assert lost.ack_of(req.client_order_id) is None      # ключ не запам'ятовано — повтор можливий
