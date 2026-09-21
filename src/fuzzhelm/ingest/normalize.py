"""Нормалізація: venue-JSON (Binance REST/WS) → канонічні DTO на Decimal.

Найменування: ingest/normalize.py
Призначення: межа нормалізації. Усе, що далі йде в журнал, БД і рушій рішень, проходить тут.
Автор: Андрій Жук, 2026.

Правила (інваріанти, перевірені тестами tests/unit/test_normalize.py):
  * СТРОГА схема ринкових повідомлень: невідоме поле → NormalizationError(field=<шлях>), відсутнє
    обов'язкове поле чи поле не того типу → теж NormalizationError. Поля, що біржа надсилає, але DTO
    їх не має (напр. `nq`, `st`, `ap` у WS), перелічені явно у білих списках нижче.
  * Числа-гроші приходять рядками й стають Decimal без проміжного float; рядок мусить мати вигляд
    `-?\\d+(\\.\\d+)?` (без експоненти, пробілів, NaN, підкреслень). Масштаб рядка зберігається.
  * Час: мс/с біржі → нс ЦІЛОЧИСЕЛЬНИМ множенням (жодного float, жодної втрати точності).
  * `ts_event_ns` береться ЛИШЕ з полів біржі, `ts_ingest_ns` — ЛИШЕ з аргументу; event_uid від
    ts_ingest_ns не залежить. Які саме поля біржі є ts_event — таблиця в docs/field_mapping.md.
  * event_uid — BLAKE2b-128 від природного ключа (docs/contracts.md §1).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Final, NoReturn

from pydantic import BaseModel, ValidationError

from fuzzhelm.core.digest import event_uid
from fuzzhelm.core.dto import BookLevel, BookSnapshot, Candle, Instrument, MarketEvent, MarkPrice, Trade
from fuzzhelm.core.enums import ContractType, Src, Stream, Venue
from fuzzhelm.core.errors import NormalizationError
from fuzzhelm.core.money import D0, dec
from fuzzhelm.ingest.symbols import (
    SymbolRef,
    make_canonical,
    symbol_ref,
)

NS_PER_MS: Final = 1_000_000
NS_PER_S: Final = 1_000_000_000
DEFAULT_MMR: Final = Decimal("0.005")     # див. docs/deviations.md D-04: у публічному exchangeInfo mmr немає

TF_MS: Final[dict[str, int]] = {
    "1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
    "1h": 3_600_000, "2h": 7_200_000, "4h": 14_400_000, "6h": 21_600_000, "8h": 28_800_000,
    "12h": 43_200_000, "1d": 86_400_000,
}

InstrumentLike = Instrument | SymbolRef

_DEC_RE: Final = re.compile(r"-?\d+(?:\.\d+)?")

_BIN = Venue.BINANCE_USDM.value

# ---------------------------------------------------------------- білі списки полів (строга схема)

WS_KLINE_FIELDS: Final = frozenset({"e", "E", "s", "k"})
WS_KLINE_K_FIELDS: Final = frozenset({"t", "T", "s", "i", "f", "L", "o", "c", "h", "l", "v", "n", "x", "q",
                                      "V", "Q", "B"})
WS_AGG_TRADE_FIELDS: Final = frozenset({"e", "E", "a", "s", "p", "q", "nq", "f", "l", "T", "m", "st"})
WS_AGG_TRADE_REQUIRED: Final = WS_AGG_TRADE_FIELDS - {"nq", "st"}
WS_MARK_FIELDS: Final = frozenset({"e", "E", "s", "p", "ap", "P", "i", "r", "T", "st"})
WS_MARK_REQUIRED: Final = WS_MARK_FIELDS - {"ap", "st"}
WS_DEPTH_FIELDS: Final = frozenset({"e", "E", "T", "s", "U", "u", "pu", "b", "a", "ps", "st"})
WS_DEPTH_REQUIRED: Final = WS_DEPTH_FIELDS - {"ps", "st"}
PREMIUM_INDEX_FIELDS: Final = frozenset({"symbol", "markPrice", "indexPrice", "estimatedSettlePrice",
                                         "lastFundingRate", "interestRate", "nextFundingTime", "time"})
REST_KLINE_LEN: Final = 12      # [t, o, h, l, c, v, T, qv, n, takerBuyBase, takerBuyQuote, ignore]

# ---------------------------------------------------------------- примітиви


def _fail(msg: str, field: str, venue: str) -> NoReturn:
    raise NormalizationError(msg, field=field, venue=venue)


def _dec(v: object, field: str, venue: str) -> Decimal:
    if type(v) is not str or _DEC_RE.fullmatch(v) is None:
        _fail(f"expected decimal string at {field!r}, got {v!r}", field, venue)
    return dec(v)


def _int(v: object, field: str, venue: str) -> int:
    if type(v) is not int:        # bool — підклас int, тому саме type(...) is int
        _fail(f"expected integer at {field!r}, got {v!r}", field, venue)
    return v


def _bool(v: object, field: str, venue: str) -> bool:
    if type(v) is not bool:
        _fail(f"expected bool at {field!r}, got {v!r}", field, venue)
    return v


def _str(v: object, field: str, venue: str) -> str:
    if type(v) is not str:
        _fail(f"expected string at {field!r}, got {v!r}", field, venue)
    return v


def _mapping(data: object, field: str, venue: str) -> Mapping[str, Any]:
    if not isinstance(data, Mapping):
        _fail(f"expected JSON object at {field!r}, got {type(data).__name__}", field, venue)
    return data


def _check_fields(data: Mapping[str, Any], allowed: frozenset[str], required: frozenset[str],
                  prefix: str, venue: str) -> None:
    unknown = data.keys() - allowed
    if unknown:
        f = sorted(unknown)[0]
        _fail(f"unknown field {prefix}{f!s}", f"{prefix}{f}", venue)
    missing = required - data.keys()
    if missing:
        f = sorted(missing)[0]
        _fail(f"missing field {prefix}{f}", f"{prefix}{f}", venue)


def _build[M: BaseModel](model: type[M], err_venue: str, /, **kw: Any) -> M:
    """Побудова DTO; ValidationError pydantic → NormalizationError з назвою поля DTO."""
    try:
        return model(**kw)
    except ValidationError as e:
        err = e.errors()[0]
        loc = ".".join(str(p) for p in err["loc"]) or "__root__"
        raise NormalizationError(f"{model.__name__}: {err['msg']}", field=loc, venue=err_venue) from e


def ms_to_ns(ms: int) -> int:
    """Мілісекунди біржі → наносекунди; лише ціле множення (float заборонено)."""
    if type(ms) is not int:
        raise TypeError(f"ms_to_ns expects int, got {type(ms).__name__}")
    return ms * NS_PER_MS


def s_to_ns(s: int) -> int:
    if type(s) is not int:
        raise TypeError(f"s_to_ns expects int, got {type(s).__name__}")
    return s * NS_PER_S


def tf_ms(tf: str) -> int:
    try:
        return TF_MS[tf]
    except KeyError:
        raise NormalizationError(f"unsupported timeframe {tf!r}", field="tf") from None


def _check_ingest(ts_ingest_ns: int) -> None:
    if type(ts_ingest_ns) is not int or ts_ingest_ns < 0:
        raise NormalizationError(f"ts_ingest_ns must be a non-negative int, got {ts_ingest_ns!r}",
                                 field="ts_ingest_ns")


# ---------------------------------------------------------------- event_uid (природні ключі, contracts §1)


def kline_uid(venue: Venue, symbol_canon: str, tf: str, open_time_ns: int) -> str:
    return event_uid(venue.value, Stream.KLINES.value, symbol_canon, tf, open_time_ns)


def trade_uid(venue: Venue, symbol_canon: str, agg_id: int) -> str:
    return event_uid(venue.value, Stream.TRADES.value, symbol_canon, agg_id)


def depth_uid(venue: Venue, symbol_canon: str, last_update_id: int) -> str:
    return event_uid(venue.value, Stream.DEPTH.value, symbol_canon, last_update_id)


def mark_uid(venue: Venue, symbol_canon: str, ts_event_ns: int) -> str:
    return event_uid(venue.value, Stream.MARK.value, symbol_canon, ts_event_ns)


# ---------------------------------------------------------------- імена WS-потоків


@dataclass(frozen=True, slots=True)
class StreamName:
    """Розібране ім'я потоку Binance, напр. `btcusdt@depth20@100ms`."""

    symbol: str                 # у верхньому регістрі, як `s` у payload
    kind: str                   # kline | aggTrade | markPrice | depth
    interval: str | None = None  # для kline
    levels: int | None = None    # для depth<N>
    speed: str | None = None     # `1s`, `100ms`, ...


