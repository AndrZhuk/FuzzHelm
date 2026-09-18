"""WebSocket-інжест: рекордер/реплей, backoff і сторож тиші, клієнт на фіктивних з'єднаннях,
детектор прогалин, агрегатор свічок, конвеєр.

Найменування: tests/unit/test_ws_ingest.py
Призначення: інваріанти фази 2 брифінгу без мережі й без сну. Дані — справжня 4-хв сесія
fixtures/ws/sample_btcusdt_4m.jsonl.gz; час — ManualClock/FrameClock; пауза backoff і очікування
кадру — ін'єктовані фіктивні функції, що лише рухають годинник.
Автор: Андрій Жук, 2026.
"""

from __future__ import annotations

import asyncio
import gzip
import json
import math
from collections import deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import orjson
import pytest
import respx
from websockets.datastructures import Headers
from websockets.exceptions import ConnectionClosedError, InvalidStatus
from websockets.frames import Close as WsClose
from websockets.http11 import Response as WsResponse

from fuzzhelm.config import Settings
from fuzzhelm.core.clock import ManualClock
from fuzzhelm.core.dto import Candle, MarketEvent, Trade
from fuzzhelm.core.enums import GapDetectorKind, GapStatus, Src, Stream, Venue
from fuzzhelm.core.errors import MainnetHostRejected, NormalizationError
from fuzzhelm.core.journal import EventJournal, verify_chain
from fuzzhelm.core.money import quantize_price
from fuzzhelm.core.ports import MarketFeed
from fuzzhelm.features.convert import bar_from_candle
from fuzzhelm.ingest.candles import CandleAggregator
from fuzzhelm.ingest.gap_detector import GapDetector, GapRecord, transition
from fuzzhelm.ingest.normalize import normalize_binance, normalize_exchange_info, normalize_rest_klines
from fuzzhelm.ingest.pipeline import (
    IngestPipeline,
    PipelineSinks,
    RestBackfiller,
    SessionCapture,
    fetch_agg_trades,
    journal_sink,
    normalize_rest_agg_trade,
    offline_rest_client,
    recovery_stats,
    replay_session,
    session_reference,
)
from fuzzhelm.ingest.ratelimit import TokenBucket
from fuzzhelm.ingest.reconnect import (
    Backoff,
    CloseClass,
    HeartbeatWatchdog,
    classify_close_code,
    classify_http_status,
)
from fuzzhelm.ingest.recorder import ControlRecord, RawFrame, SessionRecorder, dumps_record, write_session
from fuzzhelm.ingest.replay import (
    FixtureRestHandler,
    FrameClock,
    ReplayFeed,
    RestFixture,
    agg_trade_rest_row,
    iter_frames,
    iter_records,
    read_session,
)
from fuzzhelm.ingest.rest_client import BinanceRestClient
from fuzzhelm.ingest.retry import RetryPolicy
from fuzzhelm.ingest.symbols import BTC_USDT_PERP
from fuzzhelm.ingest.ws_client import BinanceWsClient, WsFatalError, classify_exception
from fuzzhelm.quality.anomaly_mlp import (
    AnomalyAutoencoder,
    AnomalyFeatureExtractor,
    AnomalyScorer,
    AnomalyVerdict,
    feature_matrix,
)

ROOT = Path(__file__).resolve().parents[2]
SAMPLE = ROOT / "fixtures" / "ws" / "sample_btcusdt_4m.jsonl.gz"
PATHO = ROOT / "fixtures" / "ws" / "pathological"
REST = ROOT / "fixtures" / "rest"
INSTR = normalize_exchange_info(json.loads((REST / "exchange_info.json").read_text()), ["BTCUSDT"])["BTCUSDT"]
SESSION = read_session(SAMPLE)
FRAMES = SESSION.frames
MIN_NS = 60_000_000_000
S = 1_000_000_000


async def _no_sleep(_: float) -> None:
    return None


def _events(frames: list[RawFrame] | tuple[RawFrame, ...]) -> list[MarketEvent]:
    return [normalize_binance(f.stream, f.data, f.ts_ingest_ns, INSTR, src=Src.REPLAY) for f in frames]


KLINE_FRAMES = [f for f in FRAMES if "@kline_" in f.stream]
TRADE_FRAMES = [f for f in FRAMES if f.stream.endswith("@aggTrade")]
CLOSED_OPENS = sorted(int(f.data["k"]["t"]) * 1_000_000 for f in KLINE_FRAMES if f.data["k"]["x"])


def _market_only(src: Path, dst: Path) -> Path:
    """Копія сесії без кадрів depth (/public/stream): юніт-тести логіки свічок і угод їх не потребують,
    а нормалізація 40 рівнів книги — найдорожча частина реплею. Повні сесії відтворює tests/e2e."""
    s = read_session(src)
    keep = [i for i in s.items if not (isinstance(i, RawFrame) and i.conn == "public")]
    return write_session(dst, s.header, keep, ended_ns=(s.footer or {}).get("ended_ns", 0))


# ================================================================ рекордер і реплей


def test_recorder_writes_contract_format_and_replay_reads_it_back(tmp_path: Path) -> None:
    clock = ManualClock(1_000)
    path = tmp_path / "s.jsonl.gz"
    urls = {"market": "wss://fstream.binance.com/market/stream?streams=btcusdt@aggTrade"}
    rec = SessionRecorder(path, symbol="BTCUSDT", urls=urls, clock=clock, minutes=0.5)
    with rec:
        assert rec.part_path.exists() and not path.exists()           # поки пишеться — лише .part
        rec.write_control("market", "connected", urls["market"])
        for f in FRAMES[:20]:
            clock.set(f.ts_ingest_ns)
            rec.write(f)
        clock.advance(S)
    assert path.exists() and not rec.part_path.exists()
    lines = gzip.decompress(path.read_bytes()).decode().splitlines()
    head, *_, foot = (json.loads(x) for x in lines)
    assert head == {"v": 1, "kind": "header", "venue": "BINANCE_USDM", "symbol": "BTCUSDT", "urls": urls,
                    "started_ns": 1_000, "minutes": 0.5}
    assert foot == {"v": 1, "kind": "footer", "ended_ns": FRAMES[19].ts_ingest_ns + S, "frames": 20}
    # рядок кадру — побайтово як у scripts/record_ws_session.py (json.dumps, компактні роздільники)
    assert lines[2] == json.dumps(FRAMES[0].to_record(), separators=(",", ":"))
    assert lines[2] == dumps_record(FRAMES[0].to_record())
    back = read_session(path)
    assert back.frames == tuple(FRAMES[:20])
    assert isinstance(back.items[0], ControlRecord) and back.items[0].event == "connected"


