"""Група C брифінгу (§10), частина «нормалізація»: venue-JSON → DTO, квантування, строгість схеми, час.

Найменування: tests/unit/test_normalize.py
Призначення: перевірити нормалізацію на СПРАВЖНІХ байтах бірж (fixtures/rest/*, fixtures/ws/sample_*)
і на золотих значеннях, порахованих вручну.
Автор: Андрій Жук, 2026.
"""

from __future__ import annotations

import copy
import functools
import gzip
from collections import Counter
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

import orjson
import pytest

from fuzzhelm.core.dto import BookSnapshot, Candle, Instrument, MarkPrice, Trade
from fuzzhelm.core.enums import ContractType, RejectCode, Src, Venue
from fuzzhelm.core.errors import NormalizationError
from fuzzhelm.core.money import quantize_step
from fuzzhelm.ingest.dedup import Deduplicator, DedupOutcome
from fuzzhelm.ingest.normalize import (
    kline_uid,
    ms_to_ns,
    normalize_binance,
    normalize_exchange_info,
    normalize_kraken_asset_pair,
    normalize_kraken_ohlc,
    normalize_premium_index,
    normalize_rest_kline,
    normalize_rest_klines,
    parse_stream,
    s_to_ns,
)
from fuzzhelm.ingest.quantize import (
    check_step,
    check_tick,
    floor_to_step,
    prepare_order_qty,
    quantize_to_tick,
    reject_below_min_notional,
)
from fuzzhelm.ingest.symbols import (
    BTC_USD_SPOT,
    BTC_USDT_PERP,
    canonical_symbol,
    kraken_result_key,
    parse_canonical,
    venue_symbol,
)

ROOT = Path(__file__).resolve().parents[2]
REST = ROOT / "fixtures" / "rest"
WS_SAMPLE = ROOT / "fixtures" / "ws" / "sample_btcusdt_4m.jsonl.gz"
INGEST_NS = 1_789_759_600_000_000_000        # після останнього бару фікстури


def _json(name: str) -> Any:
    return orjson.loads((REST / name).read_bytes())


def _klines() -> list[list[Any]]:
    return orjson.loads(gzip.decompress((REST / "binance_klines.json.gz").read_bytes()))


@functools.cache
def _frames() -> tuple[dict[str, Any], ...]:
    """Кадри записаної сесії (кеш: файл розбирається один раз; тести мутують лише deepcopy)."""
    lines = gzip.decompress(WS_SAMPLE.read_bytes()).splitlines()
    return tuple(r for r in map(orjson.loads, lines) if r["kind"] == "frame")


@functools.cache
def _normalized() -> tuple[Any, ...]:
    """DTO кожного кадру з його записаним ts_ingest (нормалізація — чиста функція, DTO заморожені; кеш)."""
    return tuple(normalize_binance(f["stream"], f["data"], f["ts_ingest_ns"], BTC_USDT_PERP)
                 for f in _frames())


def _first_frame(kind: str) -> dict[str, Any]:
    return next(f for f in _frames() if f["stream"] == f"btcusdt@{kind}")


def _instruments() -> dict[str, Instrument]:
    return normalize_exchange_info(_json("exchange_info.json"))


# ---------------------------------------------------------------- C1


def test_kline_ms_to_ns_exact() -> None:
    rows = _klines()
    candles = normalize_rest_klines(rows, BTC_USDT_PERP, INGEST_NS)
    for row, c in zip(rows, candles, strict=True):
        assert c.open_time_ns == row[0] * 1_000_000 and c.close_time_ns == row[6] * 1_000_000
        assert c.open_time_ns % 1_000_000 == 0 and c.open_time_ns // 1_000_000 == row[0]
    # float-шлях тут помилився б: 1789758599999 мс · 1e6 у double = …000064 нс (похибка 64 нс)
    close_ms = 1_789_758_599_999
    assert int(close_ms * 1e6) != close_ms * 1_000_000
    assert ms_to_ns(close_ms) == 1_789_758_599_999_000_000
    k = _first_frame("kline_1m")
    ws = normalize_binance(k["stream"], k["data"], k["ts_ingest_ns"], BTC_USDT_PERP)
    assert isinstance(ws, Candle)
    assert ws.open_time_ns == k["data"]["k"]["t"] * 10**6
    assert ws.close_time_ns == k["data"]["k"]["T"] * 10**6 == ws.open_time_ns + 59_999_000_000
    assert s_to_ns(1_789_716_300) == 1_789_716_300_000_000_000
    with pytest.raises(TypeError):
        ms_to_ns(1.789758599999e12)   # type: ignore[arg-type]