_STREAM_RE: Final = re.compile(
    r"(?P<sym>[a-z0-9]+)@(?:"
    r"kline_(?P<tf>[0-9]+[mhdwM])"
    r"|(?P<agg>aggTrade)"
    r"|(?P<mark>markPrice)(?:@(?P<mspd>1s|3s))?"
    r"|depth(?P<lv>5|10|20)(?:@(?P<dspd>100ms|250ms|500ms))?"
    r")"
)


def parse_stream(stream: str) -> StreamName:
    m = _STREAM_RE.fullmatch(stream) if isinstance(stream, str) else None
    if m is None:
        raise NormalizationError(f"unsupported Binance stream {stream!r}", field="stream", venue=_BIN)
    sym = m["sym"].upper()
    if m["tf"]:
        return StreamName(sym, "kline", interval=m["tf"])
    if m["agg"]:
        return StreamName(sym, "aggTrade")
    if m["mark"]:
        return StreamName(sym, "markPrice", speed=m["mspd"] or "3s")
    return StreamName(sym, "depth", levels=int(m["lv"]), speed=m["dspd"] or "250ms")


# ---------------------------------------------------------------- Binance WebSocket


def normalize_binance(stream: str, data: Mapping[str, Any], ts_ingest_ns: int, instrument: InstrumentLike,
                      *, src: Src = Src.WS) -> MarketEvent:
    """Сирий payload Binance USDⓈ-M (поле `data` комбінованого потоку) → MarketEvent.

    Підтримувані потоки: `<s>@kline_<tf>` → Candle, `<s>@aggTrade` → Trade,
    `<s>@markPrice[@1s]` → MarkPrice, `<s>@depth<5|10|20>[@<N>ms]` → BookSnapshot.
    `src` — Src.WS для живого потоку, Src.REPLAY для відтворення сесії.
    """
    sn = parse_stream(stream)
    if sn.symbol != instrument.symbol_venue.upper():
        _fail(f"stream {stream!r} does not belong to {instrument.symbol_venue}", "stream", _BIN)
    _check_ingest(ts_ingest_ns)
    d = _mapping(data, "$", _BIN)
    if sn.kind == "kline":
        return _ws_kline(sn, d, ts_ingest_ns, instrument, src)
    if sn.kind == "aggTrade":
        return _ws_agg_trade(d, ts_ingest_ns, instrument)
    if sn.kind == "markPrice":
        return _ws_mark(d, ts_ingest_ns, instrument)
    return _ws_depth(sn, d, ts_ingest_ns, instrument)