def test_recorder_abort_leaves_no_fixture(tmp_path: Path) -> None:
    path = tmp_path / "x.jsonl.gz"
    rec = SessionRecorder(path, symbol="BTCUSDT", urls={}, clock=ManualClock())
    with pytest.raises(RuntimeError), rec as r:
        r.write(FRAMES[0])
        raise RuntimeError("boom")
    assert not path.exists() and not path.with_name(path.name + ".part").exists()


def test_write_session_is_byte_reproducible(tmp_path: Path) -> None:
    a, b = tmp_path / "a.jsonl.gz", tmp_path / "b" / "a.jsonl.gz"
    for p in (a, b):
        write_session(p, SESSION.header, SESSION.items[:100], ended_ns=123)
    assert a.read_bytes() == b.read_bytes()


async def test_replay_feed_is_market_feed_in_ingest_order() -> None:
    feed = ReplayFeed(SAMPLE)
    assert isinstance(feed, MarketFeed)
    evs = [ev async for ev in feed]
    assert len(evs) == len(FRAMES) == SESSION.footer["frames"] == 7013      # type: ignore[index]
    ts = [e.ts_ingest_ns for e in evs]
    assert ts == sorted(ts) and feed.stats.clock_regressions == 0
    assert {type(e).__name__ for e in evs} == {"Candle", "Trade", "MarkPrice", "BookSnapshot"}
    assert all(e.src is Src.REPLAY for e in evs if isinstance(e, Candle))
    assert feed.stats.frames == 7013 and feed.stats.controls == 2 and feed.stats.errors == 0
    raw = list(iter_frames(SAMPLE))
    assert len(raw) == 7013 and raw[0]["kind"] == "frame"
    assert [r["kind"] for r in iter_records(SAMPLE)][:1] == ["header"]


async def test_replay_pacing_uses_injected_clock_and_sleep() -> None:
    clock = ManualClock(0)
    waits: list[float] = []

    async def fake_sleep(dt: float) -> None:
        waits.append(dt)
        clock.advance(math.ceil(dt * 1e9))

    feed = ReplayFeed(SAMPLE, speed=60.0, clock=clock, sleep=fake_sleep)
    n = sum([1 async for _ in feed.items()])
    assert n == 7015
    span_s = (FRAMES[-1].ts_ingest_ns - SESSION.items[0].ts_ingest_ns) / 1e9
    assert feed.stats.waited_s == pytest.approx(span_s / 60.0, rel=1e-6)       # записаний темп ÷ 60
    assert clock.now_ns() / 1e9 == pytest.approx(span_s / 60.0, rel=1e-6)
    assert all(w > 0 for w in waits)
    with pytest.raises(ValueError):
        ReplayFeed(SAMPLE, speed=0)


async def test_replay_order_arrival_vs_ingest_ts_on_clock_jump() -> None:
    path = PATHO / "clock_jump.jsonl.gz"
    feed = ReplayFeed(path)
    arrival = [i.ts_ingest_ns async for i in feed.items()]
    assert feed.stats.clock_regressions == 1                    # один стрибок назад
    by_ts = [i.ts_ingest_ns async for i in ReplayFeed(path, order="ingest_ts").items()]
    assert by_ts == sorted(arrival) and by_ts != arrival


def test_ws_closed_klines_equal_recorded_rest_rows() -> None:
    """WS-закриття x=true і записаний REST /fapi/v1/klines для тих самих хвилин збігаються дослівно —
    тому REST-добір відновлює саме ту свічку, що загубилась у потоці."""
    rest = {r[0]: r for r in orjson.loads(gzip.decompress((REST / "binance_klines.json.gz").read_bytes()))}
    closed = [f.data["k"] for f in KLINE_FRAMES if f.data["k"]["x"]]
    assert len(closed) == 3
    for k in closed:
        assert rest[k["t"]] == [k["t"], k["o"], k["h"], k["l"], k["c"], k["v"], k["T"], k["q"], k["n"],
                                k["V"], k["Q"], "0"]


# ================================================================ backoff, сторож, класифікація


def test_backoff_exponential_capped_jittered_and_reproducible() -> None:
    b = Backoff(base_s=0.5, cap_s=8.0, seed=42)
    ds = [b.next_delay() for _ in range(12)]
    ceilings = [min(8.0, 0.5 * 2**k) for k in range(12)]
    assert all(c / 2 <= d <= c for d, c in zip(ds, ceilings, strict=True))      # equal jitter
    assert max(ds) <= 8.0 and ds[-1] >= 4.0                                     # вийшло на стелю
    same, other = Backoff(base_s=0.5, cap_s=8.0, seed=42), Backoff(base_s=0.5, cap_s=8.0, seed=43)
    assert [same.next_delay() for _ in range(12)] == ds          # той самий seed — та сама серія
    assert [other.next_delay() for _ in range(12)] != ds
    b.reset()
    assert b.attempt == 0 and b.next_delay() <= 0.5
    full = Backoff(base_s=1, cap_s=4, jitter="full", seed=1)
    assert all(0 <= full.next_delay() <= min(4, 2**k) for k in range(10))
    plain = Backoff(base_s=1, cap_s=4, jitter="none")
    assert [plain.next_delay() for _ in range(4)] == [1, 2, 4, 4]
    with pytest.raises(ValueError):
        Backoff(base_s=0)


