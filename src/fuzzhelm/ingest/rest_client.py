"""Асинхронний REST-клієнт публічних (read-only) ендпоінтів Binance USDⓈ-M Futures.

Найменування: ingest/rest_client.py
Призначення: klines (добір історії), exchangeInfo (tick/step/minNotional), premiumIndex (mark/funding),
оцінка зсуву годинника. Кожен запит: token bucket (вага) → HTTP → ресинхронізація ваги за заголовком
→ класифікація відповіді → повтор за RetryPolicy.
Автор: Андрій Жук, 2026.

Базова URL-адреса перевіряється allowlist-ом read-only хостів (fuzzhelm.config.assert_readonly_url):
ордерних ендпоінтів тут немає за побудовою. JSON розбирається orjson; сирі відповіді повертаються
без змін — перетворення в DTO робить ingest.normalize.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Final

import httpx
import orjson

from fuzzhelm.config import assert_readonly_url
from fuzzhelm.core.errors import NormalizationError
from fuzzhelm.core.ports import Clock
from fuzzhelm.ingest.ratelimit import (
    BINANCE_WEIGHT_LIMIT_1M,
    USED_WEIGHT_HEADER,
    SleepFn,
    TokenBucket,
    request_weight,
)
from fuzzhelm.ingest.retry import NonRetryableHttpError, RetryPolicy, error_for_response, header_int

NS_PER_MS: Final = 1_000_000
KLINES: Final = "/fapi/v1/klines"
EXCHANGE_INFO: Final = "/fapi/v1/exchangeInfo"
PREMIUM_INDEX: Final = "/fapi/v1/premiumIndex"
TIME: Final = "/fapi/v1/time"
MAX_KLINES_LIMIT: Final = 1500


class BinanceApiError(NonRetryableHttpError):
    """Помилка Binance у форматі {"code": -1121, "msg": "Invalid symbol."}."""

    def __init__(self, status: int, code: int | None, msg: str, body: str) -> None:
        super().__init__(status, body, f"Binance HTTP {status} code={code}: {msg}")
        self.code = code
        self.msg = msg


@dataclass(frozen=True, slots=True)
class ClockSkewSample:
    """Один замір NTP-стилю: offset = server − (t_send + t_recv)/2; похибка ≤ rtt/2."""

    server_time_ms: int
    local_send_ns: int
    local_recv_ns: int

    @property
    def rtt_ms(self) -> float:
        return (self.local_recv_ns - self.local_send_ns) / NS_PER_MS

    @property
    def offset_ms(self) -> float:
        mid_ms = (self.local_send_ns + self.local_recv_ns) / (2 * NS_PER_MS)
        return self.server_time_ms - mid_ms


def _default_clock() -> Clock:
    # live-годинник живе поза межею детермінізму; ingest — пакет-межа, тож імпорт тут дозволений
    from fuzzhelm.infra.wallclock import SystemClock  # noqa: PLC0415

    return SystemClock()


def _decode(resp: httpx.Response, venue: str) -> Any:
    try:
        return orjson.loads(resp.content)
    except orjson.JSONDecodeError as e:
        raise NormalizationError(f"invalid JSON from {resp.request.url}: {e}", field="$body",
                                 venue=venue) from e


class BinanceRestClient:
    def __init__(self, base_url: str, http: httpx.AsyncClient, bucket: TokenBucket, retry: RetryPolicy, *,
                 clock: Clock | None = None, sleep: SleepFn = asyncio.sleep,
                 weight_limit_per_min: int = BINANCE_WEIGHT_LIMIT_1M) -> None:
        self.base_url = assert_readonly_url(base_url).rstrip("/")
        self.http = http
        self.bucket = bucket
        self.retry = retry
        self.clock = clock if clock is not None else _default_clock()
        self._sleep = sleep
        self.weight_limit_per_min = weight_limit_per_min
        self.last_used_weight: int | None = None
        self.requests_sent = 0

    # ------------------------------------------------------------ транспорт

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        params = {k: v for k, v in (params or {}).items() if v is not None}
        weight = request_weight(path, params)
        url = self.base_url + path

        async def attempt() -> Any:
            await self.bucket.acquire(weight)
            self.requests_sent += 1
            resp = await self.http.get(url, params=params)
            used = header_int(resp.headers, USED_WEIGHT_HEADER)
            if used is not None:
                self.last_used_weight = used
                self.bucket.observe_used_weight(used, self.weight_limit_per_min)
            if resp.status_code in (418, 429):
                self.bucket.drain()
            if resp.status_code >= 400:
                retryable = error_for_response(resp, self.clock.now_ns())
                if retryable is not None:
                    raise retryable
                raise self._api_error(resp)
            return _decode(resp, "BINANCE_USDM")

        return await self.retry.execute(attempt, sleep=self._sleep)

    @staticmethod
    def _api_error(resp: httpx.Response) -> BinanceApiError:
        text = resp.text
        try:
            body = orjson.loads(resp.content)
        except orjson.JSONDecodeError:
            body = None
        if isinstance(body, dict) and "msg" in body:
            code = body.get("code")
            return BinanceApiError(resp.status_code, code if isinstance(code, int) else None,
                                   str(body["msg"]), text)
        return BinanceApiError(resp.status_code, None, text[:200], text)

    # ------------------------------------------------------------ ендпоінти

    async def klines(self, symbol: str, interval: str = "1m", start_ms: int | None = None,
                     end_ms: int | None = None, limit: int = MAX_KLINES_LIMIT) -> list[list[Any]]:
        """Сирі рядки klines (openTime ≥ start_ms, openTime ≤ end_ms, не більше limit)."""
        if not 1 <= limit <= MAX_KLINES_LIMIT:
            raise ValueError(f"limit must be in [1, {MAX_KLINES_LIMIT}]")
        data = await self._get(KLINES, {"symbol": symbol, "interval": interval, "startTime": start_ms,
                                        "endTime": end_ms, "limit": limit})
        if not isinstance(data, list):
            raise NormalizationError("klines response is not a JSON array", field="$", venue="BINANCE_USDM")
        return data

    async def exchange_info(self) -> dict[str, Any]:
        data = await self._get(EXCHANGE_INFO)
        if not isinstance(data, dict):
            raise NormalizationError("exchangeInfo is not a JSON object", field="$", venue="BINANCE_USDM")
        return data

    async def premium_index(self, symbol: str) -> dict[str, Any]:
        data = await self._get(PREMIUM_INDEX, {"symbol": symbol})
        if not isinstance(data, dict):
            raise NormalizationError("premiumIndex is not a JSON object", field="$", venue="BINANCE_USDM")
        return data

    async def server_time_ms(self) -> int:
        data = await self._get(TIME)
        if not isinstance(data, dict) or type(data.get("serverTime")) is not int:
            raise NormalizationError("bad /time payload", field="serverTime", venue="BINANCE_USDM")
        return int(data["serverTime"])

    async def server_time_sample(self) -> ClockSkewSample:
        t0 = self.clock.now_ns()
        server_ms = await self.server_time_ms()
        t1 = self.clock.now_ns()
        return ClockSkewSample(server_ms, t0, t1)

    async def server_time_offset_ms(self, samples: int = 3) -> int:
        """Оцінка зсуву годинника (сервер − локальний), мс: замір із найменшим RTT (найточніший, як в NTP)."""
        if samples < 1:
            raise ValueError("samples must be >= 1")
        best = min([await self.server_time_sample() for _ in range(samples)], key=lambda s: s.rtt_ms)
        return round(best.offset_ms)
