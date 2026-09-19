"""Планувальник: нічний добір прогалин, погодинний Q, щоденний звіт, розклад — без справжнього часу.

Найменування: tests/unit/test_scheduler.py
Автор: Андрій Жук, 2026.

Сховище — MemoryDb (tests/helpers/api_fakes.py), час — ManualClock, мережа — фальшивий KlineSource і respx
для Telegram. Жодного sleep: задачі викликаються напряму з явним моментом часу.
"""

from __future__ import annotations

import asyncio
import math
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

import httpx
import pytest
import respx
from apscheduler.events import EVENT_SCHEDULER_STARTED
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from pydantic import SecretStr
from tests.helpers.api_fakes import NS_PER_MIN, T0_NS, MemoryDb

from fuzzhelm.core.clock import ManualClock
from fuzzhelm.core.enums import GapStatus, RunKind, RunStatus, Src, VerdictKind
from fuzzhelm.notify.telegram import SendStatus, TelegramNotifier
from fuzzhelm.quality.dq_score import dq_score
from fuzzhelm.scheduler.jobs import (
    JOB_DAILY_REPORT,
    JOB_GAP_BACKFILL,
    JOB_HOURLY_DQ,
    JobContext,
    RestGapBackfiller,
    build_scheduler,
    daily_report,
    dq_inputs_for_hour,
    expected_bars,
    hourly_dq,
    next_status,
    retry_gaps,
    serve,
)
from fuzzhelm.storage.repositories import CandleRow, EquityRow, GapRow, InstrumentRow, RunRow, UpsertResult

NS_PER_HOUR = 60 * NS_PER_MIN
NS_PER_DAY = 24 * NS_PER_HOUR
WEIGHTS = (0.455446, 0.262850, 0.140852, 0.140852)
TOKEN = "42:secret-token-for-scheduler-tests"


def inst_row() -> InstrumentRow:
    return InstrumentRow(
        id=1,
        venue="BINANCE_USDM",
        symbol_venue="BTCUSDT",
        symbol_canon="BTC-USDT-PERP",
        base_asset="BTC",
        quote_asset="USDT",
        contract_type="PERP",
        tick_size=Decimal("0.1"),
        step_size=Decimal("0.001"),
        min_notional=Decimal("5"),
        mmr=Decimal("0.005"),
        maint_amount=Decimal(0),
        max_leverage=3,
        active=True,
        spec_fetched_at_ns=None,
    )


def candle(t: int, *, src: Src = Src.REST, ingested: int | None = None, h: str = "100.5") -> CandleRow:
    return CandleRow(
        instrument_id=1,
        tf="1m",
        open_time_ns=t,
        close_time_ns=t + NS_PER_MIN - 1_000_000,
        o=Decimal("100.0"),
        h=Decimal(h),
        l=Decimal("99.5"),
        c=Decimal("100.2"),
        volume=Decimal("1.5"),
        quote_volume=Decimal("150.3"),
        trades_count=10,
        vwap=None,
        is_closed=True,
        is_synthetic=False,
        src=int(src),
        anomaly_score=None,
        ingested_at_ns=ingested,
    )


def gap(gid: int, lo: int, n: int, status: GapStatus, attempts: int, stream: str = "klines") -> GapRow:
    return GapRow(
        id=gid,
        instrument_id=1,
        stream=stream,
        ts_lo_ns=lo,
        ts_hi_ns=lo + n * NS_PER_MIN,
        expected_count=n,
        filled_rows=0,
        detector="time",
        status=status.value,
        attempts=attempts,
        detected_at_ns=T0_NS - NS_PER_DAY + gid,
        closed_at_ns=None,
    )


def make_ctx(db: MemoryDb, clock: ManualClock, **kw: Any) -> JobContext:
    db.state.instruments[1] = inst_row()
    return JobContext(uow=db.uow, clock=clock, weights=WEIGHTS, **kw)


class FillBackfiller:
    """Добирач-двійник: дописує в сховище задану кількість барів прогалини (або падає)."""

    def __init__(self, db: MemoryDb, fill: dict[int, int], fail: set[int] | None = None) -> None:
        self.db, self.fill, self.fail = db, fill, fail or set()
        self.calls: list[int] = []

    async def __call__(self, g: GapRow, instrument: InstrumentRow, repos: Any) -> int:
        self.calls.append(g.id)
        if g.id in self.fail:
            raise ConnectionError("exchange unreachable")
        rows = [candle((g.ts_lo_ns or 0) + i * NS_PER_MIN) for i in range(self.fill.get(g.id, 0))]
        repos.candles.add(rows)
        return len(rows)


# ------------------------------------------------------------------ 1. нічний добір


