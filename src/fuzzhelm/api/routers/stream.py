"""Маршрут потоку подій: GET /stream/live (Server-Sent Events) для LiveView.

Найменування: api/routers/stream.py
Призначення: браузер отримує події воркерів (рішення, ризик, ордери, здоров'я конвеєра, аудит) без
опитування. Джерело — LiveHub, який годує PostgreSQL LISTEN fuzzhelm_live (api/live.py). Підтримано
відновлення після розриву за заголовком Last-Event-ID (кільцевий буфер хабу) і фільтр типів подій.
Автор: Андрій Жук, 2026.

Автентифікація — лише заголовок Authorization: Bearer (як у решти API). Токен у query-рядку
(?access_token=…) свідомо не підтримано: він потрапляє в журнали доступу проксі/uvicorn (docs/security.md).
Браузерний клієнт використовує fetch + ReadableStream (або бібліотеку fetch-event-source), а не EventSource.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

import orjson
from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.sse import EventSourceResponse, ServerSentEvent

from fuzzhelm.api.auth import Permission, Principal
from fuzzhelm.api.deps import ServicesDep, require

router = APIRouter(prefix="/stream", tags=["stream"])

RETRY_MS = 3_000


def _parse_last_event_id(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        n = int(value.strip())
    except ValueError:
        return None
    return n if n >= 0 else None


@router.get(
    "/live",
    response_class=EventSourceResponse,
    summary="Live event stream (SSE)",
    description="Server-Sent Events: `event` is the kind (`decision`, `risk`, `order`, `health`, `run`, "
    "`strategy`, `audit`, …), `id` a per-process sequence number, `data` a JSON object. Events come "
    "from PostgreSQL `LISTEN fuzzhelm_live` (workers publish with `pg_notify` inside their "
    "transactions, so only committed facts are streamed). Send `Last-Event-ID` to resume after a "
    "reconnect (the last 512 events are buffered); `kinds` filters event types; `max_events` closes "
    "the stream after N events. Keep-alive comments are sent every 15 s.",
)
async def live(
    request: Request,
    services: ServicesDep,
    _: Annotated[Principal, Depends(require(Permission.STREAM_READ))],
    kinds: Annotated[list[str] | None, Query(max_length=16, description="Event kinds to receive.")] = None,
    max_events: Annotated[int | None, Query(ge=1, le=100_000)] = None,
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
) -> AsyncIterator[ServerSentEvent]:
    services.ensure_live()
    sent = 0
    yield ServerSentEvent(comment="fuzzhelm live stream", retry=RETRY_MS)
    async with services.live.subscribe(kinds, last_event_id=_parse_last_event_id(last_event_id)) as events:
        async for ev in events:
            if await request.is_disconnected():
                break
            # компактний JSON через orjson: NaN → null (json.dumps дав би невалідний «NaN»)
            data = orjson.dumps(dict(ev.payload), option=orjson.OPT_NON_STR_KEYS).decode()
            yield ServerSentEvent(event=ev.kind, id=str(ev.seq), raw_data=data)
            sent += 1
            if max_events is not None and sent >= max_events:
                break
