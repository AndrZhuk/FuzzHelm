"""Конвеєр інжесту: кадр → нормалізація → інваріанти → дедуплікація → свічки/угоди → прогалини → добір.

Найменування: ingest/pipeline.py
Призначення: єдиний код обробки для live (BinanceWsClient) і реплею (ReplayFeed): однакові кадри дають
однакові свічки, угоди, записи прогалин і скор якості. БД-агностичний: журнал, репозиторії свічок і
прогалин підключаються як колбеки (PipelineSinks), REST-добір — як BackfillHook.
Автор: Андрій Жук, 2026.

Порядок для кожної події (ідентичний для потоку і REST-добору):
  1. normalize_binance (NormalizationError → invalid, подію відкинуто);
  2. quality.invariants (tick/step/OHLC) → invalid;
  3. Deduplicator за event_uid: DUPLICATE → відкинуто (повтор кадру, перекриття добору);
     BookSnapshot/MarkPrice, старіші за вже прийняті (перестановка), — stale, відкинуто;
  4. стік on_event (журнал подій) — кожна прийнята зміна стану;
  5. Candle → GapDetector.on_kline + CandleAggregator (закрита свічка рівно раз, по порядку) → on_candle;
     Trade  → on_trade (перше надходження кожного agg_id) + GapDetector.on_trade;
     інші  → GapDetector.on_time (біржовий час рухає детектор свічок навіть без кадрів kline);
  6. нові прогалини → on_gap(OPEN) → FILLING → BackfillHook → події добору йдуть кроками 2–5 →
     resolve: FILLED | PARTIAL | UNFILLABLE → on_gap; недобрані хвилини → aggregator.skip.
Добір виконується в тій самій корутині (детерміновано: той самий вхід — той самий порядок виходу);
у live кадри за цей час накопичуються в черзі клієнта.

Сторож тиші (heartbeat_timeout_s) тут працює на ВІРТУАЛЬНОМУ часі кадрів — для реплею: тиша на
з'єднанні довша за timeout = розрив (у live цим займається сам BinanceWsClient, тоді тут None). «Зараз» —
мітка кожного запису будь-якого з'єднання, тож розрив фіксується в момент last + timeout, а не заднім
числом із поверненням з'єднання (і фіксується, навіть якщо воно не повернеться).
"""

from __future__ import annotations

import asyncio
import inspect
from collections import deque
from collections.abc import AsyncIterable, AsyncIterator, Awaitable, Callable, Iterable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from itertools import pairwise
from pathlib import Path
from typing import Any, Final

import httpx

from fuzzhelm.config import assert_readonly_url
from fuzzhelm.core.digest import to_canonical
from fuzzhelm.core.dto import BookSnapshot, Candle, Instrument, MarketEvent, MarkPrice, Trade
from fuzzhelm.core.enums import GapStatus, Src, Stream
from fuzzhelm.core.errors import NormalizationError
from fuzzhelm.core.journal import EventJournal
from fuzzhelm.core.money import dec
from fuzzhelm.core.ports import Clock
from fuzzhelm.features.convert import bar_from_candle
from fuzzhelm.ingest.backfill import backfill_klines
from fuzzhelm.ingest.candles import AggregatorStats, CandleAggregator
from fuzzhelm.ingest.dedup import Deduplicator, DedupOutcome
from fuzzhelm.ingest.gap_detector import GapDetector, GapRecord, GapStats
from fuzzhelm.ingest.normalize import (
    NS_PER_MS,
    InstrumentLike,
    normalize_binance,
    normalize_rest_agg_trade,  # реекспорт: історичне місце імпорту (tests, скрипти)
    tf_ms,
)
from fuzzhelm.ingest.ratelimit import ENDPOINT_WEIGHTS, binance_request_bucket
from fuzzhelm.ingest.reconnect import CloseClass, Disconnect, HeartbeatWatchdog
from fuzzhelm.ingest.recorder import ControlRecord, RawFrame, SessionItem
from fuzzhelm.ingest.replay import FixtureRestHandler, FrameClock, ReplayFeed, RestFixture
from fuzzhelm.ingest.rest_client import AGG_TRADES, MAX_AGG_TRADES_LIMIT, BinanceRestClient
from fuzzhelm.ingest.retry import RetryPolicy
from fuzzhelm.quality.anomaly_mlp import AnomalyScorer, AnomalyVerdict
from fuzzhelm.quality.dq_score import DqAccumulator, DqScore, dq_score, load_dq_weights, load_tau0_ms
from fuzzhelm.quality.health import HealthSnapshot, PipelineHealth
from fuzzhelm.quality.invariants import InstrumentSpec, check_book, check_candle, check_trade

