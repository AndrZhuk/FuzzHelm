"""«Якість даних»: інваріанти свічки і скор якості Q.

Найменування: tests/unit/test_quality.py
Призначення: перевірити інваріанти на СПРАВЖНІХ даних (fixtures/rest/binance_klines.json.gz — 3000
закритих 1m-барів BTCUSDT), формули §5.17 — на контрольних значеннях з вагами
config/dq_weights.yaml. Property-тест test_dq_score_in_unit_interval — у tests/property/.
Автор: Андрій Жук, 2026.
"""

from __future__ import annotations

import gzip
import math
from decimal import Decimal
from pathlib import Path
from typing import Any

import numpy as np
import orjson
import pytest

from fuzzhelm.core.dto import BookLevel, BookSnapshot, Candle, Instrument, Trade
from fuzzhelm.core.enums import Src, Venue
from fuzzhelm.features.convert import Bar, bar_from_candle
from fuzzhelm.ingest.normalize import kline_uid, normalize_exchange_info, normalize_rest_klines, trade_uid
from fuzzhelm.quality import invariants as inv
from fuzzhelm.quality.dq_score import (
    DqAccumulator,
    DqInputs,
    completeness,
    continuity,
    dq_score,
    load_dq_weights,
    merged_length_ns,
    timeliness,
    validity,
)
from fuzzhelm.quality.health import PipelineHealth

ROOT = Path(__file__).resolve().parents[2]
REST = ROOT / "fixtures" / "rest"
ROWS: list[list[Any]] = orjson.loads(gzip.decompress((REST / "binance_klines.json.gz").read_bytes()))
NOW_MS = ROWS[-1][6] + 1_000
INSTR: Instrument = normalize_exchange_info(orjson.loads((REST / "exchange_info.json").read_bytes()),
                                            ["BTCUSDT"])["BTCUSDT"]
CANDLES = normalize_rest_klines(ROWS, INSTR, NOW_MS * 1_000_000, server_time_ms=NOW_MS)
BARS: list[Bar] = [bar_from_candle(c) for c in CANDLES]
SEED = 20260918
MIN_NS = 60_000_000_000


def _candle(**kw: Any) -> Candle:
    base: dict[str, Any] = dict(
        instrument="BTC-USDT-PERP", venue=Venue.BINANCE_USDM, tf="1m",
        open_time_ns=ROWS[0][0] * 1_000_000, close_time_ns=ROWS[0][0] * 1_000_000 + MIN_NS - 1_000_000,
        o=Decimal("81015.40"), h=Decimal("81061.10"), l=Decimal("81015.40"), c=Decimal("81058.70"),
        volume=Decimal("75.128"), quote_volume=Decimal("6088272.65380"), trades_count=1791,
        is_closed=True, src=Src.WS, ts_event_ns=ROWS[0][6] * 1_000_000, ts_ingest_ns=ROWS[0][6] * 1_000_000,
        event_uid=kline_uid(Venue.BINANCE_USDM, "BTC-USDT-PERP", "1m", ROWS[0][0] * 1_000_000))
    base.update(kw)
    return Candle(**base)


# ================================================================ інваріанти


def test_high_below_close_rejected() -> None:
    good = _candle()
    assert inv.check_candle(good, INSTR) == []
    bad_fields = good.model_dump() | {"h": Decimal("81050.00"), "c": Decimal("81058.70")}   # h < c
    # шар 1: канонічний DTO не дає навіть сконструювати таку свічку (ті самі CHECK, що в таблиці candle)
    with pytest.raises(ValueError, match="inconsistent OHLC"):
        Candle(**bad_fields)
    # шар 2: подія, що обійшла валідацію (рядок БД / model_construct), ловиться інваріантами якості
    bypass = Candle.model_construct(**bad_fields)
    violations = inv.check_candle(bypass, INSTR)
    assert inv.HIGH_BELOW_MAX_OC in violations
    assert inv.LOW_ABOVE_MIN_OC not in violations           # l = 81015.40 ≤ min(o, c) — не порушено
    assert not inv.is_valid_candle(bypass, INSTR)
    # h нижче за close, але вище за open: саме close видає порушення (контроль, що перевірка — max(o,c))
    only_close = Candle.model_construct(**(good.model_dump() | {"h": Decimal("81058.60")}))
    assert inv.check_candle(only_close, INSTR) == [inv.HIGH_BELOW_MAX_OC]