def _check_event(d: Mapping[str, Any], expected_e: str, instrument: InstrumentLike) -> None:
    if _str(d["e"], "e", _BIN) != expected_e:
        _fail(f"event type {d['e']!r} != {expected_e!r}", "e", _BIN)
    if _str(d["s"], "s", _BIN) != instrument.symbol_venue:
        _fail(f"symbol {d['s']!r} != {instrument.symbol_venue!r}", "s", _BIN)


def _ws_kline(sn: StreamName, d: Mapping[str, Any], ts_ingest_ns: int, inst: InstrumentLike,
              src: Src) -> Candle:
    _check_fields(d, WS_KLINE_FIELDS, WS_KLINE_FIELDS, "", _BIN)
    _check_event(d, "kline", inst)
    k = _mapping(d["k"], "k", _BIN)
    _check_fields(k, WS_KLINE_K_FIELDS, WS_KLINE_K_FIELDS, "k.", _BIN)
    if _str(k["s"], "k.s", _BIN) != inst.symbol_venue:
        _fail(f"k.s {k['s']!r} != {inst.symbol_venue!r}", "k.s", _BIN)
    tf = _str(k["i"], "k.i", _BIN)
    if tf != sn.interval:
        _fail(f"k.i {tf!r} != stream interval {sn.interval!r}", "k.i", _BIN)
    step_ms = TF_MS.get(tf)
    if step_ms is None:
        _fail(f"unsupported timeframe {tf!r}", "k.i", _BIN)
    open_ms = _int(k["t"], "k.t", _BIN)
    close_ms = _int(k["T"], "k.T", _BIN)
    if close_ms != open_ms + step_ms - 1:
        _fail(f"k.T {close_ms} != k.t + {step_ms} - 1", "k.T", _BIN)
    _int(k["f"], "k.f", _BIN)
    _int(k["L"], "k.L", _BIN)
    for f in ("V", "Q", "B"):
        _dec(k[f], f"k.{f}", _BIN)
    open_ns = ms_to_ns(open_ms)
    return _build(
        Candle, _BIN,
        instrument=inst.symbol_canon, venue=inst.venue, tf=tf,
        open_time_ns=open_ns, close_time_ns=ms_to_ns(close_ms),
        o=_dec(k["o"], "k.o", _BIN), h=_dec(k["h"], "k.h", _BIN),
        l=_dec(k["l"], "k.l", _BIN), c=_dec(k["c"], "k.c", _BIN),
        volume=_dec(k["v"], "k.v", _BIN), quote_volume=_dec(k["q"], "k.q", _BIN),
        trades_count=_int(k["n"], "k.n", _BIN), vwap=None,
        is_closed=_bool(k["x"], "k.x", _BIN), src=src,
        ts_event_ns=ms_to_ns(_int(d["E"], "E", _BIN)), ts_ingest_ns=ts_ingest_ns,
        event_uid=kline_uid(inst.venue, inst.symbol_canon, tf, open_ns),
    )


