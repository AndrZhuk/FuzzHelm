"""Token bucket з вагами запитів Binance і ресинхронізацією за заголовком X-MBX-USED-WEIGHT-1M.

Найменування: ingest/ratelimit.py
Призначення: клієнт сам не перевищує ваговий бюджет біржі (замість «отримати 429 і чекати»).
Автор: Андрій Жук, 2026.

Модель: tokens(t) = min(C, tokens(t₀) + r·(t − t₀)); запит вагою w проходить, якщо tokens ≥ w.
Час — лише через ін'єктований Clock, очікування — через ін'єктований `sleep` (тести без реального сну).

ВАЖЛИВО (виміряна властивість, див. deviations.d/ingest_rest.md): Binance рахує вагу у ФІКСОВАНОМУ
хвилинному вікні, а token bucket за будь-яке вікно довжиною W пропускає до C + r·W. Відро з C = 2400
і r = 2400/60 пропустило б до 4800 за хвилину. Тому `binance_request_bucket` за замовчуванням ділить
ліміт навпіл: C = L/2, r = L/(2·60) ⇒ C + 60·r = L — гарантія «не більше L за будь-які 60 с».
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Awaitable, Callable, Mapping
from typing import Final

from fuzzhelm.core.ports import Clock

NS_PER_S: Final = 1_000_000_000
BINANCE_WEIGHT_LIMIT_1M: Final = 2400          # rateLimits.REQUEST_WEIGHT (exchangeInfo, 2026-09-18)
USED_WEIGHT_HEADER: Final = "X-MBX-USED-WEIGHT-1M"
_EPS: Final = 1e-9

SleepFn = Callable[[float], Awaitable[None]]


def klines_weight(limit: int) -> int:
    """Вага `/fapi/v1/klines` від `limit`, ВИМІРЯНА за X-MBX-USED-WEIGHT-1M (fixtures/rest/capture_meta.json):
    limit ≤ 100 → 1; 101…500 → 2; 501…1000 → 5; 1001…1500 → 10."""
    if type(limit) is not int or not 1 <= limit <= 1500:
        raise ValueError(f"klines limit must be an int in [1, 1500], got {limit!r}")
    if limit <= 100:
        return 1
    if limit <= 500:
        return 2
    if limit <= 1000:
        return 5
    return 10


# Ваги інших використовуваних ендпоінтів (для символу; premiumIndex без symbol важить 10 за документацією).
# aggTrades = 20 — ВИМІРЯНО 2026-09-18 за приростом X-MBX-USED-WEIGHT-1M (2 → 22;
# docs/deviations.d/data.md, DATA-02).
# fundingRate не списує REQUEST_WEIGHT узагалі (заголовка у відповіді немає, лічильник не зріс): у нього
# окремий ліміт 500 запитів / 5 хв / IP; 1 — консервативний облік у власному відрі (вага мусить бути > 0).
ENDPOINT_WEIGHTS: Final[dict[str, int]] = {
    "/fapi/v1/time": 1,
    "/fapi/v1/ping": 1,
    "/fapi/v1/exchangeInfo": 1,
    "/fapi/v1/premiumIndex": 1,
    "/fapi/v1/aggTrades": 20,
    "/fapi/v1/fundingRate": 1,
}


def request_weight(path: str, params: Mapping[str, object] | None = None) -> int:
    params = params or {}
    # indexPriceKlines важить як klines за тим самим limit — ВИМІРЯНО 2026-09-18 (limit 2 → 1, 1000 → 5)
    if path in ("/fapi/v1/klines", "/fapi/v1/indexPriceKlines"):
        limit = params.get("limit", 500)
        if not isinstance(limit, int):
            raise ValueError(f"klines limit must be int, got {limit!r}")
        return klines_weight(limit)
    if path == "/fapi/v1/premiumIndex" and "symbol" not in params:
        return 10
    try:
        return ENDPOINT_WEIGHTS[path]
    except KeyError:
        raise ValueError(f"unknown request weight for {path!r}") from None


class TokenBucket:
    """Відро токенів із ваговими запитами; безпечне для кількох корутин (FIFO через asyncio.Lock)."""

    def __init__(self, capacity: float, refill_per_s: float, clock: Clock, *,
                 sleep: SleepFn = asyncio.sleep, initial: float | None = None) -> None:
        finite = math.isfinite(capacity) and math.isfinite(refill_per_s)
        if not (finite and capacity > 0 and refill_per_s > 0):
            raise ValueError(f"capacity and refill_per_s must be finite and > 0, "
                             f"got {capacity!r}, {refill_per_s!r}")
        self.capacity = float(capacity)
        self.refill_per_s = float(refill_per_s)
        self._clock = clock
        self._sleep = sleep
        self._tokens = self.capacity if initial is None else min(float(initial), self.capacity)
        self._t_ns = clock.now_ns()
        self._lock = asyncio.Lock()
        self.total_wait_s = 0.0          # сумарний час очікування (метрика для звіту/health)
        self.acquired_weight = 0

    # ------------------------------------------------------------ стан

    def _refill(self) -> None:
        now = self._clock.now_ns()
        if now > self._t_ns:
            gained = (now - self._t_ns) * self.refill_per_s / NS_PER_S
            self._tokens = min(self.capacity, self._tokens + gained)
            self._t_ns = now

    @property
    def tokens(self) -> float:
        self._refill()
        return self._tokens

    def max_admitted(self, window_s: float) -> float:
        """Верхня межа ваги, яку відро може пропустити за будь-яке вікно довжиною window_s."""
        return self.capacity + self.refill_per_s * window_s

    # ------------------------------------------------------------ споживання

    def _check_weight(self, weight: int) -> None:
        if weight <= 0:
            raise ValueError(f"weight must be > 0, got {weight}")
        if weight > self.capacity:
            raise ValueError(f"weight {weight} exceeds bucket capacity {self.capacity}: never admissible")

    def wait_time(self, weight: int) -> float:
        """Скільки секунд чекати, доки вага стане доступною (0 — доступна зараз)."""
        self._check_weight(weight)
        deficit = weight - self.tokens
        return 0.0 if deficit <= _EPS else deficit / self.refill_per_s

    def try_acquire(self, weight: int) -> bool:
        self._check_weight(weight)
        self._refill()
        if self._tokens + _EPS >= weight:
            self._tokens = max(0.0, self._tokens - weight)
            self.acquired_weight += weight
            return True
        return False

    async def acquire(self, weight: int) -> float:
        """Блокує (через ін'єктований sleep), доки не набереться `weight`; повертає час очікування, с."""
        self._check_weight(weight)
        waited = 0.0
        async with self._lock:
            while not self.try_acquire(weight):
                # округлення вгору до наносекунди: після сну токенів гарантовано досить
                delay = math.ceil(self.wait_time(weight) * NS_PER_S) / NS_PER_S
                await self._sleep(delay)
                waited += delay
        self.total_wait_s += waited
        return waited

    # ------------------------------------------------------------ зворотний зв'язок від біржі

    def observe_used_weight(self, used: int, limit: int = BINANCE_WEIGHT_LIMIT_1M) -> None:
        """Ресинхронізація за X-MBX-USED-WEIGHT-1M: біржа бачить `used` у поточному вікні (враховує й
        інших клієнтів з тієї ж IP). Лише ЗМЕНШУЄ токени — заголовок ніколи не дає права на більше."""
        self._refill()
        self._tokens = max(0.0, min(self._tokens, float(limit - used)))

    def drain(self) -> None:
        """Після 429/418: біржа вже вважає бюджет вичерпаним."""
        self._refill()
        self._tokens = 0.0


def binance_request_bucket(clock: Clock, *, limit_per_min: int = BINANCE_WEIGHT_LIMIT_1M,
                           sleep: SleepFn = asyncio.sleep) -> TokenBucket:
    """Відро, що доказово не перевищує limit_per_min у будь-якому 60-секундному вікні: C + 60·r = L."""
    capacity = limit_per_min / 2
    return TokenBucket(capacity, capacity / 60.0, clock, sleep=sleep)


def kraken_public_bucket(clock: Clock, *, sleep: SleepFn = asyncio.sleep) -> TokenBucket:
    """Консервативне відро для публічних ендпоінтів Kraken: 1 запит/с без сплеску (власний вибір)."""
    return TokenBucket(1.0, 1.0, clock, sleep=sleep)