# ---------------------------------------------------------------- C2


@pytest.mark.parametrize(("price", "tick", "expected"), [
    ("81015.45", "0.10", "81015.40"),   # 810154.5 тиків → парне 810154
    ("81015.55", "0.10", "81015.60"),   # 810155.5 → парне 810156
    ("81015.451", "0.10", "81015.50"),  # не половинка — звичайне округлення вгору
    ("81015.449", "0.10", "81015.40"),
    ("100.25", "0.5", "100.0"),         # 200.5 тика → 200
    ("100.75", "0.5", "101.0"),         # 201.5 → 202
    ("2500.005", "0.01", "2500.00"),    # ETHUSDT: 250000.5 → 250000
    ("2500.015", "0.01", "2500.02"),    # 250001.5 → 250002
])
def test_price_quantized_to_tick_half_even(price: str, tick: str, expected: str) -> None:
    q = quantize_to_tick(Decimal(price), Decimal(tick))
    assert str(q) == expected and q.as_tuple().exponent == Decimal(tick).as_tuple().exponent
    assert check_tick(q, Decimal(tick))


def test_half_even_has_no_systematic_bias_unlike_half_up() -> None:
    tick = Decimal("0.10")
    halves = [Decimal("81000.05") + Decimal("0.10") * i for i in range(1000)]    # усі рівно на половинці
    drift_even = sum(quantize_to_tick(p, tick) - p for p in halves)
    drift_up = sum(quantize_step(p, tick, ROUND_HALF_UP) - p for p in halves)
    assert drift_even == 0
    assert drift_up == Decimal("50.000")                                         # +0.05 на кожній
    # пастка, якої уникає core.money: Decimal.quantize(0.10) квантує до ЕКСПОНЕНТИ (0.01), а не до тику
    assert Decimal("81000.05").quantize(tick) == Decimal("81000.05")


# ---------------------------------------------------------------- C3


@pytest.mark.parametrize(("qty", "step", "expected"), [
    ("0.0019", "0.001", "0.001"),
    ("0.999999", "0.001", "0.999"),
    ("1.2345", "0.01", "1.23"),
    ("5.000", "0.001", "5.000"),
    ("0.0009", "0.001", "0.000"),
    ("12.9999999", "1", "12"),
])
def test_qty_floored_to_step(qty: str, step: str, expected: str) -> None:
    q = floor_to_step(Decimal(qty), Decimal(step))
    assert str(q) == expected
    assert q <= Decimal(qty)                       # ніколи вгору
    assert Decimal(qty) - q < Decimal(step)        # і не більше ніж на один крок униз
    assert check_step(q, Decimal(step))


# ---------------------------------------------------------------- C4


def test_reject_below_min_notional_with_code() -> None:
    inst = _instruments()
    btc = inst["BTCUSDT"]
    below = RejectCode.BELOW_MIN_NOTIONAL
    assert btc.min_notional == Decimal("50")                                   # зі справжнього exchangeInfo
    assert reject_below_min_notional(Decimal("0.001"), Decimal("40000.0"), btc) is below
    assert reject_below_min_notional(Decimal("0.001"), Decimal("50000.0"), btc) is None   # межа включна
    assert reject_below_min_notional(Decimal("0.001"), Decimal("81015.4"), btc) is None
    assert reject_below_min_notional(Decimal("-0.001"), Decimal("40000.0"), btc) is below
    eth = inst["ETHUSDT"]
    assert eth.min_notional == Decimal("20")
    assert reject_below_min_notional(Decimal("0.007"), Decimal("2500.00"), eth) is below
    assert reject_below_min_notional(Decimal("0.008"), Decimal("2500.00"), eth) is None
    # повний шлях: floor до step, потім ZERO_QTY/BELOW_MIN_NOTIONAL
    assert prepare_order_qty(Decimal("0.0009"), Decimal("81015.4"), btc) == (Decimal("0.000"),
                                                                             RejectCode.ZERO_QTY)
    assert prepare_order_qty(Decimal("0.00069"), Decimal("81015.4"), btc)[1] is RejectCode.ZERO_QTY
    assert prepare_order_qty(Decimal("0.0019"), Decimal("30000.0"), btc) == (
        Decimal("0.001"), RejectCode.BELOW_MIN_NOTIONAL)
    assert prepare_order_qty(Decimal("0.0019"), Decimal("81015.4"), btc) == (Decimal("0.001"), None)
    # від'ємна кількість — помилка знаку у викликача, а не «нульова кількість»
    with pytest.raises(ValueError, match="direction"):
        prepare_order_qty(Decimal("-5"), Decimal("81015.4"), btc)