def _ws_agg_trade(d: Mapping[str, Any], ts_ingest_ns: int, inst: InstrumentLike) -> Trade:
    _check_fields(d, WS_AGG_TRADE_FIELDS, WS_AGG_TRADE_REQUIRED, "", _BIN)
    _check_event(d, "aggTrade", inst)
    _int(d["E"], "E", _BIN)
    if "nq" in d:
        _dec(d["nq"], "nq", _BIN)
    if "st" in d:
        _int(d["st"], "st", _BIN)
    agg_id = _int(d["a"], "a", _BIN)
    return _build(
        Trade, _BIN,
        instrument=inst.symbol_canon, venue=inst.venue, agg_id=agg_id,
        first_trade_id=_int(d["f"], "f", _BIN), last_trade_id=_int(d["l"], "l", _BIN),
        price=_dec(d["p"], "p", _BIN), qty=_dec(d["q"], "q", _BIN),
        is_buyer_maker=_bool(d["m"], "m", _BIN),
        ts_event_ns=ms_to_ns(_int(d["T"], "T", _BIN)), ts_ingest_ns=ts_ingest_ns,
        event_uid=trade_uid(inst.venue, inst.symbol_canon, agg_id),
    )


def _ws_mark(d: Mapping[str, Any], ts_ingest_ns: int, inst: InstrumentLike) -> MarkPrice:
    _check_fields(d, WS_MARK_FIELDS, WS_MARK_REQUIRED, "", _BIN)
    _check_event(d, "markPriceUpdate", inst)
    _dec(d["P"], "P", _BIN)
    if "ap" in d:
        _dec(d["ap"], "ap", _BIN)
    if "st" in d:
        _int(d["st"], "st", _BIN)
    ts_event_ns = ms_to_ns(_int(d["E"], "E", _BIN))
    return _build(
        MarkPrice, _BIN,
        instrument=inst.symbol_canon, venue=inst.venue,
        mark_price=_dec(d["p"], "p", _BIN), index_price=_dec(d["i"], "i", _BIN),
        funding_rate=_dec(d["r"], "r", _BIN),
        next_funding_time_ns=ms_to_ns(_int(d["T"], "T", _BIN)),
        ts_event_ns=ts_event_ns, ts_ingest_ns=ts_ingest_ns,
        event_uid=mark_uid(inst.venue, inst.symbol_canon, ts_event_ns),
    )


