"""Група C брифінгу (§10), «REST-інжест»: пагінація з перекриттям, token bucket, retry, добір прогалин.

Найменування: tests/unit/test_rest_ingest.py
Призначення: клієнт і backfill поверх respx-імітації Binance, що віддає СПРАВЖНІ записані свічки
(fixtures/rest/binance_klines.json.gz) за семантикою /fapi/v1/klines. Мережі й реального сну немає:
час — ManualClock, `sleep` — ін'єктований записувач, що лише рухає годинник.
Автор: Андрій Жук, 2026.
"""

from __future__ import annotations

import gzip
import itertools
import math
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import orjson
import pytest
import respx

from fuzzhelm.core.clock import ManualClock
from fuzzhelm.core.enums import GapDetectorKind, GapStatus, Src
from fuzzhelm.ingest.backfill import SeamStatus, backfill_klines, detect_gaps, fill_gaps
from fuzzhelm.ingest.dedup import Deduplicator
from fuzzhelm.ingest.normalize import normalize_exchange_info, normalize_premium_index, normalize_rest_klines
from fuzzhelm.ingest.ratelimit import TokenBucket, binance_request_bucket, klines_weight, request_weight
from fuzzhelm.ingest.rest_client import BinanceApiError, BinanceRestClient
from fuzzhelm.ingest.retry import (
    RateLimitBannedError,
    RetryExhaustedError,
    RetryPolicy,
    error_for_response,
    header_int,
    parse_retry_after,
)
from fuzzhelm.ingest.symbols import BTC_USDT_PERP

ROOT = Path(__file__).resolve().parents[2]
REST = ROOT / "fixtures" / "rest"
BINANCE = "https://fapi.binance.com"
ROWS: list[list[Any]] = orjson.loads(gzip.decompress((REST / "binance_klines.json.gz").read_bytes()))
MIN_MS = 60_000
FIRST_OPEN = ROWS[0][0]
LAST_OPEN = ROWS[-1][0]
NOW_MS = LAST_OPEN + MIN_MS + 30_000          # біржа «зараз»: останній бар фікстури вже закритий
NOW_NS = NOW_MS * 1_000_000


def _json(name: str) -> Any:
    return orjson.loads((REST / name).read_bytes())


class Sleeper:
    """Ін'єктований sleep: записує паузу і рухає ManualClock — жодного реального очікування."""

    def __init__(self, clock: ManualClock) -> None:
        self.clock = clock
        self.calls: list[float] = []

    async def __call__(self, dt: float) -> None:
        self.calls.append(dt)
        self.clock.advance(math.ceil(dt * 1e9))


class FakeBinanceKlines:
    """/fapi/v1/klines: рядки з openTime ∈ [startTime, endTime], перші `limit`; X-MBX-USED-WEIGHT-1M
    накопичується. `hide_after_first` / `mutate_after_first` змінюють «погляд біржі» після першої
    сторінки (розрив на стику)."""

    def __init__(self, rows: list[list[Any]], *, hidden: frozenset[int] = frozenset(),
                 hide_after_first: frozenset[int] = frozenset(),
                 mutate_after_first: dict[int, list[Any]] | None = None) -> None:
        self.rows = rows
        self.hidden = hidden
        self.hide_after_first = hide_after_first
        self.mutate_after_first = mutate_after_first or {}
        self.calls: list[dict[str, int | None]] = []
        self.used = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        q = request.url.params
        start = int(q["startTime"])
        end = int(q["endTime"]) if "endTime" in q else None
        limit = int(q["limit"])
        later = bool(self.calls)
        self.calls.append({"startTime": start, "endTime": end, "limit": limit})
        hidden = self.hidden | (self.hide_after_first if later else frozenset())
        page = [self.mutate_after_first.get(r[0], r) if later else r for r in self.rows
                if r[0] >= start and (end is None or r[0] <= end) and r[0] not in hidden][:limit]
        self.used += klines_weight(limit)
        return httpx.Response(200, content=orjson.dumps(page),
                              headers={"X-MBX-USED-WEIGHT-1M": str(self.used)})


def _client(http: httpx.AsyncClient, clock: ManualClock, *, retry: RetryPolicy | None = None,
            bucket: TokenBucket | None = None, sleep: Sleeper | None = None) -> BinanceRestClient:
    s = sleep or Sleeper(clock)
    return BinanceRestClient(BINANCE, http, bucket or TokenBucket(2400, 40, clock, sleep=s),
                             retry or RetryPolicy(base=0.5, cap=30.0, max_attempts=4, rng_seed=7),
                             clock=clock, sleep=s)