# ---------------------------------------------------------------- C5


@pytest.mark.parametrize(("kind", "path", "field"), [
    ("kline_1m", (), "zz"),
    ("kline_1m", ("k",), "k.zz"),
    ("aggTrade", (), "zz"),
    ("markPrice@1s", (), "zz"),
    ("depth20@100ms", (), "zz"),
])
def test_unknown_field_raises_normalization_error(kind: str, path: tuple[str, ...], field: str) -> None:
    fr = _first_frame(kind)
    data = copy.deepcopy(fr["data"])
    target = data
    for p in path:
        target = target[p]
    target["zz"] = "1"
    with pytest.raises(NormalizationError) as ei:
        normalize_binance(fr["stream"], data, fr["ts_ingest_ns"], BTC_USDT_PERP)
    assert ei.value.field == field and ei.value.venue == "BINANCE_USDM"
    # контроль: без зайвого поля той самий кадр нормалізується
    normalize_binance(fr["stream"], fr["data"], fr["ts_ingest_ns"], BTC_USDT_PERP)


def test_unknown_field_in_rest_and_kraken_payloads() -> None:
    row = _klines()[0]
    with pytest.raises(NormalizationError) as ei:
        normalize_rest_kline([*row, "extra"], BTC_USDT_PERP, INGEST_NS)
    assert ei.value.field == "row[12]"
    prem = {**_json("premium_index.json"), "newField": "0"}
    with pytest.raises(NormalizationError) as ei:
        normalize_premium_index(prem, BTC_USDT_PERP, INGEST_NS)
    assert ei.value.field == "newField"
    k = _json("kraken_ohlc.json")["result"]
    with pytest.raises(NormalizationError) as ei:
        normalize_kraken_ohlc({**k, "XETHZUSD": []}, BTC_USD_SPOT, INGEST_NS)
    assert ei.value.field == "result.XETHZUSD"
    bad = copy.deepcopy(k)
    bad["XXBTZUSD"][5].append("9")
    with pytest.raises(NormalizationError) as ei:
        normalize_kraken_ohlc(bad, BTC_USD_SPOT, INGEST_NS)
    assert ei.value.field == "result.XXBTZUSD[5][8]"


@pytest.mark.parametrize(("mutate", "field"), [
    (lambda d: d.pop("p"), "p"),                            # відсутнє обов'язкове поле
    (lambda d: d.__setitem__("p", 81015.4), "p"),           # float замість рядка
    (lambda d: d.__setitem__("q", "1e-3"), "q"),            # експонента
    (lambda d: d.__setitem__("q", " 0.005"), "q"),          # пробіл
    (lambda d: d.__setitem__("p", "NaN"), "p"),
    (lambda d: d.__setitem__("a", True), "a"),              # bool замість int
    (lambda d: d.__setitem__("m", 1), "m"),                 # int замість bool
    (lambda d: d.__setitem__("s", "ETHUSDT"), "s"),         # чужий символ
    (lambda d: d.__setitem__("e", "trade"), "e"),           # чужий тип події
    (lambda d: d.__setitem__("p", "0"), "price"),           # ціна ≤ 0 → поле DTO
])
def test_invalid_trade_payload_names_the_field(mutate: Any, field: str) -> None:
    fr = _first_frame("aggTrade")
    data = copy.deepcopy(fr["data"])
    mutate(data)
    with pytest.raises(NormalizationError) as ei:
        normalize_binance(fr["stream"], data, fr["ts_ingest_ns"], BTC_USDT_PERP)
    assert ei.value.field == field


# ---------------------------------------------------------------- C6


