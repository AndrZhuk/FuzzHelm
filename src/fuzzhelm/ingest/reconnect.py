"""Політика перепідключення WS: експоненційний backoff з джитером, heartbeat-watchdog, класифікація розривів.

Найменування: ingest/reconnect.py
Призначення: розрив з'єднання — штатна подія (Binance закриває WS щонайменше раз на 24 год, мережа
рветься, сервер перезапускається). Тут три чисті (без мережі й настінного часу) будівельні блоки, які
використовують і BinanceWsClient (live), і IngestPipeline (реплей на віртуальному часі).
Автор: Андрій Жук, 2026.

Backoff:  ceiling_k = min(cap, base·factor^k);   delay_k ~ jitter(ceiling_k)
  * "equal" (за замовчуванням): ceiling/2 + U(0, ceiling/2) — завжди є ненульова пауза, але
    клієнти не синхронізуються (thundering herd);
  * "full":  U(0, ceiling) — як retry REST (AWS Architecture Blog, 2015);  "none": ceiling.
Випадковість — лише з numpy Generator(seed): послідовність пауз відтворювана.

HeartbeatWatchdog: кадр не надходив довше за timeout → з'єднання вважається «напівмертвим» (TCP
half-open: сокет не закрито, але дані не йдуть) і примусово перепідключається. Ping/pong бібліотеки
websockets це ловить лише через ping_interval + ping_timeout (≈40 с); Binance шле markPrice@1s і
depth@100ms, тож тиша у 10 с — уже аномалія (виміряно: максимальний проміжок між кадрами market у
4-хв зразку — 0.89 с, public — 0.47 с).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final, Literal

import numpy as np

NS_PER_S: Final = 1_000_000_000
DEFAULT_HEARTBEAT_TIMEOUT_S: Final = 10.0

JitterMode = Literal["equal", "full", "none"]


class Backoff:
    """Експоненційний backoff зі стелею і відтворюваним джитером."""

    def __init__(self, base_s: float = 0.5, cap_s: float = 30.0, factor: float = 2.0, *,
                 jitter: JitterMode = "equal", seed: int = 0, rng: np.random.Generator | None = None) -> None:
        if not (base_s > 0 and cap_s >= base_s and factor >= 1.0):
            raise ValueError(
                f"need base_s > 0, cap_s >= base_s, factor >= 1 (got {base_s}, {cap_s}, {factor})")
        if jitter not in ("equal", "full", "none"):
            raise ValueError(f"unknown jitter mode {jitter!r}")
        self.base_s = base_s
        self.cap_s = cap_s
        self.factor = factor
        self.jitter: JitterMode = jitter
        self._rng = rng if rng is not None else np.random.default_rng(seed)
        self.attempt = 0
        self.history: list[float] = []

    def ceiling(self, attempt: int) -> float:
        # min() до піднесення в степінь не потрібен: factor^k для k ≤ 1000 скінченне, далі — cap
        if attempt > 1000:
            return self.cap_s
        return min(self.cap_s, self.base_s * self.factor ** attempt)

    def next_delay(self) -> float:
        c = self.ceiling(self.attempt)
        self.attempt += 1
        if self.jitter == "none":
            d = c
        elif self.jitter == "full":
            d = float(self._rng.uniform(0.0, c))
        else:
            d = c / 2 + float(self._rng.uniform(0.0, c / 2))
        self.history.append(d)
        return d

    def reset(self) -> None:
        self.attempt = 0


class HeartbeatWatchdog:
    """Сторожовий таймер за часом останнього кадру. Час — з ін'єктованого годинника/віртуальних міток."""

    def __init__(self, timeout_s: float = DEFAULT_HEARTBEAT_TIMEOUT_S, start_ns: int | None = None) -> None:
        if not timeout_s > 0:
            raise ValueError("timeout_s must be > 0")
        self.timeout_ns = round(timeout_s * NS_PER_S)
        self.last_ns = start_ns
        self.fires = 0
        self._armed = True          # одна тиша — одне спрацювання poll(); beat() «заводить» знову

    @property
    def timeout_s(self) -> float:
        return self.timeout_ns / NS_PER_S

    def beat(self, t_ns: int) -> None:
        self.last_ns = t_ns
        self._armed = True

    def poll(self, now_ns: int) -> int | None:
        """Віртуальний час реплею: «зараз» — мітка БУДЬ-ЯКОГО запису сесії (інших з'єднань теж). Якщо з
        останнього кадру цього з'єднання минуло ≥ timeout і за цю тишу сторож ще не спрацьовував —
        рахує спрацювання і повертає його момент (last + timeout); інакше None. На відміну від
        check_gap, помічає і з'єднання, що замовкло назавжди, і саме тоді, коли минув timeout."""
        d = self.deadline_ns()
        if d is None or not self._armed or now_ns < d:
            return None
        self._armed = False
        self.fires += 1
        return d

    def disarm(self) -> None:
        """Розрив уже зафіксовано іншим шляхом (запис керування `disconnected`): до наступного кадру
        або `connected` сторож мовчить — той самий розрив не рахується двічі."""
        self._armed = False

    def deadline_ns(self) -> int | None:
        return None if self.last_ns is None else self.last_ns + self.timeout_ns

    def remaining_s(self, now_ns: int) -> float:
        d = self.deadline_ns()
        return self.timeout_s if d is None else max(0.0, (d - now_ns) / NS_PER_S)

    def expired(self, now_ns: int) -> bool:
        d = self.deadline_ns()
        return d is not None and now_ns >= d

    def check_gap(self, t_ns: int) -> int | None:
        """Реплей: прийшов кадр у момент t. Якщо тиша перед ним перевищила timeout — повертає момент
        спрацювання (last + timeout) і рахує спрацювання; інакше None. Кадр записується як «удар серця».
        Від'ємний проміжок (стрибок годинника назад) спрацювання не дає. Ретроспективний варіант: тишу
        видно лише з приходом наступного кадру ТОГО САМОГО з'єднання (конвеєр використовує poll/beat)."""
        fired: int | None = None
        if self.last_ns is not None and t_ns - self.last_ns > self.timeout_ns:
            fired = self.last_ns + self.timeout_ns
            self.fires += 1
        self.beat(t_ns)
        return fired


