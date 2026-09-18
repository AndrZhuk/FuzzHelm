"""Канонічні DTO (межа нормалізації): незмінні, без зайвих полів, гроші/ціни — лише Decimal.

Найменування: core/dto.py
Призначення: venue-JSON → ці об'єкти (ingest.normalize) → журнал, БД, рушій рішень.
Автор: Андрій Жук, 2026.

Час: *_ns — наносекунди від епохи UTC (int). `ts_event_ns` — час події за біржею,
`ts_ingest_ns` — час отримання нами. Ці дві величини НІКОЛИ не змішуються
(тест test_event_and_ingest_time_never_mixed).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from fuzzhelm.core.enums import (
    ContractType,
    Liquidity,
    OrderStatus,
    OrderType,
    Side,
    Src,
    Venue,
)

NonNegDec = Annotated[Decimal, Field(ge=0)]
PosDec = Annotated[Decimal, Field(gt=0)]
Ns = Annotated[int, Field(ge=0)]


class CanonicalModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=False)


# ---------------------------------------------------------------- довідник


class Instrument(CanonicalModel):
    venue: Venue
    symbol_venue: str                    # напр. "BTCUSDT" (Binance), "XBTUSD" (Kraken)
    symbol_canon: str                    # напр. "BTC-USDT-PERP"
    base_asset: str
    quote_asset: str
    contract_type: ContractType
    tick_size: PosDec
    step_size: PosDec
    min_notional: NonNegDec
    mmr: Decimal = Decimal("0.005")      # maintenance margin rate
    maint_amount: Decimal = Decimal(0)
    max_leverage: int = 3


# ---------------------------------------------------------------- ринкові події


class Candle(CanonicalModel):
    instrument: str                      # symbol_canon
    venue: Venue
    tf: str                              # "1m"
    open_time_ns: Ns
    close_time_ns: Ns
    o: PosDec
    h: PosDec
    l: PosDec
    c: PosDec
    volume: NonNegDec
    quote_volume: NonNegDec = Decimal(0)
    trades_count: Annotated[int, Field(ge=0)] = 0
    vwap: Decimal | None = None
    is_closed: bool
    is_synthetic: bool = False
    src: Src
    ts_event_ns: Ns
    ts_ingest_ns: Ns
    event_uid: str                       # hex BLAKE2b-128 від природного ключа

    @model_validator(mode="after")
    def _ohlc_consistent(self) -> Candle:
        # ті самі інваріанти, що й CHECK-обмеження таблиці candle
        if not (self.h >= self.l and self.h >= max(self.o, self.c) and self.l <= min(self.o, self.c)):
            raise ValueError(f"inconsistent OHLC o={self.o} h={self.h} l={self.l} c={self.c}")
        if self.vwap is not None and not (self.l <= self.vwap <= self.h):
            raise ValueError("vwap outside [l, h]")
        if self.close_time_ns < self.open_time_ns:
            raise ValueError("close_time < open_time")
        return self


class Trade(CanonicalModel):
    instrument: str
    venue: Venue
    agg_id: int                          # aggTrade id (детекція прогалин за послідовністю)
    first_trade_id: int | None = None
    last_trade_id: int | None = None
    price: PosDec
    qty: PosDec
    is_buyer_maker: bool
    ts_event_ns: Ns
    ts_ingest_ns: Ns
    event_uid: str


class BookLevel(CanonicalModel):
    price: PosDec
    qty: NonNegDec


class BookSnapshot(CanonicalModel):
    instrument: str
    venue: Venue
    bids: tuple[BookLevel, ...]          # за спаданням ціни
    asks: tuple[BookLevel, ...]          # за зростанням ціни
    last_update_id: int
    ts_event_ns: Ns
    ts_ingest_ns: Ns
    event_uid: str


class MarkPrice(CanonicalModel):
    instrument: str
    venue: Venue
    mark_price: PosDec
    index_price: Decimal | None = None
    funding_rate: Decimal
    next_funding_time_ns: Ns
    ts_event_ns: Ns
    ts_ingest_ns: Ns
    event_uid: str


MarketEvent = Candle | Trade | BookSnapshot | MarkPrice


# ---------------------------------------------------------------- виконання


class OrderRequest(CanonicalModel):
    client_order_id: UUID
    instrument: str
    side: Side                           # LONG = купівля, SHORT = продаж; FLAT заборонено
    otype: OrderType
    qty: PosDec                          # уже кратна step_size (ROUND_DOWN)
    stop_price: Decimal | None = None    # обов'язкова для STOP_MARKET
    reduce_only: bool = False
    decision_ref: str | None = None      # посилання на рішення ядра (FK decision_id у БД)
    ts_created_ns: Ns

    @model_validator(mode="after")
    def _check(self) -> OrderRequest:
        if self.side == Side.FLAT:
            raise ValueError("order side must be LONG or SHORT")
        if self.otype == OrderType.STOP_MARKET and self.stop_price is None:
            raise ValueError("STOP_MARKET requires stop_price")
        return self


class OrderAck(CanonicalModel):
    client_order_id: UUID
    venue_order_id: str | None = None
    status: OrderStatus
    reject_code: str | None = None
    ts_ns: Ns


class Fill(CanonicalModel):
    client_order_id: UUID
    venue_order_id: str | None = None
    instrument: str
    side: Side
    qty: PosDec
    price: PosDec
    fee: NonNegDec
    liquidity: Liquidity
    slippage_bps: Decimal = Decimal(0)
    ts_fill_ns: Ns