def test_heartbeat_watchdog_fires_only_after_timeout() -> None:
    wd = HeartbeatWatchdog(10.0, start_ns=0)
    assert wd.remaining_s(4 * S) == 6.0 and not wd.expired(9 * S) and wd.expired(10 * S)
    assert wd.check_gap(9 * S) is None                         # тиша 9 с — норма
    assert wd.check_gap(25 * S) == 19 * S                      # 16 с тиші: спрацювання в last + 10 с
    assert wd.check_gap(5 * S) is None                         # час назад (стрибок годинника) — не тиша
    assert wd.fires == 1
    # poll: «зараз» — будь-який запис сесії; спрацювання рівно в last + timeout, одна тиша — один раз
    p = HeartbeatWatchdog(10.0, start_ns=0)
    assert p.poll(9 * S) is None and p.poll(10 * S) == 10 * S
    assert p.poll(30 * S) is None and p.fires == 1
    p.beat(31 * S)                                             # кадр знову «заводить» сторож
    assert p.poll(40 * S) is None and p.poll(41 * S) == 41 * S and p.fires == 2
    p.beat(50 * S)
    p.disarm()                                                 # розрив уже зафіксовано записом керування
    assert p.poll(99 * S) is None and p.fires == 2


def test_close_code_classification() -> None:
    assert classify_close_code(1000) is CloseClass.NORMAL
    assert classify_close_code(1001) is CloseClass.GOING_AWAY
    assert classify_close_code(None) is CloseClass.TRANSIENT
    assert classify_close_code(1006) is CloseClass.TRANSIENT
    assert classify_close_code(1011) is CloseClass.SERVER_ERROR
    assert classify_close_code(1008) is CloseClass.RATE_LIMITED
    assert classify_close_code(1002) is CloseClass.PROTOCOL_ERROR
    assert classify_close_code(1009) is CloseClass.PROTOCOL_ERROR
    assert classify_http_status(429) is CloseClass.RATE_LIMITED
    assert classify_http_status(503) is CloseClass.SERVER_ERROR
    assert classify_http_status(404) is CloseClass.HANDSHAKE_REJECTED
    closed = ConnectionClosedError(WsClose(1011, "x"), None)
    assert classify_exception(closed)[:2] == (CloseClass.SERVER_ERROR, 1011)
    assert classify_exception(ConnectionClosedError(None, None))[:2] == (CloseClass.TRANSIENT, None)
    assert classify_exception(InvalidStatus(WsResponse(404, "Not Found", Headers())))[0] is \
        CloseClass.HANDSHAKE_REJECTED
    assert classify_exception(OSError("reset"))[0] is CloseClass.TRANSIENT
    with pytest.raises(ValueError):
        classify_exception(KeyError("bug"))


# ================================================================ WS-клієнт на фіктивних з'єднаннях


class Stall:
    """Маркер сценарію: з'єднання мовчить `seconds` (сокет живий, кадрів немає)."""

    def __init__(self, seconds: float) -> None:
        self.seconds = seconds


class CloseMark:
    def __init__(self, code: int | None, reason: str = "") -> None:
        self.code = code
        self.reason = reason


class FakeWs:
    def __init__(self, script: list[Any]) -> None:
        self.script: deque[Any] = deque(script)

    async def recv(self) -> str:
        if not self.script:
            await asyncio.Event().wait()          # мовчить до скасування
        item = self.script.popleft()
        if isinstance(item, CloseMark):
            raise ConnectionClosedError(None if item.code is None else WsClose(item.code, item.reason), None)
        return str(item)


class FakeConnector:
    """connect(url): кожен виклик — наступний сценарій з'єднання для market/public (або виняток)."""

    def __init__(self, market: list[Any], public: list[Any]) -> None:
        self.sessions = {"market": deque(market), "public": deque(public)}
        self.urls: list[str] = []

    def __call__(self, url: str) -> Any:
        self.urls.append(url)
        conn = "market" if "/market/" in url else "public"
        entry = self.sessions[conn].popleft() if self.sessions[conn] else []

        @asynccontextmanager
        async def ctx() -> AsyncIterator[FakeWs]:
            if isinstance(entry, BaseException):
                raise entry
            yield FakeWs(entry)

        return ctx()


def fake_recv(clock: ManualClock, step_ns: int = 1_000_000) -> Any:
    """Замість asyncio.wait_for: Stall довший за таймаут → рухаємо годинник на таймаут і TimeoutError."""

    async def recv(ws: FakeWs, timeout: float) -> str:
        if ws.script and isinstance(ws.script[0], Stall):
            st = ws.script[0]
            if st.seconds > timeout:
                st.seconds -= timeout
                clock.advance(max(1, round(timeout * 1e9)))
                raise TimeoutError
            ws.script.popleft()
            clock.advance(round(st.seconds * 1e9))
        clock.advance(step_ns)
        return await ws.recv()

    return recv


def _wire(f: RawFrame) -> str:
    return orjson.dumps({"stream": f.stream, "data": f.data}).decode()


MARKET_WIRE = [_wire(f) for f in FRAMES if f.conn == "market"][:30]
PUBLIC_WIRE = [_wire(f) for f in FRAMES if f.conn == "public"][:10]


def _client(clock: ManualClock, conn: FakeConnector, sleeps: list[float], **kw: Any) -> BinanceWsClient:
    async def sleep(dt: float) -> None:
        sleeps.append(dt)
        clock.advance(round(dt * 1e9))

    return BinanceWsClient(Settings(symbols=("BTCUSDT",)), clock, instruments=[INSTR], connect=conn,
                           recv=fake_recv(clock), sleep=sleep, heartbeat_timeout_s=10.0,
                           backoff=lambda c: Backoff(base_s=1.0, cap_s=8.0, seed=5), **kw)