def test_event_and_ingest_time_never_mixed() -> None:
    # час біржі кожного DTO — задокументоване поле біржі (docs/field_mapping.md), ×10⁶
    event_field = {"kline_1m": lambda d: d["E"], "aggTrade": lambda d: d["T"],
                   "markPrice@1s": lambda d: d["E"], "depth20@100ms": lambda d: d["T"]}
    seen: Counter[str] = Counter()
    for fr, a in zip(_frames(), _normalized(), strict=True):
        kind = fr["stream"].split("@", 1)[1]
        b = normalize_binance(fr["stream"], fr["data"], fr["ts_ingest_ns"] + 123_456_789, BTC_USDT_PERP)
        assert a.ts_event_ns == b.ts_event_ns == event_field[kind](fr["data"]) * 1_000_000
        assert a.ts_ingest_ns == fr["ts_ingest_ns"] and b.ts_ingest_ns == fr["ts_ingest_ns"] + 123_456_789
        assert a.event_uid == b.event_uid                    # ідентичність події не залежить від отримання
        assert a.model_dump(exclude={"ts_ingest_ns"}) == b.model_dump(exclude={"ts_ingest_ns"})
        assert a.ts_event_ns != a.ts_ingest_ns
        seen[kind] += 1
    assert set(seen) == set(event_field)
    # REST: ts_event = closeTime біржі; час отримання на нього не впливає
    row = _klines()[10]
    r1 = normalize_rest_kline(row, BTC_USDT_PERP, INGEST_NS)
    r2 = normalize_rest_kline(row, BTC_USDT_PERP, INGEST_NS + 10**12)
    assert r1.ts_event_ns == r2.ts_event_ns == row[6] * 1_000_000
    assert (r1.ts_ingest_ns, r2.ts_ingest_ns) == (INGEST_NS, INGEST_NS + 10**12)
    prem = _json("premium_index.json")
    m = normalize_premium_index(prem, BTC_USDT_PERP, INGEST_NS)
    assert m.ts_event_ns == prem["time"] * 1_000_000 and m.ts_ingest_ns == INGEST_NS


# ---------------------------------------------------------------- додаткові: реальні WS-кадри


def test_every_recorded_ws_frame_normalizes_to_expected_dto() -> None:
    frames = _frames()
    kinds = Counter(type(dto).__name__ for dto in _normalized())
    streams = Counter(f["stream"] for f in frames)
    assert kinds == {"Candle": streams["btcusdt@kline_1m"], "Trade": streams["btcusdt@aggTrade"],
                     "MarkPrice": streams["btcusdt@markPrice@1s"],
                     "BookSnapshot": streams["btcusdt@depth20@100ms"]}
    kl = [dto for f, dto in zip(frames, _normalized(), strict=True) if f["stream"].endswith("kline_1m")]
    assert all(isinstance(c, Candle) and c.src is Src.WS for c in kl)
    # усі оновлення однієї хвилини мають один event_uid (природний ключ — open_time)
    by_open = {c.open_time_ns: c.event_uid for c in kl if isinstance(c, Candle)}
    assert all(c.event_uid == by_open[c.open_time_ns] for c in kl if isinstance(c, Candle))
    assert len(set(by_open.values())) == len(by_open)
    k0 = next(f for f in frames if f["stream"].endswith("kline_1m"))
    replay = normalize_binance(k0["stream"], k0["data"], 1, BTC_USDT_PERP, src=Src.REPLAY)
    assert isinstance(replay, Candle) and replay.src is Src.REPLAY


def test_ws_payload_mapping_per_stream() -> None:
    k = _first_frame("kline_1m")
    c = normalize_binance(k["stream"], k["data"], k["ts_ingest_ns"], BTC_USDT_PERP)
    kk = k["data"]["k"]
    assert isinstance(c, Candle)
    assert (c.o, c.h, c.l, c.c) == tuple(Decimal(kk[f]) for f in "ohlc")
    assert c.volume == Decimal(kk["v"]) and c.quote_volume == Decimal(kk["q"]) and c.trades_count == kk["n"]
    assert c.is_closed is kk["x"] and c.vwap is None and c.instrument == "BTC-USDT-PERP"
    assert c.event_uid == kline_uid(Venue.BINANCE_USDM, "BTC-USDT-PERP", "1m", kk["t"] * 10**6)

    a = _first_frame("aggTrade")
    t = normalize_binance(a["stream"], a["data"], a["ts_ingest_ns"], BTC_USDT_PERP)
    assert isinstance(t, Trade)
    assert (t.agg_id, t.first_trade_id, t.last_trade_id) == (a["data"]["a"], a["data"]["f"], a["data"]["l"])
    assert t.price == Decimal(a["data"]["p"]) and str(t.price) == a["data"]["p"]    # масштаб збережено
    assert t.is_buyer_maker is a["data"]["m"]

    m = _first_frame("markPrice@1s")
    mp = normalize_binance(m["stream"], m["data"], m["ts_ingest_ns"], BTC_USDT_PERP)
    assert isinstance(mp, MarkPrice)
    assert mp.mark_price == Decimal(m["data"]["p"]) and mp.index_price == Decimal(m["data"]["i"])
    assert mp.funding_rate == Decimal(m["data"]["r"])
    assert mp.next_funding_time_ns == m["data"]["T"] * 10**6

    d = _first_frame("depth20@100ms")
    b = normalize_binance(d["stream"], d["data"], d["ts_ingest_ns"], BTC_USDT_PERP)
    assert isinstance(b, BookSnapshot)
    assert len(b.bids) == len(b.asks) == 20 and b.last_update_id == d["data"]["u"]
    assert [lv.price for lv in b.bids] == sorted((lv.price for lv in b.bids), reverse=True)
    assert [lv.price for lv in b.asks] == sorted(lv.price for lv in b.asks)
    assert b.bids[0].price < b.asks[0].price
    swapped = copy.deepcopy(d["data"])
    swapped["b"][0], swapped["b"][1] = swapped["b"][1], swapped["b"][0]
    with pytest.raises(NormalizationError) as ei:
        normalize_binance(d["stream"], swapped, d["ts_ingest_ns"], BTC_USDT_PERP)
    assert ei.value.field == "b[1]"


