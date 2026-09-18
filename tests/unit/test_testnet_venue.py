"""Виконання на Binance Futures TESTNET: підпис HMAC-SHA256, allowlist, звіряння.

Усі HTTP-виклики — через respx (жодного мережевого з'єднання)."""

from __future__ import annotations

import hashlib
import hmac
import logging
from decimal import Decimal
from urllib.parse import parse_qsl
from uuid import UUID

import httpx
import pytest
import respx
from pydantic import SecretStr

from fuzzhelm.config import Settings
from fuzzhelm.core.clock import FixedClock
from fuzzhelm.core.dto import Instrument, OrderRequest
from fuzzhelm.core.enums import ContractType, Liquidity, OrderStatus, OrderType, RejectCode, Side, Venue
from fuzzhelm.core.errors import ConfigValidationError, MainnetHostRejected
from fuzzhelm.execution.router import OrderRouter
from fuzzhelm.execution.testnet_venue import BinanceTestnetVenue, VenueUnavailableError

BASE = "https://testnet.binancefuture.com"
KEY = "test-api-key-PUBLIC-part"
SECRET = "super-secret-hmac-value-0123456789"
NOW_NS = 1_758_153_600_123_456_789
NOW_MS = NOW_NS // 1_000_000
COID = UUID("12345678-1234-4234-8234-1234567890ab")
INST = Instrument(
    venue=Venue.BINANCE_TESTNET, symbol_venue="BTCUSDT", symbol_canon="BTC-USDT-PERP", base_asset="BTC",
    quote_asset="USDT", contract_type=ContractType.PERP, tick_size=Decimal("0.1"),
    step_size=Decimal("0.001"), min_notional=Decimal("100"),
)


def settings(url: str = BASE, key: str | None = KEY, secret: str | None = SECRET) -> Settings:
    # model_construct — в обхід валідаторів Settings: перевіряємо, що venue має ВЛАСНИЙ захист
    return Settings.model_construct(
        venue_base_url=url,
        binance_testnet_key=None if key is None else SecretStr(key),
        binance_testnet_secret=None if secret is None else SecretStr(secret),
    )


def order(qty: str = "0.0025", side: Side = Side.LONG, otype: OrderType = OrderType.MARKET) -> OrderRequest:
    return OrderRequest(client_order_id=COID, instrument="BTC-USDT-PERP", side=side, otype=otype,
                        qty=Decimal(qty), ts_created_ns=NOW_NS,
                        stop_price=Decimal("1") if otype == OrderType.STOP_MARKET else None)


def venue(http: httpx.Client) -> BinanceTestnetVenue:
    return BinanceTestnetVenue(settings(), http, clock=FixedClock(NOW_NS), instruments=[INST])


ORDER_FILLED = {"orderId": 4001, "clientOrderId": str(COID), "status": "FILLED", "executedQty": "0.002",
                "avgPrice": "60000.10", "updateTime": NOW_MS + 5}


def test_order_is_signed_with_hmac_sha256_over_exact_query() -> None:
    with respx.mock(base_url=BASE, assert_all_called=True) as mock, httpx.Client() as http:
        route = mock.post("/fapi/v1/order").respond(200, json=ORDER_FILLED)
        ack = venue(http).submit(order())
    req = route.calls.last.request
    assert req.url.host == "testnet.binancefuture.com" and req.url.path == "/fapi/v1/order"
    assert req.headers["X-MBX-APIKEY"] == KEY
    query = req.url.query.decode()
    unsigned, sig = query.rsplit("&signature=", 1)
    assert sig == hmac.new(SECRET.encode(), unsigned.encode(), hashlib.sha256).hexdigest()
    params = dict(parse_qsl(unsigned))
    assert params == {
        "symbol": "BTCUSDT", "side": "BUY", "type": "MARKET",
        "quantity": "0.002",                                  # 0.0025 → floor до step 0.001
        "newClientOrderId": str(COID), "newOrderRespType": "RESULT",
        "recvWindow": "5000", "timestamp": str(NOW_MS),
    }
    assert ack.status == OrderStatus.FILLED and ack.venue_order_id == "4001"
    assert ack.client_order_id == COID