def test_price_not_multiple_of_tick_rejected() -> None:
    assert INSTR.tick_size == Decimal("0.10")
    off = _candle(o=Decimal("81015.45"), l=Decimal("81015.40"))       # 81015.45 / 0.10 не ціле
    assert off.o == Decimal("81015.45")                                # DTO це пропускає: tick не його знання
    assert inv.check_candle(off, INSTR) == [f"{inv.PRICE_OFF_TICK}:o"]
    # усі чотири ціни перевіряються незалежно; обсяг — на кратність step_size = 0.001
    many = _candle(h=Decimal("81061.11"), c=Decimal("81058.75"), volume=Decimal("75.1285"),
                   quote_volume=Decimal("6088272.65380"))
    assert set(inv.check_candle(many, INSTR)) == {f"{inv.PRICE_OFF_TICK}:h", f"{inv.PRICE_OFF_TICK}:c",
                                                  f"{inv.QTY_OFF_STEP}:volume"}
    # без специфікації інструмента кратність не перевіряється (лише структурні інваріанти)
    assert inv.check_candle(off, None) == []
    # і та сама перевірка для угод
    t = Trade(instrument="BTC-USDT-PERP", venue=Venue.BINANCE_USDM, agg_id=1, price=Decimal("81015.45"),
              qty=Decimal("0.005"), is_buyer_maker=True, ts_event_ns=1, ts_ingest_ns=2,
              event_uid=trade_uid(Venue.BINANCE_USDM, "BTC-USDT-PERP", 1))
    assert inv.check_trade(t, INSTR) == [f"{inv.PRICE_OFF_TICK}:price"]


def test_real_klines_satisfy_all_invariants() -> None:
    """Негативний контроль: 3000 справжніх барів Binance не дають жодного хибного порушення."""
    assert len(CANDLES) == 3000
    assert [c.open_time_ns for c in CANDLES if inv.check_candle(c, INSTR)] == []


def test_quote_volume_bounds_and_time_grid() -> None:
    c = _candle()
    # qv ∉ [v·l, v·h] — неможливо для суми pᵢ·qᵢ з pᵢ ∈ [l, h]
    bad_qv = Candle.model_construct(**(c.model_dump() | {"quote_volume": Decimal("1")}))
    assert inv.QUOTE_VOLUME_OUT_OF_RANGE in inv.check_candle(bad_qv)
    shifted = Candle.model_construct(**(c.model_dump() | {"open_time_ns": c.open_time_ns + 1}))
    assert {inv.OPEN_TIME_UNALIGNED, inv.CLOSE_TIME_MISMATCH} <= set(inv.check_candle(shifted))


def test_book_invariants() -> None:
    lv = BookLevel
    ok = BookSnapshot(instrument="BTC-USDT-PERP", venue=Venue.BINANCE_USDM,
                      bids=(lv(price=Decimal("100.0"), qty=Decimal(1)),
                            lv(price=Decimal("99.9"), qty=Decimal(1))),
                      asks=(lv(price=Decimal("100.1"), qty=Decimal(1)),), last_update_id=1, ts_event_ns=1,
                      ts_ingest_ns=1, event_uid="x")
    assert inv.check_book(ok, INSTR) == []
    crossed = ok.model_copy(update={"asks": (lv(price=Decimal("100.0"), qty=Decimal(1)),)})
    assert inv.BOOK_CROSSED in inv.check_book(crossed, INSTR)


# ================================================================ Q


def test_perfect_hour_scores_one() -> None:
    """«Ідеальна» година: 60 з 60 кошиків, 0 невалідних, 0 с прогалин, лаг p95 = 0 мс.

    Лише за нульового лагу timeliness = exp(0) = 1; будь-який реальний лаг > 0 дає Q < 1 (перевірено нижче).
    """
    w = load_dq_weights()
    perfect = DqInputs(expected_buckets=60, observed_buckets=60, total_count=60, invalid_count=0,
                       gap_seconds=0.0, lag_p95_ms=0.0)
    s = dq_score(perfect, w)
    assert (s.completeness, s.validity, s.timeliness, s.continuity) == (1.0, 1.0, 1.0, 1.0)
    assert math.isclose(s.score, 1.0, abs_tol=1e-12)
    # і кожна окрема вада робить години неідеальною рівно на свою вагу×дефект
    lag = dq_score(DqInputs(60, 60, 60, lag_p95_ms=150.0), w)
    assert math.isclose(lag.score, 1.0 - w[2] * (1.0 - math.exp(-0.15)), rel_tol=1e-12)
    miss = dq_score(DqInputs(60, 57, 57), w)
    assert math.isclose(miss.score, 1.0 - w[0] * 3 / 60, rel_tol=1e-12)
    gap = dq_score(DqInputs(60, 60, 60, gap_seconds=180.0), w)
    assert math.isclose(gap.score, 1.0 - w[3] * 180 / 3600, rel_tol=1e-12)
    bad = dq_score(DqInputs(60, 60, 60, invalid_count=3), w)
    assert math.isclose(bad.score, 1.0 - w[1] * 3 / 60, rel_tol=1e-12)


