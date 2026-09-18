"""Детекція прогалин WS-потоку: за послідовністю aggTrade id (seq) і за часом закриття свічок (time).

Найменування: ingest/gap_detector.py
Призначення: «розрив → прогалина → REST-добір». Кожна виявлена прогалина — запис у формі таблиці
ingest_gap зі статусом OPEN → FILLING → FILLED | PARTIAL | UNFILLABLE; заповнення рахується
поштучно (id угоди / open_time свічки), незалежно від того, чи прийшла подія пізно з потоку, чи з REST.
Автор: Андрій Жук, 2026.

seq (угоди): aggTrade id біржі йдуть без пропусків (перевірено: 3 887 угод 4-хв зразка — 0 дірок).
  Стрибок a > max+1 відкриває КАНДИДАТА [max+1, a−1]. Локальна перестановка кадрів дає маленькі
  кандидати, що «заростають» наступними кадрами, тому кандидат підтверджується, лише якщо
  (а) він ширший за max_reorder_ids — перестановка сусідніх кадрів такого не дає, або
  (б) біржовий час пішов далі за момент появи кандидата на grace_ms, або (в) flush() (реконект, кінець).
time (свічки): Binance закриває кожну хвилину кадром x=true (E − T: 9–21 мс на 4-хв зразку, 7–443 мс на
  45-хв сесії — grace 2 с з запасом). Хвилина m, для якої
  біржовий час (максимум ts_event усіх потоків) перевищив m + tf + grace, а закритої свічки немає, —
  втрачена. Біржовий час береться і з інших потоків (markPrice, depth), тому втрату свічок видно ще
  до відновлення потоку kline. Хвилини до першого побаченого оновлення не очікуються (початок сесії).
bucket_count (Σ(l−f+1) угод хвилини = k.n) не реалізовано: на реальних даних рівність не точна
  (див. docs/deviations.d/ingest_ws.md), тож такий детектор давав би хибні прогалини.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field, replace
from typing import Any, Final

from fuzzhelm.core.dto import Candle, Trade
from fuzzhelm.core.enums import GapDetectorKind, GapStatus, Stream
from fuzzhelm.core.ports import Clock
from fuzzhelm.ingest.backfill import Gap
from fuzzhelm.ingest.normalize import NS_PER_MS, tf_ms

DEFAULT_GRACE_MS: Final = 2_000
DEFAULT_MAX_REORDER_IDS: Final = 50

# Дозволені переходи статусу прогалини (решта — помилка програміста)
TRANSITIONS: Final[dict[GapStatus, frozenset[GapStatus]]] = {
    GapStatus.OPEN: frozenset({GapStatus.FILLING, GapStatus.FILLED, GapStatus.UNFILLABLE}),
    GapStatus.FILLING: frozenset({GapStatus.FILLED, GapStatus.PARTIAL, GapStatus.UNFILLABLE}),
    GapStatus.PARTIAL: frozenset({GapStatus.FILLING, GapStatus.FILLED, GapStatus.UNFILLABLE}),
    GapStatus.UNFILLABLE: frozenset({GapStatus.FILLING, GapStatus.FILLED}),
    GapStatus.FILLED: frozenset(),
}
TERMINAL: Final = frozenset({GapStatus.FILLED, GapStatus.PARTIAL, GapStatus.UNFILLABLE})


@dataclass(frozen=True, slots=True)
class GapRecord:
    """Прогалина у формі рядка ingest_gap (+ seq-межі для угод).

    klines: ts_lo/ts_hi — open_time першої/останньої відсутньої свічки (включно, як backfill.Gap);
    trades: ts_lo/ts_hi — час угоди перед діркою і першої після неї (відсутні угоди — між ними),
            seq_lo/seq_hi — межі відсутніх aggTrade id (включно).
    """

    gap_id: int
    instrument: str
    stream: Stream
    detector: GapDetectorKind
    ts_lo_ns: int
    ts_hi_ns: int
    expected_count: int
    status: GapStatus = GapStatus.OPEN
    filled_rows: int = 0
    attempts: int = 0
    seq_lo: int | None = None
    seq_hi: int | None = None
    tf: str | None = None
    detected_at_ns: int = 0              # час виявлення за годинником конвеєра (ts_ingest-домен)
    closed_at_ns: int | None = None
    exchange_ns: int | None = None       # біржовий час (макс. ts_event потоку) на момент виявлення

    def to_row(self) -> dict[str, Any]:
        """Колонки таблиці ingest_gap (instrument_id підставляє репозиторій)."""
        return {"stream": self.stream.value, "ts_lo_ns": self.ts_lo_ns, "ts_hi_ns": self.ts_hi_ns,
                "expected_count": self.expected_count, "filled_rows": self.filled_rows,
                "detector": self.detector.value, "status": self.status.value, "attempts": self.attempts,
                "detected_at_ns": self.detected_at_ns, "closed_at_ns": self.closed_at_ns}

    def to_backfill_gap(self) -> Gap:
        """Прогалина свічок → backfill.Gap (для backfill.fill_gaps)."""
        if self.stream is not Stream.KLINES or self.tf is None:
            raise ValueError("only kline gaps convert to backfill.Gap")
        return Gap(self.instrument, self.tf, self.ts_lo_ns, self.ts_hi_ns, self.expected_count,
                   status=self.status, filled_rows=self.filled_rows, attempts=self.attempts)


def transition(g: GapRecord, status: GapStatus, *, at_ns: int, filled_rows: int | None = None,
               count_attempt: bool = False) -> GapRecord:
    if status is not g.status and status not in TRANSITIONS[g.status]:
        raise ValueError(f"gap {g.gap_id}: illegal transition {g.status} -> {status}")
    return replace(g, status=status,
                   filled_rows=g.filled_rows if filled_rows is None else filled_rows,
                   attempts=g.attempts + int(count_attempt),
                   closed_at_ns=at_ns if status in TERMINAL else None)


@dataclass
class _Hole:
    missing: set[int]
    t_before_ns: int
    t_after_ns: int
    created_event_ns: int


@dataclass
class GapStats:
    healed_holes: int = 0          # кандидати в дірки, що заросли пізніми кадрами (перестановка)
    late_fills: int = 0            # події, що заповнили вже підтверджену прогалину
    duplicate_trades: int = 0      # id ≤ max, не в жодній дірці (повтор кадру)
    stale_klines: int = 0          # оновлення вже врегульованої хвилини (не закриття прогалини)
    kline_minutes_settled: int = 0


@dataclass
class GapDetector:
    instrument: str
    clock: Clock
    tf: str = "1m"
    grace_ms: int = DEFAULT_GRACE_MS
    max_reorder_ids: int = DEFAULT_MAX_REORDER_IDS
    stats: GapStats = field(default_factory=GapStats)

    def __post_init__(self) -> None:
        self._tf_ns = tf_ms(self.tf) * NS_PER_MS
        self._grace_ns = self.grace_ms * NS_PER_MS
        self._latest_ns: int | None = None             # біржовий час: максимум ts_event
        self._k_lo: int | None = None                  # найменша ще не врегульована хвилина
        self._k_closed: set[int] = set()               # закриті хвилини ≥ _k_lo
        self._t_max: int | None = None
        self._t_max_ts: int = 0
        self._pending: list[_Hole] = []
        self._missing: dict[int, set[int]] = {}        # gap_id → відсутні ключі
        self._owner: dict[tuple[Stream, int], int] = {}  # (потік, ключ) → gap_id
        self.gaps: dict[int, GapRecord] = {}
        self._next_id = 1

    # ------------------------------------------------------------ спільне

    @property
    def latest_event_ns(self) -> int | None:
        return self._latest_ns

    def _advance(self, ts_event_ns: int) -> None:
        if self._latest_ns is None or ts_event_ns > self._latest_ns:
            self._latest_ns = ts_event_ns

    def _open(self, stream: Stream, detector: GapDetectorKind, keys: set[int], ts_lo: int, ts_hi: int,
              **kw: Any) -> GapRecord:
        gid = self._next_id
        self._next_id += 1
        g = GapRecord(gid, self.instrument, stream, detector, ts_lo, ts_hi, len(keys),
                      detected_at_ns=self.clock.now_ns(), exchange_ns=self._latest_ns, **kw)
        self.gaps[gid] = g
        self._missing[gid] = set(keys)
        for k in keys:
            self._owner[(stream, k)] = gid
        return g

    def _fill(self, stream: Stream, key: int, changed: list[GapRecord]) -> bool:
        """Ключ прийшов (з потоку чи REST): False, якщо він не належить жодній прогалині. Якщо
        відкрита/часткова прогалина заросла повністю — FILLED (додається в `changed`)."""
        gid = self._owner.pop((stream, key), None)
        if gid is None:
            return False
        self.stats.late_fills += 1
        miss = self._missing[gid]
        miss.discard(key)
        g = self.gaps[gid]
        filled = g.expected_count - len(miss)
        if not miss and g.status is not GapStatus.FILLING:
            g = transition(g, GapStatus.FILLED, at_ns=self.clock.now_ns(), filled_rows=filled)
            self.gaps[gid] = g
            changed.append(g)
        elif g.status is GapStatus.FILLING:
            self.gaps[gid] = replace(g, filled_rows=filled)
        return True

    def on_time(self, ts_event_ns: int) -> list[GapRecord]:
        """Біржовий час з будь-якого потоку (markPrice, depth): рухає обидва детектори."""
        self._advance(ts_event_ns)
        return self._sweep_klines() + self._confirm_holes(force=False)

    # ------------------------------------------------------------ свічки (time)

    def on_kline(self, c: Candle) -> list[GapRecord]:
        if c.tf != self.tf or c.instrument != self.instrument:
            return []
        self._advance(c.ts_event_ns)
        o = c.open_time_ns
        changed: list[GapRecord] = []
        if self._k_lo is None:
            self._k_lo = o
        if o < self._k_lo:
            if not (c.is_closed and self._fill(Stream.KLINES, o, changed)):
                self.stats.stale_klines += 1
        elif c.is_closed:
            self._k_closed.add(o)
        return changed + self._sweep_klines() + self._confirm_holes(force=False)

    def _sweep_klines(self) -> list[GapRecord]:
        if self._k_lo is None or self._latest_ns is None:
            return []
        out: list[GapRecord] = []
        run: list[int] = []
        tf, horizon = self._tf_ns, self._latest_ns - self._grace_ns
        while self._k_lo + tf <= horizon:          # хвилина закрилась на біржі щонайменше grace тому
            m = self._k_lo
            if m in self._k_closed:
                self._k_closed.discard(m)
                if run:
                    out.append(self._open_klines(run))
                    run = []
            else:
                run.append(m)
            self._k_lo = m + tf
            self.stats.kline_minutes_settled += 1
        if run:
            out.append(self._open_klines(run))
        return out

    def _open_klines(self, run: list[int]) -> GapRecord:
        return self._open(Stream.KLINES, GapDetectorKind.TIME, set(run), run[0], run[-1], tf=self.tf)

    # ------------------------------------------------------------ угоди (seq)

    def on_trade(self, t: Trade) -> list[GapRecord]:
        if t.instrument != self.instrument:
            return []
        self._advance(t.ts_event_ns)
        a = t.agg_id
        changed: list[GapRecord] = []
        if self._t_max is None:
            self._t_max, self._t_max_ts = a, t.ts_event_ns
        elif a > self._t_max:
            if a > self._t_max + 1:
                self._pending.append(_Hole(set(range(self._t_max + 1, a)), self._t_max_ts, t.ts_event_ns,
                                           self._latest_ns or t.ts_event_ns))
            self._t_max, self._t_max_ts = a, t.ts_event_ns
        elif a < self._t_max:
            hole = next((h for h in self._pending if a in h.missing), None)
            if hole is not None:
                hole.missing.discard(a)
                if not hole.missing:
                    self._pending.remove(hole)
                    self.stats.healed_holes += 1
            elif not self._fill(Stream.TRADES, a, changed):
                self.stats.duplicate_trades += 1
        else:
            self.stats.duplicate_trades += 1
        return changed + self._confirm_holes(force=False) + self._sweep_klines()

    def _confirm_holes(self, *, force: bool) -> list[GapRecord]:
        if not self._pending:
            return []
        out: list[GapRecord] = []
        keep: list[_Hole] = []
        latest = self._latest_ns or 0
        for h in self._pending:
            wide = len(h.missing) > self.max_reorder_ids
            if force or wide or latest - h.created_event_ns >= self._grace_ns:
                out.append(self._open(Stream.TRADES, GapDetectorKind.SEQ, h.missing, h.t_before_ns,
                                      h.t_after_ns, seq_lo=min(h.missing), seq_hi=max(h.missing)))
            else:
                keep.append(h)
        self._pending = keep
        return out

    # ------------------------------------------------------------ керування

    def flush(self) -> list[GapRecord]:
        """Підтвердити всіх кандидатів у дірки (після реконекту пізніх кадрів уже не буде)."""
        return self._confirm_holes(force=True)

    def begin_fill(self, gap_id: int) -> GapRecord:
        g = transition(self.gaps[gap_id], GapStatus.FILLING, at_ns=self.clock.now_ns(), count_attempt=True)
        self.gaps[gap_id] = g
        return g

    def resolve(self, gap_id: int) -> GapRecord:
        """Після спроби добору: FILLED (усе є), PARTIAL (частина), UNFILLABLE (нічого)."""
        g = self.gaps[gap_id]
        miss = self._missing[gap_id]
        filled = g.expected_count - len(miss)
        status = GapStatus.FILLED if not miss else GapStatus.PARTIAL if filled else GapStatus.UNFILLABLE
        g = transition(g, status, at_ns=self.clock.now_ns(), filled_rows=filled)
        self.gaps[gap_id] = g
        return g

    def missing(self, gap_id: int) -> frozenset[int]:
        return frozenset(self._missing[gap_id])

    def missing_keys(self, gap_id: int) -> Iterator[int]:
        return iter(sorted(self._missing[gap_id]))

    @property
    def open_gaps(self) -> list[GapRecord]:
        return [g for g in self.gaps.values() if g.status is not GapStatus.FILLED]

    @property
    def pending_holes(self) -> int:
        return len(self._pending)
