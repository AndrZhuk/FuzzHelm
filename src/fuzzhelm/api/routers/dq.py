"""Маршрут якості даних: GET /dq/score — погодинний Q з розкладкою чотирьох компонент і їх вагами.

Найменування: api/routers/dq.py
Призначення: DqPanel веб-панелі показує Q = Σ wᵢ·компонентаᵢ; ваги (config/dq_weights.yaml)
віддаються разом із рядками, щоб розкладку можна було перевірити.
Автор: Андрій Жук, 2026.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status

from fuzzhelm.api.auth import Permission, Principal
from fuzzhelm.api.deps import ServicesDep, require
from fuzzhelm.api.schemas import INT32_MAX, INT64_MAX, DqScoreOut, ErrorResponse
from fuzzhelm.api.services import resolve_instrument
from fuzzhelm.api.views import dq_out
from fuzzhelm.quality.dq_score import CRITERIA, load_dq_weights

router = APIRouter(prefix="/dq", tags=["data quality"])

NS_PER_HOUR = 3_600_000_000_000
DEFAULT_WINDOW_HOURS = 48


@router.get(
    "/score",
    response_model=DqScoreOut,
    summary="Hourly data-quality score Q",
    description="Hourly Q ∈ [0, 1] with completeness, validity, timeliness and continuity for one "
    "instrument. Without bounds the last 48 hours are returned. `weights` are the weights used for Q.",
    responses={404: {"model": ErrorResponse}},
)
async def score(
    services: ServicesDep,
    _: Annotated[Principal, Depends(require(Permission.MARKET_READ))],
    symbol: Annotated[str, Query(max_length=32)] = "BTC-USDT-PERP",
    instrument_id: Annotated[int | None, Query(ge=1, le=INT32_MAX)] = None,
    from_ns: Annotated[int | None, Query(ge=0, le=INT64_MAX, description="Inclusive, ns UTC.")] = None,
    to_ns: Annotated[int | None, Query(ge=0, le=INT64_MAX, description="Exclusive, ns UTC.")] = None,
) -> DqScoreOut:
    if from_ns is None and to_ns is None:
        now = services.clock.now_ns()
        from_ns = now - DEFAULT_WINDOW_HOURS * NS_PER_HOUR
    async with services.uow() as repos:
        inst = await resolve_instrument(repos, symbol, instrument_id)
        if inst is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="instrument not found")
        rows = await repos.dq.range(inst.id, from_ns, to_ns)
    weights = dict(zip(CRITERIA, load_dq_weights(services.settings.config_dir), strict=True))
    return DqScoreOut(
        symbol=inst.symbol_canon, instrument_id=inst.id, weights=weights, items=[dq_out(r) for r in rows]
    )