def test_stream_parsing_and_symbol_guard() -> None:
    assert parse_stream("btcusdt@depth20@100ms").levels == 20
    assert parse_stream("btcusdt@markPrice").speed == "3s"
    assert parse_stream("ethusdt@kline_5m").interval == "5m"
    for bad in ("btcusdt@depth@100ms", "btcusdt@trade", "BTCUSDT@aggTrade", "btcusdt"):
        with pytest.raises(NormalizationError) as ei:
            parse_stream(bad)
        assert ei.value.field == "stream"
    fr = _first_frame("aggTrade")
    with pytest.raises(NormalizationError) as ei:
        normalize_binance("ethusdt@aggTrade", fr["data"], fr["ts_ingest_ns"], BTC_USDT_PERP)
    assert ei.value.field == "stream"


# ---------------------------------------------------------------- додаткові: REST і довідники


def test_exchange_info_to_instrument() -> None:
    inst = _instruments()
    btc = inst["BTCUSDT"]
    assert (btc.symbol_canon, btc.contract_type, btc.base_asset, btc.quote_asset) == \
        ("BTC-USDT-PERP", ContractType.PERP, "BTC", "USDT")
    assert (btc.tick_size, btc.step_size) == (Decimal("0.10"), Decimal("0.001"))
    assert btc.mmr == Decimal("0.005")                   # D-04: у публічному exchangeInfo mmr немає
    assert inst["ETHUSDT"].tick_size == Decimal("0.01")
    with pytest.raises(NormalizationError) as ei:
        normalize_exchange_info(_json("exchange_info.json"), ["SOLUSDT"])
    assert ei.value.field == "symbols[SOLUSDT]"
    info = copy.deepcopy(_json("exchange_info.json"))
    filters = info["symbols"][0]["filters"]
    info["symbols"][0]["filters"] = [f for f in filters if f["filterType"] != "LOT_SIZE"]
    with pytest.raises(NormalizationError) as ei:
        normalize_exchange_info(info, ["BTCUSDT"])
    assert ei.value.field == "symbols[0].filters.LOT_SIZE"


