"""Один крихітний MARKET-ордер на Binance USDⓈ-M Futures TESTNET (доведення повного циклу виконання).

Найменування: testnet_one_order.py
Призначення: фаза 6/[ЛЮДИНА] — підписаний POST /fapi/v1/order → FILLED → звіряння GET /fapi/v1/order;
екранограма виводу йде в додаток звіту (брифінг §3, питання 3). Реальних коштів немає за побудовою:
базова адреса проходить allowlist testnet-хостів (fuzzhelm.config.assert_testnet_url).
Автор: Андрій Жук, 2026.

Запуск (лише людиною, після реєстрації на testnet і заповнення .env):
    BINANCE_TESTNET_KEY=... BINANCE_TESTNET_SECRET=... \\
    uv run python scripts/testnet_one_order.py --confirm [--symbol BTCUSDT] [--side BUY] [--qty 0.002]

Без --confirm або без ключів скрипт нічого не надсилає. Ключі не друкуються.
"""

from __future__ import annotations

import argparse
import sys
from decimal import Decimal, InvalidOperation

import httpx

from fuzzhelm.config import Settings, host_of
from fuzzhelm.core.dto import OrderRequest
from fuzzhelm.core.enums import OrderType, Side
from fuzzhelm.core.money import dec
from fuzzhelm.execution.router import OrderRouter
from fuzzhelm.execution.testnet_venue import BinanceTestnetVenue
from fuzzhelm.infra.wallclock import RandomIdGenerator, SystemClock


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Place ONE tiny MARKET order on Binance Futures TESTNET")
    ap.add_argument("--symbol", default="BTCUSDT", help="символ біржі (напр. BTCUSDT)")
    ap.add_argument("--side", choices=("BUY", "SELL"), default="BUY")
    ap.add_argument("--qty", default="0.002", help="кількість у базовому активі (рядок, кратний stepSize)")
    ap.add_argument("--confirm", action="store_true", help="без цього прапорця ордер НЕ надсилається")
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    settings = Settings()                       # валідатор уже відкинув би не-testnet хост
    key, secret = settings.binance_testnet_key, settings.binance_testnet_secret
    if key is None or secret is None or not key.get_secret_value() or not secret.get_secret_value():
        print("BINANCE_TESTNET_KEY / BINANCE_TESTNET_SECRET не задані — ордер не надіслано.")
        return 2
    try:
        qty = dec(args.qty)
    except InvalidOperation:
        print(f"некоректна кількість: {args.qty!r}")
        return 2
    if not qty.is_finite():
        print(f"некоректна кількість: {args.qty!r}")
        return 2
    if qty <= Decimal(0):
        print("кількість має бути > 0")
        return 2
    print(f"venue host: {host_of(settings.venue_base_url)}  (allow-listed testnet)")
    print(f"order: {args.side} {args.qty} {args.symbol} MARKET")
    if not args.confirm:
        print("--confirm не передано — ордер НЕ надіслано (сухий прогін).")
        return 1

    clock = SystemClock()
    ids = RandomIdGenerator()
    with httpx.Client(timeout=10.0) as http:
        venue = BinanceTestnetVenue(settings, http, clock=clock)
        router = OrderRouter(venue, clock=clock)
        req = OrderRequest(
            client_order_id=ids.next_uuid(), instrument=args.symbol,
            side=Side.LONG if args.side == "BUY" else Side.SHORT, otype=OrderType.MARKET,
            qty=qty, decision_ref="manual:testnet_one_order", ts_created_ns=clock.now_ns(),
        )
        ack = router.submit(req)
        print(f"client_order_id: {ack.client_order_id}")
        print(f"venue_order_id:  {ack.venue_order_id}")
        print(f"status:          {ack.status.value}" + (f" ({ack.reject_code})" if ack.reject_code else ""))
        if ack.venue_order_id is None:
            return 3
        rec = venue.query_order(req.client_order_id, args.symbol)
        if rec is not None:
            print(f"reconciled:      {rec.status.value} (GET /fapi/v1/order)")
        for fill in venue.reconcile():
            print(f"fill:            {fill.qty} @ {fill.price}, fee {fill.fee} ({fill.liquidity.value})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
