"""Виконання на Binance USDⓈ-M Futures TESTNET: підписані REST-запити HMAC-SHA256.

Найменування: execution/testnet_venue.py
Призначення: реальний повний цикл ордера (підпис → POST /fapi/v1/order → FILLED → звіряння) без
реальних коштів. Друга реалізація порту ExecutionVenue (брифінг §1 п.6, §3 питання 3).
Автор: Андрій Жук, 2026.

Безпека за побудовою:
  * базова адреса ОБОВ'ЯЗКОВО проходить `fuzzhelm.config.assert_testnet_url` — і в конструкторі,
    і перед кожним запитом (інакше MainnetHostRejected); mainnet недосяжний;
  * ключі — лише з `Settings` (SecretStr); значення не логуються, не входять у repr, секрет не
    передається по мережі (лише HMAC від рядка запиту).

Підпис Binance (SIGNED endpoints): signature = HEX(HMAC_SHA256(secret, query_string)), де
query_string містить усі параметри + timestamp (мс) + recvWindow; ключ — у заголовку X-MBX-APIKEY.
Підтримується лише тип MARKET (для доведення циклу достатньо; умовні заявки на testnet не
реалізовано — див. deviations.d/execution.md).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from urllib.parse import urlencode
from uuid import UUID

import httpx

from fuzzhelm.config import Settings, assert_testnet_url
from fuzzhelm.core.dto import Candle, Fill, Instrument, OrderAck, OrderRequest
from fuzzhelm.core.enums import Liquidity, OrderStatus, OrderType, RejectCode, Side
from fuzzhelm.core.errors import ConfigValidationError, FuzzHelmError
from fuzzhelm.core.money import D0, dec, dec_str, floor_qty, quantize_price
from fuzzhelm.core.ports import Clock
from fuzzhelm.infra.wallclock import SystemClock

log = logging.getLogger(__name__)

ORDER_PATH = "/fapi/v1/order"
USER_TRADES_PATH = "/fapi/v1/userTrades"
ALL_OPEN_ORDERS_PATH = "/fapi/v1/allOpenOrders"
NS_PER_MS = 1_000_000

_STATUS: dict[str, OrderStatus] = {
    "NEW": OrderStatus.NEW,
    "PARTIALLY_FILLED": OrderStatus.PARTIAL,
    "FILLED": OrderStatus.FILLED,
    "CANCELED": OrderStatus.CANCELED,
    "EXPIRED": OrderStatus.CANCELED,
    "EXPIRED_IN_MATCH": OrderStatus.CANCELED,
    "REJECTED": OrderStatus.REJECTED,
}
# Відомі коди помилок Binance Futures → наші коди відхилення; решта → VENUE_ERROR.
_ERROR_CODES: dict[int, RejectCode] = {
    -2019: RejectCode.INSUFFICIENT_MARGIN,   # Margin is insufficient
    -2022: RejectCode.REDUCE_ONLY,           # ReduceOnly Order is rejected
    -4164: RejectCode.BELOW_MIN_NOTIONAL,    # Order's notional must be no smaller than ...
    -4116: RejectCode.DUPLICATE_CLIENT_ID,   # ClientOrderId is duplicated
}
_ORDER_DOES_NOT_EXIST = -2013


class VenueUnavailableError(FuzzHelmError):
    """Транспортний збій або 5xx: стан заявки НЕВІДОМИЙ, потрібне звіряння (OrderRouter це робить)."""


@dataclass(slots=True)
class _Tracked:
    req: OrderRequest
    symbol: str
    venue_order_id: str | None
    status: OrderStatus
    fill_emitted: bool = False


def _canon_to_venue_symbol(canon: str) -> str:
    """'BTC-USDT-PERP' → 'BTCUSDT'; рядок без дефісів вважається вже символом біржі."""
    parts = canon.split("-")
    if len(parts) == 3 and parts[2] == "PERP":
        return parts[0] + parts[1]
    return canon


class BinanceTestnetVenue:
    """Синхронний (як і порт) клієнт testnet-виконання поверх `httpx.Client`."""

    name = "binance_testnet"

    def __init__(
        self,
        settings: Settings,
        http: httpx.Client,
        *,
        clock: Clock | None = None,
        instruments: Mapping[str, Instrument] | Iterable[Instrument] | None = None,
        recv_window_ms: int = 5000,
        time_offset_ms: int = 0,
    ) -> None:
        # повторна перевірка: Settings міг бути створений в обхід валідаторів (model_construct)
        self._base = assert_testnet_url(settings.venue_base_url).rstrip("/")
        key, secret = settings.binance_testnet_key, settings.binance_testnet_secret
        if key is None or secret is None or not key.get_secret_value() or not secret.get_secret_value():
            raise ConfigValidationError("Binance testnet API key/secret are not configured",
                                        path="BINANCE_TESTNET_KEY")
        self._key = key
        self._secret = secret
        self._http = http
        # живий клієнт за замовчуванням бере настінний годинник (підпис Binance вимагає реального часу)
        self._clock: Clock = clock if clock is not None else SystemClock()
        if instruments is None:
            specs: list[Instrument] = []
        elif isinstance(instruments, Mapping):
            specs = list(instruments.values())
        else:
            specs = list(instruments)
        self._instruments = {s.symbol_canon: s for s in specs}
        if not 0 < recv_window_ms <= 60_000:
            raise ValueError("recvWindow must be in (0, 60000] ms")
        self._recv_window = recv_window_ms
        self._offset_ms = time_offset_ms
        self._orders: dict[UUID, _Tracked] = {}

    def __repr__(self) -> str:
        return f"BinanceTestnetVenue(base={self._base!r}, api_key=***)"

    # ================================================================ підпис і транспорт

    def sign(self, query: str) -> str:
        """HEX(HMAC-SHA256(secret, query))."""
        secret = self._secret.get_secret_value().encode()
        return hmac.new(secret, query.encode(), hashlib.sha256).hexdigest()

    def _signed_query(self, params: list[tuple[str, str]]) -> str:
        ts_ms = self._clock.now_ns() // NS_PER_MS + self._offset_ms
        full = [*params, ("recvWindow", str(self._recv_window)), ("timestamp", str(ts_ms))]
        query = urlencode(full)
        return f"{query}&signature={self.sign(query)}"

    def _request(self, method: str, path: str, params: list[tuple[str, str]]) -> httpx.Response:
        url = f"{self._base}{path}?{self._signed_query(params)}"
        assert_testnet_url(url)
        try:
            resp = self._http.request(method, url, headers={"X-MBX-APIKEY": self._key.get_secret_value()})
        except httpx.TransportError as e:
            raise VenueUnavailableError(f"{method} {path}: transport error {type(e).__name__}") from e
        if resp.status_code >= 500:
            raise VenueUnavailableError(f"{method} {path}: HTTP {resp.status_code}")
        return resp

    @staticmethod
    def _json(resp: httpx.Response) -> Any:
        # числа Binance — рядки; якщо трапиться JSON-число з крапкою, воно одразу стане Decimal
        try:
            return json.loads(resp.content, parse_float=Decimal)
        except ValueError:
            return {}

    def _now(self) -> int:
        return self._clock.now_ns()

    def _symbol(self, instrument: str) -> str:
        spec = self._instruments.get(instrument)
        return spec.symbol_venue if spec is not None else _canon_to_venue_symbol(instrument)

    # ================================================================ порт ExecutionVenue

    def submit(self, req: OrderRequest) -> OrderAck:
        coid = req.client_order_id
        if req.otype != OrderType.MARKET:
            return OrderAck(client_order_id=coid, status=OrderStatus.REJECTED,
                            reject_code=RejectCode.VENUE_ERROR, ts_ns=self._now())
        spec = self._instruments.get(req.instrument)
        qty = floor_qty(req.qty, spec.step_size) if spec is not None else req.qty
        if qty <= 0:
            return OrderAck(client_order_id=coid, status=OrderStatus.REJECTED,
                            reject_code=RejectCode.ZERO_QTY, ts_ns=self._now())
        known = self._orders.get(coid)
        if known is not None and known.venue_order_id is not None:
            # біржа вже прийняла заявку з цим id — другої не надсилаємо (подвійного виконання немає)
            return OrderAck(client_order_id=coid, status=OrderStatus.REJECTED,
                            reject_code=RejectCode.DUPLICATE_CLIENT_ID, ts_ns=self._now())
        symbol = self._symbol(req.instrument)
        params = [
            ("symbol", symbol),
            ("side", "BUY" if req.side == Side.LONG else "SELL"),
            ("type", "MARKET"),
            ("quantity", dec_str(qty)),
            ("newClientOrderId", str(coid)),
            ("newOrderRespType", "RESULT"),
        ]
        if req.reduce_only:
            params.append(("reduceOnly", "true"))
        # трекер — ДО запиту: якщо відповідь загубиться (таймаут/5xx → VenueUnavailableError), заявка
        # у стані «невідомо» лишається під наглядом, і звіряння (query_order/reconcile) забере її виконання;
        # інакше маршрутизатор знав би про FILLED, а Fill до портфеля не дійшов би ніколи.
        tracked = _Tracked(req=req, symbol=symbol, venue_order_id=None, status=OrderStatus.NEW)
        self._orders[coid] = tracked
        resp = self._request("POST", ORDER_PATH, params)
        body = self._json(resp)
        if resp.status_code != 200:
            code = RejectCode.VENUE_ERROR
            if isinstance(body, dict) and isinstance(body.get("code"), int):
                code = _ERROR_CODES.get(body["code"], RejectCode.VENUE_ERROR)
                log.warning("testnet order %s rejected: code=%s msg=%s",
                            coid, body.get("code"), body.get("msg"))
            # дубль id після нашої ж спроби з невідомим результатом: оригінал є на біржі — стежимо далі;
            # в усіх інших випадках заявку точно не прийнято
            if not (code == RejectCode.DUPLICATE_CLIENT_ID and known is not None):
                del self._orders[coid]
            return OrderAck(client_order_id=coid, status=OrderStatus.REJECTED, reject_code=code,
                            ts_ns=self._now())
        ack = self._ack_from_order(coid, body)
        tracked.venue_order_id = ack.venue_order_id
        tracked.status = ack.status
        return ack

    def on_bar(self, bar: Candle) -> list[Fill]:
        """Для testnet бар — лише такт звіряння: опитати незавершені заявки і забрати нові виконання."""
        return self.reconcile()

    def cancel_all(self, instrument: str) -> None:
        resp = self._request("DELETE", ALL_OPEN_ORDERS_PATH, [("symbol", self._symbol(instrument))])
        if resp.status_code != 200:
            raise VenueUnavailableError(f"cancel_all {instrument}: HTTP {resp.status_code}")
        for tr in self._orders.values():
            if tr.req.instrument == instrument and tr.status in (OrderStatus.NEW, OrderStatus.PARTIAL):
                tr.status = OrderStatus.CANCELED

    # ================================================================ звіряння

    def query_order(self, client_order_id: UUID, instrument: str) -> OrderAck | None:
        """GET /fapi/v1/order за origClientOrderId. None — біржа такої заявки не знає."""
        symbol = self._symbol(instrument)
        resp = self._request("GET", ORDER_PATH, [("symbol", symbol),
                                                 ("origClientOrderId", str(client_order_id))])
        body = self._json(resp)
        tr = self._orders.get(client_order_id)
        if resp.status_code != 200:
            if isinstance(body, dict) and body.get("code") == _ORDER_DOES_NOT_EXIST:
                if tr is not None and tr.venue_order_id is None:
                    # заявка з невідомим результатом так і не дійшла до біржі: не опитувати її вічно
                    tr.status = OrderStatus.REJECTED
                return None
            raise VenueUnavailableError(f"query_order: HTTP {resp.status_code}")
        ack = self._ack_from_order(client_order_id, body)
        if tr is not None:
            tr.status = ack.status
            tr.venue_order_id = ack.venue_order_id
        return ack

    def fetch_fill(self, client_order_id: UUID) -> Fill | None:
        """Агрегувати власні угоди заявки (GET /fapi/v1/userTrades) в один Fill (VWAP, Σ комісій)."""
        tr = self._orders.get(client_order_id)
        if tr is None or tr.venue_order_id is None:
            return None
        resp = self._request("GET", USER_TRADES_PATH, [("symbol", tr.symbol), ("orderId", tr.venue_order_id)])
        if resp.status_code != 200:
            raise VenueUnavailableError(f"userTrades: HTTP {resp.status_code}")
        trades = self._json(resp)
        if not trades:
            return None
        qty = D0
        notional = D0
        fee = D0
        all_maker = True
        ts_ms = 0
        for t in trades:
            q = dec(str(t["qty"]))
            p = dec(str(t["price"]))
            qty += q
            notional += q * p
            fee += dec(str(t["commission"]))
            all_maker = all_maker and bool(t.get("maker", False))
            ts_ms = max(ts_ms, int(t["time"]))
        if qty <= 0:
            return None
        price = notional / qty
        spec = self._instruments.get(tr.req.instrument)
        if spec is not None:
            price = quantize_price(price, spec.tick_size)
        return Fill(client_order_id=client_order_id, venue_order_id=tr.venue_order_id,
                    instrument=tr.req.instrument, side=tr.req.side, qty=qty, price=price,
                    fee=max(fee, D0), liquidity=Liquidity.MAKER if all_maker else Liquidity.TAKER,
                    ts_fill_ns=ts_ms * NS_PER_MS)

    def reconcile(self) -> list[Fill]:
        """Оновити стан незавершених заявок; для щойно FILLED — один Fill на заявку."""
        fills: list[Fill] = []
        for coid, tr in self._orders.items():
            if tr.fill_emitted or tr.status in (OrderStatus.CANCELED, OrderStatus.REJECTED):
                continue
            if tr.status != OrderStatus.FILLED:
                self.query_order(coid, tr.req.instrument)
            if tr.status == OrderStatus.FILLED:
                fill = self.fetch_fill(coid)
                if fill is not None:
                    tr.fill_emitted = True
                    fills.append(fill)
        return fills

    def _ack_from_order(self, coid: UUID, body: Mapping[str, Any]) -> OrderAck:
        status = _STATUS.get(str(body.get("status", "")), OrderStatus.NEW)
        venue_id = body.get("orderId")
        upd = body.get("updateTime")
        ts = int(upd) * NS_PER_MS if isinstance(upd, int) else self._now()
        return OrderAck(client_order_id=coid, venue_order_id=None if venue_id is None else str(venue_id),
                        status=status, ts_ns=ts)