type Sink[T] = Callable[[T], Any]  # синхронний або async колбек
SleepFn = Callable[[float], Awaitable[None]]

HOUR_NS: Final = 3_600 * 1_000_000_000
AGG_TRADES_PATH: Final = AGG_TRADES                       # REST aggTrades живе в rest_client (WS-04)
AGG_TRADES_WEIGHT: Final = ENDPOINT_WEIGHTS[AGG_TRADES]      # 20 — виміряно за X-MBX-USED-WEIGHT-1M
AGG_TRADES_MAX_LIMIT: Final = MAX_AGG_TRADES_LIMIT
MARKET_CONN: Final = "market"
PUBLIC_CONN: Final = "public"


def conn_of(ev: MarketEvent) -> str:
    """З'єднання за типом події (D-01: depth — /public/stream, решта — /market/stream)."""
    return PUBLIC_CONN if isinstance(ev, BookSnapshot) else MARKET_CONN


def event_kind(ev: MarketEvent) -> str:
    if isinstance(ev, Candle):
        return "candle"
    if isinstance(ev, Trade):
        return "trade"
    if isinstance(ev, BookSnapshot):
        return "book"
    return "mark"


async def _call[T](sink: Sink[T] | None, arg: T) -> None:
    if sink is None:
        return
    res = sink(arg)
    if inspect.isawaitable(res):
        await res


def journal_sink(journal: EventJournal) -> Callable[[MarketEvent], None]:
    """Стік on_event → EventJournal (канонічний payload без float, ланцюг BLAKE2b)."""

    def _append(ev: MarketEvent) -> None:
        journal.append(event_kind(ev), to_canonical(ev), ev.ts_event_ns, ev.ts_ingest_ns)

    return _append


# ---------------------------------------------------------------- контракти стоків і добору


@dataclass(frozen=True, slots=True)
class InvalidEvent:
    ts_ingest_ns: int
    stream: str
    code: str
    detail: str = ""


@dataclass(frozen=True, slots=True)
class FillResult:
    """Що повернув добір для однієї прогалини."""

    events: tuple[MarketEvent, ...]
    requests: int = 0
    error: str | None = None


BackfillHook = Callable[[GapRecord], Awaitable[FillResult]]


@dataclass
class PipelineSinks:
    on_event: Sink[MarketEvent] | None = None        # кожна прийнята зміна стану → журнал
    on_candle: Sink[Candle] | None = None            # закрита свічка, рівно раз на open_time, по порядку
    on_trade: Sink[Trade] | None = None              # угода, рівно раз на agg_id
    on_gap: Sink[GapRecord] | None = None            # кожна зміна стану прогалини → ingest_gap
    on_invalid: Sink[InvalidEvent] | None = None
    on_disconnect: Sink[Disconnect] | None = None
    # скор автокодувальника кожної випущеної свічки (після прогріву; t_ns = open_time) → candle.anomaly_score
    on_anomaly: Sink[AnomalyVerdict] | None = None


@dataclass(frozen=True, slots=True)
class HourDq:
    hour_start_ns: int
    score: DqScore


@dataclass(frozen=True, slots=True)
class PipelineReport:
    health: HealthSnapshot
    gaps: tuple[GapRecord, ...]
    candles_released: int
    trades_emitted: int
    dedup: dict[str, int]
    aggregator: AggregatorStats
    gap_stats: GapStats
    dq: tuple[HourDq, ...]
    disconnects: tuple[Disconnect, ...]
    backfill_requests: int
    backfill_errors: tuple[str, ...]


