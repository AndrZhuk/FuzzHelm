"""Група C брифінгу (§10): крос-звірка Binance BTC-USDT-PERP ↔ Kraken BTC-USD-SPOT, поріг 50 б.п.

Найменування: tests/unit/test_crosscheck.py
Призначення: точна межа порогу на синтетичних свічках + звірка на СПРАВЖНІХ записаних даних двох бірж.
Автор: Андрій Жук, 2026.
"""

from __future__ import annotations

import gzip
from decimal import Decimal, localcontext
from pathlib import Path
from typing import Any

import orjson
import pytest

from fuzzhelm.core.dto import Candle
from fuzzhelm.core.enums import Src, Venue
from fuzzhelm.ingest.crosscheck import crosscheck, divergence_bps
from fuzzhelm.ingest.normalize import kline_uid, normalize_kraken_ohlc, normalize_rest_klines
from fuzzhelm.ingest.symbols import BTC_USD_SPOT, BTC_USDT_PERP, CROSSCHECK_PAIRS

REST = Path(__file__).resolve().parents[2] / "fixtures" / "rest"
T0 = 1_789_716_300_000_000_000
MIN = 60_000_000_000
INGEST_NS = 1_789_759_600_000_000_000


def _candle(inst: str, venue: Venue, i: int, close: str, *, closed: bool = True) -> Candle:
    c = Decimal(close)
    open_ns = T0 + i * MIN
    return Candle(instrument=inst, venue=venue, tf="1m", open_time_ns=open_ns,
                  close_time_ns=open_ns + MIN - 10**6, o=c, h=c, l=c, c=c, volume=Decimal("1"),
                  is_closed=closed, src=Src.REST,
                  ts_event_ns=open_ns + MIN - 10**6, ts_ingest_ns=INGEST_NS,
                  event_uid=kline_uid(venue, inst, "1m", open_ns))


def _b(i: int, close: str, **kw: Any) -> Candle:
    return _candle("BTC-USDT-PERP", Venue.BINANCE_USDM, i, close, **kw)


def _k(i: int, close: str, **kw: Any) -> Candle:
    return _candle("BTC-USD-SPOT", Venue.KRAKEN, i, close, **kw)


def test_binance_kraken_price_crosscheck_flags_divergence_above_50bps() -> None:
    assert CROSSCHECK_PAIRS["BTC-USDT-PERP"] == "BTC-USD-SPOT"
    kraken = [_k(i, "100000.0") for i in range(6)]
    binance = [
        _b(0, "100000.0"),     #   0 б.п.
        _b(1, "100500.0"),     # +50.00 б.п. рівно — НЕ позначається (строго більше порогу)
        _b(2, "100500.1"),     # +50.01 б.п. — позначається
        _b(3, "99499.9"),      # −50.01 б.п. — позначається (за модулем)
        _b(4, "99500.0"),      # −50.00 б.п. — ні
        _b(5, "100400.0"),     # +40 б.п. — ні
    ]
    rep = crosscheck(binance, kraken, threshold_bps=50)
    assert [r.diff_bps for r in rep.rows] == [Decimal(0), Decimal(50), Decimal("50.01"), Decimal("-50.01"),
                                              Decimal(-50), Decimal(40)]
    assert [r.open_time_ns for r in rep.flagged] == [T0 + 2 * MIN, T0 + 3 * MIN]
    assert [r.flagged for r in rep.rows] == [False, False, True, True, False, False]
    assert rep.n_matched == 6 and rep.max_abs_bps == Decimal("50.01")
    assert rep.table(only_flagged=True)[0] == {
        "open_time_ns": str(T0 + 2 * MIN), "binance_close": "100500.1", "kraken_close": "100000.0",
        "diff": "500.1", "diff_bps": "50.0100", "flagged": "yes"}
    # поріг параметризований: з 40 б.п. позначаються також ±50
    assert len(crosscheck(binance, kraken, threshold_bps="39.99").flagged) == 5


def test_crosscheck_on_recorded_binance_and_kraken_data() -> None:
    rows = orjson.loads(gzip.decompress((REST / "binance_klines.json.gz").read_bytes()))
    bn = normalize_rest_klines(rows, BTC_USDT_PERP, INGEST_NS)
    kr_raw = orjson.loads((REST / "kraken_ohlc.json").read_bytes())["result"]
    kr = normalize_kraken_ohlc(kr_raw, BTC_USD_SPOT, INGEST_NS)
    rep = crosscheck(bn, kr)
    closed_kraken = [c for c in kr if c.is_closed]
    assert rep.n_matched == len(closed_kraken) == 720          # 12 год. спільного вікна
    assert rep.missing_in_binance == () and rep.missing_in_kraken == ()
    # незалежний перерахунок кожного рядка
    b_close = {c.open_time_ns: c.c for c in bn}
    for r, k in zip(rep.rows, closed_kraken, strict=True):
        assert r.open_time_ns == k.open_time_ns
        assert r.diff_bps == (b_close[k.open_time_ns] - k.c) / k.c * 10_000
    # на реальних даних збою джерел не було: жодної хвилини понад 50 б.п. (базис перп/спот — одиниці б.п.)
    assert rep.flagged == ()
    assert rep.max_abs_bps is not None and rep.max_abs_bps < 50
    # ін'єкція збою Kraken (застиглий тик −1 %) у реальний ряд: позначається рівно ця хвилина
    victim = closed_kraken[300]
    stuck = (victim.c * Decimal("0.99")).quantize(Decimal("0.1"))
    frozen = victim.model_copy(update={"c": stuck, "l": min(victim.l, stuck)})
    injected = [frozen if c.open_time_ns == victim.open_time_ns else c for c in kr]
    rep2 = crosscheck(bn, injected)
    assert [r.open_time_ns for r in rep2.flagged] == [victim.open_time_ns]
    assert rep2.flagged[0].diff_bps > 99
    s = rep.summary()
    assert s["n_matched"] == 720 and s["n_flagged"] == 0 and s["threshold_bps"] == "50"


def test_crosscheck_skips_open_candles_and_rejects_bad_input() -> None:
    rep = crosscheck([_b(0, "100"), _b(1, "200", closed=False)], [_k(0, "100"), _k(1, "100")])
    assert rep.n_matched == 1 and rep.missing_in_binance == ()      # незакрита хвилина не звіряється
    with pytest.raises(ValueError, match="duplicate"):
        crosscheck([_b(0, "100"), _b(0, "100")], [_k(0, "100")])
    with pytest.raises(ValueError, match="mixed"):
        crosscheck([_b(0, "100"), _k(1, "100")], [_k(0, "100")])
    with pytest.raises(ValueError):
        divergence_bps(Decimal(1), Decimal(0))


def test_crosscheck_result_independent_of_caller_decimal_context() -> None:
    """Звіт відтворюваний: арифметика йде в core.money.DECIMAL_CONTEXT (38 знаків), а не в контексті
    потоку, що викликає (воркер без setup_decimal_context мав би prec=28 або менше)."""
    b = [_b(0, "81104.6"), _b(1, "81090.1")]
    k = [_k(0, "81089.3"), _k(1, "81133.7")]
    ref = crosscheck(b, k)
    with localcontext() as ctx:
        ctx.prec = 9
        low = crosscheck(b, k)
    assert [r.diff_bps for r in low.rows] == [r.diff_bps for r in ref.rows]
    assert low.mean_abs_bps == ref.mean_abs_bps and low.summary() == ref.summary()
    # 38 значущих цифр частки — а не 28 контексту за замовчуванням і не 9 контексту викликача
    assert len(ref.rows[0].diff_bps.as_tuple().digits) == 38
