"""Симульований брокер (paper): реалізація порту ExecutionVenue для бектесту, реплею і демо.

Найменування: execution/paper_broker.py
Призначення: виконання заявок над потоком барів без зазирання в майбутнє, з моделлю витрат.
Автор: Андрій Жук, 2026.

Семантика (брифінг §5.14, contracts §12):
  * `submit` лише ставить заявку в чергу (жодного виконання на поточному барі);
  * `on_bar(bar)`:
      1) фандинг за моменти 00/08/16 UTC у (попередній open, bar.open] — на позицію ДО виконань бару;
      2) MARKET-заявки виконуються за bar.o наступного бару (рішення на закритті t → open t+1);
         заявка виконується лише на барі з open_time_ns ≥ ts_created_ns (захист від «минулої» ціни);
      3) тригери всередині [l, h]: STOP_MARKET-заявки (теж лише на барах з open_time_ns ≥ ts_created_ns:
         діапазон бару, що минув до появи стопа, не може його спрацювати), рівень тейк-профіту,
         ціна ліквідації.
         Розрив (gap-through): якщо бар відкрився за рівнем — виконання за open.
         Шлях ціни всередині бару невідомий, тому він обирається ПЕСИМІСТИЧНО до позиції:
         лонг — O→L→H→C, шорт — O→H→L→C (стоп раніше за тейк); без позиції — спершу ближчий екстремум;
      4) оновлення оцінок σ_t (EWMA квадратів простих доходностей, λ = 0.94) і V_bar (EWMA обсягу) —
         ПІСЛЯ виконань, тож ціна виконання на барі t+1 спирається лише на бари ≤ t.
  * ідемпотентність: повторний client_order_id → REJECTED(DUPLICATE_CLIENT_ID), оригінал не зачіпається;
  * reduce-only заявки ніколи не збільшують позицію (обрізаються до її розміру, 0 → CANCELED/REDUCE_ONLY);
    після закриття/розвороту позиції скасовуються reduce-only стопи, що її захищали, TP цієї позиції
    і ціна ліквідації скидаються (семантика OCO). TP, прив'язаний до протилежного боку
    (`set_take_profit(..., side=...)`), розворот переживає — інакше тейк нової позиції, заданий у момент
    рішення, губився б на тому самому виконанні, що її відкриває. Наприкінці бару скасовуються
    reduce-only стопи-«сироти», що не захищають ні поточну позицію, ні позицію, яку відкриє заявка з черги.
Причини виходу (STOP / TP / LIQUIDATION) не входять у DTO Fill — їх дає `exit_reason(client_order_id)`.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Protocol
from uuid import UUID

from fuzzhelm.core.dto import Candle, Fill, Instrument, OrderAck, OrderRequest
from fuzzhelm.core.enums import ExitReason, Liquidity, OrderStatus, OrderType, RejectCode, Side
from fuzzhelm.core.money import D0, D1, floor_qty, quantize_price
from fuzzhelm.core.ports import Clock, IdGenerator
from fuzzhelm.execution.cost_model import CostModel, FundingCharge

DEFAULT_VOL_LAMBDA = Decimal("0.94")        # як у сайзингу (RiskMetrics), брифінг §5.8
# Базова ставка фандингу Binance USDⓈ-M: відсоткова складова 0.01 % за 8 год; коли премія в коридорі
# ±0.05 %, фактична ставка дорівнює саме їй. Використовується, доки не надійшла ставка з premiumIndex.
DEFAULT_FUNDING_RATE = Decimal("0.0001")


class BarLike(Protocol):
    """Мінімальний інтерфейс бару для брокера: Candle (DTO) або легкий DecBar."""

    @property
    def instrument(self) -> str: ...
    @property
    def open_time_ns(self) -> int: ...
    @property
    def o(self) -> Decimal: ...
    @property
    def h(self) -> Decimal: ...
    @property
    def l(self) -> Decimal: ...  # noqa: E743 — ім'я поля зафіксоване DTO Candle
    @property
    def c(self) -> Decimal: ...
    @property
    def volume(self) -> Decimal: ...


@dataclass(frozen=True, slots=True)
class DecBar:
    """Легкий Decimal-бар для гарячого циклу бектесту (без pydantic-валідації Candle)."""

    instrument: str
    open_time_ns: int
    o: Decimal
    h: Decimal
    l: Decimal
    c: Decimal
    volume: Decimal


def dec_bar_from_candle(c: Candle) -> DecBar:
    return DecBar(c.instrument, c.open_time_ns, c.o, c.h, c.l, c.c, c.volume)


@dataclass(slots=True)
class _Order:
    req: OrderRequest
    venue_order_id: str | None
    status: OrderStatus
    reject_code: RejectCode | None
    ts_ns: int
    filled_qty: Decimal = D0
    avg_price: Decimal | None = None
    exit_reason: ExitReason | None = None


@dataclass(slots=True)
class _InstState:
    spec: Instrument
    funding_rate: Decimal
    position: Decimal = D0
    last_open_ns: int | None = None
    last_close: Decimal | None = None
    var: Decimal | None = None             # EWMA дисперсії простих доходностей (за бар)
    v_bar: Decimal | None = None           # EWMA обсягу бару
    tp_price: Decimal | None = None
    tp_side: int = 0                       # 0 — TP діє на будь-яку позицію; ±1 — лише на позицію цього знака
    liq_price: Decimal | None = None
    market_queue: list[UUID] = field(default_factory=list)
    stop_orders: list[UUID] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class _Trigger:
    kind: str                  # "stop" | "tp" | "liq"
    level: Decimal
    fires_up: bool             # спрацьовує при ціні ≥ level (інакше ≤ level)
    coid: UUID | None          # для stop-заявок
    priority: int              # tie-break на однаковому рівні: liq < stop < tp


def _sign(x: Decimal) -> int:
    return (x > 0) - (x < 0)


class PaperBroker:
    """Порт ExecutionVenue поверх потоку барів. Один брокер може вести кілька інструментів."""

    name = "paper"

    def __init__(
        self,
        cost_model: CostModel,
        instruments: Mapping[str, Instrument] | Iterable[Instrument],
        ids: IdGenerator,
        clock: Clock,
        *,
        vol_lambda: Decimal = DEFAULT_VOL_LAMBDA,
        default_funding_rate: Decimal = DEFAULT_FUNDING_RATE,
    ) -> None:
        specs = list(instruments.values()) if isinstance(instruments, Mapping) else list(instruments)
        if not D0 < vol_lambda < D1:
            raise ValueError(f"vol_lambda must be in (0, 1), got {vol_lambda}")
        self.cost = cost_model
        self._ids = ids
        self._clock = clock
        self._lam = vol_lambda
        self._one_minus_lam = D1 - vol_lambda
        self._state: dict[str, _InstState] = {
            s.symbol_canon: _InstState(spec=s, funding_rate=default_funding_rate) for s in specs
        }
        self._orders: dict[UUID, _Order] = {}
        self._funding: list[FundingCharge] = []
        self._seq = 0

    # ================================================================ порт ExecutionVenue

    def submit(self, req: OrderRequest) -> OrderAck:
        now = self._clock.now_ns()
        coid = req.client_order_id
        if coid in self._orders:
            # ідемпотентність: дубль не створює другої заявки і не змінює стан оригіналу
            return OrderAck(client_order_id=coid, status=OrderStatus.REJECTED,
                            reject_code=RejectCode.DUPLICATE_CLIENT_ID, ts_ns=now)
        st = self._state.get(req.instrument)
        if st is None:
            return self._reject(req, RejectCode.VENUE_ERROR, now)
        qty = floor_qty(req.qty, st.spec.step_size)
        if qty <= 0:
            return self._reject(req, RejectCode.ZERO_QTY, now)
        if (not req.reduce_only and st.last_close is not None
                and qty * st.last_close < st.spec.min_notional):
            return self._reject(req, RejectCode.BELOW_MIN_NOTIONAL, now)
        update: dict[str, object] = {}
        if qty != req.qty:
            update["qty"] = qty
        if req.otype == OrderType.STOP_MARKET and req.stop_price is not None:
            sp = quantize_price(req.stop_price, st.spec.tick_size)
            if sp <= 0:
                return self._reject(req, RejectCode.VENUE_ERROR, now)
            if sp != req.stop_price:
                update["stop_price"] = sp
        if update:
            req = req.model_copy(update=update)
        self._seq += 1
        order = _Order(req=req, venue_order_id=f"PB-{self._seq:08d}", status=OrderStatus.NEW,
                       reject_code=None, ts_ns=now)
        self._orders[coid] = order
        if req.otype == OrderType.MARKET:
            st.market_queue.append(coid)
        else:
            st.stop_orders.append(coid)
        return OrderAck(client_order_id=coid, venue_order_id=order.venue_order_id,
                        status=OrderStatus.NEW, ts_ns=now)

    def on_bar(self, bar: BarLike) -> list[Fill]:
        st = self._state.get(bar.instrument)
        if st is None:
            raise ValueError(f"unknown instrument {bar.instrument!r}")
        t = bar.open_time_ns
        if st.last_open_ns is not None and t <= st.last_open_ns:
            raise ValueError(f"bars must be strictly increasing: open_time {t} <= {st.last_open_ns}")
        fills: list[Fill] = []
        if (st.position != 0 and st.last_open_ns is not None and self.cost.charges_funding
                and t >= self.cost.next_funding_time(st.last_open_ns)):
            self._charge_funding(st, st.last_open_ns, t, bar.o)
        if st.market_queue:
            self._fill_market_orders(st, bar, fills)
        if st.stop_orders or (st.position != 0 and (st.tp_price is not None or st.liq_price is not None)):
            self._walk_triggers(st, bar, fills)
        if st.stop_orders:
            self._expire_orphan_stops(st)
        self._update_stats(st, bar)
        st.last_open_ns = t
        return fills

    def cancel_all(self, instrument: str) -> None:
        st = self._state.get(instrument)
        if st is None:
            return
        for coid in (*st.market_queue, *st.stop_orders):
            o = self._orders[coid]
            if o.status == OrderStatus.NEW:
                o.status = OrderStatus.CANCELED
        st.market_queue.clear()
        st.stop_orders.clear()
        st.tp_price = None     # TP — біржова заявка; ціна ліквідації — механізм біржі, лишається
        st.tp_side = 0

    # ================================================================ керування рівнями

    def set_take_profit(self, instrument: str, price: Decimal | None, *, side: Side | None = None) -> None:
        """Рівень TP. Напрям спрацювання визначається знаком позиції на барі.

        side=None — TP діє на поточну (або наступну відкриту) позицію і скидається при її закритті чи
        розвороті. side=LONG|SHORT — TP діє лише на позицію цього боку і переживає закриття позиції
        протилежного боку: так задається тейк нової позиції при розвороті ДО виконання розворотної заявки.
        """
        st = self._require(instrument)
        if side == Side.FLAT:
            raise ValueError("take-profit side must be LONG, SHORT or None")
        st.tp_price = None if price is None else quantize_price(price, st.spec.tick_size)
        st.tp_side = 0 if side is None or price is None else int(side)

    def set_liquidation_price(self, instrument: str, price: Decimal | None) -> None:
        """Ціна ліквідації, розрахована ризик-модулем (risk.margin.liq_price_*)."""
        st = self._require(instrument)
        st.liq_price = price

    def set_funding_rate(self, instrument: str, rate: Decimal) -> None:
        """Остання відома ставка фандингу (з MarkPrice/premiumIndex); діє до наступного оновлення."""
        self._require(instrument).funding_rate = rate

    # ================================================================ запити стану

    def position(self, instrument: str) -> Decimal:
        return self._require(instrument).position

    def sigma(self, instrument: str) -> Decimal:
        """Поточна оцінка σ_t за бар (0 до появи двох закриттів)."""
        var = self._require(instrument).var
        return D0 if var is None or var <= 0 else var.sqrt()

    def v_bar(self, instrument: str) -> Decimal:
        v = self._require(instrument).v_bar
        return D0 if v is None else v

    def order_ack(self, client_order_id: UUID) -> OrderAck | None:
        o = self._orders.get(client_order_id)
        if o is None:
            return None
        return OrderAck(client_order_id=client_order_id, venue_order_id=o.venue_order_id,
                        status=o.status, reject_code=o.reject_code, ts_ns=o.ts_ns)

    def order_request(self, client_order_id: UUID) -> OrderRequest | None:
        """Заявка (після квантування брокером) за client_order_id — зокрема синтетичні TP/ліквідації,
        яких рушій не створював (потрібні для рядка sim_order)."""
        o = self._orders.get(client_order_id)
        return None if o is None else o.req

    def exit_reason(self, client_order_id: UUID) -> ExitReason | None:
        o = self._orders.get(client_order_id)
        return None if o is None else o.exit_reason

    def open_orders(self, instrument: str) -> list[OrderRequest]:
        st = self._require(instrument)
        return [self._orders[c].req for c in (*st.market_queue, *st.stop_orders)
                if self._orders[c].status == OrderStatus.NEW]

    def drain_funding(self) -> list[FundingCharge]:
        """Забрати накопичені нарахування фандингу (для Portfolio.apply_funding)."""
        out, self._funding = self._funding, []
        return out

    # ================================================================ внутрішнє

    def _require(self, instrument: str) -> _InstState:
        st = self._state.get(instrument)
        if st is None:
            raise KeyError(f"unknown instrument {instrument!r}")
        return st

    def _reject(self, req: OrderRequest, code: RejectCode, now: int) -> OrderAck:
        self._orders[req.client_order_id] = _Order(req=req, venue_order_id=None,
                                                   status=OrderStatus.REJECTED, reject_code=code, ts_ns=now)
        return OrderAck(client_order_id=req.client_order_id, status=OrderStatus.REJECTED,
                        reject_code=code, ts_ns=now)

    def _charge_funding(self, st: _InstState, t_from: int, t_to: int, open_price: Decimal) -> None:
        for ts in self.cost.funding_times(t_from, t_to):
            # mark ≈ ціна відкриття бару, що починається в момент фандингу (інакше — останнє закриття)
            mark = open_price if ts == t_to or st.last_close is None else st.last_close
            amount = self.cost.funding_payment(st.position, mark, st.funding_rate)
            self._funding.append(FundingCharge(st.spec.symbol_canon, ts, st.position, mark,
                                               st.funding_rate, amount))

    def _fill_market_orders(self, st: _InstState, bar: BarLike, fills: list[Fill]) -> None:
        t = bar.open_time_ns
        keep: list[UUID] = []
        for coid in st.market_queue:
            o = self._orders[coid]
            if o.status != OrderStatus.NEW:
                continue
            if t < o.req.ts_created_ns:
                keep.append(coid)      # бар старший за рішення — виконання за цією ціною було б неможливим
                continue
            self._execute(st, o, bar.o, t, fills)
        st.market_queue = keep

    def _executable_qty(self, st: _InstState, req: OrderRequest) -> Decimal:
        if not req.reduce_only:
            return req.qty
        if st.position == 0 or _sign(st.position) == int(req.side):
            return D0
        return min(req.qty, abs(st.position))

    def _execute(self, st: _InstState, o: _Order, p_ref: Decimal, ts: int, fills: list[Fill],
                 *, reason: ExitReason | None = None) -> None:
        req = o.req
        qty = self._executable_qty(st, req)
        if qty <= 0:
            o.status = OrderStatus.CANCELED
            o.reject_code = RejectCode.REDUCE_ONLY
            return
        side = req.side
        old = st.position
        if reason is None and req.otype == OrderType.STOP_MARKET and old != 0 and _sign(old) != int(side):
            reason = ExitReason.STOP
        q = self.cost.quote(side, p_ref, qty, sigma=self._sigma(st), v_bar=self._vbar(st),
                            tick=st.spec.tick_size)
        fee = self.cost.fee(qty * q.price, Liquidity.TAKER)
        fills.append(Fill(client_order_id=req.client_order_id, venue_order_id=o.venue_order_id,
                          instrument=req.instrument, side=side, qty=qty, price=q.price, fee=fee,
                          liquidity=Liquidity.TAKER, slippage_bps=q.slippage_bps, ts_fill_ns=ts))
        o.status = OrderStatus.FILLED
        o.filled_qty = qty
        o.avg_price = q.price
        o.exit_reason = reason
        new = old + int(side) * qty
        st.position = new
        if old != 0 and (new == 0 or _sign(new) != _sign(old)):
            self._on_position_closed(st, old)

    def _on_position_closed(self, st: _InstState, old: Decimal) -> None:
        """Стара позиція закрита (у нуль або розворотом): скасувати її захисні reduce-only стопи."""
        protecting_side = Side(-_sign(old))
        keep: list[UUID] = []
        for coid in st.stop_orders:
            o = self._orders[coid]
            if o.status != OrderStatus.NEW:
                continue
            if o.req.reduce_only and o.req.side == protecting_side:
                o.status = OrderStatus.CANCELED
                continue
            keep.append(coid)
        st.stop_orders = keep
        if st.tp_side == 0 or st.tp_side == _sign(old):
            st.tp_price = None
            st.tp_side = 0
        st.liq_price = None

    def _expire_orphan_stops(self, st: _InstState) -> None:
        """Reduce-only стоп, який не може зменшити жодної позиції — ні поточної, ні тієї, що відкриє
        активна заявка на вхід (MARKET у черзі чи стоп-вхід), — скасовується (як біржа знімає
        reduce-only заявки без позиції). Інакше такі «сироти» накопичувались би і могли б спрацювати
        проти майбутньої, чужої їм позиції."""
        pos_sign = _sign(st.position)
        # заявки, що можуть відкрити позицію: MARKET у черзі та стоп-входи без reduce_only
        entry_sides = {self._orders[c].req.side for c in (*st.market_queue, *st.stop_orders)
                       if self._orders[c].status == OrderStatus.NEW and not self._orders[c].req.reduce_only}
        keep: list[UUID] = []
        for coid in st.stop_orders:
            o = self._orders[coid]
            if o.status != OrderStatus.NEW:
                continue
            if o.req.reduce_only:
                protected = -int(o.req.side)          # знак позиції, яку ця заявка може зменшити
                if pos_sign != protected and Side(protected) not in entry_sides:
                    o.status = OrderStatus.CANCELED
                    o.reject_code = RejectCode.REDUCE_ONLY
                    continue
            keep.append(coid)
        st.stop_orders = keep

    @staticmethod
    def _sigma(st: _InstState) -> Decimal:
        return D0 if st.var is None or st.var <= 0 else st.var.sqrt()

    @staticmethod
    def _vbar(st: _InstState) -> Decimal:
        return D0 if st.v_bar is None else st.v_bar

    # ---------------------------------------------------------------- тригери всередині бару

    def _triggers(self, st: _InstState, t: int) -> list[_Trigger]:
        out: list[_Trigger] = []
        for coid in st.stop_orders:
            o = self._orders[coid]
            # стоп, створений пізніше за початок бару, не бачить його діапазону (як і MARKET)
            if o.status == OrderStatus.NEW and o.req.stop_price is not None and o.req.ts_created_ns <= t:
                out.append(_Trigger("stop", o.req.stop_price, o.req.side == Side.LONG, coid, 1))
        if st.position != 0:
            if st.tp_price is not None and (st.tp_side == 0 or st.tp_side == _sign(st.position)):
                out.append(_Trigger("tp", st.tp_price, st.position > 0, None, 2))
            if st.liq_price is not None:
                out.append(_Trigger("liq", st.liq_price, st.position < 0, None, 0))
        return out

    def _walk_triggers(self, st: _InstState, bar: BarLike, fills: list[Fill]) -> None:
        o, h, l = bar.o, bar.h, bar.l
        t = bar.open_time_ns
        # швидкий шлях: жоден рівень недосяжний у межах бару (типовий випадок — стоп далеко від ціни)
        if not any(tr.level <= h if tr.fires_up else tr.level >= l for tr in self._triggers(st, t)):
            return
        # 1) розрив на відкритті: усі рівні, що вже «позаду» open, виконуються за open
        while True:
            at_open = [tr for tr in self._triggers(st, t)
                       if (tr.fires_up and o >= tr.level) or (not tr.fires_up and o <= tr.level)]
            if not at_open:
                break
            first = min(at_open, key=lambda tr: tr.priority)
            self._fire(st, first, o, t, fills)
        # 2) песимістичний шлях всередині бару
        if st.position > 0:
            path = (o, l, h, bar.c)
        elif st.position < 0:
            path = (o, h, l, bar.c)
        else:
            path = (o, l, h, bar.c) if (o - l) <= (h - o) else (o, h, l, bar.c)
        cur = o
        for nxt in path[1:]:
            cur = self._walk_segment(st, cur, nxt, t, fills)

    def _walk_segment(self, st: _InstState, a: Decimal, b: Decimal, t: int, fills: list[Fill]) -> Decimal:
        cur = a
        while b != cur:
            trigs = self._triggers(st, t)
            if b < cur:
                cands = [tr for tr in trigs if not tr.fires_up and b <= tr.level <= cur]
                if not cands:
                    break
                first = min(cands, key=lambda tr: (-tr.level, tr.priority))
            else:
                cands = [tr for tr in trigs if tr.fires_up and cur <= tr.level <= b]
                if not cands:
                    break
                first = min(cands, key=lambda tr: (tr.level, tr.priority))
            self._fire(st, first, first.level, t, fills)
            cur = first.level
        return b

    def _fire(self, st: _InstState, tr: _Trigger, p_ref: Decimal, t: int, fills: list[Fill]) -> None:
        if tr.kind == "stop":
            assert tr.coid is not None
            st.stop_orders.remove(tr.coid)
            self._execute(st, self._orders[tr.coid], p_ref, t, fills)
            return
        # TP / ліквідація — синтетична reduce-only заявка на всю позицію
        pos = st.position
        reason = ExitReason.TP if tr.kind == "tp" else ExitReason.LIQUIDATION
        if tr.kind == "tp":
            st.tp_price = None
            st.tp_side = 0
        else:
            st.liq_price = None
        req = OrderRequest(client_order_id=self._ids.next_uuid(), instrument=st.spec.symbol_canon,
                           side=Side(-_sign(pos)), otype=OrderType.STOP_MARKET, qty=abs(pos),
                           stop_price=tr.level, reduce_only=True, decision_ref=reason.value,
                           ts_created_ns=t)
        self._seq += 1
        order = _Order(req=req, venue_order_id=f"PB-{self._seq:08d}", status=OrderStatus.NEW,
                       reject_code=None, ts_ns=t)
        self._orders[req.client_order_id] = order
        self._execute(st, order, p_ref, t, fills, reason=reason)

    def _update_stats(self, st: _InstState, bar: BarLike) -> None:
        c = bar.c
        lam = self._lam
        if st.last_close is not None:
            r = c / st.last_close - D1
            r2 = r * r
            st.var = r2 if st.var is None else lam * st.var + self._one_minus_lam * r2
        st.last_close = c
        vol = bar.volume
        st.v_bar = vol if st.v_bar is None else lam * st.v_bar + self._one_minus_lam * vol