# ---------------------------------------------------------------- конвеєр


class IngestPipeline:
    def __init__(self, instrument: InstrumentLike, *, sinks: PipelineSinks | None = None,
                 backfill: BackfillHook | None = None, clock: Clock | None = None, src: Src = Src.REPLAY,
                 tf: str = "1m", heartbeat_timeout_s: float | None = None, grace_ms: int = 2_000,
                 max_reorder_ids: int = 50, dedup_window: int | None = 500_000,
                 anomaly: AnomalyScorer | None = None,
                 dq_weights: tuple[float, float, float, float] | None = None,
                 tau0_ms: float | None = None) -> None:
        self.instrument = instrument
        self.sinks = sinks or PipelineSinks()
        self.backfill = backfill
        self.clock: Clock = clock if clock is not None else FrameClock()
        self._frame_clock = self.clock if isinstance(self.clock, FrameClock) else None
        self.src = src
        self.tf = tf
        self._tf_ns = tf_ms(tf) * NS_PER_MS
        self.spec: InstrumentSpec | None = (InstrumentSpec(instrument.tick_size, instrument.step_size)
                                            if isinstance(instrument, Instrument) else None)
        canon = instrument.symbol_canon
        self.dedup = Deduplicator(dedup_window)
        self.detector = GapDetector(canon, self.clock, tf=tf, grace_ms=grace_ms,
                                    max_reorder_ids=max_reorder_ids)
        self.aggregator = CandleAggregator(tf, instrument=canon)
        self.health = PipelineHealth()
        self.anomaly = anomaly
        self.dq_weights = dq_weights if dq_weights is not None else load_dq_weights()
        self.tau0_ms = tau0_ms if tau0_ms is not None else load_tau0_ms()
        self._heartbeat_s = heartbeat_timeout_s
        self._watchdogs: dict[str, HeartbeatWatchdog] = {}
        self._dq: dict[int, DqAccumulator] = {}
        self._to_fill: deque[int] = deque()
        self._filling = False
        self._last_book_u: int | None = None
        self._last_mark_ns: int | None = None
        self._first_open_ns: int | None = None
        self.trades_emitted = 0
        self.disconnects: list[Disconnect] = []
        self.backfill_requests = 0
        self.backfill_errors: list[str] = []

    # ------------------------------------------------------------ вхід

    async def process(self, item: SessionItem) -> None:
        """Один запис сесії (кадр або керування) у порядку надходження."""
        if self._frame_clock is not None:
            self._frame_clock.observe(item.ts_ingest_ns)
        if self._heartbeat_s is not None:
            await self._poll_watchdogs(item.ts_ingest_ns)
        if isinstance(item, ControlRecord):
            await self._on_control(item)
            return
        await self._on_frame(item)

    async def run(self, items: AsyncIterable[SessionItem] | Iterable[SessionItem]) -> PipelineReport:
        if isinstance(items, AsyncIterable):
            async for it in items:
                await self.process(it)
        else:
            for it in items:
                await self.process(it)
        return await self.finish()

    async def process_event(self, ev: MarketEvent) -> None:
        """Уже нормалізована подія (напр. з ReplayFeed.__aiter__ або іншого MarketFeed)."""
        if self._frame_clock is not None:
            self._frame_clock.observe(ev.ts_ingest_ns)
        self.health.on_frame(conn_of(ev), ev.ts_ingest_ns)
        await self._accept(ev, from_stream=True)

    async def _poll_watchdogs(self, now_ns: int) -> None:
        """Сторожі всіх з'єднань на віртуальному «зараз» (мітка будь-якого запису): розрив фіксується,
        щойно минув timeout, — навіть якщо з'єднання більше ніколи не озветься (кадри інших з'єднань
        рухають час)."""
        for conn, wd in list(self._watchdogs.items()):
            fired = wd.poll(now_ns)
            if fired is not None:
                await self._disconnect(Disconnect(conn, fired, CloseClass.HEARTBEAT_TIMEOUT,
                                                  reason=f"no frames for {wd.timeout_s:g}s"), watchdog=True)

    async def _on_frame(self, fr: RawFrame) -> None:
        self.health.on_frame(fr.conn, fr.ts_ingest_ns)
        if self._heartbeat_s is not None:
            wd = self._watchdogs.get(fr.conn)
            if wd is None:
                wd = self._watchdogs[fr.conn] = HeartbeatWatchdog(self._heartbeat_s)
            wd.beat(fr.ts_ingest_ns)
        try:
            ev = normalize_binance(fr.stream, fr.data, fr.ts_ingest_ns, self.instrument, src=self.src)
        except NormalizationError as e:
            await self._invalid(fr.ts_ingest_ns, fr.stream, f"NORMALIZATION:{e.field}", str(e))
            return
        await self._accept(ev, from_stream=True)

    async def _on_control(self, rec: ControlRecord) -> None:
        if rec.event == "disconnected":
            wd = self._watchdogs.get(rec.conn)
            if wd is not None:
                wd.disarm()          # тиша до `connected` — той самий розрив, а не нове спрацювання сторожа
            cls = _parse_class(rec.detail)
            await self._disconnect(Disconnect(rec.conn, rec.ts_ingest_ns, cls, reason=rec.detail),
                                   watchdog=cls is CloseClass.HEARTBEAT_TIMEOUT)
        elif rec.event == "connected":
            wd = self._watchdogs.get(rec.conn)
            if wd is not None:
                wd.beat(rec.ts_ingest_ns)

    async def _disconnect(self, d: Disconnect, *, watchdog: bool) -> None:
        self.disconnects.append(d)
        self.health.on_disconnect(d.cls.value, watchdog=watchdog)
        await _call(self.sinks.on_disconnect, d)
        # після розриву пізніх кадрів не буде: кандидати в дірки aggTrade id — справжні прогалини. Угоди
        # ідуть лише з'єднанням market (D-01), тож розрив public (depth) кандидатів не підтверджує
        if d.conn != PUBLIC_CONN:
            await self._on_gaps(self.detector.flush())

    async def _invalid(self, ts_ns: int, stream: str, code: str, detail: str,
                       bucket_ns: int | None = None) -> None:
        """bucket_ns — час для години Q: ts_event валідно розібраної події (як у валідних подій того ж
        потоку); кадр, що не нормалізувався, біржового часу не має — тоді ts_ingest."""
        self.health.on_invalid(code)
        self._acc(ts_ns if bucket_ns is None else bucket_ns).add_checked(invalid=True)
        await _call(self.sinks.on_invalid, InvalidEvent(ts_ns, stream, code, detail))

    # ------------------------------------------------------------ ядро

    def _violations(self, ev: MarketEvent) -> list[str]:
        if isinstance(ev, Candle):
            return check_candle(ev, self.spec)
        if isinstance(ev, Trade):
            return check_trade(ev, self.spec)
        if isinstance(ev, BookSnapshot):
            return check_book(ev, self.spec)
        return []

    def _stale(self, ev: MarketEvent) -> bool:
        if isinstance(ev, BookSnapshot):
            if self._last_book_u is not None and ev.last_update_id < self._last_book_u:
                return True
            self._last_book_u = ev.last_update_id
        elif isinstance(ev, MarkPrice):
            if self._last_mark_ns is not None and ev.ts_event_ns < self._last_mark_ns:
                return True
            self._last_mark_ns = ev.ts_event_ns
        return False

    async def _accept(self, ev: MarketEvent, *, from_stream: bool) -> None:
        bad = self._violations(ev)
        if bad:
            await self._invalid(ev.ts_ingest_ns, event_kind(ev), bad[0], ",".join(bad), ev.ts_event_ns)
            return
        outcome = self.dedup.offer(ev)
        if outcome is DedupOutcome.DUPLICATE:
            self.health.on_duplicate()
            return
        if self._stale(ev):
            self.health.on_stale()
            return
        self._acc(ev.ts_event_ns).add_checked()
        if from_stream:
            # лаг — лише для подій потоку: у REST-доборі ts_ingest − ts_event = вік бару, а не затримка
            lag = self.health.on_event(event_kind(ev), ev.ts_event_ns, ev.ts_ingest_ns)
            self._acc(ev.ts_event_ns).add_lag_ms(lag)
        else:
            self.health.events_by_kind[f"{event_kind(ev)}_backfill"] += 1
        await _call(self.sinks.on_event, ev)
        if isinstance(ev, Candle):
            if self._first_open_ns is None and ev.tf == self.tf:
                self._first_open_ns = ev.open_time_ns
            if ev.is_closed and ev.tf == self.tf and self.aggregator.is_skipped(ev.open_time_ns):
                # хвилину вже оголошено недобірною і пізніші свічки випущено: по порядку її не випустити,
                # on_candle її не отримає — тож і прогалину вона НЕ закриває. Інакше ingest_gap казав би
                # FILLED без свічки в стоці, і нічний добір цю хвилину оминув би (тиха втрата).
                changed = self.detector.on_time(ev.ts_event_ns)
            else:
                changed = self.detector.on_kline(ev)
            for c in self.aggregator.offer(ev):
                await self._release(c)
        elif isinstance(ev, Trade):
            if outcome is DedupOutcome.NEW:
                self.trades_emitted += 1
                await _call(self.sinks.on_trade, ev)
            changed = self.detector.on_trade(ev)
        else:
            changed = self.detector.on_time(ev.ts_event_ns)
        await self._on_gaps(changed)

    async def _release(self, c: Candle) -> None:
        v = self.anomaly.update(bar_from_candle(c)) if self.anomaly is not None else None
        anomaly = v is not None and v.anomaly
        self.health.on_candle_closed(anomaly=anomaly)
        acc = self._acc(c.open_time_ns)
        acc.add_bucket(c.open_time_ns)
        if anomaly:
            acc.anomalies += 1
        await _call(self.sinks.on_candle, c)
        if v is not None:
            await _call(self.sinks.on_anomaly, v)

    # ------------------------------------------------------------ прогалини і добір

    async def _on_gaps(self, changed: list[GapRecord]) -> None:
        for g in changed:
            await self._gap_changed(g)
            if g.status is GapStatus.OPEN:
                lo = g.ts_lo_ns
                hi = g.ts_hi_ns + (self._tf_ns if g.stream is Stream.KLINES else 0)
                for h in range(self._hour(lo), max(lo, hi - 1) + 1, HOUR_NS):   # кожна зачеплена година
                    self._acc(h).add_gap(lo, hi)
                self._to_fill.append(g.gap_id)
        if not self._filling:
            await self._fill_loop()

    async def _gap_changed(self, g: GapRecord) -> None:
        self.health.on_gap(g.gap_id, g.status.value)
        await _call(self.sinks.on_gap, g)

    async def _fill_loop(self) -> None:
        self._filling = True
        try:
            while self._to_fill:
                gid = self._to_fill.popleft()
                if self.detector.gaps[gid].status is GapStatus.FILLED:
                    continue
                if self.backfill is None:
                    # добору немає: прогалина лишається OPEN (для нічного добору), а агрегатор не
                    # чекає на неї вічно — наступні свічки випускаються з діркою
                    await self._skip_missing(gid)
                    continue
                await self._gap_changed(self.detector.begin_fill(gid))
                try:
                    res = await self.backfill(self.detector.gaps[gid])
                except Exception as e:
                    res = FillResult((), error=f"{type(e).__name__}: {e}")
                self.backfill_requests += res.requests
                if res.error:
                    self.backfill_errors.append(res.error)
                for ev in res.events:
                    await self._accept(ev, from_stream=False)
                g = self.detector.resolve(gid)
                await self._gap_changed(g)
                if g.status is not GapStatus.FILLED:
                    await self._skip_missing(gid)
        finally:
            self._filling = False

    async def _skip_missing(self, gid: int) -> None:
        if self.detector.gaps[gid].stream is not Stream.KLINES:
            return
        for m in self.detector.missing_keys(gid):
            for c in self.aggregator.skip(m, m):
                await self._release(c)

    # ------------------------------------------------------------ якість (Q)

    @staticmethod
    def _hour(t_ns: int) -> int:
        return t_ns - t_ns % HOUR_NS

    def _acc(self, t_ns: int) -> DqAccumulator:
        h = self._hour(t_ns)
        acc = self._dq.get(h)
        if acc is None:
            acc = self._dq[h] = DqAccumulator(h, HOUR_NS)
        return acc

    def dq_scores(self) -> list[HourDq]:
        """Скор Q по годинах. N_exp — хвилини від першої побаченої свічки до останньої, що вже
        закрилась на біржі (за біржовим часом потоку), у межах кожної години."""
        latest = self.detector.latest_event_ns
        out: list[HourDq] = []
        for h in sorted(self._dq):
            acc = self._dq[h]
            expected = 0
            if self._first_open_ns is not None and latest is not None:
                lo = max(h, self._first_open_ns)
                hi = min(h + HOUR_NS, latest - self._tf_ns + 1)      # m + tf ≤ latest
                if hi > lo:
                    expected = (hi - lo + self._tf_ns - 1) // self._tf_ns
            out.append(HourDq(h, dq_score(acc.inputs(expected), self.dq_weights, self.tau0_ms)))
        if out:
            self.health.q = out[-1].score.score
        return out

    # ------------------------------------------------------------ завершення

    async def finish(self) -> PipelineReport:
        await self._on_gaps(self.detector.flush())
        for c in self.aggregator.flush():
            await self._release(c)
        dq = tuple(self.dq_scores())
        return PipelineReport(
            health=self.health.snapshot(), gaps=tuple(self.detector.gaps.values()),
            candles_released=self.aggregator.stats.released, trades_emitted=self.trades_emitted,
            dedup={k.value: v for k, v in self.dedup.stats.items()}, aggregator=self.aggregator.stats,
            gap_stats=self.detector.stats, dq=dq, disconnects=tuple(self.disconnects),
            backfill_requests=self.backfill_requests, backfill_errors=tuple(self.backfill_errors))


