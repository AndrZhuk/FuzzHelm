"""Асинхронний REST-клієнт публічних ендпоінтів Kraken (друге незалежне джерело для крос-звірки).

Найменування: ingest/kraken_client.py
Призначення: OHLC XBTUSD (спот) для звірки з перпетуалом Binance; довідник пари (tick/lot/costmin).
Автор: Андрій Жук, 2026.

Особливість Kraken: помилки (зокрема ліміт запитів) приходять із HTTP 200 у полі `error` тіла JSON,
тому класифікація «повторювати чи ні» робиться за вмістом, а не лише за статусом.
"""

from __future__ import annotations

import asyncio
from typing import Any, Final

import httpx
import orjson

from fuzzhelm.config import assert_readonly_url
from fuzzhelm.core.errors import NormalizationError
from fuzzhelm.core.ports import Clock
from fuzzhelm.ingest.ratelimit import SleepFn, TokenBucket
from fuzzhelm.ingest.retry import (
    IngestError,
    NonRetryableHttpError,
    RetryableError,
    RetryPolicy,
    error_for_response,
)

OHLC: Final = "/0/public/OHLC"
ASSET_PAIRS: Final = "/0/public/AssetPairs"
KRAKEN_INTERVALS_MIN: Final = frozenset({1, 5, 15, 30, 60, 240, 1440, 10080, 21600})
# префікси помилок Kraken, що означають «зачекай і повтори»
RETRYABLE_ERROR_PREFIXES: Final = ("EAPI:Rate limit exceeded", "EGeneral:Too many requests",
                                   "EService:Unavailable", "EService:Busy", "EGeneral:Temporary lockout")


class KrakenApiError(IngestError):
    def __init__(self, errors: list[str]) -> None:
        super().__init__(f"Kraken API error: {errors}")
        self.errors = errors


class KrakenRestClient:
    def __init__(self, base_url: str, http: httpx.AsyncClient, retry: RetryPolicy, *,
                 bucket: TokenBucket | None = None, clock: Clock | None = None,
                 sleep: SleepFn = asyncio.sleep) -> None:
        self.base_url = assert_readonly_url(base_url).rstrip("/")
        self.http = http
        self.retry = retry
        self.bucket = bucket
        self.clock = clock
        self._sleep = sleep
        self.requests_sent = 0

    async def _public(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        url = self.base_url + path
        params = {k: v for k, v in params.items() if v is not None}

        async def attempt() -> dict[str, Any]:
            if self.bucket is not None:
                await self.bucket.acquire(1)
            self.requests_sent += 1
            resp = await self.http.get(url, params=params)
            if resp.status_code >= 400:
                now = self.clock.now_ns() if self.clock is not None else None
                retryable = error_for_response(resp, now)
                if retryable is not None:
                    raise retryable
                raise NonRetryableHttpError(resp.status_code, resp.text)
            try:
                body = orjson.loads(resp.content)
            except orjson.JSONDecodeError as e:
                raise NormalizationError(f"invalid JSON from Kraken: {e}", field="$body",
                                         venue="KRAKEN") from e
            if not isinstance(body, dict) or set(body) - {"error", "result"} or "error" not in body:
                raise NormalizationError("unexpected Kraken envelope", field="$", venue="KRAKEN")
            errors = body["error"]
            if errors:
                if any(str(err).startswith(RETRYABLE_ERROR_PREFIXES) for err in errors):
                    raise RetryableError(f"Kraken: {errors}")
                raise KrakenApiError([str(err) for err in errors])
            result = body.get("result")
            if not isinstance(result, dict):
                raise NormalizationError("Kraken result is not an object", field="result", venue="KRAKEN")
            return result

        return await self.retry.execute(attempt, sleep=self._sleep)

    async def ohlc(self, pair: str = "XBTUSD", interval: int = 1, since: int | None = None) -> dict[str, Any]:
        """Сирий `result` OHLC: {<ключ пари, напр. XXBTZUSD>: [[t,o,h,l,c,vwap,vol,count], …], "last": int}.
        Kraken віддає не більше ~720 останніх барів незалежно від `since` (с)."""
        if interval not in KRAKEN_INTERVALS_MIN:
            raise ValueError(f"unsupported Kraken interval {interval}")
        return await self._public(OHLC, {"pair": pair, "interval": interval, "since": since})

    async def asset_pairs(self, pair: str = "XBTUSD") -> dict[str, Any]:
        return await self._public(ASSET_PAIRS, {"pair": pair})
