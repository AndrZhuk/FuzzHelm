"""Періодичні задачі сервісу (APScheduler): нічний добір прогалин, погодинний Q, щоденний звіт.

Найменування: scheduler/jobs.py
Призначення: «сервіс як сервіс» (брифінг §4.3, §12 фаза 8). Кожна задача — звичайна async-функція від
`JobContext` (сховище через одиницю роботи, годинник, добирач, нотифікатор) і явного моменту часу, тому
тестується без справжнього часу і без очікування; APScheduler лише викликає її за розкладом (UTC).
Автор: Андрій Жук, 2026.

Розклад (UTC): 02:30 — повторний добір прогалин PARTIAL/UNFILLABLE; hh:05 — Q за попередню годину
(лише для годин без рядка dq_score: живий конвеєр пише власний Q, його не перезаписуємо); 00:10 — звіт
за попередню добу в Telegram. Одна задача не блокує інші: max_instances=1, coalesce=True.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Protocol
from uuid import UUID

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from fuzzhelm.api.services import DbUnitOfWork
from fuzzhelm.config import Settings, get_settings
from fuzzhelm.core.enums import GapStatus, RunKind, RunStatus, Src
from fuzzhelm.core.ports import Clock
from fuzzhelm.features.convert import to_float
from fuzzhelm.infra.wallclock import SystemClock
from fuzzhelm.ingest.backfill import backfill_klines
from fuzzhelm.notify.telegram import SendResult, TelegramNotifier
from fuzzhelm.quality.dq_score import (
    DqInputs,
    dq_score,
    load_dq_weights,
    load_tau0_ms,
    merged_length_ns,
    p95,
)
from fuzzhelm.quality.invariants import InstrumentSpec, check_candle
from fuzzhelm.storage.repositories import CandleRow, DqRow, GapRow, InstrumentRow
from fuzzhelm.storage.session import get_default_engine, session_factory

log = logging.getLogger(__name__)

NS_PER_MS = 1_000_000
NS_PER_S = 1_000_000_000
NS_PER_MIN = 60 * NS_PER_S
NS_PER_HOUR = 60 * NS_PER_MIN
NS_PER_DAY = 24 * NS_PER_HOUR
TF = "1m"
RETRY_STATUSES: tuple[GapStatus, ...] = (GapStatus.PARTIAL, GapStatus.UNFILLABLE)
LIVE_RUN_KINDS: tuple[RunKind, ...] = (RunKind.PAPER, RunKind.REPLAY, RunKind.TESTNET)

JOB_GAP_BACKFILL = "nightly_gap_backfill"
JOB_HOURLY_DQ = "hourly_dq_score"
JOB_DAILY_REPORT = "daily_report"


# ------------------------------------------------------------------ порти


class SchedulerRepos(Protocol):
    """Підмножина storage-репозиторіїв (api.services.DbRepos задовольняє її структурно)."""

    gaps: Any
    candles: Any
    instruments: Any
    dq: Any
    risk: Any
    runs: Any
    equity: Any


UnitOfWork = Callable[[], contextlib.AbstractAsyncContextManager[SchedulerRepos]]


class GapBackfiller(Protocol):
    async def __call__(self, gap: GapRow, instrument: InstrumentRow, repos: SchedulerRepos) -> int:
        """Добрати свічки прогалини і записати їх; повертає кількість записаних рядків."""
        ...


@dataclass
class JobContext:
    uow: UnitOfWork
    clock: Clock
    weights: tuple[float, float, float, float]
    tau0_ms: float = 1000.0
    backfill: GapBackfiller | None = None
    notifier: TelegramNotifier | None = None
    anomaly_threshold: float | None = None  # поріг anomaly_score (q99 навчання MLP); None — не рахувати
    max_attempts: int = 5
    runs: dict[str, Any] = field(default_factory=dict)  # останні звіти задач (для /health і тестів)


# ------------------------------------------------------------------ 1. нічний добір прогалин


@dataclass(frozen=True, slots=True)
class GapOutcome:
    gap_id: int
    status_before: str | None
    status_after: str | None
    expected: int
    present: int
    error: str | None = None


def expected_bars(gap: GapRow, step_ns: int = NS_PER_MIN) -> int:
    """Очікувана кількість барів прогалини: expected_count або ⌈(ts_hi − ts_lo)/крок⌉ (інтервал [lo, hi))."""
    if gap.expected_count:
        return int(gap.expected_count)
    lo, hi = gap.ts_lo_ns or 0, gap.ts_hi_ns or 0
    return max(1, -(-(hi - lo) // step_ns))


async def _present_bars(repos: SchedulerRepos, gap: GapRow, expected: int) -> int:
    page = await repos.candles.range(
        gap.instrument_id, TF, gap.ts_lo_ns, gap.ts_hi_ns, limit=expected + 1, closed_only=True
    )
    return len(page.items)


def next_status(present: int, expected: int, attempts_after: int, max_attempts: int) -> GapStatus:
    """FILLED — усі бари є; інакше PARTIAL, а після вичерпання спроб — UNFILLABLE (біржа даних не має)."""
    if present >= expected:
        return GapStatus.FILLED
    if attempts_after >= max_attempts:
        return GapStatus.UNFILLABLE
    return GapStatus.PARTIAL


async def retry_gaps(
    ctx: JobContext, *, statuses: Sequence[GapStatus] = RETRY_STATUSES, limit: int = 100
) -> list[GapOutcome]:
    """Повторний добір незаповнених прогалин свічок; кожна прогалина — власна транзакція."""
    if ctx.backfill is None:
        return []
    async with ctx.uow() as repos:
        gaps: list[GapRow] = await repos.gaps.list_by_status(
            statuses, max_attempts=ctx.max_attempts, limit=limit
        )
        instruments = {i.id: i for i in await repos.instruments.list()}
    outcomes: list[GapOutcome] = []
    now_ns = ctx.clock.now_ns()
    for gap in gaps:
        inst = instruments.get(gap.instrument_id or -1)
        if gap.stream != "klines" or inst is None:
            continue  # угоди/книга через REST не добираються; невідомий інструмент — пропуск
        expected = expected_bars(gap)
        error = None
        try:
            async with ctx.uow() as repos:
                await ctx.backfill(gap, inst, repos)
                present = await _present_bars(repos, gap, expected)
                status = next_status(present, expected, (gap.attempts or 0) + 1, ctx.max_attempts)
                row = await repos.gaps.update_status(gap.id, status, filled_rows=present, at_ns=now_ns)
        except Exception as e:  # одна зіпсована прогалина не зупиняє решту
            error = type(e).__name__
            log.warning("gap %s backfill failed: %s", gap.id, error)
            async with ctx.uow() as repos:
                attempts_after = (gap.attempts or 0) + 1
                fallback = (
                    GapStatus.UNFILLABLE if attempts_after >= ctx.max_attempts else GapStatus(gap.status)
                )
                row = await repos.gaps.update_status(gap.id, fallback, at_ns=now_ns)
            present = gap.filled_rows or 0
        outcomes.append(GapOutcome(gap.id, gap.status, row.status, expected, present, error))
    ctx.runs[JOB_GAP_BACKFILL] = {
        "at_ns": now_ns,
        "gaps": len(outcomes),
        "filled": sum(o.status_after == GapStatus.FILLED for o in outcomes),
    }
    return outcomes


class RestGapBackfiller:
    """Типовий добирач: REST klines (ingest.backfill.backfill_klines) → CandleRepo.upsert (ідемпотентно)."""

    def __init__(self, client: Any) -> None:
        self.client = client

    async def __call__(self, gap: GapRow, instrument: InstrumentRow, repos: SchedulerRepos) -> int:
        start_ms = (gap.ts_lo_ns or 0) // NS_PER_MS
        end_ms = max(start_ms, ((gap.ts_hi_ns or 0) - NS_PER_MIN) // NS_PER_MS)
        result = await backfill_klines(
            self.client, instrument.symbol_venue, start_ms, end_ms, instrument=instrument.to_dto()
        )
        if not result.candles:
            return 0
        res = await repos.candles.upsert(list(result.candles), instrument.id)
        return int(res.inserted + res.updated)


# ------------------------------------------------------------------ 2. погодинний Q


def hour_floor(ns: int) -> int:
    return ns - ns % NS_PER_HOUR


def dq_inputs_for_hour(
    candles: Sequence[CandleRow],
    gaps: Iterable[GapRow],
    hour_start_ns: int,
    instrument: InstrumentRow | None,
    anomaly_threshold: float | None,
) -> DqInputs:
    """Вхід скору Q з рядків БД за годину [h, h+1 год): 60 очікуваних кошиків 1m (брифінг §5.17)."""
    hour_end = hour_start_ns + NS_PER_HOUR
    spec = None if instrument is None else InstrumentSpec(instrument.tick_size, instrument.step_size)
    closed = [c for c in candles if c.is_closed and hour_start_ns <= c.open_time_ns < hour_end]
    invalid = sum(1 for c in closed if check_candle(c, spec, check_volume_step=False))
    anomalies = 0
    if anomaly_threshold is not None:
        anomalies = sum(
            1 for c in closed if c.anomaly_score is not None and to_float(c.anomaly_score) > anomaly_threshold
        )
    # своєчасність — лише для WS-свічок: REST-добір за визначенням «запізнілий», це не лаг потоку
    lags = [
        max(0, c.ingested_at_ns - (c.open_time_ns + NS_PER_MIN)) / NS_PER_MS
        for c in closed
        if c.src == Src.WS and c.ingested_at_ns is not None
    ]
    gap_ns = merged_length_ns(
        (max(g.ts_lo_ns or 0, hour_start_ns), min(g.ts_hi_ns or 0, hour_end)) for g in gaps
    )
    return DqInputs(
        expected_buckets=60,
        observed_buckets=len({c.open_time_ns for c in closed}),
        total_count=len(closed),
        invalid_count=invalid,
        anomaly_count=anomalies,
        gap_seconds=gap_ns / NS_PER_S,
        lag_p95_ms=p95(lags),
    )


async def hourly_dq(ctx: JobContext, hour_start_ns: int | None = None, *, force: bool = False) -> list[DqRow]:
    """Q за годину (типово — попередня повна година) для кожного активного інструмента."""
    h = hour_start_ns if hour_start_ns is not None else hour_floor(ctx.clock.now_ns()) - NS_PER_HOUR
    if h % NS_PER_HOUR:
        raise ValueError("hour_start_ns must be aligned to a UTC hour")
    written: list[DqRow] = []
    async with ctx.uow() as repos:
        for inst in await repos.instruments.list(active_only=True):
            if not force and await repos.dq.range(inst.id, h, h + NS_PER_HOUR):
                continue  # рядок уже записав живий конвеєр — не перезаписуємо
            page = await repos.candles.range(inst.id, TF, h, h + NS_PER_HOUR, limit=1000)
            gaps = await repos.gaps.list_overlapping(inst.id, h, h + NS_PER_HOUR, stream="klines")
            inputs = dq_inputs_for_hour(page.items, gaps, h, inst, ctx.anomaly_threshold)
            score = dq_score(inputs, ctx.weights, ctx.tau0_ms)
            row = DqRow(instrument_id=inst.id, hour_start_ns=h, **score.as_row())
            await repos.dq.upsert(row)
            written.append(row)
    ctx.runs[JOB_HOURLY_DQ] = {"hour_start_ns": h, "rows": len(written)}
    return written


# ------------------------------------------------------------------ 3. щоденний звіт


@dataclass(frozen=True, slots=True)
class DailyReport:
    day: str
    day_start_ns: int
    candles: Mapping[str, int]
    q_min: Mapping[str, float | None]
    gaps: Mapping[str, int]
    run_id: UUID | None
    vetoes: int
    transitions: tuple[str, ...]
    equity: Decimal | None
    drawdown: float | None
    sent: SendResult | None


def day_floor(ns: int) -> int:
    return ns - ns % NS_PER_DAY


def _hhmm(ns: int) -> str:
    return datetime.fromtimestamp(ns // NS_PER_S, tz=UTC).strftime("%H:%M")


async def _live_run(repos: SchedulerRepos) -> Any | None:
    best = None
    for kind in LIVE_RUN_KINDS:
        for status in (RunStatus.RUNNING, None):
            rows = await repos.runs.list(kind=kind, status=status, limit=1)
            if rows and (best is None or (rows[0].started_at_ns or 0) > (best.started_at_ns or 0)):
                best = rows[0]
    return best


async def daily_report(ctx: JobContext, day_start_ns: int | None = None) -> DailyReport:
    """Підсумок доби (типово — попередньої UTC-доби) і відправка в Telegram (якщо налаштовано)."""
    d = day_start_ns if day_start_ns is not None else day_floor(ctx.clock.now_ns()) - NS_PER_DAY
    end = d + NS_PER_DAY
    candles: dict[str, int] = {}
    q_min: dict[str, float | None] = {}
    async with ctx.uow() as repos:
        for inst in await repos.instruments.list(active_only=True):
            page = await repos.candles.range(inst.id, TF, d, end, limit=2000, closed_only=True)
            candles[inst.symbol_canon] = len(page.items)
            scores = [
                to_float(r.score) if isinstance(r.score, Decimal) else r.score
                for r in await repos.dq.range(inst.id, d, end)
                if r.score is not None
            ]
            q_min[inst.symbol_canon] = min(scores) if scores else None
        gaps = await repos.gaps.stats()
        run = await _live_run(repos)
        vetoes, transitions, equity, drawdown = 0, [], None, None
        if run is not None:
            events = await repos.risk.list_for_run(run.id, since_ns=d, limit=100_000)
            vetoes = sum(1 for e in events if e.verdict == "VETO" and (e.ts_ns or 0) < end)
            transitions = [
                f"{_hhmm(t.ts_ns or 0)} {t.state_from}→{t.state_to}"
                for t in await repos.risk.transitions(run.id)
                if d <= (t.ts_ns or 0) < end
            ]
            curve = await repos.equity.curve(run.id, d, end)
            if curve:
                equity = curve[-1].equity
                drawdown = None if curve[-1].drawdown is None else to_float(Decimal(curve[-1].drawdown))
    day = datetime.fromtimestamp(d // NS_PER_S, tz=UTC).strftime("%Y-%m-%d")
    sent = None
    if ctx.notifier is not None:
        sent = await ctx.notifier.daily_report(
            day=day,
            candles=candles,
            q_min=q_min,
            gaps=gaps,
            vetoes=vetoes,
            transitions=transitions,
            equity=equity,
            drawdown=drawdown,
        )
    report = DailyReport(
        day=day,
        day_start_ns=d,
        candles=candles,
        q_min=q_min,
        gaps=gaps,
        run_id=None if run is None else run.id,
        vetoes=vetoes,
        transitions=tuple(transitions),
        equity=equity,
        drawdown=drawdown,
        sent=sent,
    )
    ctx.runs[JOB_DAILY_REPORT] = {"day": day, "sent": None if sent is None else sent.status.value}
    return report


# ------------------------------------------------------------------ розклад


@dataclass(frozen=True, slots=True)
class JobSpec:
    id: str
    trigger: CronTrigger
    func: Callable[[], Awaitable[Any]]


def job_specs(ctx: JobContext) -> list[JobSpec]:
    return [
        JobSpec(JOB_GAP_BACKFILL, CronTrigger(hour=2, minute=30, timezone=UTC), lambda: retry_gaps(ctx)),
        JobSpec(JOB_HOURLY_DQ, CronTrigger(minute=5, timezone=UTC), lambda: hourly_dq(ctx)),
        JobSpec(JOB_DAILY_REPORT, CronTrigger(hour=0, minute=10, timezone=UTC), lambda: daily_report(ctx)),
    ]


def build_db_context(
    settings: Settings | None = None,
    *,
    backfill: GapBackfiller | None = None,
    notifier: TelegramNotifier | None = None,
    clock: Clock | None = None,
) -> JobContext:
    """Робоча збірка: PostgreSQL (роль fuzzhelm_app), ваги AHP і τ₀ з dq_weights.yaml, Telegram із .env.

    Жодного з'єднання під час збирання. REST-добирач передає викликач (воркер), бо йому потрібен
    httpx-клієнт і token bucket процесу інжесту.
    """
    s = settings or get_settings()
    return JobContext(
        uow=DbUnitOfWork(session_factory(get_default_engine())),
        clock=clock or SystemClock(),
        weights=load_dq_weights(s.config_dir),
        tau0_ms=load_tau0_ms(s.config_dir),
        backfill=backfill,
        notifier=notifier or TelegramNotifier.from_settings(s),
    )


def build_scheduler(ctx: JobContext, scheduler: AsyncIOScheduler | None = None) -> AsyncIOScheduler:
    """Зареєструвати три задачі; запуск (`.start()`) — справа викликача (воркер або lifespan)."""
    sched = scheduler or AsyncIOScheduler(timezone=UTC)
    for spec in job_specs(ctx):
        sched.add_job(
            spec.func,
            spec.trigger,
            id=spec.id,
            name=spec.id,
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=600,
        )
    return sched