def _parse_class(detail: str) -> CloseClass:
    head = detail.split(":", 1)[0]
    try:
        return CloseClass(head)
    except ValueError:
        return CloseClass.TRANSIENT


# ---------------------------------------------------------------- REST-добір (klines + aggTrades)

async def _no_sleep(_: float) -> None:
    return None


async def fetch_agg_trades(client: BinanceRestClient, symbol: str, from_id: int, to_id: int,
                           instrument: InstrumentLike, *, limit: int = AGG_TRADES_MAX_LIMIT,
                           sleep: SleepFn | None = None) -> tuple[list[Trade], int]:
    """Угоди з a ∈ [from_id, to_id] через `client.agg_trades(fromId)` (пагінація по ≤ 1000).

    Запити йдуть звичайним транспортом клієнта (token bucket з виміряною вагою 20, RetryPolicy, облік
    `requests_sent`/`last_used_weight`), тож паузи повторів задає `sleep` самого клієнта; параметр `sleep`
    лишено для сумісності викликів (не використовується). Повертає (угоди, кількість запитів).
    """
    del sleep
    if not 1 <= limit <= AGG_TRADES_MAX_LIMIT:
        raise ValueError(f"limit must be in [1, {AGG_TRADES_MAX_LIMIT}]")
    out: list[Trade] = []
    requests = 0
    cursor = from_id
    while cursor <= to_id:
        want = min(limit, to_id - cursor + 1)
        rows = await client.agg_trades(symbol, from_id=cursor, limit=want)
        requests += 1
        now = client.clock.now_ns()
        page = [normalize_rest_agg_trade(r, instrument, now) for r in rows]
        out.extend(t for t in page if from_id <= t.agg_id <= to_id)
        if not page or page[-1].agg_id < cursor or len(page) < want:
            break
        cursor = page[-1].agg_id + 1
    return out, requests