def _levels(raw: object, field: str, max_levels: int, descending: bool) -> tuple[BookLevel, ...]:
    if not isinstance(raw, list):
        _fail(f"expected list at {field!r}", field, _BIN)
    if len(raw) > max_levels:
        _fail(f"{field!r} has {len(raw)} levels > {max_levels}", field, _BIN)
    out: list[BookLevel] = []
    prev: Decimal | None = None
    for i, lv in enumerate(raw):
        f = f"{field}[{i}]"
        if not isinstance(lv, list) or len(lv) != 2:
            _fail(f"level must be [price, qty] at {f!r}", f, _BIN)
        price = _dec(lv[0], f"{f}[0]", _BIN)
        qty = _dec(lv[1], f"{f}[1]", _BIN)
        if price <= D0 or qty < D0:
            _fail(f"non-positive price or negative qty at {f!r}", f, _BIN)
        if prev is not None and (price >= prev if descending else price <= prev):
            _fail(f"{field!r} not strictly {'descending' if descending else 'ascending'} at {i}", f, _BIN)
        prev = price
        # поля вже перевірено вище (price > 0, qty ≥ 0) — повторна валідація pydantic на 40 рівнях
        # кожні 100 мс не дає нової гарантії, лише витрачає час реплею
        out.append(BookLevel.model_construct(price=price, qty=qty))
    return tuple(out)


def _ws_depth(sn: StreamName, d: Mapping[str, Any], ts_ingest_ns: int, inst: InstrumentLike) -> BookSnapshot:
    _check_fields(d, WS_DEPTH_FIELDS, WS_DEPTH_REQUIRED, "", _BIN)
    _check_event(d, "depthUpdate", inst)
    _int(d["E"], "E", _BIN)
    _int(d["U"], "U", _BIN)
    _int(d["pu"], "pu", _BIN)
    if "ps" in d:
        _str(d["ps"], "ps", _BIN)
    if "st" in d:
        _int(d["st"], "st", _BIN)
    levels = sn.levels or 20
    u = _int(d["u"], "u", _BIN)
    return _build(
        BookSnapshot, _BIN,
        instrument=inst.symbol_canon, venue=inst.venue,
        bids=_levels(d["b"], "b", levels, descending=True),
        asks=_levels(d["a"], "a", levels, descending=False),
        last_update_id=u,
        ts_event_ns=ms_to_ns(_int(d["T"], "T", _BIN)), ts_ingest_ns=ts_ingest_ns,
        event_uid=depth_uid(inst.venue, inst.symbol_canon, u),
    )


# ---------------------------------------------------------------- Binance REST


def normalize_rest_kline(row: Sequence[Any], instrument: InstrumentLike, ts_ingest_ns: int, *, tf: str = "1m",
                         server_time_ms: int | None = None, src: Src = Src.REST) -> Candle:
    """Рядок `/fapi/v1/klines` → Candle.

    is_closed: `closeTime < server_time_ms` (якщо не передано — порівняння з ts_ingest_ns; це лише
    класифікація, у жодне часове поле ts_ingest не потрапляє). ts_event_ns — момент, станом на який біржа
    зафіксувала значення: для закритого бару closeTime; для НЕЗАКРИТОГО з відомим server_time_ms — сам
    server_time_ms (closeTime ще в майбутньому, і з ним новіший зріз того самого бару мав би той самий
    ts_event, тож дедуплікатор лишав би застарілий); без server_time_ms — closeTime. vwap = None.
    """
    _check_ingest(ts_ingest_ns)
    if not isinstance(row, list | tuple):
        _fail("kline row must be a JSON array", "row", _BIN)
    if len(row) != REST_KLINE_LEN:
        _fail(f"kline row has {len(row)} fields, expected {REST_KLINE_LEN}",
              f"row[{min(len(row), REST_KLINE_LEN)}]", _BIN)
    step_ms = TF_MS.get(tf)
    if step_ms is None:
        _fail(f"unsupported timeframe {tf!r}", "tf", _BIN)
    open_ms = _int(row[0], "row[0]", _BIN)
    close_ms = _int(row[6], "row[6]", _BIN)
    if close_ms != open_ms + step_ms - 1:
        _fail(f"closeTime {close_ms} != openTime + {step_ms} - 1", "row[6]", _BIN)
    for i in (9, 10, 11):
        _dec(row[i], f"row[{i}]", _BIN)
    ref_ms = server_time_ms if server_time_ms is not None else ts_ingest_ns // NS_PER_MS
    open_ns = ms_to_ns(open_ms)
    close_ns = ms_to_ns(close_ms)
    is_closed = close_ms < ref_ms
    ts_event_ns = close_ns if is_closed or server_time_ms is None else ms_to_ns(server_time_ms)
    return _build(
        Candle, _BIN,
        instrument=instrument.symbol_canon, venue=instrument.venue, tf=tf,
        open_time_ns=open_ns, close_time_ns=close_ns,
        o=_dec(row[1], "row[1]", _BIN), h=_dec(row[2], "row[2]", _BIN),
        l=_dec(row[3], "row[3]", _BIN), c=_dec(row[4], "row[4]", _BIN),
        volume=_dec(row[5], "row[5]", _BIN), quote_volume=_dec(row[7], "row[7]", _BIN),
        trades_count=_int(row[8], "row[8]", _BIN), vwap=None,
        is_closed=is_closed, src=src,
        ts_event_ns=ts_event_ns, ts_ingest_ns=ts_ingest_ns,
        event_uid=kline_uid(instrument.venue, instrument.symbol_canon, tf, open_ns),
    )


