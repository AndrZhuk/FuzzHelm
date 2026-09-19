"""Маршрут запуску бектесту: POST /backtests → 202 і run_id; прогін іде у фоні.

Найменування: api/routers/backtests.py
Призначення: інтерфейс BacktestView «запуск прогону». Сам прогін виконує ін'єктований BacktestService
(типово — рушій fuzzhelm.backtest.engine у потоці), стан — GET /runs/{run_id}. Запуск — зміна стану
системи (новий прогін у БД), тому він пишеться в audit_log.
Автор: Андрій Жук, 2026.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status

from fuzzhelm.api.auth import Permission, Principal
from fuzzhelm.api.backtests import BacktestQueueFull
from fuzzhelm.api.deps import ServicesDep, client_ip, require
from fuzzhelm.api.live import LIVE_CHANNEL
from fuzzhelm.api.schemas import BacktestAccepted, BacktestIn, ErrorResponse

router = APIRouter(prefix="/backtests", tags=["backtests"])

MAX_WINDOW_NS = 400 * 86_400 * 1_000_000_000  # > 45 днів брифінгу з запасом; більше — помилка вводу


@router.post(
    "",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=BacktestAccepted,
    summary="Submit a backtest run",
    description="Queues an event-driven backtest (bar close → fill at next open) over stored candles and "
    "returns its `run_id` immediately. Poll `GET /runs/{run_id}` for the status; the passport, "
    "metrics and equity appear there when the engine writes them. At most 8 unfinished jobs per "
    "API process (429 beyond that).",
    responses={404: {"model": ErrorResponse}, 422: {"model": ErrorResponse}, 429: {"model": ErrorResponse}},
)
async def submit_backtest(
    body: BacktestIn,
    request: Request,
    services: ServicesDep,
    principal: Annotated[Principal, Depends(require(Permission.BACKTEST_RUN))],
) -> BacktestAccepted:
    if body.ts_to_ns <= body.ts_from_ns:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail="ts_to_ns must be > ts_from_ns")
    if body.ts_to_ns - body.ts_from_ns > MAX_WINDOW_NS:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail="window longer than 400 days")
    run_id = services.ids.next_uuid()
    spec = {**body.model_dump(), "seed": body.seed if body.seed is not None else services.settings.seed}
    job = None
    try:
        async with services.uow() as repos:
            if await repos.instruments.get_by_canon(body.symbol) is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND, detail="instrument not found")
            if body.strategy_id is not None and await repos.strategies.get(body.strategy_id) is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND, detail="strategy not found")
            await repos.audit.append(
                "backtest.submit",
                f"run/{run_id}",
                before=None,
                after={"run_id": str(run_id), **spec},
                user_id=principal.uid,
                ip=client_ip(request),
            )
            await repos.notify(
                LIVE_CHANNEL, "run", {"run_id": str(run_id), "status": "PENDING", "actor": principal.login}
            )
            try:
                job = await services.backtests.submit(run_id, spec, actor=principal.login)
            except BacktestQueueFull as e:
                raise HTTPException(
                    status.HTTP_429_TOO_MANY_REQUESTS, detail=str(e), headers={"Retry-After": "60"}
                ) from e
    except BaseException:
        # COMMIT аудиту не вдався — задача без сліду в audit_log не має виконуватись
        if job is not None and job.task is not None:
            job.task.cancel()
        raise
    return BacktestAccepted(run_id=run_id, status=job.status.value, status_url=f"/runs/{run_id}")
