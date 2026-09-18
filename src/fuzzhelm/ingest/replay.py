"""Відтворення записаної WS-сесії: сирі записи, ReplayFeed (порт MarketFeed), офлайн-REST для добору.

Найменування: ingest/replay.py
Призначення: усе демо і всі тести інжесту працюють без мережі — з файлу fixtures/ws/*.jsonl.gz
(формат docs/contracts.md §7). Один і той самий конвеєр отримує кадри з живого BinanceWsClient або
звідси. Для патологічних сесій тут же — FixtureRestHandler: імітація /fapi/v1/klines і
/fapi/v1/aggTrades поверх JSON-фікстури, щоб REST-добір прогалин теж ішов офлайн.
Автор: Андрій Жук, 2026.

Порядок видачі: за замовчуванням — порядок файлу, тобто порядок НАДХОДЖЕННЯ (рекордер пише кадри в
момент отримання; у справних записах він збігається з порядком ts_ingest_ns — на 4-хв зразку 0 інверсій).
Для сесії з «стрибком годинника» порядок надходження лишається фізично правильним, а ts_ingest_ns —
ні, тому сортування за ts_ingest_ns — лише опція `order="ingest_ts"` (див. deviations.d/ingest_ws.md).

Темп: `speed=inf` — без жодного очікування (тести, бектест); скінченна швидкість — очікування через
ін'єктовані `clock` і `sleep` (у тестах — фіктивні, реального сну немає).
"""

from __future__ import annotations

import asyncio
import gzip
import math
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Literal

import httpx
import orjson

from fuzzhelm.core.dto import MarketEvent
from fuzzhelm.core.enums import Src, Venue
from fuzzhelm.core.errors import NormalizationError
from fuzzhelm.core.ports import Clock
from fuzzhelm.ingest.normalize import NS_PER_MS, InstrumentLike, normalize_binance, tf_ms
from fuzzhelm.ingest.ratelimit import klines_weight
from fuzzhelm.ingest.recorder import ControlRecord, RawFrame, SessionItem
from fuzzhelm.ingest.symbols import symbol_ref

SleepFn = Callable[[float], Awaitable[None]]
KLINES_PATH: Final = "/fapi/v1/klines"
AGG_TRADES_PATH: Final = "/fapi/v1/aggTrades"
AGG_TRADES_MAX_LIMIT: Final = 1000


# ---------------------------------------------------------------- сирий доступ


def _open_lines(path: Path) -> Iterator[bytes]:
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rb") as f:
        for line in f:
            if line.strip():
                yield line


def iter_records(path: str | Path) -> Iterator[dict[str, Any]]:
    """Усі записи файлу сесії як dict (header, frame, control, footer) у порядку файлу."""
    for line in _open_lines(Path(path)):
        rec = orjson.loads(line)
        if not isinstance(rec, dict) or rec.get("v") != 1 or "kind" not in rec:
            raise ValueError(f"{path}: not a v1 session record: {line[:80]!r}")
        yield rec


def iter_frames(path: str | Path) -> Iterator[dict[str, Any]]:
    """Лише записи kind="frame" (сирий доступ для інструментів і тестів)."""
    return (r for r in iter_records(path) if r["kind"] == "frame")


def to_item(rec: Mapping[str, Any]) -> SessionItem | None:
    """dict-запис → RawFrame | ControlRecord; header/footer → None."""
    kind = rec["kind"]
    if kind == "frame":
        return RawFrame(str(rec["conn"]), int(rec["ts_ingest_ns"]), str(rec["stream"]), rec["data"])
    if kind == "control":
        return ControlRecord(str(rec["conn"]), int(rec["ts_ingest_ns"]), str(rec["event"]),
                             str(rec.get("detail", "")))
    return None


@dataclass(frozen=True, slots=True)
class Session:
    header: dict[str, Any]
    items: tuple[SessionItem, ...]
    footer: dict[str, Any] | None

    @property
    def frames(self) -> tuple[RawFrame, ...]:
        return tuple(i for i in self.items if isinstance(i, RawFrame))


def read_session(path: str | Path) -> Session:
    header: dict[str, Any] | None = None
    footer: dict[str, Any] | None = None
    items: list[SessionItem] = []
    for rec in iter_records(path):
        if rec["kind"] == "header":
            header = rec
        elif rec["kind"] == "footer":
            footer = rec
        else:
            it = to_item(rec)
            if it is not None:
                items.append(it)
    if header is None:
        raise ValueError(f"{path}: session has no header")
    return Session(header, tuple(items), footer)