def test_ws_client_two_connections_from_settings() -> None:
    c = BinanceWsClient(Settings(symbols=("BTCUSDT",)), ManualClock(), instruments=[INSTR])
    u = c.urls()
    assert u["market"] == ("wss://fstream.binance.com/market/stream?streams="
                           "btcusdt@kline_1m/btcusdt@aggTrade/btcusdt@markPrice@1s")
    assert u["public"] == "wss://fstream.binance.com/public/stream?streams=btcusdt@depth20@100ms"
    # потоки збігаються з тими, що записав scripts/record_ws_session.py (D-01)
    assert u == SESSION.header["urls"]
    with pytest.raises(MainnetHostRejected):
        Settings(binance_ws_market="wss://evil.example.com/stream")


async def test_ws_client_yields_normalized_events_stamped_by_injected_clock(tmp_path: Path) -> None:
    clock = ManualClock(10 * S)
    conn = FakeConnector([[*MARKET_WIRE, CloseMark(1000)]], [[*PUBLIC_WIRE, CloseMark(1000)]])
    rec = SessionRecorder(tmp_path / "live.jsonl.gz", symbol="BTCUSDT", urls={}, clock=clock).open()
    client = _client(clock, conn, [], max_connects=1, recorder=rec)
    assert isinstance(client, MarketFeed)
    evs = [ev async for ev in client]
    rec.close()
    assert len(evs) == len(MARKET_WIRE) + len(PUBLIC_WIRE)
    assert all(e.ts_ingest_ns > 10 * S for e in evs)         # мітки — з ін'єктованого годинника
    assert all(e.src is Src.WS for e in evs if isinstance(e, Candle))
    assert sorted(e.event_uid for e in evs) == sorted(e.event_uid for e in _events(
        [f for f in FRAMES if f.conn == "market"][:30] + [f for f in FRAMES if f.conn == "public"][:10]))
    # дзеркало в рекордер: той самий формат, керування + кадри обох з'єднань
    back = read_session(tmp_path / "live.jsonl.gz")
    assert len(back.frames) == 40
    assert [i.event for i in back.items if isinstance(i, ControlRecord)].count("connected") == 2


async def test_ws_client_heartbeat_watchdog_forces_reconnect() -> None:
    clock = ManualClock(0)
    sleeps: list[float] = []
    # 15 с тиші > 10 с → примусовий розрив
    market = [[*MARKET_WIRE[:5], Stall(15.0), *MARKET_WIRE[5:6]],
              [*MARKET_WIRE[6:10], CloseMark(1000)]]
    conn = FakeConnector(market, [[*PUBLIC_WIRE[:3], CloseMark(1000)], [*PUBLIC_WIRE[3:4], CloseMark(1000)]])
    client = _client(clock, conn, sleeps, max_connects=2)
    items = [it async for it in client.items()]
    frames = [i for i in items if isinstance(i, RawFrame) and i.conn == "market"]
    assert [f.data for f in frames] == [orjson.loads(w)["data"] for w in MARKET_WIRE[:5] + MARKET_WIRE[6:10]]
    discs = [i for i in items
             if isinstance(i, ControlRecord) and i.event == "disconnected" and i.conn == "market"]
    assert discs[0].detail.startswith("HEARTBEAT_TIMEOUT")
    assert client.watchdog_fires == {"market": 1, "public": 0}
    # розрив зафіксовано рівно через timeout після останнього кадру (віртуальний час)
    last_frame_ts = frames[4].ts_ingest_ns
    assert discs[0].ts_ingest_ns - last_frame_ts == pytest.approx(10 * S, abs=2_000_000)
    assert client.connects == {"market": 2, "public": 2}
    assert len(conn.urls) == 4 and len(sleeps) >= 1 and 0.5 <= sleeps[0] <= 1.0   # перша пауза backoff


async def test_ws_client_close_codes_drive_backoff_and_fatal_stops() -> None:
    clock = ManualClock(0)
    sleeps: list[float] = []
    market = [[CloseMark(None)],                     # 1006 без жодного кадру: backoff росте
              [CloseMark(1011, "internal")],
              OSError("connection refused"),         # відмова TCP теж тимчасова
              [*MARKET_WIRE[:2], CloseMark(1000)],   # робоча сесія скидає backoff, NORMAL — без паузи
              [*MARKET_WIRE[2:3], CloseMark(1002, "protocol")]]    # фатально
    client = _client(clock, FakeConnector(market, [[]]), sleeps)
    got: list[Any] = []
    with pytest.raises(WsFatalError) as ei:
        async for it in client.items():
            got.append(it)
    assert ei.value.disconnect.cls is CloseClass.PROTOCOL_ERROR and ei.value.disconnect.code == 1002
    classes = [d.cls for d in client.disconnects]
    assert classes == [CloseClass.TRANSIENT, CloseClass.SERVER_ERROR, CloseClass.TRANSIENT, CloseClass.NORMAL,
                       CloseClass.PROTOCOL_ERROR]
    # паузи: equal jitter на стелях 1, 2, 4 с; після робочої сесії NORMAL → 0 (без паузи);
    # після фатального — жодної паузи
    assert len(sleeps) == 4
    assert 0.5 <= sleeps[0] <= 1.0 and 1.0 <= sleeps[1] <= 2.0 and 2.0 <= sleeps[2] <= 4.0
    assert sleeps[3] == 0.0
    assert sum(1 for i in got if isinstance(i, RawFrame)) == 3


async def test_ws_client_handshake_rejection_is_fatal() -> None:
    bad = InvalidStatus(WsResponse(404, "Not Found", Headers()))
    client = _client(ManualClock(0), FakeConnector([bad], [[]]), [])
    with pytest.raises(WsFatalError) as ei:
        _ = [it async for it in client.items()]
    assert ei.value.disconnect.cls is CloseClass.HANDSHAKE_REJECTED and ei.value.disconnect.code == 404


# ================================================================ детектор прогалин