def normalize_rest_klines(rows: Iterable[Sequence[Any]], instrument: InstrumentLike, ts_ingest_ns: int, *,
                          tf: str = "1m", server_time_ms: int | None = None,
                          src: Src = Src.REST) -> list[Candle]:
    if not isinstance(rows, list | tuple):
        _fail("klines response must be a JSON array", "$", _BIN)
    out: list[Candle] = []
    for i, row in enumerate(rows):
        try:
            out.append(normalize_rest_kline(row, instrument, ts_ingest_ns, tf=tf,
                                            server_time_ms=server_time_ms, src=src))
        except NormalizationError as e:
            raise NormalizationError(f"klines[{i}]: {e}", field=f"[{i}].{e.field}", venue=e.venue) from e
    return out


def normalize_premium_index(data: Mapping[str, Any], instrument: InstrumentLike,
                            ts_ingest_ns: int) -> MarkPrice:
    """`/fapi/v1/premiumIndex?symbol=…` → MarkPrice (ts_event = поле `time`)."""
    _check_ingest(ts_ingest_ns)
    d = _mapping(data, "$", _BIN)
    _check_fields(d, PREMIUM_INDEX_FIELDS, PREMIUM_INDEX_FIELDS, "", _BIN)
    if _str(d["symbol"], "symbol", _BIN) != instrument.symbol_venue:
        _fail(f"symbol {d['symbol']!r} != {instrument.symbol_venue!r}", "symbol", _BIN)
    _dec(d["estimatedSettlePrice"], "estimatedSettlePrice", _BIN)
    _dec(d["interestRate"], "interestRate", _BIN)
    ts_event_ns = ms_to_ns(_int(d["time"], "time", _BIN))
    return _build(
        MarkPrice, _BIN,
        instrument=instrument.symbol_canon, venue=instrument.venue,
        mark_price=_dec(d["markPrice"], "markPrice", _BIN),
        index_price=_dec(d["indexPrice"], "indexPrice", _BIN),
        funding_rate=_dec(d["lastFundingRate"], "lastFundingRate", _BIN),
        next_funding_time_ns=ms_to_ns(_int(d["nextFundingTime"], "nextFundingTime", _BIN)),
        ts_event_ns=ts_event_ns, ts_ingest_ns=ts_ingest_ns,
        event_uid=mark_uid(instrument.venue, instrument.symbol_canon, ts_event_ns),
    )


def _filter(filters: Sequence[Mapping[str, Any]], ftype: str, key: str, prefix: str) -> Decimal:
    for f in filters:
        if isinstance(f, Mapping) and f.get("filterType") == ftype:
            if key not in f:
                _fail(f"{ftype}.{key} missing", f"{prefix}.filters.{ftype}.{key}", _BIN)
            return _dec(f[key], f"{prefix}.filters.{ftype}.{key}", _BIN)
    _fail(f"filter {ftype} missing", f"{prefix}.filters.{ftype}", _BIN)