# ---------------------------------------------------------------- класифікація розривів


class CloseClass(StrEnum):
    NORMAL = "NORMAL"                        # 1000: сервер штатно закрив (напр. ротація 24 год)
    GOING_AWAY = "GOING_AWAY"                # 1001/1012: перезапуск/обслуговування сервера
    TRANSIENT = "TRANSIENT"                  # 1006 (без close-кадру), мережеві помилки, таймаут відкриття
    SERVER_ERROR = "SERVER_ERROR"            # 1011/1013/1014, HTTP 5xx під час рукостискання
    RATE_LIMITED = "RATE_LIMITED"            # 1008 (policy), HTTP 429/418
    HEARTBEAT_TIMEOUT = "HEARTBEAT_TIMEOUT"  # сторожовий таймер: немає кадрів довше за timeout
    PROTOCOL_ERROR = "PROTOCOL_ERROR"        # 1002/1003/1007/1009/1010: помилка нашої сторони — фатально
    HANDSHAKE_REJECTED = "HANDSHAKE_REJECTED"  # HTTP 4xx (крім 429/418): невірний URL/потік — фатально


FATAL: Final = frozenset({CloseClass.PROTOCOL_ERROR, CloseClass.HANDSHAKE_REJECTED})
IMMEDIATE: Final = frozenset({CloseClass.NORMAL, CloseClass.GOING_AWAY})

_BY_CODE: Final[dict[int, CloseClass]] = {
    1000: CloseClass.NORMAL, 1001: CloseClass.GOING_AWAY, 1012: CloseClass.GOING_AWAY,
    1005: CloseClass.TRANSIENT, 1006: CloseClass.TRANSIENT,
    1011: CloseClass.SERVER_ERROR, 1013: CloseClass.SERVER_ERROR, 1014: CloseClass.SERVER_ERROR,
    1008: CloseClass.RATE_LIMITED,
    1002: CloseClass.PROTOCOL_ERROR, 1003: CloseClass.PROTOCOL_ERROR, 1007: CloseClass.PROTOCOL_ERROR,
    1009: CloseClass.PROTOCOL_ERROR, 1010: CloseClass.PROTOCOL_ERROR,
}


def classify_close_code(code: int | None) -> CloseClass:
    """Код закриття WebSocket (RFC 6455 §7.4) → клас реакції. None (close-кадру не було) = 1006."""
    if code is None:
        return CloseClass.TRANSIENT
    if code in _BY_CODE:
        return _BY_CODE[code]
    if 4000 <= code < 5000:                  # прикладні коди сервера: не знаємо — вважаємо тимчасовим
        return CloseClass.SERVER_ERROR
    return CloseClass.TRANSIENT


def classify_http_status(status: int) -> CloseClass:
    """Статус HTTP відмови в рукостисканні → клас реакції."""
    if status in (418, 429):
        return CloseClass.RATE_LIMITED
    if 500 <= status < 600:
        return CloseClass.SERVER_ERROR
    if 400 <= status < 500:
        return CloseClass.HANDSHAKE_REJECTED
    return CloseClass.TRANSIENT


@dataclass(frozen=True, slots=True)
class Disconnect:
    conn: str
    ts_ns: int
    cls: CloseClass
    code: int | None = None
    reason: str = ""

    @property
    def fatal(self) -> bool:
        return self.cls in FATAL

    @property
    def detail(self) -> str:
        return f"{self.cls.value}:{'' if self.code is None else self.code}:{self.reason}"
