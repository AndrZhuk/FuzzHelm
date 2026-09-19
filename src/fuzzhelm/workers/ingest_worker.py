"""Ingest-воркер: живий публічний WS Binance → IngestPipeline → PostgreSQL (свічки, прогалини, Q, журнал).

Найменування: workers/ingest_worker.py
Призначення: «WebSocket … у реальному часі … збереження» (§2 пп. 5, 7; §4.1): два з'єднання BinanceWsClient
    (market: kline/aggTrade/markPrice, public: depth20) → один IngestPipeline на інструмент (нормалізація,
    інваріанти, дедуплікація, прогалини з REST-добором, погодинний Q) → стоки в БД:
      * закриті свічки → `candle` (src = WS, ідемпотентний upsert);
      * кожна зміна стану прогалини → `ingest_gap` (OPEN → FILLING → FILLED | PARTIAL | UNFILLABLE);
      * кожна прийнята подія → `event_journal` (хеш-ланцюг сесії інжесту, пакетами);
      * MLP-скор закритої свічки (QualityGate §4.1, модель `data/anomaly_mlp_<SYMBOL>.json`) →
        `candle.anomaly_score` у тій самій транзакції, що й свічка; аномалії → N_invalid години Q (§5.17);
      * Q кожної ЗАВЕРШЕНОЇ години → `dq_score` (незавершену годину не пишемо: інакше нічний
        scheduler.hourly_dq, що рахує лише години без рядка, уже не порахував би її повністю);
      * знімок PipelineHealth → NOTIFY `fuzzhelm_live` (kind `health`) → GET /market/health
        (зокрема `anomalies` і `anomaly_scored`).
Запуск: python -m fuzzhelm.workers.ingest_worker [--symbols BTCUSDT,ETHUSDT] [--minutes N]
Автор: Андрій Жук, 2026.

Прогрів MLP-скорера: перед першою закритою свічкою потоку — 218 закритих барів, що їй передують, з БД;
яких бракує — публічним REST (лише для живого WS; для підставленого потоку — тільки БД). Моделі немає —
попередження в журналі й інжест без MLP. Сторож тиші WS — монотонний годинник (WS-07), мітки кадрів —
настінний.

Мережа: лише read-only хости з allowlist (Settings + assert_readonly_url у клієнтах); ордерних ендпоінтів
немає. Журнал сесії інжесту має власний run_id (uuid4) без рядка `run`: тип прогону «ingest» у CHECK
таблиці `run` не передбачений (W-09).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import signal
import sys
import time
from collections.abc import AsyncIterable, Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx

from fuzzhelm.config import Settings, get_settings
from fuzzhelm.core.dto import Candle, Instrument, MarketEvent
from fuzzhelm.core.enums import GapStatus, Src, Venue
from fuzzhelm.core.journal import EventJournal, JournalEntry
from fuzzhelm.core.ports import Clock
from fuzzhelm.features.convert import Bar, bar_from_candle
from fuzzhelm.ingest.gap_detector import GapRecord
from fuzzhelm.ingest.pipeline import (
    HOUR_NS,
    AnomalyWarmupHook,
    IngestPipeline,
    PipelineSinks,
    event_kind,
    journal_sink,
)
from fuzzhelm.ingest.recorder import ControlRecord, RawFrame, SessionItem
from fuzzhelm.quality.anomaly_mlp import (
    AnomalyScorer,
    AnomalyVerdict,
    anomaly_health,
    db_anomaly_score,
    load_anomaly_scorer,
)

log = logging.getLogger("fuzzhelm.workers.ingest")

JOURNAL_FLUSH = 2_000          # записів журналу в одній транзакції (плюс скидання на кожну закриту свічку)


@dataclass
class IngestStats:
    frames: int = 0
    events: int = 0
    candles_written: int = 0
    gap_rows: int = 0
    journal_entries: int = 0
    dq_rows: int = 0
    anomaly_scores: int = 0          # candle.anomaly_score, записані разом зі свічками
    anomalies: int = 0               # з них позначено аномаліями (скор > q99 навчання)
    by_symbol: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {k: getattr(self, k) for k in ("frames", "events", "candles_written", "gap_rows",
                                                "journal_entries", "dq_rows", "anomaly_scores", "anomalies",
                                                "by_symbol")}


class InstrumentSink:
    """Стоки одного інструмента → БД. Усі записи — окремими короткими транзакціями воркера."""

    def __init__(self, factory: Any, instrument: Instrument, instrument_id: int, *, journal: EventJournal,
                 buffer: list[JournalEntry], stats: IngestStats, journal_kinds: frozenset[str],
                 publish: bool) -> None:
        self.factory = factory
        self.instrument = instrument
        self.iid = instrument_id
        self.journal = journal
        self.buffer = buffer
        self.stats = stats
        self.kinds = journal_kinds
        self.publish = publish
        self._to_journal = journal_sink(journal)
        self._gap_ids: dict[int, int] = {}
        self._dq_written: set[int] = set()
        self._scores: dict[int, AnomalyVerdict] = {}       # open_time_ns → скор (on_anomaly йде ДО on_candle)
        self.pipeline: IngestPipeline | None = None

    def sinks(self) -> PipelineSinks:
        return PipelineSinks(on_event=self.on_event, on_candle=self.on_candle, on_gap=self.on_gap,
                             on_anomaly=self.on_anomaly)

    def on_anomaly(self, v: AnomalyVerdict) -> None:
        self._scores[v.t_ns] = v

    async def on_event(self, ev: MarketEvent) -> None:
        self.stats.events += 1
        if event_kind(ev) in self.kinds:
            self._to_journal(ev)
        if len(self.buffer) >= JOURNAL_FLUSH:
            await flush_journal(self.factory, self.buffer, self.stats)

    async def on_candle(self, c: Candle) -> None:
        from fuzzhelm.storage.repositories import CandleRepo, JournalRepo  # noqa: PLC0415
        from fuzzhelm.storage.session import session_scope  # noqa: PLC0415

        entries = list(self.buffer)
        self.buffer.clear()
        v = self._scores.pop(c.open_time_ns, None)
        score = None if v is None else db_anomaly_score(v.score)
        async with session_scope(self.factory) as s:
            repo = CandleRepo(s)
            res = await repo.upsert([c], self.iid)
            if score is not None:
                # явний UPDATE: upsert не чіпає вже закриту свічку (напр. записану REST-добором раніше)
                await repo.set_anomaly_scores(self.iid, c.tf, [(c.open_time_ns, score)])
            if entries:
                await JournalRepo(s).append_many(entries)
            if self.publish:
                await self._notify(s, "candle", {"symbol": c.instrument, "open_time_ns": c.open_time_ns,
                                                 "src": "ws", "o": str(c.o), "h": str(c.h), "l": str(c.l),
                                                 "c": str(c.c), "v": str(c.volume)})
                await self._notify(s, "health", self.health())
        self.stats.candles_written += res.inserted + res.updated
        self.stats.journal_entries += len(entries)
        if score is not None and v is not None:
            self.stats.anomaly_scores += 1
            self.stats.anomalies += int(v.anomaly)
        self.stats.by_symbol[c.instrument] = self.stats.by_symbol.get(c.instrument, 0) + 1
        await self.write_complete_hours(latest_ns=c.close_time_ns)

    async def on_gap(self, g: GapRecord) -> None:
        from fuzzhelm.storage.repositories import GapRepo  # noqa: PLC0415
        from fuzzhelm.storage.session import session_scope  # noqa: PLC0415

        async with session_scope(self.factory) as s:
            repo = GapRepo(s)
            if g.gap_id not in self._gap_ids:
                self._gap_ids[g.gap_id] = await repo.open(
                    self.iid, g.stream, g.ts_lo_ns, g.ts_hi_ns, expected_count=g.expected_count,
                    detector=g.detector, detected_at_ns=g.detected_at_ns)
                self.stats.gap_rows += 1
                if g.status is GapStatus.OPEN:
                    return
            await repo.update_status(self._gap_ids[g.gap_id], g.status, filled_rows=g.filled_rows,
                                     count_attempt=g.status is GapStatus.FILLING, at_ns=g.closed_at_ns)

    async def write_complete_hours(self, *, latest_ns: int | None) -> int:
        """Q завершених годин (година завершена, якщо біржовий час пішов за її кінець)."""
        from fuzzhelm.storage.repositories import DqRepo, DqRow  # noqa: PLC0415
        from fuzzhelm.storage.session import session_scope  # noqa: PLC0415

        if self.pipeline is None or latest_ns is None:
            return 0
        rows = [h for h in self.pipeline.dq_scores()
                if h.hour_start_ns + HOUR_NS <= latest_ns and h.hour_start_ns not in self._dq_written]
        if not rows:
            return 0
        async with session_scope(self.factory) as s:
            for h in rows:
                cols: dict[str, Any] = h.score.as_row()
                await DqRepo(s).upsert(DqRow(instrument_id=self.iid, hour_start_ns=h.hour_start_ns, **cols))
        self._dq_written.update(h.hour_start_ns for h in rows)
        self.stats.dq_rows += len(rows)
        return len(rows)

    def health(self) -> dict[str, Any]:
        assert self.pipeline is not None
        snap = self.pipeline.health.snapshot()
        return {"source": "ingest_worker", "symbol": self.instrument.symbol_canon, "frames": snap.frames,
                "lag_p95_ms": snap.lag_p95_ms, "reconnects": snap.reconnects, "gaps_open": snap.gaps_open,
                "gaps_by_status": snap.gaps_by_status, "candles_closed": snap.candles_closed,
                "invalid": snap.invalid, "duplicates": snap.duplicates, "q": snap.q,
                **anomaly_health(self.pipeline.anomaly, snap.anomalies)}

    async def _notify(self, s: Any, kind: str, payload: dict[str, Any]) -> None:
        from fuzzhelm.api.live import LIVE_CHANNEL, encode_notify_payload  # noqa: PLC0415
        from fuzzhelm.workers.persist import pg_notify  # noqa: PLC0415

        try:
            text = encode_notify_payload(kind, payload)
        except ValueError as e:        # подія для браузера не може зірвати запис кроку/свічки
            log.warning("NOTIFY %s skipped: %s", kind, e)
            return
        await pg_notify(s, LIVE_CHANNEL, text)


async def flush_journal(factory: Any, buffer: list[JournalEntry], stats: IngestStats) -> int:
    from fuzzhelm.storage.repositories import JournalRepo  # noqa: PLC0415
    from fuzzhelm.storage.session import session_scope  # noqa: PLC0415

    entries = list(buffer)
    buffer.clear()
    if not entries:
        return 0
    async with session_scope(factory) as s:
        await JournalRepo(s).append_many(entries)
    stats.journal_entries += len(entries)
    return len(entries)


async def pump(source: AsyncIterable[SessionItem], handle: Callable[[SessionItem], Awaitable[None]], *,
               stop: asyncio.Event, deadline_s: float | None = None,
               halted: asyncio.Event | None = None) -> str:
    """Подавати записи потоку в `handle` до кінця потоку, `stop` або дедлайну (причина: eof|stop|deadline).

    Зупинка ніколи не перериває `handle` посередині: обробка запису — це транзакції БД (свічка разом із
    пакетом журналу, прогалина, Q години), і скасування всередині них загубило б уже вийняті з буфера записи
    журналу — ланцюг у БД мав би дірку, а `ingest.end` ліг би після неї. Тож скасовується лише очікування
    НАСТУПНОГО запису (безпечна точка), а запис, що обробляється, дообробляється. `halted` (опційно)
    встановлюється в мить рішення зупинитись (для тестів).
    """
    halt = halted if halted is not None else asyncio.Event()
    busy = False

    async def consume() -> None:
        nonlocal busy
        it = aiter(source)
        while not halt.is_set():
            try:
                item = await anext(it)
            except StopAsyncIteration:
                return
            busy = True
            try:
                await handle(item)
            finally:
                busy = False

    task = asyncio.create_task(consume())
    stopper = asyncio.create_task(stop.wait())
    try:
        done, _ = await asyncio.wait({task, stopper}, timeout=deadline_s, return_when=asyncio.FIRST_COMPLETED)
        reason = "eof" if task in done else ("stop" if stopper in done else "deadline")
        halt.set()
        if not task.done() and not busy:
            task.cancel()                     # чекає наступного запису — жодна транзакція не почата
        try:
            await task
        except asyncio.CancelledError:
            cur = asyncio.current_task()
            if not task.cancelled() or (cur is not None and cur.cancelling()):
                raise
        return reason
    finally:
        if not stopper.done():
            stopper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await stopper


def route(item: SessionItem, by_symbol: dict[str, IngestPipeline]) -> list[IngestPipeline]:
    """Кадр → конвеєр свого інструмента (ім'я потоку `btcusdt@...`); керування — усім конвеєрам."""
    if isinstance(item, ControlRecord):
        return list(by_symbol.values())
    assert isinstance(item, RawFrame)
    p = by_symbol.get(item.stream.split("@", 1)[0].upper())
    return [] if p is None else [p]


def make_ws_client(settings: Settings, clock: Clock, instruments: Sequence[Instrument]) -> Any:
    """Живий WS-клієнт: мітки кадрів — `clock` (час події), сторож тиші — MonotonicClock (WS-07)."""
    from fuzzhelm.infra.wallclock import MonotonicClock  # noqa: PLC0415
    from fuzzhelm.ingest.ws_client import BinanceWsClient  # noqa: PLC0415

    return BinanceWsClient(settings, clock, instruments=list(instruments), src=Src.WS,
                           monotonic=MonotonicClock())


def anomaly_warmup_hook(factory: Any, instrument_id: int, instrument: Instrument, *,
                        rest: Any | None = None) -> AnomalyWarmupHook:
    """Історія для прогріву MLP-скорера: закриті 1m-свічки [lo, hi) з БД; якщо БД не має їх ПОВНІСТЮ і
    впритул, а `rest` задано (живий WS) — добрати публічним REST (klines, лише читання; у БД не пишеться).
    Конвеєр сам бере з результату безперервний хвіст, що прилягає до першої свічки потоку."""
    from fuzzhelm.ingest.backfill import backfill_klines  # noqa: PLC0415
    from fuzzhelm.storage.repositories import CandleRepo  # noqa: PLC0415
    from fuzzhelm.storage.session import session_scope  # noqa: PLC0415

    async def hook(lo_ns: int, hi_ns: int) -> list[Bar]:
        async with session_scope(factory) as s:
            arr = await CandleRepo(s).load_arrays(instrument_id, "1m", lo_ns, hi_ns, closed_only=True)
        bars = arr.bars()
        want = (hi_ns - lo_ns) // 60_000_000_000
        if len(bars) >= want or rest is None:
            return bars
        res = await backfill_klines(rest, instrument.symbol_venue, lo_ns // 1_000_000, hi_ns // 1_000_000 - 1,
                                    instrument=instrument, now_ms=hi_ns // 1_000_000)
        log.info("%s: anomaly warm-up — %d bars in the DB, %d via REST", instrument.symbol_venue, len(bars),
                 len(res.candles))
        return [*bars, *(bar_from_candle(c) for c in res.candles)]

    return hook


@dataclass(frozen=True)
class IngestOptions:
    symbols: tuple[str, ...] = ("BTCUSDT", "ETHUSDT")
    minutes: float | None = None
    database_url: str | None = None
    journal_kinds: tuple[str, ...] = ("candle", "trade", "book", "mark")
    publish: bool = True
    anomaly: bool = True                       # MLP-скорер з data/anomaly_mlp_<SYMBOL>.json
    anomaly_model_dir: Path | None = None


async def run_ingest(opts: IngestOptions, *, settings: Settings | None = None,
                     stop: asyncio.Event | None = None, feed: AsyncIterable[SessionItem] | None = None,
                     http_transport: httpx.AsyncBaseTransport | None = None) -> dict[str, Any]:
    """Сесія інжесту. `feed` — готовий потік записів сесії замість живого WS (тести, реплей у БД);
    `http_transport` — транспорт REST-добору (тести: без мережі)."""
    from fuzzhelm.infra.wallclock import SystemClock, new_run_id  # noqa: PLC0415
    from fuzzhelm.ingest.pipeline import RestBackfiller  # noqa: PLC0415
    from fuzzhelm.ingest.ratelimit import binance_request_bucket  # noqa: PLC0415
    from fuzzhelm.ingest.rest_client import BinanceRestClient  # noqa: PLC0415
    from fuzzhelm.ingest.retry import RetryPolicy  # noqa: PLC0415
    from fuzzhelm.storage.models import APP_ROLE  # noqa: PLC0415
    from fuzzhelm.storage.repositories import InstrumentRepo  # noqa: PLC0415
    from fuzzhelm.storage.session import make_engine, session_factory, session_scope  # noqa: PLC0415

    settings = settings or get_settings()
    stop = stop or asyncio.Event()
    clock = SystemClock()
    engine = make_engine(opts.database_url or settings.database_url, role=APP_ROLE)
    factory = session_factory(engine)
    stats = IngestStats()
    journal_id: UUID = new_run_id()
    buffer: list[JournalEntry] = []
    journal = EventJournal(journal_id, sink=buffer.append, keep=False)
    t0 = time.perf_counter()
    started_ns = clock.now_ns()
    try:
        instruments: list[tuple[Instrument, int]] = []
        async with session_scope(factory) as s:
            for sym in opts.symbols:
                row = await InstrumentRepo(s).get_by_venue_symbol(Venue.BINANCE_USDM, sym)
                if row is None:
                    raise SystemExit(f"instrument {sym} is not in the DB: run `fuzzhelm backfill` first")
                instruments.append((row.to_dto(), row.id))
        scorers: dict[str, AnomalyScorer] = {}
        for inst, _ in instruments:
            sc = (load_anomaly_scorer(inst.symbol_venue, model_dir=opts.anomaly_model_dir)
                  if opts.anomaly else None)
            if sc is not None:
                scorers[inst.symbol_venue] = sc
        journal.append("ingest.start", {
            "symbols": list(opts.symbols), "market": settings.binance_ws_market,
            "public": settings.binance_ws_public, "rest": settings.binance_rest_base,
            "feed": "ws" if feed is None else "injected",
            "anomaly_models": {sym: sc.label for sym, sc in sorted(scorers.items())}}, started_ns, started_ns)
        async with httpx.AsyncClient(timeout=15, transport=http_transport) as http:
            rest = BinanceRestClient(settings.binance_rest_base, http, binance_request_bucket(clock),
                                     RetryPolicy(rng_seed=settings.seed), clock=clock)
            sinks: dict[str, InstrumentSink] = {}
            pipes: dict[str, IngestPipeline] = {}
            for inst, iid in instruments:
                sink = InstrumentSink(factory, inst, iid, journal=journal, buffer=buffer, stats=stats,
                                      journal_kinds=frozenset(opts.journal_kinds), publish=opts.publish)
                scorer = scorers.get(inst.symbol_venue)
                pipe = IngestPipeline(
                    inst, sinks=sink.sinks(), backfill=RestBackfiller(rest, inst), clock=clock, src=Src.WS,
                    heartbeat_timeout_s=None, anomaly=scorer,
                    anomaly_warmup=None if scorer is None else anomaly_warmup_hook(
                        factory, iid, inst, rest=rest if feed is None else None))
                sink.pipeline = pipe
                sinks[inst.symbol_venue] = sink
                pipes[inst.symbol_venue] = pipe
            ws = make_ws_client(settings, clock, [i for i, _ in instruments]) if feed is None else None
            source: AsyncIterable[SessionItem] = ws.items() if ws is not None else feed  # type: ignore[assignment]

            async def handle(item: SessionItem) -> None:
                if isinstance(item, RawFrame):
                    stats.frames += 1
                for p in route(item, pipes):
                    await p.process(item)

            deadline = None if opts.minutes is None else opts.minutes * 60.0
            try:
                stop_reason = await pump(source, handle, stop=stop, deadline_s=deadline)
            finally:
                if ws is not None:
                    await ws.aclose()
            reports = {}
            for sym, pipe in pipes.items():
                rep = await pipe.finish()                 # добір кандидатів у дірки, випуск утриманих свічок
                reports[sym] = rep
                await sinks[sym].write_complete_hours(latest_ns=pipe.detector.latest_event_ns)
            end_ns = clock.now_ns()
            journal.append("ingest.end", {"frames": stats.frames, "events": stats.events,
                                          "candles_written": stats.candles_written, "stop": stop_reason},
                           end_ns, end_ns)
            await flush_journal(factory, buffer, stats)
            ws_stats = ws.stats if ws is not None else {"feed": "injected"}
    finally:
        await engine.dispose()
    elapsed = time.perf_counter() - t0
    return {
        "journal_run_id": str(journal_id), "journal_head": journal.head.hex(), "elapsed_s": round(elapsed, 1),
        "stop": stop_reason, **stats.as_dict(), "ws": ws_stats,
        "pipelines": {sym: {"candles_released": r.candles_released, "trades": r.trades_emitted,
                            "gaps": len(r.gaps), "gap_status": sorted({g.status.value for g in r.gaps}),
                            "dq_hours": [(h.hour_start_ns, round(h.score.score, 4)) for h in r.dq],
                            "backfill_requests": r.backfill_requests, "invalid": r.health.invalid,
                            "duplicates": r.health.duplicates, "lag_p95_ms": r.health.lag_p95_ms,
                            "anomalies": r.health.anomalies,
                            "anomaly_scored": _scored(pipes[sym]),
                            "anomaly_warmup_error": pipes[sym].anomaly_warmup_error}
                      for sym, r in reports.items()},
    }


def _scored(pipe: IngestPipeline) -> int:
    return 0 if pipe.anomaly is None else pipe.anomaly.scored


def parse_args(argv: Sequence[str] | None = None) -> IngestOptions:
    ap = argparse.ArgumentParser(prog="python -m fuzzhelm.workers.ingest_worker",
                                 description="FuzzHelm live ingest worker (public read-only streams).")
    ap.add_argument("--symbols", default="BTCUSDT,ETHUSDT")
    ap.add_argument("--minutes", type=float, default=None, help="stop after N minutes (default: SIGINT)")
    ap.add_argument("--database-url", default=None)
    ap.add_argument("--journal-kinds", default="candle,trade,book,mark",
                    help="event kinds written to event_journal (book snapshots are the bulk)")
    ap.add_argument("--no-notify", action="store_true")
    ap.add_argument("--no-anomaly", action="store_true",
                    help="do not load the MLP anomaly models (data/anomaly_mlp_<SYMBOL>.json)")
    a = ap.parse_args(argv)
    return IngestOptions(symbols=tuple(s.strip().upper() for s in a.symbols.split(",") if s.strip()),
                         minutes=a.minutes, database_url=a.database_url,
                         journal_kinds=tuple(k.strip() for k in a.journal_kinds.split(",") if k.strip()),
                         publish=not a.no_notify, anomaly=not a.no_anomaly)


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=os.environ.get("FUZZHELM_LOG", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    opts = parse_args(argv)

    async def amain() -> dict[str, Any]:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, stop.set)
        return await run_ingest(opts, stop=stop)

    out = asyncio.run(amain())
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