def _trade(a: int, t_ms: int) -> Trade:
    base = normalize_binance(TRADE_FRAMES[0].stream, TRADE_FRAMES[0].data, 1, INSTR)
    assert isinstance(base, Trade)
    return base.model_copy(update={"agg_id": a, "ts_event_ns": t_ms * 1_000_000, "event_uid": f"t{a}"})


def test_gap_detector_seq_confirms_wide_hole_and_heals_reordered_ids() -> None:
    clock = ManualClock(0)
    d = GapDetector("BTC-USDT-PERP", clock, grace_ms=2_000, max_reorder_ids=50)
    t = 1_000_000
    for a in range(100, 110):
        assert d.on_trade(_trade(a, t + a)) == []
    # перестановка: 111 раніше за 110 — кандидат [110] заростає, прогалини немає
    assert d.on_trade(_trade(111, t + 111)) == [] and d.pending_holes == 1
    assert d.on_trade(_trade(110, t + 110)) == [] and d.pending_holes == 0
    assert d.stats.healed_holes == 1
    # маленька дірка без дозаповнення підтверджується, коли біржовий час пішов на grace далі
    assert d.on_trade(_trade(113, t + 200)) == []
    assert d.on_time((t + 200 + 1_999) * 1_000_000) == []
    [g] = d.on_time((t + 200 + 2_000) * 1_000_000)
    assert (g.stream, g.detector, g.seq_lo, g.seq_hi, g.expected_count) == (
        Stream.TRADES, GapDetectorKind.SEQ, 112, 112, 1)
    # широка дірка (> max_reorder_ids) — одразу
    clock.set(7 * S)
    [w] = d.on_trade(_trade(300, t + 300))
    assert (w.seq_lo, w.seq_hi, w.expected_count, w.detected_at_ns) == (114, 299, 186, 7 * S)
    assert w.ts_lo_ns == (t + 200) * 1_000_000 and w.ts_hi_ns == (t + 300) * 1_000_000
    # добір: FILLING → угоди → resolve
    d.begin_fill(w.gap_id)
    for a in range(114, 300):
        d.on_trade(_trade(a, t + 250))
    done = d.resolve(w.gap_id)
    assert done.status is GapStatus.FILLED and done.filled_rows == 186 and done.closed_at_ns == 7 * S
    # пізнє надходження 112 з потоку закриває відкриту прогалину саме (без REST)
    [healed] = d.on_trade(_trade(112, t + 112))
    assert healed.gap_id == g.gap_id and healed.status is GapStatus.FILLED
    assert d.on_trade(_trade(112, t + 112)) == [] and d.stats.duplicate_trades == 1


def test_gap_detector_time_detects_lost_close_from_other_streams() -> None:
    d = GapDetector("BTC-USDT-PERP", FrameClock(), grace_ms=2_000)
    evs = [e for e in _events(KLINE_FRAMES) if isinstance(e, Candle)]
    m0 = CLOSED_OPENS[0]
    no_close_m0 = [e for e in evs if not (e.open_time_ns == m0 and e.is_closed)]
    out: list[GapRecord] = []
    for e in no_close_m0:
        if e.open_time_ns > m0:
            break
        out += d.on_kline(e)
    assert out == []
    # біржовий час markPrice/depth: на 1.999 с після закриття ще чекаємо, на 2 с — прогалина
    assert d.on_time(m0 + MIN_NS + 1_999_000_000) == []
    [g] = d.on_time(m0 + MIN_NS + 2_000_000_000)
    assert (g.stream, g.detector, g.ts_lo_ns, g.ts_hi_ns, g.expected_count, g.tf) == (
        Stream.KLINES, GapDetectorKind.TIME, m0, m0, 1, "1m")
    assert g.exchange_ns == m0 + MIN_NS + 2_000_000_000
    bf = g.to_backfill_gap()
    assert (bf.ts_lo_ns, bf.ts_hi_ns, bf.expected_count, bf.detector) == (m0, m0, 1, GapDetectorKind.TIME)
    # весь потік без x=true для m0: інших прогалин (хибних) немає
    for e in no_close_m0:
        assert d.on_kline(e) == []


def test_gap_status_transitions_are_enforced() -> None:
    g = GapRecord(1, "X", Stream.KLINES, GapDetectorKind.TIME, 0, 0, 1)
    f = transition(g, GapStatus.FILLING, at_ns=5, count_attempt=True)
    assert f.attempts == 1 and f.closed_at_ns is None
    p = transition(f, GapStatus.PARTIAL, at_ns=9, filled_rows=0)
    assert p.closed_at_ns == 9
    again = transition(p, GapStatus.FILLING, at_ns=10)
    assert transition(again, GapStatus.FILLED, at_ns=11).closed_at_ns == 11
    with pytest.raises(ValueError, match="illegal"):
        transition(transition(g, GapStatus.FILLED, at_ns=1), GapStatus.FILLING, at_ns=2)
    assert g.to_row()["status"] == "OPEN" and g.to_row()["detector"] == "time"


# ================================================================ агрегатор свічок


def test_candle_aggregator_emits_each_closed_candle_exactly_once() -> None:
    evs = [e for e in _events(KLINE_FRAMES) if isinstance(e, Candle)]
    # дублікати кожного оновлення + локальні перестановки + повтор закриттів наприкінці
    noisy: list[Candle] = []
    for a, b in zip(evs[::2], evs[1::2], strict=False):
        noisy += [b, a, a]
    noisy += [e for e in evs if e.is_closed]
    agg = CandleAggregator("1m", instrument="BTC-USDT-PERP")
    out = [c for e in noisy for c in agg.offer(e)]
    assert [c.open_time_ns for c in out] == CLOSED_OPENS
    assert all(c.is_closed for c in out)
    assert agg.stats.duplicates >= 3 and agg.stats.conflicts == 0
    assert agg.current is not None and agg.current.open_time_ns == CLOSED_OPENS[-1] + MIN_NS


