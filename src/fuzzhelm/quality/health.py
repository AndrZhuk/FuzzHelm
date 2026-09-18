"""Стан здоров'я конвеєра інжесту: кадри, лаг, реконекти, прогалини, Q.

Найменування: quality/health.py
Призначення: один об'єкт, у який конвеєр (live або реплей) пише лічильники, а API/панель/ризик-контур
(StaleDataGuard: лаг і Q) читають знімок. Час — лише ts_ingest_ns/ts_event_ns подій, без настінного годинника.
Автор: Андрій Жук, 2026.

Лаг події = ts_ingest_ns − ts_event_ns (мс). Від'ємний лаг (локальний годинник відстає від біржового)
зберігається як є у мінімумі, але в p95 входить як 0 — інакше зсув годинника «покращував» би timeliness.
"""

from __future__ import annotations

from collections import Counter, deque
from dataclasses import dataclass, field
from typing import Final

from fuzzhelm.quality.dq_score import p95

DEFAULT_LAG_WINDOW: Final = 4096      # останні N подій для ковзного p95 лагу


@dataclass(frozen=True, slots=True)
class HealthSnapshot:
    frames: int
    frames_by_conn: dict[str, int]
    events_by_kind: dict[str, int]
    invalid: int
    invalid_by_code: dict[str, int]
    duplicates: int
    stale: int
    reconnects: int
    watchdog_fires: int
    disconnects_by_class: dict[str, int]
    gaps_opened: int
    gaps_open: int
    gaps_by_status: dict[str, int]
    candles_closed: int
    anomalies: int
    lag_p95_ms: float | None
    lag_max_ms: float | None
    last_ingest_ns: int | None
    last_event_ns: int | None
    q: float | None

    def as_dict(self) -> dict[str, object]:
        return {k: getattr(self, k) for k in self.__slots__}


@dataclass
class PipelineHealth:
    lag_window: int = DEFAULT_LAG_WINDOW
    frames: int = 0
    frames_by_conn: Counter[str] = field(default_factory=Counter)
    events_by_kind: Counter[str] = field(default_factory=Counter)
    invalid: int = 0
    invalid_by_code: Counter[str] = field(default_factory=Counter)
    duplicates: int = 0
    stale: int = 0
    reconnects: int = 0
    watchdog_fires: int = 0
    disconnects_by_class: Counter[str] = field(default_factory=Counter)
    gaps_opened: int = 0
    gap_status: dict[int, str] = field(default_factory=dict)      # gap_id → поточний статус
    candles_closed: int = 0
    anomalies: int = 0
    q: float | None = None
    last_ingest_ns: int | None = None
    last_event_ns: int | None = None
    lag_max_ms: float | None = None
    _lags: deque[float] = field(default_factory=deque)

    def __post_init__(self) -> None:
        self._lags = deque(maxlen=self.lag_window)

    # ------------------------------------------------------------ запис

    def on_frame(self, conn: str, ts_ingest_ns: int) -> None:
        self.frames += 1
        self.frames_by_conn[conn] += 1
        self.last_ingest_ns = ts_ingest_ns

    def on_event(self, kind: str, ts_event_ns: int, ts_ingest_ns: int) -> float:
        """Облік прийнятої події; повертає її лаг у мс."""
        self.events_by_kind[kind] += 1
        if self.last_event_ns is None or ts_event_ns > self.last_event_ns:
            self.last_event_ns = ts_event_ns
        lag = (ts_ingest_ns - ts_event_ns) / 1e6
        self._lags.append(max(0.0, lag))
        if self.lag_max_ms is None or lag > self.lag_max_ms:
            self.lag_max_ms = lag
        return lag

    def on_invalid(self, code: str) -> None:
        self.invalid += 1
        self.invalid_by_code[code] += 1

    def on_duplicate(self) -> None:
        self.duplicates += 1

    def on_stale(self) -> None:
        self.stale += 1

    def on_disconnect(self, cls: str, *, watchdog: bool = False) -> None:
        self.reconnects += 1
        self.disconnects_by_class[cls] += 1
        if watchdog:
            self.watchdog_fires += 1

    def on_gap(self, gap_id: int, status: str) -> None:
        if gap_id not in self.gap_status:
            self.gaps_opened += 1
        self.gap_status[gap_id] = status

    def on_candle_closed(self, *, anomaly: bool = False) -> None:
        self.candles_closed += 1
        self.anomalies += int(anomaly)

    # ------------------------------------------------------------ читання

    @property
    def lag_p95_ms(self) -> float | None:
        return p95(self._lags) if self._lags else None

    @property
    def open_gaps(self) -> int:
        return sum(1 for s in self.gap_status.values() if s in ("OPEN", "FILLING"))

    def snapshot(self) -> HealthSnapshot:
        return HealthSnapshot(
            frames=self.frames, frames_by_conn=dict(self.frames_by_conn),
            events_by_kind=dict(self.events_by_kind), invalid=self.invalid,
            invalid_by_code=dict(self.invalid_by_code), duplicates=self.duplicates, stale=self.stale,
            reconnects=self.reconnects, watchdog_fires=self.watchdog_fires,
            disconnects_by_class=dict(self.disconnects_by_class), gaps_opened=self.gaps_opened,
            gaps_open=self.open_gaps, gaps_by_status=dict(Counter(self.gap_status.values())),
            candles_closed=self.candles_closed, anomalies=self.anomalies, lag_p95_ms=self.lag_p95_ms,
            lag_max_ms=self.lag_max_ms, last_ingest_ns=self.last_ingest_ns,
            last_event_ns=self.last_event_ns, q=self.q,
        )
