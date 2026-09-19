"""Маршрути ринкових даних: GET /market/candles (keyset-пагінація), GET /market/health.

Найменування: api/routers/market.py
Призначення: свічки з БД сторінками за open_time (стабільно при дозаписі, на відміну від OFFSET) і
стан конвеєра: лаг даних, Q, відкриті прогалини, останній знімок PipelineHealth від воркера.
Автор: Андрій Жук, 2026.
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status

from fuzzhelm.api.auth import Permission, Principal
from fuzzhelm.api.deps import ServicesDep, require
from fuzzhelm.api.schemas import CandlePageOut, ErrorResponse, HealthOut
from fuzzhelm.api.services import resolve_instrument
from fuzzhelm.api.views import candle_out, dq_out

router = APIRouter(prefix="/market", tags=["market"])

TF_NS = {"1m": 60_000_000_000}
NS_PER_S = 1_000_000_000


@router.get(
    "/candles",
    response_model=CandlePageOut,
    summary="Candles with keyset pagination",
    description="Candles of one instrument ordered by `open_time`. Pagination is keyset-based: pass "
    "`next_after_ns` from the previous page as `after_ns` (stable while new candles are appended). "
    "Prices and volumes are decimal strings (NUMERIC(38,18) precision is preserved).",
    responses={404: {"model": ErrorResponse}},
)
async def candles(
    *,
    services: ServicesDep,
    _: Annotated[Principal, Depends(require(Permission.MARKET_READ))],
    symbol: Annotated[
        str, Query(max_length=32, description="Canonical symbol, e.g. BTC-USDT-PERP.")
    ] = "BTC-USDT-PERP",
    instrument_id: Annotated[int | None, Query(description="Instrument id (overrides `symbol`).")] = None,
    tf: Annotated[Literal["1m"], Query(description="Timeframe.")] = "1m",
    from_ns: Annotated[
        int | None, Query(ge=0, description="Inclusive lower bound of open_time, ns UTC.")
    ] = None,
    to_ns: Annotated[
        int | None, Query(ge=0, description="Exclusive upper bound of open_time, ns UTC.")
    ] = None,
    after_ns: Annotated[int | None, Query(ge=0, description="Keyset cursor (`next_after_ns`).")] = None,
    limit: Annotated[int, Query(ge=1, le=5000, description="Page size.")] = 500,
    closed_only: Annotated[bool, Query(description="Only closed candles.")] = False,
    order: Annotated[Literal["asc", "desc"], Query(description="Sort order by open_time.")] = "asc",
) -> CandlePageOut:
    async with services.uow() as repos:
        inst = await resolve_instrument(repos, symbol, instrument_id)
        if inst is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="instrument not found")
        page = await repos.candles.range(
            inst.id,
            tf,
            from_ns,
            to_ns,
            after_ns=after_ns,
            limit=limit,
            closed_only=closed_only,
            descending=order == "desc",
        )
    return CandlePageOut(
        symbol=inst.symbol_canon,
        instrument_id=inst.id,
        tf=tf,
        items=[candle_out(c) for c in page.items],  # type: ignore[misc]
        next_after_ns=page.next_after_ns,
    )


@router.get(
    "/health",
    response_model=HealthOut,
    summary="Pipeline health",
    description="Per instrument: age of the last closed 1m candle, latest hourly quality score Q with its "
    "four components, open ingest gaps; gap counts by status; and the last PipelineHealth snapshot "
    "(frames, reconnects, lag p95, Q) that the ingest worker published on the live channel.",
)
async def health(
    services: ServicesDep,
    _: Annotated[Principal, Depends(require(Permission.MARKET_READ))],
) -> HealthOut:
    services.ensure_live()
    now_ns = services.clock.now_ns()
    out = []
    async with services.uow() as repos:
        instruments = await repos.instruments.list(active_only=True)
        for inst in instruments:
            last = await repos.candles.latest_open_time_ns(inst.id, "1m", closed_only=True)
            lag = None if last is None else (now_ns - (last + TF_NS["1m"])) / NS_PER_S
            dq = await repos.dq.latest(inst.id)
            gaps = await repos.gaps.list_open(inst.id)
            out.append(
                {
                    "symbol": inst.symbol_canon,
                    "instrument_id": inst.id,
                    "last_closed_open_time_ns": last,
                    "data_lag_s": lag,
                    "latest_q": None if dq is None else dq_out(dq),
                    "open_gaps": len(gaps),
                }
            )
        stats = await repos.gaps.stats()
    snap = services.live.last("health")
    return HealthOut(
        now_ns=now_ns,
        instruments=out,  # type: ignore[arg-type]
        gaps_by_status=stats,
        open_gaps_total=stats.get("OPEN", 0) + stats.get("FILLING", 0),
        pipeline=None if snap is None else {"seq": snap.seq, **snap.payload},
    )
