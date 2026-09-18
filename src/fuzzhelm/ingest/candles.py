"""Агрегатор свічок: потік оновлень kline (x=false … x=true) → закриті 1m-свічки рівно раз на open_time.

Найменування: ingest/candles.py
Призначення: споживач (ознаки, детектори, БД) бачить кожну закриту свічку ОДИН раз і в порядку
зростання open_time, хоч би що робив транспорт: дублікати кадрів, перестановки, повтори після
реконекту, пізнє закриття, REST-добір прогалини.
Автор: Андрій Жук, 2026.

Інваріанти (перевіряються тестами і e2e-сценаріями):
  * випуск ідемпотентний: друга закрита версія того ж open_time не випускається (лічильник
    duplicates; якщо значення OHLCV відрізняються — ще й conflicts, перша версія лишається);
  * у режимі ordered=True випуск строго зростає і не має «дірок»: свічка m+1 чекає (held), доки
    не випущено m або m не оголошено недобірною (skip) — індикаторам не можна подавати ряд з дірою;
  * незакриті оновлення лише оновлюють `current` (знімок поточного бару для панелі), старіше
    оновлення (менший ts_event) новіше не перезаписує.
Перша очікувана хвилина — open_time першого побаченого оновлення (сесія почалась усередині хвилини:
її закриття x=true однаково містить повну хвилину).
"""

from __future__ import annotations

from dataclasses import dataclass

from fuzzhelm.core.dto import Candle
from fuzzhelm.ingest.normalize import NS_PER_MS, tf_ms

_VALUE_FIELDS = ("o", "h", "l", "c", "volume", "quote_volume", "trades_count")


def same_values(a: Candle, b: Candle) -> bool:
    return all(getattr(a, f) == getattr(b, f) for f in _VALUE_FIELDS)


@dataclass
class AggregatorStats:
    released: int = 0
    duplicates: int = 0          # повторне закриття вже випущеної / пропущеної хвилини
    conflicts: int = 0           # … з іншими значеннями OHLCV
    skipped: int = 0             # хвилини, оголошені недобірними
    late_after_skip: int = 0     # підмножина duplicates: закриття хвилини, УЖЕ оголошеної недобірною
    stale_updates: int = 0       # незакрите оновлення вже закритої хвилини
    max_held: int = 0


class CandleAggregator:
    def __init__(self, tf: str = "1m", *, instrument: str | None = None, ordered: bool = True,
                 start_open_ns: int | None = None, retention: int = 100_000) -> None:
        self.tf = tf
        self.instrument = instrument
        self.ordered = ordered
        self._tf_ns = tf_ms(tf) * NS_PER_MS
        self._next: int | None = start_open_ns          # наступний open_time до випуску (ordered)
        self._held: dict[int, Candle] = {}              # закриті, що чекають попередніх
        self._released: dict[int, Candle] = {}          # випущені (для перевірки дублікатів/конфліктів)
        # пам'ять live-процесу обмежена: старші за retention випущені хвилини забуваються; у режимі
        # ordered їхні повтори однаково відсікає перевірка o < next_open_ns
        self.retention = retention
        self._skipped: set[int] = set()
        self.current: Candle | None = None
        self.stats = AggregatorStats()

    @property
    def next_open_ns(self) -> int | None:
        return self._next

    @property
    def held(self) -> int:
        return len(self._held)

    def released_opens(self) -> list[int]:
        return sorted(self._released)

    def is_skipped(self, open_ns: int) -> bool:
        """Хвилину оголошено недобірною (skip) і не випущено: її пізнє закриття вже не буде випущене."""
        return open_ns in self._skipped and open_ns not in self._released

    def _accepts(self, c: Candle) -> bool:
        return c.tf == self.tf and (self.instrument is None or c.instrument == self.instrument)

    def _track_current(self, c: Candle) -> None:
        cur = self.current
        same_minute_newer = (c.open_time_ns == cur.open_time_ns and c.ts_event_ns >= cur.ts_event_ns
                             and not cur.is_closed) if cur is not None else False
        if cur is None or c.open_time_ns > cur.open_time_ns or same_minute_newer:
            self.current = c

    def offer(self, c: Candle) -> list[Candle]:
        """Прийняти оновлення; повертає свічки, випущені внаслідок цього (у порядку open_time)."""
        if not self._accepts(c):
            return []
        o = c.open_time_ns
        if self.ordered and self._next is None:
            self._next = o
        if not c.is_closed:
            if o in self._released or o in self._skipped:
                self.stats.stale_updates += 1
            else:
                self._track_current(c)
            return []
        self._track_current(c)
        prior = self._released.get(o)
        if prior is not None or o in self._skipped or o in self._held:
            self.stats.duplicates += 1
            if prior is None and o in self._skipped:
                self.stats.late_after_skip += 1
            other = prior if prior is not None else self._held.get(o)
            if other is not None and not same_values(other, c):
                self.stats.conflicts += 1
            return []
        if not self.ordered:
            return self._emit([c])
        assert self._next is not None
        if o < self._next:
            # закриття хвилини, що старша за початок сесії агрегатора: не очікувалась
            self.stats.duplicates += 1
            return []
        self._held[o] = c
        self.stats.max_held = max(self.stats.max_held, len(self._held))
        return self._drain()

    def _drain(self) -> list[Candle]:
        out: list[Candle] = []
        assert self._next is not None
        while True:
            nxt = self._next
            if nxt in self._held:
                out.append(self._held.pop(nxt))
            elif nxt not in self._skipped:
                break
            self._next = nxt + self._tf_ns
        return self._emit(out)

    def _emit(self, cs: list[Candle]) -> list[Candle]:
        for c in cs:
            self._released[c.open_time_ns] = c
        while len(self._released) > self.retention:
            del self._released[next(iter(self._released))]     # найстаріший за порядком випуску
        self.stats.released += len(cs)
        return cs

    def skip(self, lo_open_ns: int, hi_open_ns: int) -> list[Candle]:
        """Оголосити хвилини [lo, hi] недобірними (REST теж їх не має) — розблокувати випуск наступних."""
        for m in range(lo_open_ns, hi_open_ns + 1, self._tf_ns):
            if m not in self._released and m not in self._held:
                self._skipped.add(m)
                self.stats.skipped += 1
        return self._drain() if self.ordered and self._next is not None else []

    def flush(self) -> list[Candle]:
        """Кінець потоку: випустити все утримане в порядку open_time, пропускаючи дірки."""
        if not self._held:
            return []
        out = [self._held.pop(k) for k in sorted(self._held)]
        self._next = out[-1].open_time_ns + self._tf_ns
        return self._emit(out)