class RestBackfiller:
    """BackfillHook поверх BinanceRestClient: свічки — backfill.backfill_klines, угоди — aggTrades."""

    def __init__(self, client: BinanceRestClient, instrument: InstrumentLike, *, interval: str = "1m",
                 sleep: SleepFn = asyncio.sleep) -> None:
        self.client = client
        self.instrument = instrument
        self.symbol = instrument.symbol_venue
        self.interval = interval
        self._sleep = sleep
        self.calls: list[int] = []

    async def __call__(self, gap: GapRecord) -> FillResult:
        self.calls.append(gap.gap_id)
        if gap.stream is Stream.KLINES:
            lo_ms, hi_ms = gap.ts_lo_ns // NS_PER_MS, gap.ts_hi_ns // NS_PER_MS
            # «зараз» біржі — з самого потоку (макс. ts_event на момент виявлення): детектор оголосив
            # хвилини втраченими лише після їх закриття за біржовим часом, тож локальний годинник
            # (і запас clock_guard_ms на його зсув) тут не потрібен
            now_ms = gap.exchange_ns // NS_PER_MS if gap.exchange_ns is not None else None
            res = await backfill_klines(self.client, self.symbol, lo_ms, hi_ms, interval=self.interval,
                                        limit=min(1500, max(2, gap.expected_count)),
                                        instrument=self.instrument, now_ms=now_ms)
            got = tuple(c for c in res.candles if gap.ts_lo_ns <= c.open_time_ns <= gap.ts_hi_ns)
            return FillResult(got, res.requests)
        if gap.stream is Stream.TRADES and gap.seq_lo is not None and gap.seq_hi is not None:
            trades, n = await fetch_agg_trades(self.client, self.symbol, gap.seq_lo, gap.seq_hi,
                                               self.instrument, sleep=self._sleep)
            return FillResult(tuple(trades), n)
        return FillResult((), error=f"unsupported gap stream {gap.stream}")


