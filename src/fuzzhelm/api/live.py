"""Потік подій для SSE (/stream/live): розсилка в пам'яті + джерело PostgreSQL LISTEN/NOTIFY.

Найменування: api/live.py
Призначення: воркери пишуть події в канал `fuzzhelm_live` (`SELECT pg_notify(...)` у своїй транзакції),
процес API тримає ОДНЕ з'єднання LISTEN і розсилає події всім SSE-клієнтам через обмежені черги.
Автор: Андрій Жук, 2026.

Чому LISTEN/NOTIFY, а не опитування БД і не брокер (обґрунтування — docs/api/api.md, «SSE»):
  * брифінг свідомо відкидає Kafka/Redis (§9), а PostgreSQL уже є;
  * NOTIFY транзакційний: подію отримають лише після COMMIT, тож браузер не побачить рішення,
    якого немає в БД (опитування дало б те саме, але із затримкою періоду і навантаженням N клієнтів);
  * одне з'єднання LISTEN на процес незалежно від кількості клієнтів; payload ≤ 8000 байт —
    великі об'єкти передаються посиланням ({"kind": "decision", "id": 123}), деталі — через REST.
Тестованість: `LiveHub` не знає про PostgreSQL (publish/subscribe у пам'яті, кільцевий буфер для
Last-Event-ID), тому SSE-маршрут тестується офлайн; `PgLiveSource` лише годує хаб і тестується
інтеграційно.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import deque
from collections.abc import AsyncIterator, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

import asyncpg
import orjson

from fuzzhelm.storage.session import json_dumps

log = logging.getLogger(__name__)

LIVE_CHANNEL = "fuzzhelm_live"  # події для браузера (рішення, ризик, здоров'я конвеєра)
CONTROL_CHANNEL = "fuzzhelm_control"  # команди для воркерів (зняття kill-switch, перечитати ліміти)
NOTIFY_MAX_BYTES = 7_900  # межа PostgreSQL — 8000 байт payload (із запасом)
KIND_MAX_CHARS = 64


@dataclass(frozen=True, slots=True)
class LiveEvent:
    seq: int  # монотонний номер у межах процесу API → SSE `id`
    kind: str  # SSE `event`: decision | risk | health | order | audit | ...
    payload: Mapping[str, Any]


def encode_notify_payload(kind: str, payload: Mapping[str, Any]) -> str:
    """JSON для pg_notify: {"kind": ..., "data": ...}; > 7900 байт → ValueError (передавайте посилання)."""
    if not kind or len(kind) > KIND_MAX_CHARS or any(ch in kind for ch in "\r\n"):
        raise ValueError("event kind must be a non-empty single line of ≤ 64 chars")
    text = json_dumps({"kind": kind, "data": dict(payload)})
    if len(text.encode("utf-8")) > NOTIFY_MAX_BYTES:
        raise ValueError(f"NOTIFY payload exceeds {NOTIFY_MAX_BYTES} bytes; publish a reference instead")
    return text


def decode_notify_payload(raw: str) -> tuple[str, dict[str, Any]] | None:
    """Зворотне до encode_notify_payload; сміття (не JSON / не та форма) → None, а не виняток."""
    try:
        obj = orjson.loads(raw)
    except orjson.JSONDecodeError:
        return None
    if not isinstance(obj, dict) or not isinstance(obj.get("kind"), str):
        return None
    kind = obj["kind"]
    data = obj.get("data")
    if not kind or len(kind) > KIND_MAX_CHARS or "\n" in kind or "\r" in kind:
        return None
    return kind, data if isinstance(data, dict) else {"value": data}


@dataclass
class _Subscriber:
    queue: asyncio.Queue[LiveEvent | None]
    kinds: frozenset[str] | None
    dropped: int = 0
    closed: bool = False


@dataclass
class LiveHub:
    """Розсилка подій у пам'яті: кожен підписник має обмежену чергу; переповнення → найстаріша
    подія викидається (повільний клієнт не гальмує інших), лічильник `dropped` — у статистиці."""

    queue_size: int = 256
    replay_size: int = 512
    _seq: int = 0
    _subs: list[_Subscriber] = field(default_factory=list)
    _ring: deque[LiveEvent] = field(default_factory=deque)
    _last_by_kind: dict[str, LiveEvent] = field(default_factory=dict)
    _closed: bool = False

    def publish(self, kind: str, payload: Mapping[str, Any]) -> LiveEvent:
        self._seq += 1
        ev = LiveEvent(seq=self._seq, kind=kind, payload=dict(payload))
        self._ring.append(ev)
        while len(self._ring) > self.replay_size:
            self._ring.popleft()
        self._last_by_kind[kind] = ev
        for sub in self._subs:
            if sub.kinds is not None and kind not in sub.kinds:
                continue
            if sub.queue.full():
                with contextlib.suppress(asyncio.QueueEmpty):
                    sub.queue.get_nowait()
                sub.dropped += 1
            sub.queue.put_nowait(ev)
        return ev

    def last(self, kind: str) -> LiveEvent | None:
        return self._last_by_kind.get(kind)

    @property
    def subscribers(self) -> int:
        return len(self._subs)

    @property
    def closed(self) -> bool:
        return self._closed

    def replay_after(self, last_seq: int, kinds: frozenset[str] | None = None) -> list[LiveEvent]:
        """Події з кільцевого буфера з seq > last_seq (відновлення за заголовком Last-Event-ID)."""
        return [e for e in self._ring if e.seq > last_seq and (kinds is None or e.kind in kinds)]

    @contextlib.asynccontextmanager
    async def subscribe(
        self, kinds: Iterable[str] | None = None, *, last_event_id: int | None = None
    ) -> AsyncIterator[AsyncIterator[LiveEvent]]:
        """Підписка: спершу пропущене з буфера (якщо last_event_id), далі нові події до close()."""
        kset = frozenset(kinds) if kinds else None
        sub = _Subscriber(queue=asyncio.Queue(maxsize=self.queue_size), kinds=kset)
        backlog = self.replay_after(last_event_id, kset) if last_event_id is not None else []
        closed_at_start = self._closed
        self._subs.append(sub)

        async def events() -> AsyncIterator[LiveEvent]:
            for ev in backlog:
                yield ev
            if closed_at_start:
                return
            while True:
                if sub.closed and sub.queue.empty():
                    return
                item = await sub.queue.get()
                if item is None:
                    return
                yield item

        try:
            yield events()
        finally:
            with contextlib.suppress(ValueError):
                self._subs.remove(sub)

    def close(self) -> None:
        """Завершити всі підписки (shutdown API): кожен потік SSE закривається штатно."""
        self._closed = True
        for sub in self._subs:
            sub.closed = True
            # сигнал-заглушка лише для того, хто чекає на порожній черзі: події в черзі не втрачаються
            if sub.queue.empty():
                sub.queue.put_nowait(None)

    def stats(self) -> dict[str, Any]:
        return {
            "subscribers": len(self._subs),
            "last_seq": self._seq,
            "dropped": sum(s.dropped for s in self._subs),
            "buffered": len(self._ring),
        }


class PgLiveSource:
    """Одне asyncpg-з'єднання LISTEN fuzzhelm_live → LiveHub.publish; перепідключення з backoff.

    Запускається ліниво (перший SSE-клієнт або /market/health), щоб API стартував і віддавав /docs
    навіть без БД. dsn — звичайний postgresql://… (без +asyncpg).
    """

    def __init__(
        self,
        dsn: str,
        hub: LiveHub,
        *,
        channel: str = LIVE_CHANNEL,
        role: str | None = None,
        max_backoff_s: float = 30.0,
    ) -> None:
        self.dsn = dsn
        self.hub = hub
        self.channel = channel
        self.role = role
        self.max_backoff_s = max_backoff_s
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self.connected = False
        self.errors = 0

    def _on_notify(self, _conn: Any, _pid: int, _channel: str, payload: str) -> None:
        decoded = decode_notify_payload(payload)
        if decoded is None:
            log.warning("ignored malformed NOTIFY payload on %s", self.channel)
            return
        self.hub.publish(*decoded)

    async def _run(self) -> None:
        backoff = 0.5
        while not self._stop.is_set():
            conn = None
            try:
                settings = {"role": self.role} if self.role else None
                conn = await asyncpg.connect(self.dsn, server_settings=settings, timeout=5)
                await conn.add_listener(self.channel, self._on_notify)
                self.connected = True
                backoff = 0.5
                closed = asyncio.Event()
                conn.add_termination_listener(lambda _c, ev=closed: ev.set())
                stop_wait = asyncio.create_task(self._stop.wait())
                closed_wait = asyncio.create_task(closed.wait())
                await asyncio.wait({stop_wait, closed_wait}, return_when=asyncio.FIRST_COMPLETED)
                for t in (stop_wait, closed_wait):
                    t.cancel()
            except (OSError, asyncpg.PostgresError, TimeoutError) as e:
                self.errors += 1
                log.warning("LISTEN %s failed: %s", self.channel, type(e).__name__)
            finally:
                self.connected = False
                if conn is not None:
                    with contextlib.suppress(Exception):
                        await conn.close()
            if not self._stop.is_set():
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._stop.wait(), timeout=backoff)
                backoff = min(self.max_backoff_s, backoff * 2)

    def ensure_started(self) -> None:
        if self._task is None or self._task.done():
            self._stop.clear()
            self._task = asyncio.get_running_loop().create_task(self._run(), name="pg-live-source")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.wait_for(self._task, timeout=5)
            self._task = None


def asyncpg_dsn(sqlalchemy_url: str) -> str:
    """postgresql+asyncpg://… → postgresql://… (asyncpg.connect не розуміє суфікс драйвера)."""
    return sqlalchemy_url.replace("postgresql+asyncpg://", "postgresql://", 1)