@asynccontextmanager
async def _binance(handler: Callable[[httpx.Request], httpx.Response] | list[Any]) -> AsyncIterator[Any]:
    async with respx.mock(assert_all_called=False) as router:
        route = router.route(method="GET", host="fapi.binance.com", path="/fapi/v1/klines").mock(
            side_effect=handler)
        async with httpx.AsyncClient() as http:
            yield route, http


# ---------------------------------------------------------------- C7


async def test_paginator_stitches_segments_with_one_bar_overlap() -> None:
    clock = ManualClock(NOW_NS)
    fake = FakeBinanceKlines(ROWS)
    async with _binance(fake) as (_, http):
        res = await backfill_klines(_client(http, clock), "BTCUSDT", FIRST_OPEN, LAST_OPEN, limit=500,
                                    now_ms=NOW_MS)
    # 500 + 6·499 ≥ 3000 → 7 сторінок; кожна наступна починається рівно з останнього бару попередньої
    assert res.requests == len(fake.calls) == 7
    assert fake.calls[0]["startTime"] == FIRST_OPEN
    for prev, nxt in itertools.pairwise(res.segments):
        assert nxt.start_ms == prev.last_open_ms == nxt.first_open_ms
        assert nxt.seam is SeamStatus.OK
    assert sum(s.rows for s in res.segments) == len(ROWS) + len(res.segments) - 1   # стики — по одному бару
    assert res.ok and res.gaps == () and res.overlap_mismatches == ()
    assert [c.open_time_ns // 1_000_000 for c in res.candles] == [r[0] for r in ROWS]
    expected = normalize_rest_klines(ROWS, BTC_USDT_PERP, NOW_NS, server_time_ms=NOW_MS)
    assert [c.model_dump(exclude={"ts_ingest_ns"}) for c in res.candles] == \
        [c.model_dump(exclude={"ts_ingest_ns"}) for c in expected]
    assert all(c.is_closed and c.src is Src.REST for c in res.candles)


# ---------------------------------------------------------------- C8


async def test_paginator_detects_gap_in_overlap() -> None:
    clock = ManualClock(NOW_NS)
    # після першої сторінки біржа «втратила» бари 499…502 — зокрема бар стику 499
    missing = frozenset(ROWS[i][0] for i in range(499, 503))
    fake = FakeBinanceKlines(ROWS, hide_after_first=missing)
    async with _binance(fake) as (_, http):
        res = await backfill_klines(_client(http, clock), "BTCUSDT", FIRST_OPEN, LAST_OPEN, limit=500,
                                    now_ms=NOW_MS)
    assert res.segments[1].seam is SeamStatus.MISSING
    assert all(s.seam is SeamStatus.OK for s in res.segments[2:])
    assert not res.ok
    # бар 499 уже є з першої сторінки, отже справжня прогалина — 500…502
    assert len(res.gaps) == 1
    g = res.gaps[0]
    assert (g.ts_lo_ns, g.ts_hi_ns, g.expected_count) == (ROWS[500][0] * 10**6, ROWS[502][0] * 10**6, 3)
    assert (g.instrument, g.tf, g.detector, g.status) == ("BTC-USDT-PERP", "1m", GapDetectorKind.TIME,
                                                          GapStatus.OPEN)
    assert len(res.candles) == len(ROWS) - 3


async def test_overlap_bar_with_different_content_is_flagged() -> None:
    clock = ManualClock(NOW_NS)
    seam_row = [*ROWS[499]]
    seam_row[5] = "999.999"                           # біржа «переписала» обсяг бару стику
    fake = FakeBinanceKlines(ROWS, mutate_after_first={ROWS[499][0]: seam_row})
    async with _binance(fake) as (_, http):
        res = await backfill_klines(_client(http, clock), "BTCUSDT", FIRST_OPEN, LAST_OPEN, limit=500,
                                    now_ms=NOW_MS)
    assert res.segments[1].seam is SeamStatus.MISMATCH and res.gaps == ()
    (mm,) = res.overlap_mismatches
    assert mm.open_time_ms == ROWS[499][0]
    assert mm.previous == tuple(ROWS[499]) and mm.current == tuple(seam_row)
    assert res.candles[499].volume == Decimal("999.999")    # зберігається новіша версія
    assert not res.ok


async def test_gap_inside_segment_and_in_progress_bar_excluded() -> None:
    clock = ManualClock(NOW_NS)
    in_progress = [LAST_OPEN + MIN_MS, "81104.60", "81110.00", "81100.00", "81105.00", "1.000",
                   LAST_OPEN + 2 * MIN_MS - 1, "81105.00000", 10, "0.500", "40552.50000", "0"]   # синтетичний
    fake = FakeBinanceKlines([*ROWS, in_progress], hidden=frozenset(ROWS[i][0] for i in range(100, 105)))
    pages: list[int] = []

    async def on_page(cs: list[Any]) -> None:
        pages.append(len(cs))

    async with _binance(fake) as (_, http):
        res = await backfill_klines(_client(http, clock), "BTCUSDT", FIRST_OPEN, LAST_OPEN + MIN_MS,
                                    limit=1500, now_ms=NOW_MS, on_page=on_page)
    assert [(g.ts_lo_ns // 10**6, g.expected_count) for g in res.gaps] == [(ROWS[100][0], 5)]
    assert res.candles[-1].open_time_ns == LAST_OPEN * 10**6        # незакритий бар не потрапив
    assert sum(pages) == len(res.candles) == len(ROWS) - 5


# ---------------------------------------------------------------- C9


async def test_token_bucket_blocks_on_weight_exhaustion() -> None:
    clock = ManualClock(NOW_NS)
    admitted_while_sleeping: list[int] = []
    sleeps: list[float] = []
    bucket: TokenBucket

    async def fake_sleep(dt: float) -> None:
        sleeps.append(dt)
        admitted_while_sleeping.append(bucket.acquired_weight)
        clock.advance(math.ceil(dt * 1e9))

    bucket = TokenBucket(capacity=2400, refill_per_s=40, clock=clock, sleep=fake_sleep)
    w = klines_weight(1500)
    assert w == 10
    for _ in range(240):                                # 240 × 10 = 2400 = уся ємність
        assert await bucket.acquire(w) == 0.0
    assert sleeps == [] and bucket.tokens == pytest.approx(0.0, abs=1e-9)
    assert bucket.try_acquire(w) is False               # неблокуюча спроба відмовляє
    assert bucket.wait_time(w) == pytest.approx(0.25)   # дефіцит 10 / 40 за с
    t0 = clock.now_ns()
    waited = await bucket.acquire(w)                    # блокує, доки відро не наповниться
    assert sleeps == [0.25] and waited == 0.25
    assert admitted_while_sleeping == [2400]            # під час очікування запит НЕ пропущено
    assert clock.now_ns() - t0 == 250_000_000
    assert bucket.acquired_weight == 2410
    # часткове наповнення: через 0.1 с є лише 4 токени — вага 10 чекає ще 0.15 с
    clock.advance(100_000_000)
    assert await bucket.acquire(w) == pytest.approx(0.15)
    with pytest.raises(ValueError, match="never admissible"):
        await bucket.acquire(2401)


def test_bucket_window_bound_default_is_provably_within_binance_limit() -> None:
    """Жадібний споживач 3 хв: максимум ваги в будь-якому 60-с вікні. Відро C=2400, r=40 (буквальне
    прочитання «2400/хв») пропускає майже 2× ліміт; фабрика за замовчуванням (C + 60·r = 2400) — ні."""

    def worst_window(bucket: TokenBucket, clock: ManualClock) -> int:
        taken: list[tuple[int, int]] = []
        step = 50_000_000                                 # 50 мс
        for _ in range(int(180e9 // step)):
            while bucket.try_acquire(10):
                taken.append((clock.now_ns(), 10))
            clock.advance(step)
        best, lo, acc = 0, 0, 0
        for t, wgt in taken:                              # ковзне вікно [t − 60 с, t]
            acc += wgt
            while taken[lo][0] <= t - 60_000_000_000:
                acc -= taken[lo][1]
                lo += 1
            best = max(best, acc)
        return best

    c1 = ManualClock(0)
    naive = worst_window(TokenBucket(2400, 40, c1), c1)
    c2 = ManualClock(0)
    safe_bucket = binance_request_bucket(c2)
    safe = worst_window(safe_bucket, c2)
    assert naive > 4700                                  # ≈ C + 60·r = 4800 > 2400
    assert safe <= 2400 == safe_bucket.max_admitted(60)


def test_klines_weight_table_matches_measured_headers() -> None:
    measured = _json("capture_meta.json")["kline_weight_measurements"]
    assert len(measured) >= 8
    for m in measured:
        assert klines_weight(m["limit"]) == m["weight"], m
        assert request_weight("/fapi/v1/klines", {"limit": m["limit"]}) == m["weight"]
    assert request_weight("/fapi/v1/premiumIndex", {"symbol": "BTCUSDT"}) == 1
    assert request_weight("/fapi/v1/premiumIndex") == 10


async def test_used_weight_header_resyncs_bucket() -> None:
    clock = ManualClock(NOW_NS)
    resp = httpx.Response(200, content=orjson.dumps(ROWS[:2]), headers={"X-MBX-USED-WEIGHT-1M": "2390"})
    async with _binance([resp]) as (_, http):
        client = _client(http, clock)
        await client.klines("BTCUSDT", "1m", FIRST_OPEN, ROWS[1][0], limit=2)
    assert client.last_used_weight == 2390
    assert client.bucket.tokens <= 10 + 1e-9              # біржа бачить 2390 з 2400 — лишилось ≤ 10
    assert client.bucket.try_acquire(10) and not client.bucket.try_acquire(1)


# ---------------------------------------------------------------- C10


async def test_retry_after_header_honored() -> None:
    clock = ManualClock(NOW_NS)
    sleeper = Sleeper(clock)
    limited = httpx.Response(429, headers={"Retry-After": "7"},
                             content=b'{"code":-1003,"msg":"Too many requests."}')
    ok = httpx.Response(200, content=orjson.dumps(ROWS[:3]))
    async with _binance([limited, ok]) as (route, http):
        client = _client(http, clock, sleep=sleeper)
        rows = await client.klines("BTCUSDT", "1m", FIRST_OPEN, ROWS[2][0], limit=3)
    assert rows == ROWS[:3] and route.call_count == 2
    assert sleeper.calls == [7.0]                         # рівно Retry-After, без джитера
    assert client.retry.sleeps == [7.0]

    # 418 (бан IP) із помірною паузою — теж шанується
    clock2 = ManualClock(NOW_NS)
    s2 = Sleeper(clock2)
    banned = httpx.Response(418, headers={"Retry-After": "120"}, content=b'{"code":-1003,"msg":"banned"}')
    async with _binance([banned, httpx.Response(200, content=b"[]")]) as (_, http):
        assert await _client(http, clock2, sleep=s2).klines("BTCUSDT", limit=5) == []
    assert s2.calls == [120.0]

    # без заголовка — повний джитер у межах [0, base·2^0]
    clock3 = ManualClock(NOW_NS)
    s3 = Sleeper(clock3)
    async with _binance([httpx.Response(503), httpx.Response(200, content=b"[]")]) as (_, http):
        await _client(http, clock3, sleep=s3).klines("BTCUSDT", limit=5)
    assert len(s3.calls) == 1 and 0.0 <= s3.calls[0] <= 0.5

    # багатогодинний бан — не спимо, а відмовляємо одразу
    clock4 = ManualClock(NOW_NS)
    s4 = Sleeper(clock4)
    long_ban = httpx.Response(418, headers={"Retry-After": "7200"})
    async with _binance([long_ban]) as (route4, http):
        with pytest.raises(RateLimitBannedError) as ei:
            await _client(http, clock4, sleep=s4).klines("BTCUSDT", limit=5)
    assert ei.value.retry_after_s == 7200.0 and s4.calls == [] and route4.call_count == 1


async def test_retry_exhausted_on_persistent_5xx_and_transport_errors_retried() -> None:
    clock = ManualClock(NOW_NS)
    s = Sleeper(clock)
    policy = RetryPolicy(base=0.5, cap=30.0, max_attempts=3, rng_seed=11)
    async with _binance([httpx.Response(503) for _ in range(3)]) as (route, http):
        with pytest.raises(RetryExhaustedError) as ei:
            await _client(http, clock, retry=policy, sleep=s).klines("BTCUSDT", limit=5)
    assert route.call_count == 3 and ei.value.attempts == 3
    assert len(s.calls) == 2 and 0 <= s.calls[0] <= 0.5 and 0 <= s.calls[1] <= 1.0
    clock2 = ManualClock(NOW_NS)
    s2 = Sleeper(clock2)
    async with _binance([httpx.ConnectTimeout("boom"), httpx.Response(200, content=b"[]")]) as (route2, http):
        assert await _client(http, clock2, sleep=s2).klines("BTCUSDT", limit=5) == []
    assert route2.call_count == 2 and len(s2.calls) == 1


async def test_binance_api_error_is_not_retried() -> None:
    clock = ManualClock(NOW_NS)
    body = (REST / "binance_error_invalid_symbol.json").read_bytes()    # справжня відповідь Binance
    async with _binance([httpx.Response(400, content=body)]) as (route, http):
        with pytest.raises(BinanceApiError) as ei:
            await _client(http, clock).klines("NOSUCHSYMBOL", limit=5)
    assert (ei.value.status, ei.value.code, ei.value.msg) == (400, -1121, "Invalid symbol.")
    assert route.call_count == 1


async def test_reference_endpoints_and_clock_offset() -> None:
    class SteppingClock:
        """Показники годинника до/після кожного /time: RTT 40, 10 і 80 мс."""

        def __init__(self, readings: list[int]) -> None:
            self.readings = iter(readings)

        def now_ns(self) -> int:
            return next(self.readings)

    t0 = 1_789_759_537_000_000_000
    readings = [t0, t0 + 40_000_000, t0 + 1_000_000_000, t0 + 1_010_000_000, t0 + 2_000_000_000,
                t0 + 2_080_000_000]
    server = [1_789_759_537_671, 1_789_759_538_655, 1_789_759_539_700]
    bucket_clock = ManualClock(t0)
    async with respx.mock(assert_all_called=True) as router:
        router.get(f"{BINANCE}/fapi/v1/time").mock(side_effect=[
            httpx.Response(200, content=orjson.dumps({"serverTime": s})) for s in server])
        router.get(f"{BINANCE}/fapi/v1/exchangeInfo").mock(
            return_value=httpx.Response(200, content=(REST / "exchange_info.json").read_bytes()))
        router.route(method="GET", host="fapi.binance.com", path="/fapi/v1/premiumIndex").mock(
            return_value=httpx.Response(200, content=(REST / "premium_index.json").read_bytes()))
        async with httpx.AsyncClient() as http:
            client = BinanceRestClient(BINANCE, http, TokenBucket(2400, 40, bucket_clock), RetryPolicy(),
                                       clock=SteppingClock(readings))
            offset = await client.server_time_offset_ms(samples=3)
            client.clock = ManualClock(NOW_NS)
            info = await client.exchange_info()
            prem = await client.premium_index("BTCUSDT")
    # найменший RTT (10 мс) у другому замірі: 538655 − (1789759538000 + 5) = 650 мс
    assert offset == 650
    assert normalize_exchange_info(info, ["BTCUSDT"])["BTCUSDT"].tick_size == Decimal("0.10")
    mark = normalize_premium_index(prem, BTC_USDT_PERP, NOW_NS)
    assert mark.funding_rate == Decimal(prem["lastFundingRate"])


# ---------------------------------------------------------------- C13


async def test_gap_detected_and_backfilled_idempotently() -> None:
    clock = ManualClock(NOW_NS)
    full = normalize_rest_klines(ROWS[:300], BTC_USDT_PERP, NOW_NS, server_time_ms=NOW_MS)
    holey = [c for i, c in enumerate(full) if not 100 <= i < 110]
    # 1) прогалину виявлено за неперервністю open_time
    gaps = detect_gaps(holey, "1m")
    assert [(g.ts_lo_ns, g.ts_hi_ns, g.expected_count) for g in gaps] == \
        [(full[100].open_time_ns, full[109].open_time_ns, 10)]
    fake = FakeBinanceKlines(ROWS)
    dedup = Deduplicator()
    async with _binance(fake) as (_, http):
        client = _client(http, clock)
        # 2) REST (мок) добирає прогалину
        r1 = await fill_gaps(client, "BTCUSDT", holey, gaps, dedup=dedup, now_ms=NOW_MS)
        calls_after_first = len(fake.calls)
        clock.advance(5 * 60 * 10**9)                     # повторний запуск пізніше: інший ts_ingest
        # 3) повторний добір тих самих прогалин нічого не додає
        r2 = await fill_gaps(client, "BTCUSDT", r1.candles, gaps, dedup=dedup, now_ms=NOW_MS)
        r3 = await fill_gaps(client, "BTCUSDT", r1.candles, gaps, now_ms=NOW_MS)   # і зі свіжим Deduplicator
    assert [c.open_time_ns for c in r1.added] == [c.open_time_ns for c in full[100:110]]
    assert [c.event_uid for c in r1.candles] == [c.event_uid for c in full]
    assert detect_gaps(r1.candles, "1m") == []
    assert [(g.status, g.filled_rows, g.attempts) for g in r1.gaps] == [(GapStatus.FILLED, 10, 1)]
    assert len(fake.calls) > calls_after_first            # REST справді опитано вдруге…
    assert r2.added == () and r3.added == ()              # …але дедуплікація за event_uid нічого не додала
    assert r2.candles == r1.candles and r3.candles == r1.candles
    assert r2.gaps[0].status is GapStatus.FILLED


async def test_unfillable_gap_when_venue_has_no_data() -> None:
    clock = ManualClock(NOW_NS)
    full = normalize_rest_klines(ROWS[:50], BTC_USDT_PERP, NOW_NS, server_time_ms=NOW_MS)
    holey = [c for i, c in enumerate(full) if i not in (20, 21)]
    fake = FakeBinanceKlines(ROWS, hidden=frozenset({ROWS[20][0], ROWS[21][0]}))   # у біржі теж немає
    async with _binance(fake) as (_, http):
        r = await fill_gaps(_client(http, clock), "BTCUSDT", holey, detect_gaps(holey, "1m"), now_ms=NOW_MS)
    assert [(g.status, g.filled_rows) for g in r.gaps] == [(GapStatus.UNFILLABLE, 0)] and r.added == ()


def test_parse_retry_after_forms() -> None:
    now_ns = 1_789_759_537_000_000_000                       # 2026-09-18 19:25:37 UTC
    assert parse_retry_after("7") == 7.0
    assert parse_retry_after(" 120 ") == 120.0
    assert parse_retry_after("Fri, 18 Sep 2026 19:26:07 GMT", now_ns) == 30.0
    assert parse_retry_after("Fri, 18 Sep 2026 19:20:00 GMT", now_ns) == 0.0   # дата в минулому
    assert parse_retry_after("Fri, 18 Sep 2026 19:26:07 GMT") is None            # без годинника — не вгадуємо
    assert parse_retry_after("soon", now_ns) is None and parse_retry_after(None) is None
    assert parse_retry_after("Fri, 18 Sep 2026 19:26:07 -0000", now_ns) == 30.0  # naive-дата = UTC


def test_non_ascii_digit_headers_do_not_crash_classification() -> None:
    # байт 0xB2 декодується latin-1 як «²»: str.isdigit() каже True, але int("²") падає ValueError
    resp = httpx.Response(429, headers=[(b"Retry-After", b"\xb2"), (b"X-MBX-USED-WEIGHT-1M", b"\xb9\xb2")])
    assert resp.headers["Retry-After"] == "\u00b2"
    err = error_for_response(resp, 0)
    assert err is not None and err.status == 429 and err.retry_after_s is None   # → звичайний backoff
    assert header_int(resp.headers, "X-MBX-USED-WEIGHT-1M") is None
    assert parse_retry_after("\u0661\u0662") is None                             # арабсько-індійські цифри


async def test_backfill_without_exchange_time_keeps_clock_guard() -> None:
    """Без now_ms бар, що закрився менше ніж clock_guard_ms тому за ЛОКАЛЬНИМ годинником, не вважається
    закритим (годинник міг випереджати біржу, а помилково закритий бар upsert уже не виправить)."""
    local_now_ms = LAST_OPEN + MIN_MS + 1_000          # останній бар «закрився» 1 с тому (наш годинник)
    fake = FakeBinanceKlines(ROWS[-10:])
    async with _binance(fake) as (_, http):
        client = _client(http, ManualClock(local_now_ms * 1_000_000))
        guarded = await backfill_klines(client, "BTCUSDT", ROWS[-10][0], LAST_OPEN, limit=100)
        unguarded = await backfill_klines(client, "BTCUSDT", ROWS[-10][0], LAST_OPEN, limit=100,
                                          clock_guard_ms=0)
    assert guarded.candles[-1].open_time_ns == ROWS[-2][0] * 10**6 and len(guarded.candles) == 9
    assert guarded.gaps == ()                             # це не прогалина: бар просто ще не підтверджено
    assert unguarded.candles[-1].open_time_ns == LAST_OPEN * 10**6 and len(unguarded.candles) == 10


def test_token_bucket_rejects_non_finite_parameters() -> None:
    clock = ManualClock(0)
    for cap, rate in ((float("nan"), 1.0), (10.0, float("nan")), (float("inf"), 1.0), (10.0, float("inf")),
                      (0.0, 1.0), (10.0, -1.0)):
        with pytest.raises(ValueError, match="finite"):
            TokenBucket(cap, rate, clock)