def normalize_exchange_info(data: Mapping[str, Any], symbols: Iterable[str] | None = None, *,
                            mmr: Decimal = DEFAULT_MMR) -> dict[str, Instrument]:
    """`/fapi/v1/exchangeInfo` → {symbol_venue: Instrument} для PERPETUAL-контрактів.

    exchangeInfo — довідковий документ (~25 полів на символ, Binance регулярно додає нові), тому тут
    перевіряється наявність і тип ПОТРІБНИХ полів, а зайві ігноруються (див. deviations.d/ingest_rest.md).
    mmr у публічному exchangeInfo немає → значення за замовчуванням (D-04).
    """
    d = _mapping(data, "$", _BIN)
    if "symbols" not in d or not isinstance(d["symbols"], list):
        _fail("exchangeInfo.symbols missing", "symbols", _BIN)
    wanted = {s.upper() for s in symbols} if symbols is not None else None
    out: dict[str, Instrument] = {}
    for i, raw_sym in enumerate(d["symbols"]):
        p = f"symbols[{i}]"
        s = _mapping(raw_sym, p, _BIN)
        for req in ("symbol", "baseAsset", "quoteAsset", "contractType", "filters"):
            if req not in s:
                _fail(f"{p}.{req} missing", f"{p}.{req}", _BIN)
        sym = _str(s["symbol"], f"{p}.symbol", _BIN)
        if wanted is not None and sym not in wanted:
            continue
        if s["contractType"] != "PERPETUAL":
            if wanted is None:
                continue
            _fail(f"{sym}: contractType {s['contractType']!r} is not PERPETUAL", f"{p}.contractType", _BIN)
        base = _str(s["baseAsset"], f"{p}.baseAsset", _BIN)
        quote = _str(s["quoteAsset"], f"{p}.quoteAsset", _BIN)
        filters = s["filters"]
        if not isinstance(filters, list):
            _fail("filters must be a list", f"{p}.filters", _BIN)
        try:
            canon = symbol_ref(Venue.BINANCE_USDM, sym).symbol_canon
        except NormalizationError:
            canon = make_canonical(base, quote, ContractType.PERP)
        out[sym] = _build(
            Instrument, _BIN,
            venue=Venue.BINANCE_USDM, symbol_venue=sym, symbol_canon=canon,
            base_asset=base, quote_asset=quote, contract_type=ContractType.PERP,
            tick_size=_filter(filters, "PRICE_FILTER", "tickSize", p),
            step_size=_filter(filters, "LOT_SIZE", "stepSize", p),
            min_notional=_filter(filters, "MIN_NOTIONAL", "notional", p),
            mmr=mmr,
        )
    if wanted is not None and (missing := wanted - out.keys()):
        _fail(f"symbols not found in exchangeInfo: {sorted(missing)}", f"symbols[{sorted(missing)[0]}]", _BIN)
    return out


# ---------------------------------------------------------------- REST aggTrades (добір угод, WS-04)

REST_AGG_TRADE_FIELDS: Final = frozenset({"a", "p", "q", "f", "l", "T", "m", "nq"})
REST_AGG_TRADE_REQUIRED: Final = REST_AGG_TRADE_FIELDS - {"nq"}


def normalize_rest_agg_trade(row: Mapping[str, Any], instrument: InstrumentLike, ts_ingest_ns: int) -> Trade:
    """Рядок `GET /fapi/v1/aggTrades` → Trade з тим самим event_uid, що й WS aggTrade (дедуплікація добору).

    Строга схема {a, p, q, f, l, T, m[, nq]} (у відповідях 2026 року є `nq` — як у WS, ING-04).
    Перенесено з ingest/pipeline.py на запит WS-04 (docs/deviations.d/ingest_ws.md).
    """
    venue = instrument.venue.value
    d = _mapping(row, "$", venue)
    _check_fields(d, REST_AGG_TRADE_FIELDS, REST_AGG_TRADE_REQUIRED, "", venue)
    if "nq" in d:
        _dec(d["nq"], "nq", venue)
    agg_id = _int(d["a"], "a", venue)
    return _build(
        Trade, venue,
        instrument=instrument.symbol_canon, venue=instrument.venue, agg_id=agg_id,
        first_trade_id=_int(d["f"], "f", venue), last_trade_id=_int(d["l"], "l", venue),
        price=_dec(d["p"], "p", venue), qty=_dec(d["q"], "q", venue),
        is_buyer_maker=_bool(d["m"], "m", venue),
        ts_event_ns=ms_to_ns(_int(d["T"], "T", venue)), ts_ingest_ns=ts_ingest_ns,
        event_uid=trade_uid(instrument.venue, instrument.symbol_canon, agg_id),
    )


