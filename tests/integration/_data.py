"""Побудова тестових даних storage: інструмент, свічки, прогін, рішення (детерміновано, без випадковості).

Найменування: tests/integration/_data.py
Автор: Андрій Жук, 2026.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from fuzzhelm.core.clock import NS_PER_MIN, SeededIdGenerator
from fuzzhelm.core.digest import event_uid
from fuzzhelm.core.dto import Candle, Instrument
from fuzzhelm.core.enums import ContractType, EngineKind, RunKind, Src, Stream, Venue
from fuzzhelm.storage.repositories.decision import DecisionRecord, DecisionRepo
from fuzzhelm.storage.repositories.instrument import InstrumentRepo
from fuzzhelm.storage.repositories.run import RunRepo

T0_NS = 1_757_000_000_000 * 1_000_000 // NS_PER_MIN * NS_PER_MIN  # межа хвилини, вересень 2025
SYMBOL = "BTC-USDT-PERP"
IDS = SeededIdGenerator(20260918, b"storage-tests")

BTC = Instrument(
    venue=Venue.BINANCE_USDM,
    symbol_venue="BTCUSDT",
    symbol_canon=SYMBOL,
    base_asset="BTC",
    quote_asset="USDT",
    contract_type=ContractType.PERP,
    tick_size=Decimal("0.10"),
    step_size=Decimal("0.001"),
    min_notional=Decimal("100"),
    mmr=Decimal("0.004"),
    maint_amount=Decimal("0"),
    max_leverage=3,
)


def candle(
    i: int,
    *,
    src: Src = Src.REST,
    closed: bool = True,
    close_px: Decimal | None = None,
    symbol: str = SYMBOL,
    base: Decimal = Decimal("60000.00"),
) -> Candle:
    """i-та хвилинна свічка детермінованої «пилки»; ціни кратні tick 0.10."""
    o = base + Decimal(i % 97) * Decimal("0.10")
    c = close_px if close_px is not None else o + Decimal((i * 7) % 13 - 6) * Decimal("0.10")
    h = max(o, c) + Decimal("1.20")
    lo = min(o, c) - Decimal("0.90")
    t = T0_NS + i * NS_PER_MIN
    return Candle(
        instrument=symbol,
        venue=Venue.BINANCE_USDM,
        tf="1m",
        open_time_ns=t,
        close_time_ns=t + NS_PER_MIN - 1_000_000,
        o=o,
        h=h,
        l=lo,
        c=c,
        volume=Decimal("12.345") + Decimal(i % 5),
        quote_volume=Decimal("740700.5"),
        trades_count=100 + i % 50,
        vwap=(h + lo) / 2,
        is_closed=closed,
        src=src,
        ts_event_ns=t + NS_PER_MIN - 1_000_000,
        ts_ingest_ns=t + NS_PER_MIN,
        event_uid=event_uid(Venue.BINANCE_USDM.value, Stream.KLINES.value, symbol, "1m", t),
    )


async def add_instrument(s: AsyncSession, inst: Instrument = BTC) -> int:
    return await InstrumentRepo(s).upsert(inst)


async def add_run(
    s: AsyncSession, instrument_id: int | None = None, *, seed: int = 1, git_sha: str | None = "a" * 40
) -> UUID:
    run_id = IDS.next_uuid()
    await RunRepo(s).create(
        run_id,
        kind=RunKind.BACKTEST,
        config={"engine": "mamdani", "kappa_min": 0.35},
        config_hash=bytes(32),
        dataset_hash=bytes([seed % 256]) * 32,
        seed=seed,
        engine=EngineKind.MAMDANI,
        git_sha=git_sha,
        instrument_id=instrument_id,
        tf="1m",
        ts_from_ns=T0_NS,
        ts_to_ns=T0_NS + 60 * NS_PER_MIN,
    )
    return run_id


def trace_dict(open_time_ns: int, rule_id: str = "R07") -> dict[str, Any]:
    """Форма decision.trace.DecisionTrace.to_dict() (лише ключі, які читає DecisionRecord.from_trace)."""
    return {
        "open_time_ns": open_time_ns,
        "inputs": {"T": 0.6123456789, "R": -0.25, "V": 0.5},
        "detector_outputs": [
            {
                "name": "ema_slope",
                "group": "TREND",
                "s": 0.7,
                "c": 0.9,
                "weight": 1.0,
                "features": {"slope": 1.5},
            },
            {
                "name": "vol_regime",
                "group": "CONTEXT",
                "s": 0.0,
                "c": 1.0,
                "weight": 1.0,
                "features": {"V": 0.5},
            },
        ],
        "memberships": {"T": {"UP": 0.71, "ZERO": 0.29}, "R": {"NO_PRESSURE": 1.0}, "V": {"MID": 1.0}},
        "fired_rules": [
            {
                "rule_id": rule_id,
                "alpha": 0.62,
                "consequent": "SL",
                "antecedent": {"T": "UP", "R": "NO_PRESSURE"},
            },
            {"rule_id": "R12", "alpha": 0.29, "consequent": "HOLD", "antecedent": {"T": "ZERO"}},
        ],
        "u_raw": 0.4412345,
        "kappa": 0.8765432,
        "u_final": 0.38675,
        "agreement": {"A_g": 0.8100001, "H": 0.19},
    }


async def add_decision(
    s: AsyncSession, run_id: UUID, instrument_id: int, i: int = 0, rule_id: str = "R07"
) -> int:
    rec = DecisionRecord.from_trace(
        trace_dict(T0_NS + i * NS_PER_MIN, rule_id),
        run_id=run_id,
        instrument_id=instrument_id,
        target_side=1,
        target_qty=Decimal("0.012"),
        binding_constraint="ATR_RISK",
        stop_price=Decimal("59000.0"),
    )
    return await DecisionRepo(s).insert(rec)