def test_candle_aggregator_holds_after_hole_and_releases_in_order() -> None:
    closed = [e for e in _events(KLINE_FRAMES) if isinstance(e, Candle) and e.is_closed]
    first_update = next(e for e in _events(KLINE_FRAMES) if isinstance(e, Candle))
    agg = CandleAggregator("1m")
    assert agg.offer(first_update) == []                      # старт — хвилина першого оновлення (m0)
    assert agg.offer(closed[1]) == [] and agg.held == 1       # m1 чекає на m0
    assert agg.offer(closed[2]) == [] and agg.held == 2
    released = agg.offer(closed[0])                           # m0 прийшла (напр. з REST) → усе по порядку
    assert [c.open_time_ns for c in released] == CLOSED_OPENS and agg.held == 0
    agg2 = CandleAggregator("1m")
    agg2.offer(first_update)
    agg2.offer(closed[2])
    assert [c.open_time_ns for c in agg2.skip(CLOSED_OPENS[0], CLOSED_OPENS[1])] == [CLOSED_OPENS[2]]
    assert agg2.offer(closed[0]) == [] and agg2.stats.duplicates == 1       # пропущену вже не випускаємо


# ================================================================ конвеєр


async def test_pipeline_clean_sample_dq_and_journal_chain(tmp_path: Path) -> None:
    journal = EventJournal(run_id=__import__("uuid").UUID(int=1))
    cap = SessionCapture()
    sinks = cap.sinks()
    sinks.on_event = journal_sink(journal)
    rep = await replay_session(_market_only(SAMPLE, tmp_path / "m.jsonl.gz"), INSTR, sinks=sinks)
    assert [c.open_time_ns for c in cap.candles] == CLOSED_OPENS
    assert len(cap.trades) == 3887 and rep.gaps == () and rep.health.invalid == 0
    # кожна прийнята зміна стану — у журналі; ланцюг цілий
    accepted = rep.dedup["NEW"] + rep.dedup["REPLACED"] - rep.health.stale
    assert verify_chain(journal.entries) is None
    assert journal.next_seq == len(journal.entries) == accepted > 4000
    [h] = rep.dq
    s = h.score
    assert (s.inputs.expected_buckets, s.inputs.observed_buckets, s.inputs.gap_seconds) == (3, 3, 0.0)
    assert s.completeness == s.validity == s.continuity == 1.0
    assert s.timeliness == pytest.approx(math.exp(-s.inputs.lag_p95_ms / 1000.0))
    assert 100.0 < s.inputs.lag_p95_ms < 1000.0 and 0.9 < s.score < 1.0     # реальний лаг ~0.1–0.3 с
    assert rep.health.q == s.score


async def test_pipeline_invalid_close_is_rejected_and_minute_recovered_by_backfill(tmp_path: Path) -> None:
    """Закриття m1 зіпсоване (h < c): нормалізація його відкидає (invalid), детектор оголошує m1
    втраченою, REST-добір (записаний справжній рядок) відновлює свічку — втрат немає."""
    m1 = CLOSED_OPENS[1] // 1_000_000
    items: list[Any] = []
    for it in (i for i in SESSION.items if not (isinstance(i, RawFrame) and i.conn == "public")):
        is_close_m1 = isinstance(it, RawFrame) and "@kline_" in it.stream and it.data["k"]["t"] == m1 \
            and it.data["k"]["x"]
        if is_close_m1 and isinstance(it, RawFrame):
            k = dict(it.data["k"]) | {"h": "1.00"}                     # h нижче за o і c
            items.append(RawFrame(it.conn, it.ts_ingest_ns, it.stream, dict(it.data) | {"k": k}))
        else:
            items.append(it)
    path = tmp_path / "bad_close.jsonl.gz"
    write_session(path, SESSION.header, items, ended_ns=SESSION.footer["ended_ns"])   # type: ignore[index]
    fx = RestFixture.load(PATHO / "gap.rest.json")                    # записані REST-рядки хвилин зразка
    clock = FrameClock()
    cap = SessionCapture()
    async with offline_rest_client(fx, clock) as client:
        rep = await replay_session(path, INSTR, backfill=RestBackfiller(client, INSTR, sleep=_no_sleep),
                                   sinks=cap.sinks(), clock=clock)
    # модельний валідатор Candle (h ≥ max(o,c), l ≤ min(o,c)) не має поля — шлях помилки `__root__`
    assert rep.health.invalid == 1 and [i.code for i in cap.invalid] == ["NORMALIZATION:__root__"]
    [g] = rep.gaps
    assert (g.stream, g.ts_lo_ns, g.status) == (Stream.KLINES, CLOSED_OPENS[1], GapStatus.FILLED)
    st = recovery_stats(session_reference(SAMPLE), cap, rep)
    assert st.zero_loss and st.lost_candles == 0 and st.mismatched_candles == 0
    assert next(c for c in cap.candles if c.open_time_ns == CLOSED_OPENS[1]).src is Src.REST


async def test_pipeline_without_backfill_leaves_gap_open_and_skips_nothing_silently(tmp_path: Path) -> None:
    cap = SessionCapture()
    rep = await replay_session(_market_only(PATHO / "gap.jsonl.gz", tmp_path / "g.jsonl.gz"), INSTR,
                               sinks=cap.sinks())
    assert {g.status for g in rep.gaps} == {GapStatus.OPEN}
    st = recovery_stats(session_reference(SAMPLE), cap, rep)
    assert st.lost_candles == 2 and st.lost_trades > 1000          # без добору втрати видно і пораховано
    # агрегатор не чекає вічно на недобрані хвилини: m2 випущено одразу після оголошення прогалини
    assert [c.open_time_ns for c in cap.candles] == [CLOSED_OPENS[2]] and rep.aggregator.skipped == 2
    assert rep.aggregator.max_held <= 1
    assert rep.dq[0].score.continuity < 1.0 and rep.dq[0].score.completeness < 1.0


