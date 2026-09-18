"""Добір історії klines (backfill): пагінація з перекриттям в один бар, детекція і добір прогалин.

Найменування: ingest/backfill.py
Призначення: «REST … для збору»: N днів 1m-свічок сторінками ≤ 1500 барів; стик сторінок перевіряється,
а не приймається на віру; прогалини (за неперервністю openTime) повертаються як Gap і доповнюються
ідемпотентно (повторний добір не додає жодного рядка — дедуплікація за event_uid).
Автор: Андрій Жук, 2026.

Перекриття: наступна сторінка запитується з startTime = openTime останнього бару попередньої, тому
її перший бар мусить ДОСЛІВНО збігтися з останнім баром попередньої:
  * збіг          → стик OK (доказ, що між сторінками нічого не загубилось і дані не переписані);
  * інший вміст   → SeamStatus.MISMATCH + OverlapMismatch (зберігається новіша версія);
  * бару немає    → SeamStatus.MISSING, а пропущені бари потрапляють у gaps.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any, Final, Protocol

from fuzzhelm.core.dto import Candle
from fuzzhelm.core.enums import GapDetectorKind, GapStatus, Stream, Venue
from fuzzhelm.core.errors import NormalizationError
from fuzzhelm.core.ports import Clock
from fuzzhelm.ingest.dedup import Deduplicator, DedupOutcome
from fuzzhelm.ingest.normalize import NS_PER_MS, InstrumentLike, normalize_rest_klines, tf_ms
from fuzzhelm.ingest.symbols import symbol_ref

MAX_LIMIT: Final = 1500
# Запас на зсув локального годинника, коли час біржі не передано (власний консервативний вибір, не замір):
# бар, що закрився за останні CLOCK_GUARD_MS, просто перейде в наступний запуск.
DEFAULT_CLOCK_GUARD_MS: Final = 5_000


class KlineSource(Protocol):
    """Будь-що з методом klines як у BinanceRestClient (у тестах — він же поверх respx)."""

    clock: Clock

    async def klines(self, symbol: str, interval: str = ..., start_ms: int | None = ...,
                     end_ms: int | None = ..., limit: int = ...) -> list[list[Any]]: ...


class SeamStatus(StrEnum):
    OK = "OK"
    MISMATCH = "MISMATCH"
    MISSING = "MISSING"


@dataclass(frozen=True, slots=True)
class Segment:
    index: int
    start_ms: int
    limit: int
    rows: int
    first_open_ms: int | None
    last_open_ms: int | None
    seam: SeamStatus | None          # None для першої сторінки


@dataclass(frozen=True, slots=True)
class OverlapMismatch:
    open_time_ms: int
    segment: int
    previous: tuple[Any, ...]
    current: tuple[Any, ...]


@dataclass(frozen=True, slots=True)
class Gap:
    """Прогалина свічок: [ts_lo_ns, ts_hi_ns] — openTime першого і останнього відсутніх барів (включно).
    Поля відповідають таблиці ingest_gap."""

    instrument: str
    tf: str
    ts_lo_ns: int
    ts_hi_ns: int
    expected_count: int
    stream: Stream = Stream.KLINES
    detector: GapDetectorKind = GapDetectorKind.TIME
    status: GapStatus = GapStatus.OPEN
    filled_rows: int = 0
    attempts: int = 0


@dataclass(frozen=True, slots=True)
class BackfillResult:
    candles: tuple[Candle, ...]              # закриті, унікальні, за зростанням open_time
    gaps: tuple[Gap, ...]
    segments: tuple[Segment, ...]
    overlap_mismatches: tuple[OverlapMismatch, ...]
    requests: int

    @property
    def ok(self) -> bool:
        return not self.gaps and not self.overlap_mismatches and all(
            s.seam in (None, SeamStatus.OK) for s in self.segments)


@dataclass(frozen=True, slots=True)
class GapFillReport:
    candles: tuple[Candle, ...]              # об'єднаний ряд після добору (унікальний, за open_time)
    gaps: tuple[Gap, ...]                    # ті самі прогалини з оновленими status/filled_rows/attempts
    added: tuple[Candle, ...]                # що реально додалося (NEW або REPLACED)
    requests: int


def _ceil_to(x: int, step: int) -> int:
    return -(-x // step) * step


def _floor_to(x: int, step: int) -> int:
    return (x // step) * step


def detect_gaps(candles: Sequence[Candle], tf: str, *, instrument: str | None = None,
                expected_from_ns: int | None = None, expected_to_ns: int | None = None) -> list[Gap]:
    """Прогалини за неперервністю open_time: сусідні бари мусять відрізнятися рівно на tf.
    expected_from/to (open_time першого/останнього очікуваного бару) додають крайові прогалини."""
    iv_ns = tf_ms(tf) * NS_PER_MS
    opens = sorted({c.open_time_ns for c in candles if c.tf == tf})
    name = instrument or (candles[0].instrument if candles else "")
    if not name:
        raise ValueError("instrument is required when candles are empty")
    gaps: list[Gap] = []

    def add(lo: int, hi: int) -> None:
        gaps.append(Gap(name, tf, lo, hi, (hi - lo) // iv_ns + 1))

    prev = expected_from_ns - iv_ns if expected_from_ns is not None else None
    for o in opens:
        if prev is not None:
            if (o - prev) % iv_ns:
                raise ValueError(f"open_time {o} is not aligned to {tf} grid")
            if o - prev > iv_ns:
                add(prev + iv_ns, o - iv_ns)
        prev = o
    if expected_to_ns is not None:
        if prev is None:
            if expected_from_ns is not None and expected_to_ns >= expected_from_ns:
                add(expected_from_ns, expected_to_ns)
        elif expected_to_ns > prev:
            add(prev + iv_ns, expected_to_ns)
    return gaps


def _row_open(row: Any, i: int) -> int:
    if not isinstance(row, list) or not row or type(row[0]) is not int:
        raise NormalizationError(f"kline row {i} has no integer openTime", field=f"[{i}][0]",
                                 venue="BINANCE_USDM")
    return row[0]


async def backfill_klines(client: KlineSource, symbol: str, start_ms: int, end_ms: int, *,
                          interval: str = "1m", limit: int = MAX_LIMIT,
                          instrument: InstrumentLike | None = None, now_ms: int | None = None,
                          on_page: Callable[[list[Candle]], Awaitable[None]] | None = None,
                          clock_guard_ms: int = DEFAULT_CLOCK_GUARD_MS) -> BackfillResult:
    """Закриті бари з openTime ∈ [start_ms, end_ms]; `now_ms` (час біржі) визначає, які бари вже закриті.

    Без `now_ms` береться локальний `client.clock` мінус `clock_guard_ms`: якщо локальний годинник
    випереджає біржу, ще відкритий бар позначився б закритим, а такий бар upsert (`WHERE is_closed = FALSE`)
    уже ніколи не виправить. Точний шлях — `now_ms = local_ms + await client.server_time_offset_ms()`.
    """
    if end_ms < start_ms:
        raise ValueError("end_ms < start_ms")
    if not 2 <= limit <= MAX_LIMIT:
        raise ValueError(f"limit must be in [2, {MAX_LIMIT}] (one bar of every page is the overlap)")
    if clock_guard_ms < 0:
        raise ValueError("clock_guard_ms must be >= 0")
    iv = tf_ms(interval)
    ref = instrument if instrument is not None else symbol_ref(Venue.BINANCE_USDM, symbol)
    if now_ms is None:
        now_ms = client.clock.now_ns() // NS_PER_MS - clock_guard_ms
    first_expected = _ceil_to(start_ms, iv)
    last_expected = min(_floor_to(end_ms, iv), _floor_to(now_ms, iv) - iv)   # останній ЗАКРИТИЙ бар

    by_open: dict[int, Candle] = {}
    segments: list[Segment] = []
    mismatches: list[OverlapMismatch] = []
    prev_last: list[Any] | None = None
    cursor = start_ms
    requests = 0
    while True:
        page = await client.klines(symbol, interval, start_ms=cursor, end_ms=end_ms, limit=limit)
        requests += 1
        ts_ingest = client.clock.now_ns()
        opens = [_row_open(r, i) for i, r in enumerate(page)]
        seam: SeamStatus | None = None
        new_rows = page
        if prev_last is not None:
            if page and opens[0] == prev_last[0]:
                seam = SeamStatus.OK if page[0] == prev_last else SeamStatus.MISMATCH
                if seam is SeamStatus.MISMATCH:
                    mismatches.append(OverlapMismatch(opens[0], len(segments), tuple(prev_last),
                                                      tuple(page[0])))
                # при MISMATCH рядок стику лишається у new_rows: новіша версія заміщує стару
                new_rows = page if seam is SeamStatus.MISMATCH else page[1:]
            else:
                seam = SeamStatus.MISSING
        segments.append(Segment(len(segments), cursor, limit, len(page), opens[0] if opens else None,
                                opens[-1] if opens else None, seam))
        candles = normalize_rest_klines(new_rows, ref, ts_ingest, tf=interval, server_time_ms=now_ms)
        fresh = [c for c in candles if c.is_closed and first_expected * NS_PER_MS <= c.open_time_ns]
        for c in fresh:
            by_open[c.open_time_ns] = c
        if on_page is not None and fresh:
            await on_page(fresh)
        if not page:
            break
        last = page[-1]
        progressed = prev_last is None or opens[-1] > prev_last[0]
        if len(page) < limit or opens[-1] >= last_expected or not progressed:
            break
        prev_last = last
        cursor = opens[-1]                                    # перекриття: рівно один бар

    series = tuple(by_open[k] for k in sorted(by_open))
    gaps = detect_gaps(series, interval, instrument=ref.symbol_canon,
                       expected_from_ns=first_expected * NS_PER_MS,
                       expected_to_ns=last_expected * NS_PER_MS) if last_expected >= first_expected else []
    return BackfillResult(series, tuple(gaps), tuple(segments), tuple(mismatches), requests)


async def fill_gaps(client: KlineSource, symbol: str, candles: Sequence[Candle], gaps: Sequence[Gap], *,
                    dedup: Deduplicator | None = None, instrument: InstrumentLike | None = None,
                    interval: str = "1m", limit: int = MAX_LIMIT, now_ms: int | None = None) -> GapFillReport:
    """Добір прогалин через REST. Ідемпотентно: уже відомі event_uid відкидає Deduplicator, тож
    повторний виклик з тими самими прогалинами повертає added = () і той самий ряд."""
    ref = instrument if instrument is not None else symbol_ref(Venue.BINANCE_USDM, symbol)
    d = dedup if dedup is not None else Deduplicator()
    for c in candles:
        d.offer(c)
    added: list[Candle] = []
    updated: list[Gap] = []
    requests = 0
    for g in gaps:
        res = await backfill_klines(client, symbol, g.ts_lo_ns // NS_PER_MS, g.ts_hi_ns // NS_PER_MS,
                                    interval=interval, limit=limit, instrument=ref, now_ms=now_ms)
        requests += res.requests
        in_range = [c for c in res.candles if g.ts_lo_ns <= c.open_time_ns <= g.ts_hi_ns]
        added.extend(c for c in in_range if d.offer(c) is not DedupOutcome.DUPLICATE)
        n = len(in_range)
        if n == g.expected_count:
            status = GapStatus.FILLED
        else:
            status = GapStatus.PARTIAL if n else GapStatus.UNFILLABLE
        updated.append(replace(g, status=status, filled_rows=n, attempts=g.attempts + 1))
    merged = sorted((e for e in d.values()
                     if isinstance(e, Candle) and e.instrument == ref.symbol_canon and e.tf == interval),
                    key=lambda c: c.open_time_ns)
    return GapFillReport(tuple(merged), tuple(updated), tuple(added), requests)