def test_rest_kline_row_mapping_and_closedness() -> None:
    row = _klines()[-1]
    c = normalize_rest_kline(row, BTC_USDT_PERP, INGEST_NS)
    assert c.src is Src.REST and c.is_closed and c.vwap is None
    expected = tuple(Decimal(row[i]) for i in (1, 2, 3, 4, 5, 7))
    assert (c.o, c.h, c.l, c.c, c.volume, c.quote_volume) == expected
    assert c.trades_count == row[8]
    # той самий рядок до закриття бару (час біржі < closeTime) — незакритий
    early = normalize_rest_kline(row, BTC_USDT_PERP, INGEST_NS, server_time_ms=row[6] - 5)
    assert not early.is_closed and early.event_uid == c.event_uid
    assert c.ts_event_ns == row[6] * 10**6 and early.ts_event_ns == (row[6] - 5) * 10**6

    # два REST-зрізи ОДНОГО незакритого бару: ts_event — час біржі зрізу (не майбутній closeTime),
    # тож новіший зріз витісняє старіший у дедуплікаторі (раніше обидва мали ts_event = closeTime
    # і перемагав перший, тобто застарілий)
    stale_row = [*row]
    stale_row[4], stale_row[5], stale_row[8] = row[1], "10.000", 100
    t1, t2 = row[0] + 20_000, row[0] + 40_000
    stale = normalize_rest_kline(stale_row, BTC_USDT_PERP, t1 * 10**6, server_time_ms=t1)
    fresh = normalize_rest_kline(row, BTC_USDT_PERP, t2 * 10**6, server_time_ms=t2)
    assert not stale.is_closed and not fresh.is_closed
    assert (stale.ts_event_ns, fresh.ts_event_ns) == (t1 * 10**6, t2 * 10**6)
    assert stale.event_uid == fresh.event_uid
    cases = ((stale, fresh, DedupOutcome.REPLACED), (fresh, stale, DedupOutcome.DUPLICATE))
    for first, second, outcome in cases:
        d = Deduplicator()
        assert d.offer(first) is DedupOutcome.NEW
        assert d.offer(second) is outcome
        assert d.get(fresh.event_uid) == fresh

    broken = [*row]
    broken[6] = row[6] + 1
    with pytest.raises(NormalizationError) as ei:
        normalize_rest_kline(broken, BTC_USDT_PERP, INGEST_NS)
    assert ei.value.field == "row[6]"
    swapped = [*row]
    swapped[2], swapped[3] = row[3], row[2]           # high < low
    with pytest.raises(NormalizationError) as ei:
        normalize_rest_kline(swapped, BTC_USDT_PERP, INGEST_NS)
    assert ei.value.field == "__root__"


def test_kraken_ohlc_and_asset_pair() -> None:
    res = _json("kraken_ohlc.json")["result"]
    rows = res["XXBTZUSD"]
    cs = normalize_kraken_ohlc(res, BTC_USD_SPOT, INGEST_NS)
    assert len(cs) == len(rows)
    assert all(c.instrument == "BTC-USD-SPOT" and c.venue is Venue.KRAKEN for c in cs)
    # `last` — останній зафіксований бар; усі пізніші — незакриті
    assert [c.is_closed for c in cs] == [r[0] <= res["last"] for r in rows]
    assert not cs[-1].is_closed and cs[-2].is_closed
    c0, r0 = cs[0], rows[0]
    assert c0.open_time_ns == r0[0] * 10**9 and c0.close_time_ns == c0.open_time_ns + 59_999_000_000
    assert (c0.o, c0.c, c0.vwap, c0.volume, c0.trades_count) == \
        (Decimal(r0[1]), Decimal(r0[4]), Decimal(r0[5]), Decimal(r0[6]), r0[7])
    assert c0.quote_volume == 0                          # Kraken не надає — значення DTO «не надано»
    zero = copy.deepcopy(res)
    zero["XXBTZUSD"][0][6] = "0.00000000"
    zero["XXBTZUSD"][0][5] = "0.0"
    # VWAP без обсягу не визначений
    assert normalize_kraken_ohlc(zero, BTC_USD_SPOT, INGEST_NS)[0].vwap is None
    pair = normalize_kraken_asset_pair(_json("kraken_asset_pairs.json")["result"])
    assert (pair.symbol_venue, pair.symbol_canon, pair.contract_type) == \
        ("XBTUSD", "BTC-USD-SPOT", ContractType.SPOT)
    assert pair.tick_size == Decimal("0.1") and pair.step_size == Decimal("0.00000001")
    assert pair.min_notional == Decimal("0.5")


def test_symbols_mapping() -> None:
    assert canonical_symbol(Venue.BINANCE_USDM, "BTCUSDT") == "BTC-USDT-PERP"
    assert canonical_symbol(Venue.BINANCE_USDM, "ethusdt") == "ETH-USDT-PERP"
    assert canonical_symbol(Venue.KRAKEN, "XBTUSD") == "BTC-USD-SPOT"
    assert canonical_symbol(Venue.KRAKEN, "XXBTZUSD") == "BTC-USD-SPOT"
    assert venue_symbol(Venue.KRAKEN, "BTC-USD-SPOT") == "XBTUSD"
    assert kraken_result_key("XBTUSD") == "XXBTZUSD"
    assert parse_canonical("BTC-USDT-PERP") == ("BTC", "USDT", ContractType.PERP)
    with pytest.raises(NormalizationError):
        canonical_symbol(Venue.BINANCE_USDM, "DOGEUSDT")
    with pytest.raises(NormalizationError):
        parse_canonical("BTCUSDT")