def _cut(items: list[Any], lo_ns: int, hi_ns: int) -> list[Any]:
    """Записи сесії з ts_ingest < hi, де кадри market з ts_ingest ≥ lo викинуто (з'єднання замовкло)."""
    return [i for i in items if i.ts_ingest_ns < hi_ns
            and not (isinstance(i, RawFrame) and i.conn == "market" and i.ts_ingest_ns >= lo_ns)]


async def test_pipeline_watchdog_fires_on_virtual_time_even_if_connection_never_returns() -> None:
    """Сторож реплею рухається часом УСІХ з'єднань: market, що замовк назавжди (depth іде далі), дає рівно
    один розрив — у момент last + timeout, а не «заднім числом» із поверненням з'єднання (якого тут нема)."""
    lo = CLOSED_OPENS[1] + 30 * S
    items = _cut(list(SESSION.items), lo, lo + 20 * S)
    last_market = max(i.ts_ingest_ns for i in items if isinstance(i, RawFrame) and i.conn == "market")
    clock = FrameClock()
    seen_at: list[int] = []
    pipe = IngestPipeline(INSTR, clock=clock, heartbeat_timeout_s=10.0,
                          sinks=PipelineSinks(on_disconnect=lambda d: seen_at.append(clock.now_ns())))
    rep = await pipe.run(items)
    assert rep.health.watchdog_fires == 1
    [d] = rep.disconnects
    assert (d.conn, d.cls, d.ts_ns) == ("market", CloseClass.HEARTBEAT_TIMEOUT, last_market + 10 * S)
    # стік отримав розрив тоді, коли віртуальний час минув дедлайн (кадри depth — щонайменше раз на 0.5 с)
    assert d.ts_ns <= seen_at[0] < d.ts_ns + S


async def test_pipeline_control_disconnect_is_not_double_counted_by_watchdog() -> None:
    """Запис керування `disconnected` + 15 с до `connected`: це ОДИН розрив (класу з запису), сторож
    тиші за ту саму паузу не спрацьовує вдруге."""
    lo = CLOSED_OPENS[1] + 30 * S
    items = _cut(list(SESSION.items), lo, lo + 15 * S)
    items.insert(next(k for k, i in enumerate(items) if i.ts_ingest_ns >= lo),
                 ControlRecord("market", lo, "disconnected", "TRANSIENT::no close frame"))
    items.append(ControlRecord("market", lo + 15 * S, "connected", "wss://fstream.binance.com/market/stream"))
    items += [i for i in SESSION.items
              if isinstance(i, RawFrame) and lo + 15 * S <= i.ts_ingest_ns < lo + 18 * S]
    rep = await IngestPipeline(INSTR, heartbeat_timeout_s=10.0).run(items)
    assert [(d.conn, d.cls) for d in rep.disconnects] == [("market", CloseClass.TRANSIENT)]
    assert rep.health.watchdog_fires == 0


async def test_public_disconnect_does_not_confirm_pending_trade_holes() -> None:
    """Угоди йдуть лише з'єднанням market (D-01): розрив public (depth) не робить кандидата в дірку
    aggTrade id (перестановка кадрів) підтвердженою прогалиною; розрив market — робить."""
    f0, f1, f2 = TRADE_FRAMES[:3]
    assert [f["a"] for f in (f0.data, f1.data, f2.data)] == [f0.data["a"] + k for k in range(3)]
    p = IngestPipeline(INSTR)
    await p.process(f0)
    await p.process(f2)                                             # f1 ще «в дорозі»
    assert p.detector.pending_holes == 1
    await p.process(ControlRecord("public", f2.ts_ingest_ns, "disconnected", "TRANSIENT::x"))
    assert p.detector.pending_holes == 1 and p.detector.gaps == {}
    await p.process(ControlRecord("market", f2.ts_ingest_ns, "disconnected", "TRANSIENT::x"))
    [g] = p.detector.gaps.values()
    assert (g.stream, g.seq_lo, g.seq_hi) == (Stream.TRADES, f1.data["a"], f1.data["a"])


@pytest.mark.parametrize(("delay_s", "released", "late_after_skip"), [(1, 3, 0), (5, 2, 1)])
async def test_late_close_after_skip_does_not_mark_gap_filled(delay_s: int, released: int,
                                                              late_after_skip: int) -> None:
    """Без REST-добору закриття m0 затримано на delay_s. У межах grace (1 с) — звичайне пізнє закриття,
    прогалини немає. Після grace (5 с) m0 оголошено втраченою і пропущено, m1 уже випущено: пізнє закриття
    по порядку не випустити, тож прогалина лишається OPEN (для нічного добору), а не FILLED без свічки."""
    market = [i for i in SESSION.items if not (isinstance(i, RawFrame) and i.conn == "public")]
    k = next(j for j, i in enumerate(market) if isinstance(i, RawFrame) and "@kline_" in i.stream
             and i.data["k"]["x"] and int(i.data["k"]["t"]) * 1_000_000 == CLOSED_OPENS[0])
    close = market.pop(k)
    assert isinstance(close, RawFrame)
    t_late = close.ts_ingest_ns + delay_s * S
    market.insert(next(j for j, i in enumerate(market) if i.ts_ingest_ns >= t_late),
                  RawFrame(close.conn, t_late, close.stream, close.data))
    cap = SessionCapture()
    rep = await IngestPipeline(INSTR, sinks=cap.sinks()).run(market)
    assert len(cap.candles) == released and rep.aggregator.late_after_skip == late_after_skip
    st = recovery_stats(session_reference(SAMPLE), cap, rep)
    assert st.lost_candles == 3 - released and st.candles_in_order and st.duplicated_candles == 0
    if late_after_skip:
        [g] = rep.gaps
        assert (g.stream, g.ts_lo_ns, g.status) == (Stream.KLINES, CLOSED_OPENS[0], GapStatus.OPEN)
        assert [x.status for x in cap.gaps] == [GapStatus.OPEN]       # жодного хибного FILLED у ingest_gap
    else:
        assert rep.gaps == ()