@asynccontextmanager
async def offline_rest_client(fixture: RestFixture, clock: Clock, *,
                              base_url: str = "https://fapi.binance.com") -> AsyncIterator[BinanceRestClient]:
    """BinanceRestClient поверх httpx.MockTransport(FixtureRestHandler) — офлайн-демо без мережі."""
    handler = FixtureRestHandler(fixture, clock)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        yield BinanceRestClient(assert_readonly_url(base_url), http,
                                binance_request_bucket(clock, sleep=_no_sleep), RetryPolicy(rng_seed=0),
                                clock=clock, sleep=_no_sleep)


# ---------------------------------------------------------------- реплей сесії з добором


@dataclass
class SessionCapture:
    """Стоки, що просто збирають вихід (для тестів, звітів, демо)."""

    candles: list[Candle] = field(default_factory=list)
    trades: list[Trade] = field(default_factory=list)
    gaps: list[GapRecord] = field(default_factory=list)
    invalid: list[InvalidEvent] = field(default_factory=list)
    disconnects: list[Disconnect] = field(default_factory=list)
    events: int = 0

    def sinks(self) -> PipelineSinks:
        def _count(_: MarketEvent) -> None:
            self.events += 1

        return PipelineSinks(on_event=_count, on_candle=self.candles.append, on_trade=self.trades.append,
                             on_gap=self.gaps.append, on_invalid=self.invalid.append,
                             on_disconnect=self.disconnects.append)