def test_secret_never_leaves_process_or_logs(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    with respx.mock(base_url=BASE) as mock, httpx.Client() as http:
        body = {"code": -2019, "msg": "Margin is insufficient."}
        route = mock.post("/fapi/v1/order").respond(400, json=body)
        v = venue(http)
        ack = v.submit(order())
    req = route.calls.last.request
    assert SECRET not in str(req.url) and all(SECRET not in val for val in req.headers.values())
    assert SECRET not in repr(v) and KEY not in repr(v)
    assert SECRET not in caplog.text and KEY not in caplog.text
    assert ack.status == OrderStatus.REJECTED and ack.reject_code == RejectCode.INSUFFICIENT_MARGIN


@pytest.mark.parametrize("url", ["https://fapi.binance.com", "https://testnet.binancefuture.com.evil.example"])
def test_non_testnet_base_url_refused(url: str) -> None:
    with httpx.Client() as http, pytest.raises(MainnetHostRejected):
        BinanceTestnetVenue(settings(url=url), http, clock=FixedClock(NOW_NS))


def test_missing_keys_refused() -> None:
    with httpx.Client() as http, pytest.raises(ConfigValidationError):
        BinanceTestnetVenue(settings(key=None), http, clock=FixedClock(NOW_NS))


def test_error_codes_mapped_and_5xx_raises_unknown_state() -> None:
    with respx.mock(base_url=BASE) as mock, httpx.Client() as http:
        v = venue(http)
        mock.post("/fapi/v1/order").respond(400, json={"code": -4164, "msg": "notional too small"})
        assert v.submit(order()).reject_code == RejectCode.BELOW_MIN_NOTIONAL
        mock.post("/fapi/v1/order").respond(400, json={"code": -9999, "msg": "?"})
        assert v.submit(order()).reject_code == RejectCode.VENUE_ERROR
        mock.post("/fapi/v1/order").respond(503, text="Service Unavailable")
        with pytest.raises(VenueUnavailableError):
            v.submit(order())


def test_stop_market_not_sent_to_testnet() -> None:
    with respx.mock(base_url=BASE, assert_all_mocked=True) as mock, httpx.Client() as http:
        ack = venue(http).submit(order(otype=OrderType.STOP_MARKET))
        assert mock.calls.call_count == 0
    assert ack.status == OrderStatus.REJECTED and ack.reject_code == RejectCode.VENUE_ERROR


def test_reconcile_queries_status_and_emits_single_fill_from_user_trades() -> None:
    new_resp = {**ORDER_FILLED, "status": "NEW", "executedQty": "0"}
    trades = [
        {"orderId": 4001, "price": "60000.0", "qty": "0.001", "commission": "0.024", "maker": False,
         "time": NOW_MS + 7},
        {"orderId": 4001, "price": "60000.2", "qty": "0.001", "commission": "0.02400008", "maker": False,
         "time": NOW_MS + 9},
    ]
    with respx.mock(base_url=BASE) as mock, httpx.Client() as http:
        v = venue(http)
        mock.post("/fapi/v1/order").respond(200, json=new_resp)
        get_order = mock.get("/fapi/v1/order").respond(200, json=ORDER_FILLED)
        user_trades = mock.get("/fapi/v1/userTrades").respond(200, json=trades)
        assert v.submit(order()).status == OrderStatus.NEW
        fills = v.on_bar(None)  # type: ignore[arg-type]   # для testnet бар — лише такт звіряння
        assert v.reconcile() == []                                    # другий такт не дублює виконання
    q = dict(parse_qsl(get_order.calls.last.request.url.query.decode()))
    assert q["origClientOrderId"] == str(COID) and q["symbol"] == "BTCUSDT" and "signature" in q
    assert dict(parse_qsl(user_trades.calls.last.request.url.query.decode()))["orderId"] == "4001"
    (f,) = fills
    assert f.qty == Decimal("0.002") and f.price == Decimal("60000.1")    # VWAP, квантований до tick
    assert f.fee == Decimal("0.04800008") and f.liquidity == Liquidity.TAKER
    assert f.ts_fill_ns == (NOW_MS + 9) * 1_000_000 and f.venue_order_id == "4001"


def test_query_unknown_order_returns_none_and_router_reconciles_after_timeout() -> None:
    trades = [{"orderId": 4001, "price": "60000.1", "qty": "0.002", "commission": "0.048", "maker": False,
               "time": NOW_MS + 7}]
    with respx.mock(base_url=BASE) as mock, httpx.Client() as http:
        v = venue(http)
        mock.get("/fapi/v1/order").respond(400, json={"code": -2013, "msg": "Order does not exist."})
        assert v.query_order(COID, "BTC-USDT-PERP") is None
        # таймаут на POST: стан невідомий → маршрутизатор питає біржу і бачить, що ордер таки виконано
        mock.post("/fapi/v1/order").mock(side_effect=httpx.ReadTimeout("timeout"))
        mock.get("/fapi/v1/order").respond(200, json=ORDER_FILLED)
        mock.get("/fapi/v1/userTrades").respond(200, json=trades)
        ack = OrderRouter(v).submit(order())
        # і виконання цієї заявки доходить до обліку рівно один раз (інакше позиція на біржі є, а в
        # портфелі — ні)
        fills = v.on_bar(None)  # type: ignore[arg-type]
        assert v.reconcile() == []
    assert ack.status == OrderStatus.FILLED and ack.venue_order_id == "4001"
    (f,) = fills
    assert f.client_order_id == COID and f.qty == Decimal("0.002") and f.venue_order_id == "4001"


def test_order_lost_before_exchange_is_not_polled_forever_and_can_be_retried() -> None:
    with respx.mock(base_url=BASE) as mock, httpx.Client() as http:
        v = venue(http)
        post = mock.post("/fapi/v1/order").mock(side_effect=httpx.ConnectTimeout("no route"))
        missing = {"code": -2013, "msg": "Order does not exist."}
        get_order = mock.get("/fapi/v1/order").respond(400, json=missing)
        router = OrderRouter(v)
        lost = router.submit(order())
        assert lost.status == OrderStatus.REJECTED and lost.reject_code == RejectCode.VENUE_ERROR
        polls = get_order.call_count
        assert v.reconcile() == [] and v.reconcile() == []
        assert get_order.call_count == polls                  # біржа сказала «немає» — більше не питаємо
        # повтор з тим самим id дозволено, і він проходить
        post.mock(side_effect=None, return_value=httpx.Response(200, json=ORDER_FILLED))
        again = router.submit(order())
    assert again.status == OrderStatus.FILLED and again.venue_order_id == "4001"


def test_duplicate_submit_of_accepted_order_is_not_resent() -> None:
    with respx.mock(base_url=BASE) as mock, httpx.Client() as http:
        v = venue(http)
        route = mock.post("/fapi/v1/order").respond(200, json=ORDER_FILLED)
        assert v.submit(order()).status == OrderStatus.FILLED
        dup = v.submit(order())
    assert route.call_count == 1
    assert dup.status == OrderStatus.REJECTED and dup.reject_code == RejectCode.DUPLICATE_CLIENT_ID


def test_cancel_all_uses_signed_delete() -> None:
    with respx.mock(base_url=BASE) as mock, httpx.Client() as http:
        route = mock.delete("/fapi/v1/allOpenOrders").respond(200, json={"code": 200, "msg": "ok"})
        venue(http).cancel_all("BTC-USDT-PERP")
    q = dict(parse_qsl(route.calls.last.request.url.query.decode()))
    assert q["symbol"] == "BTCUSDT" and "signature" in q and "timestamp" in q