def read_header(path: str | Path) -> dict[str, Any]:
    first = next(iter_records(path), None)
    if first is None or first["kind"] != "header":
        raise ValueError(f"{path}: first record is not a header")
    return first


# ---------------------------------------------------------------- годинник реплею


class FrameClock:
    """Clock, що показує ts_ingest_ns поточного відтворюваного кадру (віртуальний «зараз» реплею).

    На відміну від core.clock.ManualClock, може йти назад — так моделюється стрибок настінного
    годинника в патологічній сесії clock_jump.
    """

    def __init__(self, t_ns: int = 0) -> None:
        self._t = t_ns
        self.regressions = 0

    def now_ns(self) -> int:
        return self._t

    def observe(self, t_ns: int) -> None:
        if t_ns < self._t:
            self.regressions += 1
        self._t = t_ns


# ---------------------------------------------------------------- ReplayFeed


@dataclass
class ReplayStats:
    frames: int = 0
    controls: int = 0
    events: int = 0
    errors: int = 0
    clock_regressions: int = 0      # кроків назад за ts_ingest_ns у порядку надходження
    waited_s: float = 0.0
    error_fields: dict[str, int] = field(default_factory=dict)


class ReplayFeed:
    """Реалізує core.ports.MarketFeed: `async for ev in ReplayFeed(path)` → нормалізовані MarketEvent."""

    def __init__(self, path: str | Path, speed: float = math.inf, clock: Clock | None = None, *,
                 sleep: SleepFn | None = None, instrument: InstrumentLike | None = None,
                 src: Src = Src.REPLAY, on_error: Literal["raise", "skip"] = "raise",
                 order: Literal["arrival", "ingest_ts"] = "arrival") -> None:
        if not speed > 0:
            raise ValueError(f"speed must be > 0 (inf = no pacing), got {speed}")
        self.path = Path(path)
        self.speed = speed
        self._clock = clock
        self._sleep: SleepFn = sleep if sleep is not None else asyncio.sleep
        self.src = src
        self.on_error = on_error
        self.order = order
        self.header = read_header(self.path)
        self.instrument: InstrumentLike = instrument if instrument is not None else symbol_ref(
            Venue(self.header.get("venue", "BINANCE_USDM")), str(self.header["symbol"]))
        self.stats = ReplayStats()

    def _clock_or_system(self) -> Clock:
        if self._clock is None:
            # настінний годинник потрібен лише для темпу «як у записі»; infra — поза межею детермінізму
            from fuzzhelm.infra.wallclock import SystemClock  # noqa: PLC0415

            self._clock = SystemClock()
        return self._clock

    def _source(self) -> Iterator[SessionItem]:
        items = (it for rec in iter_records(self.path) if (it := to_item(rec)) is not None)
        if self.order == "ingest_ts":
            return iter(sorted(items, key=lambda i: i.ts_ingest_ns))     # стабільне сортування
        return items

    async def items(self) -> AsyncIterator[SessionItem]:
        """Кадри й записи керування з темпом `speed` (inf — без очікування)."""
        paced = math.isfinite(self.speed)
        t0_rec: int | None = None
        t0_wall = 0
        env = 0                     # монотонна обвідна ts_ingest: стрибок назад не дає від'ємного очікування
        prev: int | None = None
        for it in self._source():
            ts = it.ts_ingest_ns
            if prev is not None and ts < prev:
                self.stats.clock_regressions += 1
            prev = ts
            if paced:
                clock = self._clock_or_system()
                if t0_rec is None:
                    t0_rec, t0_wall, env = ts, clock.now_ns(), ts
                env = max(env, ts)
                delay_ns = t0_wall + (env - t0_rec) / self.speed - clock.now_ns()
                if delay_ns > 0:
                    self.stats.waited_s += delay_ns / 1e9
                    await self._sleep(delay_ns / 1e9)
            if isinstance(it, RawFrame):
                self.stats.frames += 1
            else:
                self.stats.controls += 1
            yield it

    async def frames(self) -> AsyncIterator[RawFrame]:
        async for it in self.items():
            if isinstance(it, RawFrame):
                yield it

    def normalize(self, frame: RawFrame) -> MarketEvent | None:
        try:
            ev = normalize_binance(frame.stream, frame.data, frame.ts_ingest_ns, self.instrument,
                                   src=self.src)
        except NormalizationError as e:
            self.stats.errors += 1
            key = e.field or "?"
            self.stats.error_fields[key] = self.stats.error_fields.get(key, 0) + 1
            if self.on_error == "raise":
                raise
            return None
        self.stats.events += 1
        return ev

    async def events(self) -> AsyncIterator[MarketEvent]:
        async for fr in self.frames():
            ev = self.normalize(fr)
            if ev is not None:
                yield ev

    def __aiter__(self) -> AsyncIterator[MarketEvent]:
        return self.events()