async def replay_session(path: str | Path, instrument: InstrumentLike, *,
                         backfill: BackfillHook | None = None, sinks: PipelineSinks | None = None,
                         clock: FrameClock | None = None,
                         heartbeat_timeout_s: float | None = 10.0, **kw: Any) -> PipelineReport:
    """Відтворити файл сесії через IngestPipeline на віртуальному часі (без очікування)."""
    feed = ReplayFeed(path, instrument=instrument)
    pipe = IngestPipeline(instrument, sinks=sinks, backfill=backfill, clock=clock or FrameClock(),
                          src=Src.REPLAY, heartbeat_timeout_s=heartbeat_timeout_s, **kw)
    return await pipe.run(feed.items())


# ---------------------------------------------------------------- оцінка відновлення (патологічні сесії)


@dataclass(frozen=True, slots=True)
class SessionReference:
    """Еталон, узятий із СИРИХ кадрів файлу (незалежно від конвеєра): закриті свічки й aggTrade id."""

    closed: dict[int, dict[str, Any]]      # open_time_ns → payload `k` кадру x=true
    agg_ids: frozenset[int]


def session_reference(path: str | Path) -> SessionReference:
    from fuzzhelm.ingest.replay import iter_frames  # noqa: PLC0415 — лише для цієї утиліти

    closed: dict[int, dict[str, Any]] = {}
    ids: set[int] = set()
    for rec in iter_frames(path):
        d = rec["data"]
        if rec["stream"].endswith("@aggTrade"):
            ids.add(int(d["a"]))
        elif "@kline_" in rec["stream"] and d["k"]["x"]:
            closed[int(d["k"]["t"]) * NS_PER_MS] = d["k"]
    return SessionReference(closed, frozenset(ids))


