"""Маршрути результатів прогонів: GET /runs, /runs/{id}, /runs/{id}/metrics, /runs/{id}/equity.

Найменування: api/routers/runs.py
Призначення: паспорт відтворюваності (config_hash, dataset_hash, git_sha, seed, equity_hash), 17 метрик
і крива капіталу з просадкою і режимами ризику. Для щойно поставлених у чергу бектестів, яких ще немає
в БД, віддається стан задачі з черги процесу (PENDING/RUNNING/FAILED з причиною).
Автор: Андрій Жук, 2026.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status

from fuzzhelm.api.auth import Permission, Principal
from fuzzhelm.api.deps import ServicesDep, require
from fuzzhelm.api.schemas import INT64_MAX, EquityOut, ErrorResponse, MetricsOut, RunOut
from fuzzhelm.api.views import equity_out, finite_metrics, run_out

router = APIRouter(prefix="/runs", tags=["runs"])

RunRead = Annotated[Principal, Depends(require(Permission.RUN_READ))]


@router.get(
    "",
    response_model=list[RunOut],
    summary="List runs",
    description="Most recent runs first; filter by kind and status. `config` is omitted here.",
)
async def list_runs(
    services: ServicesDep,
    _: RunRead,
    kind: Annotated[Literal["backtest", "paper", "replay", "grid_cell", "testnet"] | None, Query()] = None,
    run_status: Annotated[Literal["RUNNING", "DONE", "FAILED"] | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[dict[str, Any]]:
    async with services.uow() as repos:
        rows = await repos.runs.list(kind=kind, status=run_status, limit=limit)
    return [run_out(r, with_config=False) for r in rows]


@router.get(
    "/{run_id}",
    response_model=RunOut,
    summary="Run passport",
    description="Reproducibility passport of a run. For a backtest submitted through POST /backtests "
    "that the engine has not written yet, the in-process job state is returned in `job`.",
    responses={404: {"model": ErrorResponse}},
)
async def get_run(run_id: UUID, services: ServicesDep, _: RunRead) -> dict[str, Any]:
    async with services.uow() as repos:
        row = await repos.runs.get(run_id)
    job = services.backtests.job(run_id)
    if row is None and job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="run not found")
    if row is None:
        assert job is not None
        return {
            "id": run_id,
            "kind": "backtest",
            "status": job.status.value,
            "engine": job.spec.get("engine"),
            "strategy_id": job.spec.get("strategy_id"),
            "instrument_id": None,
            "tf": job.spec.get("tf"),
            "ts_from_ns": job.spec.get("ts_from_ns"),
            "ts_to_ns": job.spec.get("ts_to_ns"),
            "seed": job.spec.get("seed"),
            "git_sha": None,
            "config_hash": None,
            "dataset_hash": None,
            "journal_head_hash": None,
            "equity_hash": None,
            "error": job.error,
            "started_at_ns": None,
            "finished_at_ns": None,
            "config": None,
            "job": job.to_dict(),
        }
    return {**run_out(row), "job": None if job is None else job.to_dict()}


@router.get(
    "/{run_id}/metrics",
    response_model=MetricsOut,
    summary="Run metrics",
    description="The 17 backtest metrics (+PSR/DSR when stored). Non-finite values are null.",
    responses={404: {"model": ErrorResponse}},
)
async def get_metrics(run_id: UUID, services: ServicesDep, _: RunRead) -> dict[str, Any]:
    async with services.uow() as repos:
        if await repos.runs.get(run_id) is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="run not found")
        metrics = await repos.runs.get_metrics(run_id)
    return {"run_id": run_id, "metrics": finite_metrics(metrics)}


@router.get(
    "/{run_id}/equity",
    response_model=EquityOut,
    summary="Equity curve",
    description="Equity points with drawdown, risk state and κ. With `max_points` the curve is "
    "decimated by a constant stride (the last point is always kept).",
    responses={404: {"model": ErrorResponse}},
)
async def get_equity(
    run_id: UUID,
    services: ServicesDep,
    _: RunRead,
    from_ns: Annotated[int | None, Query(ge=0, le=INT64_MAX)] = None,
    to_ns: Annotated[int | None, Query(ge=0, le=INT64_MAX)] = None,
    max_points: Annotated[int, Query(ge=2, le=100_000)] = 5_000,
) -> dict[str, Any]:
    async with services.uow() as repos:
        if await repos.runs.get(run_id) is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="run not found")
        rows = await repos.equity.curve(run_id, from_ns, to_ns)
    n = len(rows)
    stride = max(1, -(-n // max_points))
    picked = rows[::stride]
    if rows and picked[-1] is not rows[-1]:
        picked.append(rows[-1])
    return {"run_id": run_id, "n_total": n, "stride": stride, "items": [equity_out(r) for r in picked]}