# ---------------------------------------------------------------- офлайн-REST (добір із фікстури)


def agg_trade_rest_row(data: Mapping[str, Any]) -> dict[str, Any]:
    """Поля REST /fapi/v1/aggTrades з WS-кадру aggTrade (a, p, q, f, l, T, m — ті самі значення)."""
    return {k: data[k] for k in ("a", "p", "q", "f", "l", "T", "m")}


@dataclass(frozen=True, slots=True)
class RestFixture:
    """Відповіді REST, потрібні для офлайн-добору: сирі рядки klines і aggTrades (формат Binance)."""

    symbol: str
    interval: str
    klines: tuple[list[Any], ...]
    agg_trades: tuple[dict[str, Any], ...]
    meta: dict[str, Any]

    @classmethod
    def load(cls, path: str | Path) -> RestFixture:
        d = orjson.loads(Path(path).read_bytes())
        return cls(symbol=str(d["symbol"]), interval=str(d.get("interval", "1m")),
                   klines=tuple(sorted(d["klines"], key=lambda r: r[0])),
                   agg_trades=tuple(sorted(d["aggTrades"], key=lambda r: r["a"])),
                   meta={k: v for k, v in d.items() if k not in ("klines", "aggTrades")})


class FixtureRestHandler:
    """httpx-обробник (для respx або httpx.MockTransport) з семантикою Binance USDⓈ-M:

    * GET /fapi/v1/klines?symbol&interval&startTime&endTime&limit — рядки з openTime ∈ [start, end],
      лише вже ЗАКРИТІ на момент `clock.now_ns()` (незакритого поточного бару фікстура не знає);
    * GET /fapi/v1/aggTrades?symbol&fromId&limit (≤ 1000) — угоди з a ≥ fromId і T ≤ now.
    Заголовок X-MBX-USED-WEIGHT-1M накопичується (klines — за limit, aggTrades — 20), як у справжньому API.
    """

    def __init__(self, fixture: RestFixture, clock: Clock) -> None:
        self.fx = fixture
        self.clock = clock
        self.used_weight = 0
        self.calls: list[tuple[str, dict[str, str]]] = []
        self._iv_ms = tf_ms(fixture.interval)

    def _now_ms(self) -> int:
        return self.clock.now_ns() // NS_PER_MS

    @staticmethod
    def _error(status: int, code: int, msg: str) -> httpx.Response:
        return httpx.Response(status, content=orjson.dumps({"code": code, "msg": msg}))

    def _ok(self, body: Any, weight: int) -> httpx.Response:
        self.used_weight += weight
        return httpx.Response(200, content=orjson.dumps(body),
                              headers={"X-MBX-USED-WEIGHT-1M": str(self.used_weight)})

    def __call__(self, request: httpx.Request) -> httpx.Response:
        q = dict(request.url.params)
        self.calls.append((request.url.path, q))
        if q.get("symbol") != self.fx.symbol:
            return self._error(400, -1121, "Invalid symbol.")
        now = self._now_ms()
        if request.url.path == KLINES_PATH:
            if q.get("interval", "1m") != self.fx.interval:
                return self._error(400, -1120, "Invalid interval.")
            limit = int(q.get("limit", 500))
            start = int(q["startTime"]) if "startTime" in q else None
            end = int(q["endTime"]) if "endTime" in q else None
            rows = [r for r in self.fx.klines
                    if (start is None or r[0] >= start) and (end is None or r[0] <= end)
                    and r[0] + self._iv_ms <= now][:limit]
            return self._ok(rows, klines_weight(limit))
        if request.url.path == AGG_TRADES_PATH:
            limit = min(int(q.get("limit", 500)), AGG_TRADES_MAX_LIMIT)
            from_id = int(q["fromId"]) if "fromId" in q else None
            rows_t = [t for t in self.fx.agg_trades
                      if (from_id is None or t["a"] >= from_id) and t["T"] <= now][:limit]
            return self._ok(rows_t, 20)
        return self._error(404, -1, f"unknown path {request.url.path}")
