"""WebSocket-клієнт Binance USDⓈ-M Futures (публічні ринкові потоки, лише читання).

Найменування: ingest/ws_client.py
Призначення: «WebSocket … у реальному часі»: два з'єднання (D-01: kline/aggTrade/markPrice —
/market/stream, depth — /public/stream), мітка ts_ingest_ns з ін'єктованого Clock, heartbeat-watchdog,
класифікація розривів за кодом закриття, перепідключення з reconnect.Backoff, опційне дзеркалення
сирого потоку в SessionRecorder. Реалізує core.ports.MarketFeed (async-ітератор MarketEvent).
Автор: Андрій Жук, 2026.

Два годинники (WS-07): `clock` — час події (мітка ts_ingest_ns кадрів і записів керування, час епохи), а
`monotonic` — лише для сторожа тиші. Сторож на настінному годиннику реагував на крок NTP: стрибок уперед між
кадрами давав нульовий залишок таймауту і хибний розрив, стрибок назад — відкладав виявлення справжньої тиші.
Воркери передають infra.wallclock.MonotonicClock; без `monotonic` (тести, реплей) сторож іде за `clock`.

Шви для тестів (мережа в тестах не використовується ніколи): `connect(url)` — фабрика async-контекстного
менеджера з'єднання (за замовчуванням websockets.asyncio.client.connect), `recv(ws, timeout)` —
отримання кадру з таймаутом (за замовчуванням asyncio.wait_for), `sleep` — пауза backoff. Фіктивні
реалізації рухають ManualClock замість реального очікування.

Що відбувається при розриві: запис керування `disconnected` (деталь `КЛАС:код:причина`) → пауза
backoff (для NORMAL/GOING_AWAY після робочої сесії — без паузи) → нове з'єднання → `connected`.
Втрачені за час розриву події клієнт НЕ відновлює — це робота конвеєра (gap_detector + REST-добір).
Фатальні класи (PROTOCOL_ERROR, HANDSHAKE_REJECTED) зупиняють клієнт з WsFatalError: повторювати
невірний URL чи порушення протоколу нескінченно — лише маскувати помилку.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import AbstractAsyncContextManager
from typing import Any, Final, Protocol

import orjson

from fuzzhelm.config import Settings, assert_readonly_url
from fuzzhelm.core.dto import MarketEvent
from fuzzhelm.core.enums import Src, Venue
from fuzzhelm.core.errors import FuzzHelmError, NormalizationError
from fuzzhelm.core.ports import Clock
from fuzzhelm.ingest.normalize import InstrumentLike, normalize_binance
from fuzzhelm.ingest.reconnect import (
    DEFAULT_HEARTBEAT_TIMEOUT_S,
    IMMEDIATE,
    Backoff,
    CloseClass,
    Disconnect,
    HeartbeatWatchdog,
    classify_close_code,
    classify_http_status,
)
from fuzzhelm.ingest.recorder import ControlRecord, RawFrame, SessionItem, SessionRecorder
from fuzzhelm.ingest.symbols import symbol_ref

MARKET: Final = "market"
PUBLIC: Final = "public"


class WsConnection(Protocol):
    async def recv(self) -> str | bytes: ...


ConnectFactory = Callable[[str], AbstractAsyncContextManager[WsConnection]]
RecvFn = Callable[[WsConnection, float], Awaitable[str | bytes]]
SleepFn = Callable[[float], Awaitable[None]]


def websockets_connect(url: str) -> AbstractAsyncContextManager[WsConnection]:  # pragma: no cover - мережа
    from websockets.asyncio.client import connect  # noqa: PLC0415

    # ping кожні 20 с (Binance вимагає pong протягом 10 хв); max_size — depth20 з запасом
    return connect(url, max_size=2**22, open_timeout=15, ping_interval=20, ping_timeout=20, close_timeout=5)


async def recv_with_timeout(ws: WsConnection, timeout_s: float) -> str | bytes:
    return await asyncio.wait_for(ws.recv(), timeout_s)


class HeartbeatTimeoutError(FuzzHelmError):
    """Сторожовий таймер: з'єднання мовчить довше за timeout."""


class WsFatalError(FuzzHelmError):
    def __init__(self, disconnect: Disconnect) -> None:
        super().__init__(f"fatal WebSocket error on {disconnect.conn}: {disconnect.detail}")
        self.disconnect = disconnect