def _same_as_kline(c: Candle, k: Mapping[str, Any]) -> bool:
    return (c.o, c.h, c.l, c.c, c.volume, c.quote_volume, c.trades_count) == (
        dec(k["o"]), dec(k["h"]), dec(k["l"]), dec(k["c"]), dec(k["v"]), dec(k["q"]), int(k["n"]))


@dataclass(frozen=True, slots=True)
class RecoveryStats:
    expected_candles: int
    lost_candles: int
    duplicated_candles: int
    extra_candles: int
    mismatched_candles: int
    candles_in_order: bool
    expected_trades: int
    lost_trades: int
    duplicated_trades: int
    extra_trades: int
    gaps: int
    gap_status: dict[str, int]
    watchdog_fires: int
    first_detect_ns: int | None
    last_closed_ns: int | None

    @property
    def zero_loss(self) -> bool:
        return (self.lost_candles == 0 and self.duplicated_candles == 0 and self.mismatched_candles == 0
                and self.candles_in_order and self.lost_trades == 0 and self.duplicated_trades == 0)


def recovery_stats(ref: SessionReference, cap: SessionCapture, report: PipelineReport) -> RecoveryStats:
    """«Нуль втрачених подій»: кожна закрита свічка еталона рівно раз і з тими самими OHLCV/qv/n,
    свічки строго за зростанням open_time; кожен aggTrade id еталона рівно раз."""
    from collections import Counter  # noqa: PLC0415

    got_c = Counter(c.open_time_ns for c in cap.candles)
    by_open = {c.open_time_ns: c for c in cap.candles}
    opens = [c.open_time_ns for c in cap.candles]
    got_t = Counter(t.agg_id for t in cap.trades)
    final = list(report.gaps)
    detected = [g.detected_at_ns for g in final]
    closed = [g.closed_at_ns for g in final if g.closed_at_ns is not None]
    return RecoveryStats(
        expected_candles=len(ref.closed),
        lost_candles=sum(1 for o in ref.closed if got_c[o] == 0),
        duplicated_candles=sum(n - 1 for n in got_c.values() if n > 1),
        extra_candles=sum(1 for o in got_c if o not in ref.closed),
        mismatched_candles=sum(1 for o, k in ref.closed.items()
                               if o in by_open and not _same_as_kline(by_open[o], k)),
        candles_in_order=all(a < b for a, b in pairwise(opens)),
        expected_trades=len(ref.agg_ids),
        lost_trades=sum(1 for a in ref.agg_ids if got_t[a] == 0),
        duplicated_trades=sum(n - 1 for n in got_t.values() if n > 1),
        extra_trades=sum(1 for a in got_t if a not in ref.agg_ids),
        gaps=len(final), gap_status={s: sum(1 for g in final if g.status.value == s)
                                     for s in sorted({g.status.value for g in final})},
        watchdog_fires=report.health.watchdog_fires,
        first_detect_ns=min(detected) if detected else None, last_closed_ns=max(closed) if closed else None)