async def test_pipeline_scores_released_candles_and_reports_anomaly_verdicts() -> None:
    """QualityGate §4.1: кожна випущена свічка після прогріву отримує скор автокодувальника (стік
    on_anomaly → candle.anomaly_score); аномалії потрапляють у лічильники здоров'я і N_invalid години Q."""
    rows = orjson.loads(gzip.decompress((REST / "binance_klines.json.gz").read_bytes()))
    now_ms = rows[-1][6] + 1_000
    candles = normalize_rest_klines(rows, INSTR, now_ms * 1_000_000, server_time_ms=now_ms)
    train_X, _ = feature_matrix([bar_from_candle(c) for c in candles[:1500]])
    model = AnomalyAutoencoder(seed=20260918).fit(train_X)
    feed = list(candles[1700:2001])
    spike = quantize_price(feed[-2].c * Decimal("1.01"), INSTR.tick_size)          # +1 % за хвилину
    feed[-1] = feed[-1].model_copy(update={"c": spike, "h": max(feed[-1].h, spike)})
    verdicts: list[AnomalyVerdict] = []
    cap = SessionCapture()
    sinks = cap.sinks()
    sinks.on_anomaly = verdicts.append
    pipe = IngestPipeline(INSTR, sinks=sinks, anomaly=AnomalyScorer(model))
    for c in feed:
        await pipe.process_event(c)
    rep = await pipe.finish()
    assert [c.open_time_ns for c in cap.candles] == [c.open_time_ns for c in feed] and rep.health.invalid == 0
    assert len(verdicts) == len(feed) - (AnomalyFeatureExtractor().warmup - 1)
    assert [v.t_ns for v in verdicts] == [c.open_time_ns for c in feed[-len(verdicts):]]
    assert verdicts[-1].anomaly and verdicts[-1].score > verdicts[-1].threshold
    n_anomalies = sum(v.anomaly for v in verdicts)
    assert rep.health.anomalies == n_anomalies
    assert sum(h.score.inputs.anomaly_count for h in rep.dq) == n_anomalies


def test_rest_agg_trade_row_normalizes_to_same_event_as_ws() -> None:
    f = TRADE_FRAMES[0]
    ws = normalize_binance(f.stream, f.data, 5, INSTR)
    rest = normalize_rest_agg_trade(agg_trade_rest_row(f.data), INSTR, 5)
    assert rest == ws                                           # той самий event_uid → дедуплікація
    with pytest.raises(NormalizationError):
        normalize_rest_agg_trade(agg_trade_rest_row(f.data) | {"x": 1}, INSTR, 5)
    with pytest.raises(NormalizationError):
        normalize_rest_agg_trade(agg_trade_rest_row(f.data) | {"p": 81000.1}, INSTR, 5)


async def test_fetch_agg_trades_paginates_by_from_id() -> None:
    fx = RestFixture.load(PATHO / "gap.rest.json")
    clock = FrameClock(FRAMES[-1].ts_ingest_ns)
    ids = [t["a"] for t in fx.agg_trades]
    lo, hi = ids[10], ids[10] + 2_345
    async with offline_rest_client(fx, clock) as client:
        trades, n = await fetch_agg_trades(client, "BTCUSDT", lo, hi, INSTR, limit=1000, sleep=_no_sleep)
    assert [t.agg_id for t in trades] == list(range(lo, hi + 1))
    assert n == 3 and all(t.venue is Venue.BINANCE_USDM for t in trades)


async def test_rest_backfiller_over_respx_serves_only_closed_klines() -> None:
    fx = RestFixture.load(PATHO / "gap.rest.json")
    m0 = CLOSED_OPENS[0]
    clock = FrameClock(m0 + MIN_NS + 3 * S)
    async with respx.mock(assert_all_called=True) as router:
        router.route(method="GET", host="fapi.binance.com").mock(side_effect=FixtureRestHandler(fx, clock))
        async with httpx.AsyncClient() as http:
            bucket = TokenBucket(1200, 40, clock, sleep=_no_sleep)
            client = BinanceRestClient("https://fapi.binance.com", http, bucket, RetryPolicy(rng_seed=1),
                                       clock=clock, sleep=_no_sleep)
            bf = RestBackfiller(client, INSTR, sleep=_no_sleep)
            g = GapRecord(1, "BTC-USDT-PERP", Stream.KLINES, GapDetectorKind.TIME, m0, m0 + 2 * MIN_NS, 3,
                          tf="1m", exchange_ns=clock.now_ns())
            res = await bf(g)
    # «зараз» = m0 + 63 с: закрита лише m0; m1, m2 ще не існують як закриті бари
    assert [c.open_time_ns for c in res.events if isinstance(c, Candle)] == [m0]
    assert res.error is None and res.requests == 1


def test_session_reference_is_contiguous_and_matches_fixture() -> None:
    ref = session_reference(SAMPLE)
    assert sorted(ref.closed) == CLOSED_OPENS
    assert len(ref.agg_ids) == 3887 and max(ref.agg_ids) - min(ref.agg_ids) + 1 == 3887
    assert Decimal(ref.closed[CLOSED_OPENS[0]]["o"]) == Decimal("81015.40")


def test_pipeline_rejects_trade_off_tick() -> None:
    async def run() -> IngestPipeline:
        p = IngestPipeline(INSTR)
        f = TRADE_FRAMES[0]
        bad = RawFrame(f.conn, f.ts_ingest_ns, f.stream, dict(f.data) | {"p": "81015.45"})
        await p.process(bad)
        await p.process(f)
        return p

    p = asyncio.run(run())
    assert p.health.invalid == 1 and p.health.invalid_by_code == {"PRICE_OFF_TICK:price": 1}
    assert p.trades_emitted == 1 and p.health.events_by_kind["trade"] == 1


def test_symbol_ref_pipeline_skips_tick_checks() -> None:
    p = IngestPipeline(BTC_USDT_PERP)
    assert p.spec is None
