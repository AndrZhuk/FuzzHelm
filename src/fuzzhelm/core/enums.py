"""Перелічувані типи домену FuzzHelm.

Найменування: core/enums.py
Призначення: єдине джерело значень, що потрапляють у БД (CHECK-обмеження), API і журнал.
Автор: Андрій Жук, 2026.
"""

from __future__ import annotations

from enum import IntEnum, StrEnum


class Venue(StrEnum):
    BINANCE_USDM = "BINANCE_USDM"      # публічні read-only дані Binance USDⓈ-M Futures
    BINANCE_TESTNET = "BINANCE_TESTNET"  # testnet-виконання (єдине місце, де існують ордери поза симуляцією)
    KRAKEN = "KRAKEN"                  # лише для читання старих рядків instrument
    PAPER = "PAPER"                    # симульоване виконання


class ContractType(StrEnum):
    SPOT = "SPOT"
    PERP = "PERP"


class Src(IntEnum):
    """Джерело свічки; менше значення = пріоритетніше при upsert (EXCLUDED.src <= candle.src)."""

    WS = 1
    REST = 2
    REPLAY = 3


class Stream(StrEnum):
    KLINES = "klines"
    TRADES = "trades"
    DEPTH = "depth"
    MARK = "mark"


class Side(IntEnum):
    SHORT = -1
    FLAT = 0
    LONG = 1


class OrderType(StrEnum):
    MARKET = "MARKET"
    STOP_MARKET = "STOP_MARKET"


class OrderStatus(StrEnum):
    NEW = "NEW"
    PARTIAL = "PARTIAL"
    FILLED = "FILLED"
    REJECTED = "REJECTED"
    CANCELED = "CANCELED"


class Liquidity(StrEnum):
    MAKER = "maker"
    TAKER = "taker"


class RejectCode(StrEnum):
    BELOW_MIN_NOTIONAL = "BELOW_MIN_NOTIONAL"
    ZERO_QTY = "ZERO_QTY"
    RISK_VETO = "RISK_VETO"
    HALTED = "HALTED"
    REDUCE_ONLY = "REDUCE_ONLY"
    INSUFFICIENT_MARGIN = "INSUFFICIENT_MARGIN"
    DUPLICATE_CLIENT_ID = "DUPLICATE_CLIENT_ID"
    VENUE_ERROR = "VENUE_ERROR"


class RiskState(StrEnum):
    NORMAL = "NORMAL"
    WARNING = "WARNING"
    COOLDOWN = "COOLDOWN"
    HALTED = "HALTED"


class VerdictKind(StrEnum):
    ALLOW = "ALLOW"
    SHRINK = "SHRINK"
    VETO = "VETO"


class ExitReason(StrEnum):
    SIGNAL = "SIGNAL"
    STOP = "STOP"
    TP = "TP"
    RISK_VETO = "RISK_VETO"
    HALT = "HALT"
    LIQUIDATION = "LIQUIDATION"


class RunKind(StrEnum):
    BACKTEST = "backtest"
    PAPER = "paper"
    REPLAY = "replay"
    GRID_CELL = "grid_cell"
    TESTNET = "testnet"


class RunStatus(StrEnum):
    RUNNING = "RUNNING"
    DONE = "DONE"
    FAILED = "FAILED"


class EngineKind(StrEnum):
    MAMDANI = "mamdani"
    LINEAR = "linear"


class Role(StrEnum):
    OPERATOR = "operator"
    ANALYST = "analyst"
    AUDITOR = "auditor"
    ADMIN = "admin"


class GapStatus(StrEnum):
    OPEN = "OPEN"
    FILLING = "FILLING"
    FILLED = "FILLED"
    PARTIAL = "PARTIAL"
    UNFILLABLE = "UNFILLABLE"


class GapDetectorKind(StrEnum):
    SEQ = "seq"
    BUCKET_COUNT = "bucket_count"
    TIME = "time"
