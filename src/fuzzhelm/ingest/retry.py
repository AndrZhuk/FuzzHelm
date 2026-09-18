"""Повтори REST-запитів: експоненційний backoff із повним джитером і пошана до Retry-After.

Найменування: ingest/retry.py
Призначення: переживати тимчасові збої (мережа, 5xx, 429/418), не створюючи «шторму» повторів.
Автор: Андрій Жук, 2026.

Повний джитер (full jitter):  delay_k = U(0, min(cap, base·2^k)),  k = 0, 1, … — номер повтору.
Рівномірний розподіл на всьому інтервалі розводить у часі повтори багатьох клієнтів, що впали
одночасно. Генератор — numpy Generator із ЯВНИМ seed (ingest поза AST-сканом детермінізму, але
відтворюваність повторів потрібна тестам і реплею).

Retry-After: на 429/418 біржа каже, скільки чекати; тоді чекаємо рівно стільки (без джитера). Якщо
пауза більша за `max_retry_after_s` (418 — бан IP, буває годинами) — не спимо, а кидаємо RateLimitBannedError.
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC
from email.utils import parsedate_to_datetime
from typing import Final, TypeVar

import httpx
import numpy as np

from fuzzhelm.core.errors import FuzzHelmError

T = TypeVar("T")
SleepFn = Callable[[float], Awaitable[None]]

RETRY_AFTER_STATUS: Final = frozenset({418, 429})
RETRYABLE_5XX: Final = frozenset({500, 502, 503, 504})


class IngestError(FuzzHelmError):
    """Базова помилка REST-інжесту."""


class RetryableError(IngestError):
    """Тимчасова помилка: запит варто повторити (після `retry_after_s`, якщо біржа його назвала)."""

    def __init__(self, message: str, *, retry_after_s: float | None = None,
                 status: int | None = None) -> None:
        super().__init__(message)
        self.retry_after_s = retry_after_s
        self.status = status


class NonRetryableHttpError(IngestError):
    def __init__(self, status: int, body: str, message: str = "") -> None:
        super().__init__(message or f"HTTP {status}: {body[:200]}")
        self.status = status
        self.body = body


class RetryExhaustedError(IngestError):
    def __init__(self, attempts: int, last_error: BaseException) -> None:
        super().__init__(f"gave up after {attempts} attempts: {last_error!r}")
        self.attempts = attempts
        self.last_error = last_error


class RateLimitBannedError(IngestError):
    def __init__(self, retry_after_s: float, status: int | None) -> None:
        super().__init__(f"venue asks to wait {retry_after_s:.0f}s (HTTP {status}); "
                         "refusing to sleep that long")
        self.retry_after_s = retry_after_s
        self.status = status


def _ascii_uint(v: str) -> int | None:
    # str.isdigit() істинне й для «²», «١٢» тощо, а int("²") падає — заголовок (декодований latin-1)
    # від проксі не мусить валити класифікацію помилки, тому лише ASCII-цифри
    return int(v) if v.isascii() and v.isdigit() else None


def parse_retry_after(value: str | None, now_ns: int | None = None) -> float | None:
    """`Retry-After`: секунди (`"7"`) або HTTP-дата (потрібен now_ns). Некоректне значення → None."""
    if value is None:
        return None
    v = value.strip()
    secs = _ascii_uint(v)
    if secs is not None:
        return float(secs)
    if now_ns is None:
        return None
    try:
        when = parsedate_to_datetime(v)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:              # зона «-0000» дає naive datetime; HTTP-дати — завжди UTC
        when = when.replace(tzinfo=UTC)
    return max(0.0, when.timestamp() - now_ns / 1e9)


def error_for_response(resp: httpx.Response, now_ns: int | None = None) -> RetryableError | None:
    """Повертає RetryableError для 429/418/5xx, інакше None (рішення про інші 4xx — за клієнтом)."""
    status = resp.status_code
    if status in RETRY_AFTER_STATUS:
        ra = parse_retry_after(resp.headers.get("Retry-After"), now_ns)
        return RetryableError(f"HTTP {status} rate limited", retry_after_s=ra, status=status)
    if status in RETRYABLE_5XX:
        ra = parse_retry_after(resp.headers.get("Retry-After"), now_ns)
        return RetryableError(f"HTTP {status} server error", retry_after_s=ra, status=status)
    return None


RETRYABLE_TRANSPORT: Final = (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError)


class RetryPolicy:
    """Політика повторів. `rng_seed` (або готовий `rng`) робить послідовність затримок відтворюваною."""

    def __init__(self, base: float = 0.5, cap: float = 30.0, max_attempts: int = 5, rng_seed: int = 0, *,
                 max_retry_after_s: float = 300.0, rng: np.random.Generator | None = None) -> None:
        if not (math.isfinite(base) and math.isfinite(cap) and base > 0 and cap > 0):
            raise ValueError(f"base and cap must be finite and > 0, got base={base!r}, cap={cap!r}")
        if max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        self.base = float(base)
        self.cap = float(cap)
        self.max_attempts = int(max_attempts)
        self.max_retry_after_s = float(max_retry_after_s)
        self.rng_seed = rng_seed
        self._rng = rng if rng is not None else np.random.default_rng(rng_seed)
        self.sleeps: list[float] = []            # фактичні паузи (діагностика, тести)

    def ceiling(self, attempt: int) -> float:
        """min(cap, base·2^attempt) — верхня межа джитера для повтору номер `attempt` (з 0)."""
        if attempt < 0:
            raise ValueError("attempt must be >= 0")
        try:
            # ldexp — точне множення на 2^attempt; жодних припущень «2^k·base уже > cap»
            return min(self.cap, math.ldexp(self.base, attempt))
        except OverflowError:
            return self.cap

    def backoff(self, attempt: int) -> float:
        """Повний джитер: U(0, min(cap, base·2^attempt))."""
        return float(self._rng.uniform(0.0, self.ceiling(attempt)))

    def delay_for(self, error: BaseException, attempt: int) -> float:
        if isinstance(error, RetryableError) and error.retry_after_s is not None:
            if error.retry_after_s > self.max_retry_after_s:
                raise RateLimitBannedError(error.retry_after_s, error.status) from error
            return error.retry_after_s
        return self.backoff(attempt)

    async def execute(self, fn: Callable[[], Awaitable[T]], *, sleep: SleepFn = asyncio.sleep,
                      on_retry: Callable[[int, BaseException, float], None] | None = None) -> T:
        """Викликає `fn` до max_attempts разів; повторює RetryableError і мережеві помилки httpx."""
        last: BaseException | None = None
        for attempt in range(self.max_attempts):
            try:
                return await fn()
            except (RetryableError, *RETRYABLE_TRANSPORT) as e:
                last = e
                if attempt == self.max_attempts - 1:
                    break
                delay = self.delay_for(e, attempt)
                if on_retry is not None:
                    on_retry(attempt, e, delay)
                self.sleeps.append(delay)
                await sleep(delay)
        assert last is not None
        raise RetryExhaustedError(self.max_attempts, last) from last


def header_int(headers: Mapping[str, str], name: str) -> int | None:
    v = headers.get(name)
    if v is None:
        return None
    return _ascii_uint(v.strip())