# ---------------------------------------------------------------- REST fundingRate (історія ставок)

FUNDING_RATE_FIELDS: Final = frozenset({"symbol", "fundingTime", "fundingRate", "markPrice", "rateType"})
FUNDING_RATE_REQUIRED: Final = frozenset({"symbol", "fundingTime", "fundingRate"})


@dataclass(frozen=True, slots=True)
class FundingRate:
    """Одна ставка фінансування перпетуала (`GET /fapi/v1/fundingRate`).

    `funding_time_ns` — момент нарахування за біржею (у відповіді є мілісекундний «хвіст», напр.
    …600004 мс, він зберігається як є); `mark_price` — mark на момент нарахування (у старих записах
    Binance повертає порожній рядок → None); `rate_type` — поле `rateType` (2026: "Regular"), не
    інтерпретується.
    """

    instrument: str
    venue: Venue
    funding_time_ns: int
    funding_rate: Decimal
    mark_price: Decimal | None
    rate_type: str | None = None

    @property
    def funding_time_ms(self) -> int:
        return self.funding_time_ns // NS_PER_MS


def normalize_funding_rate(row: Mapping[str, Any], instrument: InstrumentLike) -> FundingRate:
    """Рядок `fundingRate` → FundingRate; невідоме поле / не той тип / чужий символ → NormalizationError."""
    d = _mapping(row, "$", _BIN)
    _check_fields(d, FUNDING_RATE_FIELDS, FUNDING_RATE_REQUIRED, "", _BIN)
    if _str(d["symbol"], "symbol", _BIN) != instrument.symbol_venue:
        _fail(f"symbol {d['symbol']!r} != {instrument.symbol_venue!r}", "symbol", _BIN)
    mark: Decimal | None = None
    if "markPrice" in d and d["markPrice"] != "":
        mark = _dec(d["markPrice"], "markPrice", _BIN)
        if mark <= 0:
            _fail(f"markPrice must be > 0, got {mark}", "markPrice", _BIN)
    t_ms = _int(d["fundingTime"], "fundingTime", _BIN)
    if t_ms < 0:
        _fail("fundingTime must be >= 0", "fundingTime", _BIN)
    return FundingRate(
        instrument=instrument.symbol_canon, venue=instrument.venue, funding_time_ns=ms_to_ns(t_ms),
        funding_rate=_dec(d["fundingRate"], "fundingRate", _BIN), mark_price=mark,
        rate_type=_str(d["rateType"], "rateType", _BIN) if "rateType" in d else None,
    )


def normalize_funding_rates(rows: Iterable[Mapping[str, Any]],
                            instrument: InstrumentLike) -> list[FundingRate]:
    """Пакетно; помилка у рядку i → NormalizationError(field="[i].<поле>"). Результат — за зростанням часу,
    без дублікатів часу (дублікат з іншим значенням — помилка: дві різні ставки на один момент)."""
    out: dict[int, FundingRate] = {}
    for i, r in enumerate(rows):
        try:
            fr = normalize_funding_rate(r, instrument)
        except NormalizationError as e:
            raise NormalizationError(str(e), field=f"[{i}].{e.field}", venue=_BIN) from e
        prev = out.get(fr.funding_time_ns)
        if prev is not None and prev != fr:
            _fail(f"conflicting funding records at {fr.funding_time_ns}", f"[{i}].fundingTime", _BIN)
        out[fr.funding_time_ns] = fr
    return [out[k] for k in sorted(out)]