def classify_exception(e: BaseException) -> tuple[CloseClass, int | None, str]:
    """Виняток з'єднання → (клас, код, причина). Невідомі винятки (баги) не ковтаються — ValueError."""
    from websockets.exceptions import ConnectionClosed, InvalidHandshake, InvalidStatus  # noqa: PLC0415

    if isinstance(e, HeartbeatTimeoutError):
        return CloseClass.HEARTBEAT_TIMEOUT, None, str(e)
    if isinstance(e, ConnectionClosed):
        code = e.rcvd.code if e.rcvd is not None else None
        reason = e.rcvd.reason if e.rcvd is not None else "no close frame"
        return classify_close_code(code), code, reason
    if isinstance(e, InvalidStatus):
        status = e.response.status_code
        return classify_http_status(status), status, f"HTTP {status}"
    if isinstance(e, InvalidHandshake | TimeoutError | OSError | EOFError):
        return CloseClass.TRANSIENT, None, f"{type(e).__name__}: {e}"
    raise ValueError(f"unclassified connection error {type(e).__name__}") from e


class _Stop:
    pass


_STOP: Final = _Stop()


class BinanceWsClient:
    def __init__(self, settings: Settings, clock: Clock, *,
                 instruments: Sequence[InstrumentLike] | None = None,
                 connect: ConnectFactory = websockets_connect, recv: RecvFn = recv_with_timeout,
                 sleep: SleepFn = asyncio.sleep, backoff: Callable[[str], Backoff] | None = None,
                 heartbeat_timeout_s: float = DEFAULT_HEARTBEAT_TIMEOUT_S,
                 recorder: SessionRecorder | None = None, src: Src = Src.WS, kline_interval: str = "1m",
                 depth_levels: int = 20, depth_speed: str = "100ms", mark_speed: str = "1s",
                 max_connects: int | None = None, queue_size: int = 10_000,
                 monotonic: Clock | None = None) -> None:
        self.settings = settings
        self.clock = clock
        # сторож тиші міряє інтервали монотонним годинником: крок NTP не є тишею з'єднання (WS-07)
        self.monotonic: Clock = monotonic if monotonic is not None else clock
        insts = (list(instruments) if instruments
                 else [symbol_ref(Venue.BINANCE_USDM, s) for s in settings.symbols])
        self.instruments: dict[str, InstrumentLike] = {i.symbol_venue.lower(): i for i in insts}
        self._connect = connect
        self._recv = recv
        self._sleep = sleep
        self._backoff_factory = backoff or (lambda conn: Backoff(seed=settings.seed + (conn == PUBLIC)))
        self.heartbeat_timeout_s = heartbeat_timeout_s
        self.recorder = recorder
        self.src = src
        self._kline = kline_interval
        self._depth = f"depth{depth_levels}@{depth_speed}"
        self._mark = f"markPrice@{mark_speed}"
        self.max_connects = max_connects
        self._queue: asyncio.Queue[SessionItem | _Stop] = asyncio.Queue(queue_size)
        self._tasks: list[asyncio.Task[None]] = []
        self._closing = False
        self._fatal: Disconnect | None = None
        self._crash: BaseException | None = None
        self._stop_task: asyncio.Task[None] | None = None
        # статистика
        self.frames_received = 0
        self.bad_frames = 0
        self.normalization_errors = 0
        self.connects: dict[str, int] = {MARKET: 0, PUBLIC: 0}
        self.disconnects: list[Disconnect] = []
        self.backoffs: dict[str, Backoff] = {}
        self.watchdog_fires: dict[str, int] = {MARKET: 0, PUBLIC: 0}

    # ------------------------------------------------------------ адреси

    def streams(self) -> dict[str, list[str]]:
        syms = sorted(self.instruments)
        return {
            MARKET: [s for sym in syms
                     for s in (f"{sym}@kline_{self._kline}", f"{sym}@aggTrade", f"{sym}@{self._mark}")],
            PUBLIC: [f"{sym}@{self._depth}" for sym in syms],
        }

    def urls(self) -> dict[str, str]:
        bases = {MARKET: self.settings.binance_ws_market, PUBLIC: self.settings.binance_ws_public}
        return {conn: assert_readonly_url(f"{bases[conn]}?streams={'/'.join(st)}")
                for conn, st in self.streams().items()}

    # ------------------------------------------------------------ з'єднання

    async def _put(self, item: SessionItem) -> None:
        if self.recorder is not None:
            self.recorder.write(item)
        await self._queue.put(item)

    def _parse(self, conn: str, raw: str | bytes, ts: int) -> RawFrame | None:
        try:
            msg = orjson.loads(raw)
        except orjson.JSONDecodeError:
            self.bad_frames += 1
            return None
        if (not isinstance(msg, dict) or not isinstance(msg.get("stream"), str)
                or not isinstance(msg.get("data"), dict)):
            self.bad_frames += 1          # напр. відповідь на SUBSCRIBE {"result":null,"id":1}
            return None
        self.frames_received += 1
        return RawFrame(conn, ts, msg["stream"], msg["data"])

    async def _session(self, conn: str, url: str, backoff: Backoff) -> None:
        async with self._connect(url) as ws:
            self.connects[conn] += 1
            await self._put(ControlRecord(conn, self.clock.now_ns(), "connected", url))
            mono = self.monotonic
            wd = HeartbeatWatchdog(self.heartbeat_timeout_s, mono.now_ns())
            healthy = False
            while not self._closing:
                try:
                    raw = await self._recv(ws, wd.remaining_s(mono.now_ns()))
                except TimeoutError:
                    if wd.expired(mono.now_ns()):
                        wd.fires += 1
                        self.watchdog_fires[conn] += 1
                        raise HeartbeatTimeoutError(f"no frames for {wd.timeout_s:g}s") from None
                    continue
                ts = self.clock.now_ns()                  # мітка кадру — час події
                wd.beat(mono.now_ns())                    # сторож — монотонний інтервал
                frame = self._parse(conn, raw, ts)
                if frame is None:
                    continue
                if not healthy:
                    backoff.reset()          # скидаємо лише після першого справжнього кадру
                    healthy = True
                await self._put(frame)

    async def _run_conn(self, conn: str, url: str) -> None:
        backoff = self.backoffs[conn] = self._backoff_factory(conn)
        attempts = 0
        while not self._closing:
            if self.max_connects is not None and attempts >= self.max_connects:
                return
            attempts += 1
            try:
                await self._session(conn, url, backoff)
                continue                                  # вихід без винятку — лише при закритті
            except asyncio.CancelledError:
                raise
            except Exception as e:
                cls, code, reason = classify_exception(e)
            d = Disconnect(conn, self.clock.now_ns(), cls, code, reason)
            self.disconnects.append(d)
            await self._put(ControlRecord(conn, d.ts_ns, "disconnected", d.detail))
            if d.fatal:
                self._fatal = d
                await self._queue.put(_STOP)
                return
            delay = 0.0 if cls in IMMEDIATE and backoff.attempt == 0 else backoff.next_delay()
            if cls in IMMEDIATE and delay == 0.0:
                backoff.attempt += 1              # повторний NORMAL підряд уже піде з паузою
            await self._sleep(delay)

    def _on_task_done(self, t: asyncio.Task[None]) -> None:
        if t.cancelled():
            return
        exc = t.exception()
        if exc is not None and self._crash is None:
            self._crash = exc
        if exc is not None or all(x.done() for x in self._tasks):
            try:
                self._queue.put_nowait(_STOP)
            except asyncio.QueueFull:     # споживач відстає: доставити сигнал зупинки після черги
                self._stop_task = asyncio.get_running_loop().create_task(self._queue.put(_STOP))

    def _start(self) -> None:
        if self._tasks:
            return
        for conn, url in self.urls().items():
            t = asyncio.create_task(self._run_conn(conn, url), name=f"ws-{conn}")
            t.add_done_callback(self._on_task_done)
            self._tasks.append(t)

    # ------------------------------------------------------------ споживання

    async def items(self) -> AsyncIterator[SessionItem]:
        """Кадри і записи керування обох з'єднань у порядку надходження."""
        self._start()
        try:
            while True:
                item = await self._queue.get()
                if isinstance(item, _Stop):
                    if self._crash is not None:
                        raise self._crash
                    if self._fatal is not None:
                        raise WsFatalError(self._fatal)
                    if all(t.done() for t in self._tasks):
                        return
                    continue
                yield item
        finally:
            await self.aclose()

    async def frames(self) -> AsyncIterator[RawFrame]:
        async for it in self.items():
            if isinstance(it, RawFrame):
                yield it

    def normalize(self, fr: RawFrame) -> MarketEvent | None:
        sym = fr.stream.split("@", 1)[0]
        inst = self.instruments.get(sym)
        if inst is None:
            self.normalization_errors += 1
            return None
        try:
            return normalize_binance(fr.stream, fr.data, fr.ts_ingest_ns, inst, src=self.src)
        except NormalizationError:
            self.normalization_errors += 1
            return None

    async def events(self) -> AsyncIterator[MarketEvent]:
        async for fr in self.frames():
            ev = self.normalize(fr)
            if ev is not None:
                yield ev

    def __aiter__(self) -> AsyncIterator[MarketEvent]:
        return self.events()

    async def aclose(self) -> None:
        self._closing = True
        for t in self._tasks:
            if not t.done():
                t.cancel()
        for t in self._tasks:
            # помилки задач уже повідомлено споживачу через items()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t

    @property
    def stats(self) -> dict[str, Any]:
        return {"frames": self.frames_received, "bad_frames": self.bad_frames,
                "connects": dict(self.connects), "disconnects": [d.detail for d in self.disconnects],
                "watchdog_fires": dict(self.watchdog_fires),
                "normalization_errors": self.normalization_errors}