async def test_retry_gaps_fills_partial_and_gives_up_after_max_attempts() -> None:
    db, clock = MemoryDb(), ManualClock(T0_NS + 2 * NS_PER_HOUR + 30 * NS_PER_MIN)
    db.state.gaps = {
        1: gap(1, T0_NS, 5, GapStatus.PARTIAL, attempts=1),  # доберемо повністю
        2: gap(2, T0_NS + 10 * NS_PER_MIN, 3, GapStatus.UNFILLABLE, attempts=4),  # 1 з 3, остання спроба
        3: gap(3, T0_NS + 20 * NS_PER_MIN, 2, GapStatus.PARTIAL, attempts=0, stream="trades"),
        4: gap(4, T0_NS + 30 * NS_PER_MIN, 2, GapStatus.FILLED, attempts=1),
        5: gap(5, T0_NS + 40 * NS_PER_MIN, 2, GapStatus.PARTIAL, attempts=5),  # спроби вичерпано
        6: gap(6, T0_NS + 50 * NS_PER_MIN, 2, GapStatus.PARTIAL, attempts=0),  # мережа впала
    }
    bf = FillBackfiller(db, fill={1: 5, 2: 1}, fail={6})
    out = await retry_gaps(make_ctx(db, clock, backfill=bf, max_attempts=5))
    assert bf.calls == [1, 2, 6]  # угоди, FILLED і вичерпані не беремо
    by_id = {o.gap_id: o for o in out}
    assert (by_id[1].status_after, by_id[1].present, by_id[1].expected) == ("FILLED", 5, 5)
    assert (by_id[2].status_after, by_id[2].present) == ("UNFILLABLE", 1)
    assert by_id[6].status_after == "PARTIAL" and by_id[6].error == "ConnectionError"
    g = db.state.gaps
    assert [g[i].attempts for i in (1, 2, 3, 4, 5, 6)] == [2, 5, 0, 1, 5, 1]  # кожна спроба врахована
    assert g[1].closed_at_ns == clock.now_ns() and g[1].filled_rows == 5
    # падіння однієї прогалини відкотило лише її транзакцію: свічки інших на місці
    assert len(db.state.candles[(1, "1m")]) == 6
    # повтор ідемпотентний: FILLED більше не чіпається
    bf2 = FillBackfiller(db, fill={})
    await retry_gaps(make_ctx(db, clock, backfill=bf2, max_attempts=5))
    assert 1 not in bf2.calls


def test_gap_status_rules() -> None:
    g = gap(1, T0_NS, 3, GapStatus.PARTIAL, 0)
    assert expected_bars(g) == 3
    assert expected_bars(replace(g, expected_count=None, ts_hi_ns=T0_NS + 150 * 1_000_000_000)) == 3  # ⌈2.5⌉
    assert next_status(3, 3, 1, 5) is GapStatus.FILLED
    assert next_status(2, 3, 4, 5) is GapStatus.PARTIAL
    assert next_status(2, 3, 5, 5) is GapStatus.UNFILLABLE
    assert next_status(3, 3, 9, 5) is GapStatus.FILLED  # повне заповнення важливіше за ліміт спроб