def test_timeliness_decays_exponentially() -> None:
    tau = 1000.0
    assert timeliness(0.0) == 1.0
    assert math.isclose(timeliness(tau), math.exp(-1), rel_tol=1e-15)
    assert math.isclose(timeliness(tau * math.log(2)), 0.5, rel_tol=1e-12)     # «напіврозпад» τ₀·ln2
    lags = np.linspace(0.0, 5000.0, 51)
    t = np.array([timeliness(x) for x in lags])
    assert np.all(np.diff(t) < 0)                                              # строго спадає
    # лінійність логарифма: ln T(lag) = −lag/τ₀ (саме експонента, а не, напр., гіпербола 1/(1+lag))
    np.testing.assert_allclose(np.log(t), -lags / tau, rtol=0, atol=1e-12)
    # функціональне рівняння експоненти: T(a + b) = T(a)·T(b)
    for a, b in [(100.0, 250.0), (1000.0, 1000.0), (37.5, 4200.0)]:
        assert math.isclose(timeliness(a + b), timeliness(a) * timeliness(b), rel_tol=1e-12)
    assert timeliness(-50.0) == 1.0                    # від'ємний лаг (зсув годинника) не «кращий» за 0
    assert math.isclose(timeliness(500.0, tau0_ms=500.0), math.exp(-1), rel_tol=1e-15)


def test_dq_component_conventions() -> None:
    assert completeness(0, 0) == 1.0 and completeness(61, 60) == 1.0 and completeness(30, 60) == 0.5
    assert validity(0, 0) == 1.0 and validity(110, 100) == 0.0 and validity(25, 100) == 0.75
    assert continuity(3600.0) == 0.0 and continuity(7200.0) == 0.0 and continuity(0.0) == 1.0
    with pytest.raises(ValueError):
        dq_score(DqInputs(60, 60, 60), (0.5, 0.5, 0.5, 0.5))
    with pytest.raises(ValueError):
        dq_score(DqInputs(60, 60, 60), (1.2, -0.2, 0.0, 0.0))
    # NaN не стає «ідеальною» компонентою (max(0.0, nan) у Python — 0.0): лише явна помилка
    with pytest.raises(ValueError, match="NaN"):
        timeliness(math.nan)
    with pytest.raises(ValueError, match="NaN"):
        continuity(math.nan)
    with pytest.raises(ValueError, match="NaN"):
        dq_score(DqInputs(60, 60, 60, gap_seconds=math.nan), load_dq_weights())
    assert timeliness(math.inf) == 0.0 and continuity(math.inf) == 0.0


def test_dq_accumulator_merges_overlapping_gaps() -> None:
    assert merged_length_ns([(0, 10), (5, 20), (30, 40), (40, 45)]) == 35
    acc = DqAccumulator(window_start_ns=0, window_ns=3_600 * 10**9)
    for m in range(58):
        acc.add_bucket(m * MIN_NS)
        acc.add_checked()
    acc.add_checked(invalid=True)
    acc.add_checked(invalid=True)
    acc.add_gap(58 * MIN_NS, 60 * MIN_NS)
    acc.add_gap(59 * MIN_NS, 61 * MIN_NS)                 # перекриття + вихід за межу години
    for x in (100.0, 110.0, 120.0):
        acc.add_lag_ms(x)
    i = acc.inputs(expected_buckets=60)
    assert (i.observed_buckets, i.total_count, i.invalid_count) == (58, 60, 2)
    assert i.gap_seconds == 120.0
    assert math.isclose(i.lag_p95_ms, 119.0)





# ================================================================ health


def test_pipeline_health_snapshot() -> None:
    h = PipelineHealth(lag_window=3)
    for k, lag in enumerate((100, 200, 300, 400)):
        h.on_frame("market", 10**9 + k)
        h.on_event("trade", 10**9, 10**9 + lag * 1_000_000)
    h.on_event("trade", 10**9, 10**9 - 5_000_000)      # від'ємний лаг — у p95 як 0
    h.on_gap(1, "OPEN")
    h.on_gap(1, "FILLED")
    h.on_gap(2, "OPEN")
    h.on_disconnect("HEARTBEAT_TIMEOUT", watchdog=True)
    s = h.snapshot()
    assert s.frames == 4 and s.events_by_kind == {"trade": 5}
    assert s.lag_p95_ms is not None and math.isclose(s.lag_p95_ms, float(np.percentile([300, 400, 0], 95)))
    assert s.gaps_opened == 2 and s.gaps_open == 1 and s.gaps_by_status == {"FILLED": 1, "OPEN": 1}
    assert s.reconnects == 1 and s.watchdog_fires == 1
    assert s.as_dict()["frames"] == 4