async def test_rest_gap_backfiller_requests_gap_bars_and_upserts() -> None:
    class Klines:
        def __init__(self) -> None:
            self.clock = ManualClock(T0_NS + NS_PER_HOUR)
            self.requests: list[tuple[int | None, int | None]] = []

        async def klines(
            self,
            symbol: str,
            interval: str = "1m",
            start_ms: int | None = None,
            end_ms: int | None = None,
            limit: int = 1500,
        ) -> list[list[Any]]:
            self.requests.append((start_ms, end_ms))
            assert start_ms is not None and end_ms is not None
            step = 60_000
            first = -(-start_ms // step) * step
            return [
                [t, "100.0", "100.5", "99.5", "100.2", "1.5", t + step - 1, "150.3", 10, "0.7", "70.1", "0"]
                for t in range(first, end_ms + 1, step)
            ]

    class Candles:
        def __init__(self) -> None:
            self.got: list[Any] = []

        async def upsert(self, candles: list[Any], instrument_id: int) -> UpsertResult:
            self.got = list(candles)
            return UpsertResult(inserted=len(candles), updated=0, skipped=0)

    class Repos:
        candles = Candles()

    client = Klines()
    repos = Repos()
    n = await RestGapBackfiller(client)(gap(1, T0_NS, 4, GapStatus.PARTIAL, 0), inst_row(), repos)  # type: ignore[arg-type]
    assert n == 4 and client.requests[0] == (T0_NS // 1_000_000, (T0_NS + 3 * NS_PER_MIN) // 1_000_000)
    assert [c.open_time_ns for c in repos.candles.got] == [T0_NS + i * NS_PER_MIN for i in range(4)]
    assert all(
        c.is_closed and c.src is Src.REST and c.instrument == "BTC-USDT-PERP" for c in repos.candles.got
    )


# ------------------------------------------------------------------ 2. погодинний Q


async def test_hourly_dq_scores_previous_hour_from_stored_rows() -> None:
    db, clock = MemoryDb(), ManualClock(T0_NS + NS_PER_HOUR + 5 * NS_PER_MIN)  # hh:05 наступної години
    ctx = make_ctx(db, clock)
    rows = []
    for i in range(60):
        if i in (10, 11, 12):
            continue  # 3 відсутні бари
        t = T0_NS + i * NS_PER_MIN
        lag = 250_000_000 if i % 2 else 50_000_000  # WS-лаг 250 мс на непарних барах
        rows.append(candle(t, src=Src.WS, ingested=t + NS_PER_MIN + lag, h="99.9" if i == 30 else "100.5"))
    db.repos().candles.add(rows)
    db.state.gaps = {1: gap(1, T0_NS + 10 * NS_PER_MIN, 3, GapStatus.OPEN, 0)}
    written = await hourly_dq(ctx)
    assert len(written) == 1 and written[0].hour_start_ns == T0_NS
    row = db.state.dq[(1, T0_NS)]
    assert (row.expected_buckets, row.observed_buckets, row.invalid_count) == (60, 57, 1)  # h=99.9 < c=100.2
    assert row.gap_seconds == 180.0
    assert row.completeness == round(57 / 60, 4) and row.validity == round(1 - 1 / 57, 4)
    assert row.continuity == round(1 - 180 / 3600, 4)
    assert row.lag_p95_ms == 250.0 and row.timeliness == round(math.exp(-0.25), 4)
    expected_q = (
        WEIGHTS[0] * 57 / 60
        + WEIGHTS[1] * (1 - 1 / 57)
        + WEIGHTS[2] * math.exp(-0.25)
        + WEIGHTS[3] * (1 - 180 / 3600)
    )
    assert row.score == round(expected_q, 4)
    # рядок живого конвеєра не перезаписується (лише force=True)
    db.state.dq[(1, T0_NS)] = replace(row, score=0.5)
    assert await hourly_dq(ctx) == [] and db.state.dq[(1, T0_NS)].score == 0.5
    assert len(await hourly_dq(ctx, T0_NS, force=True)) == 1
    with pytest.raises(ValueError):
        await hourly_dq(ctx, T0_NS + 1)


def test_dq_inputs_ignore_rest_lag_and_clip_gaps_to_hour() -> None:
    rows = [
        candle(T0_NS + i * NS_PER_MIN, src=Src.REST, ingested=T0_NS + 10 * NS_PER_HOUR) for i in range(60)
    ]
    long_gap = replace(gap(1, T0_NS - NS_PER_HOUR, 0, GapStatus.OPEN, 0), ts_hi_ns=T0_NS + 30 * NS_PER_MIN)
    inputs = dq_inputs_for_hour(rows, [long_gap], T0_NS, inst_row(), None)
    assert inputs.lag_p95_ms == 0.0  # REST-добір — не лаг потоку
    assert inputs.gap_seconds == 1800.0  # обрізано до межі години
    assert dq_score(inputs, WEIGHTS).completeness == 1.0


# ------------------------------------------------------------------ 3. щоденний звіт


async def test_daily_report_summarizes_previous_utc_day_and_sends_telegram() -> None:
    db = MemoryDb()
    day = T0_NS  # 2025-09-18 00:00 UTC
    clock = ManualClock(day + NS_PER_DAY + 10 * NS_PER_MIN)  # 00:10 наступної доби
    run_id = UUID(int=5)
    db.state.runs[run_id] = RunRow(
        id=run_id,
        kind=RunKind.PAPER.value,
        strategy_id=None,
        instrument_id=1,
        tf="1m",
        ts_from_ns=day,
        ts_to_ns=None,
        config={},
        config_hash=bytes(32),
        dataset_hash=bytes(32),
        git_sha=None,
        seed=1,
        engine="mamdani",
        journal_head_hash=None,
        equity_hash=None,
        status=RunStatus.RUNNING.value,
        error=None,
        started_at_ns=day,
        finished_at_ns=None,
    )
    db.repos().candles.add([candle(day + i * NS_PER_MIN) for i in range(3)] + [candle(day - NS_PER_MIN)])
    risk = db.repos().risk
    risk.add(run_id=run_id, ts_ns=day + NS_PER_HOUR, rule="stale_data", verdict=VerdictKind.VETO)
    risk.add(run_id=run_id, ts_ns=day + 2 * NS_PER_HOUR, rule="max_daily_loss", verdict=VerdictKind.VETO)
    risk.add(run_id=run_id, ts_ns=day + NS_PER_DAY + NS_PER_MIN, rule="stale_data", verdict=VerdictKind.VETO)
    risk.add(
        run_id=run_id,
        ts_ns=day + 10 * NS_PER_HOUR + 15 * NS_PER_MIN,
        rule="risk_state",
        state_from="NORMAL",
        state_to="WARNING",
    )
    db.state.equity[run_id] = [
        EquityRow(
            run_id=run_id,
            ts_ns=day + 23 * NS_PER_HOUR,
            equity=Decimal("10012.5"),
            cash=None,
            unrealized=None,
            gross_exposure=None,
            leverage=None,
            drawdown=Decimal("0.004"),
            risk_state="WARNING",
            kappa=None,
            var95=None,
            cvar95=None,
        )
    ]
    async with httpx.AsyncClient() as http:
        notifier = TelegramNotifier(SecretStr(TOKEN), "7", http=http, now_s=lambda: 0.0)
        with respx.mock() as mock:
            route = mock.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage").mock(
                return_value=httpx.Response(200, json={"ok": True})
            )
            rep = await daily_report(make_ctx(db, clock, notifier=notifier))
    assert rep.day == "2025-09-18" and rep.day_start_ns == day
    assert rep.candles == {"BTC-USDT-PERP": 3} and rep.q_min == {"BTC-USDT-PERP": None}
    assert rep.vetoes == 2 and rep.transitions == ("10:15 NORMAL→WARNING",)
    assert rep.equity == Decimal("10012.5") and rep.drawdown == pytest.approx(0.004)
    assert rep.sent is not None and rep.sent.status is SendStatus.SENT
    text = httpx.Response(200, content=route.calls[0].request.content).json()["text"]
    assert text.startswith("Щоденний звіт FuzzHelm за 2025-09-18 (UTC).")
    assert "Відхилень ризик-контуром (VETO): 2." in text and "10:15 NORMAL→WARNING" in text


# ------------------------------------------------------------------ розклад


def test_build_scheduler_registers_three_utc_cron_jobs() -> None:
    ctx = make_ctx(MemoryDb(), ManualClock(T0_NS))
    sched = build_scheduler(ctx)
    jobs = {j.id: j for j in sched.get_jobs()}
    assert set(jobs) == {JOB_GAP_BACKFILL, JOB_HOURLY_DQ, JOB_DAILY_REPORT}
    now = datetime(2026, 9, 18, 13, 7, tzinfo=UTC)
    nxt = {k: j.trigger.get_next_fire_time(None, now) for k, j in jobs.items()}
    assert nxt[JOB_HOURLY_DQ] == datetime(2026, 9, 18, 14, 5, tzinfo=UTC)
    assert nxt[JOB_GAP_BACKFILL] == datetime(2026, 9, 19, 2, 30, tzinfo=UTC)
    assert nxt[JOB_DAILY_REPORT] == datetime(2026, 9, 19, 0, 10, tzinfo=UTC)
    assert all(j.max_instances == 1 and j.coalesce for j in jobs.values())
    assert not sched.running  # тест нічого не запускає


async def test_scheduled_callables_use_injected_clock() -> None:
    db = MemoryDb()
    clock = ManualClock(T0_NS + NS_PER_HOUR + 5 * NS_PER_MIN)
    ctx = make_ctx(db, clock)
    sched = build_scheduler(ctx)
    job = next(j for j in sched.get_jobs() if j.id == JOB_HOURLY_DQ)
    await job.func()  # те, що викличе APScheduler
    assert ctx.runs[JOB_HOURLY_DQ]["hour_start_ns"] == T0_NS
    assert await retry_gaps(replace(ctx, backfill=None)) == []  # без добирача — нічого не робить


async def test_serve_runs_scheduler_until_stop_event() -> None:
    """serve() реєструє три задачі, запускає AsyncIOScheduler і зупиняє його за подією (без очікування часу:
    старт фіксує слухач EVENT_SCHEDULER_STARTED, зупинку — stop.set())."""
    ctx = make_ctx(MemoryDb(), ManualClock(T0_NS))
    stop, started = asyncio.Event(), asyncio.Event()
    sched = AsyncIOScheduler(timezone=UTC)
    sched.add_listener(lambda _ev: started.set(), EVENT_SCHEDULER_STARTED)
    task = asyncio.create_task(serve(ctx, stop, sched))
    await asyncio.wait_for(started.wait(), timeout=5)
    assert sched.running
    assert {j.id for j in sched.get_jobs()} == {JOB_GAP_BACKFILL, JOB_HOURLY_DQ, JOB_DAILY_REPORT}
    stop.set()
    await asyncio.wait_for(task, timeout=5)
    assert not sched.running
