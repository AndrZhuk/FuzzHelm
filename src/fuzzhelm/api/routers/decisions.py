"""Маршрут пояснення рішення: GET /decisions/{id}/explain — ядро демо (ExplainView).

Найменування: api/routers/decisions.py
Призначення: віддати формальне виведення рішення: МФ з поточними значеннями, спрацьовані правила з α
(за спаданням), μ_agg(u) з центроїдом, κ, розкладку сайзера, ціни стоп/тейк/ліквідації й україномовний
текст. Обчислення — api/explain.py (детермінований перерахунок тим самим MamdaniEngine).
Автор: Андрій Жук, 2026.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status

from fuzzhelm.api.auth import Permission, Principal
from fuzzhelm.api.deps import ServicesDep, require
from fuzzhelm.api.explain import ExplainError, explain_decision
from fuzzhelm.api.schemas import ErrorResponse
from fuzzhelm.config import load_yaml

router = APIRouter(prefix="/decisions", tags=["decisions"])

EXPLAIN_EXAMPLE: dict[str, Any] = {
    "decision_id": 1,
    "engine": "mamdani",
    "inputs": {"T": 0.42, "R": -0.1, "V": 0.35},
    "fired_rules": [
        {
            "rule_id": "R22",
            "alpha": 0.62,
            "consequent": "LONG",
            "consequent_uk": "ЛОНГ",
            "text_uk": "R22: ЯКЩО тренд ПОМІРНЕ_ЗРОСТАННЯ І реверсія НЕМА_ТИСКУ І волатильність "
            "ПОМІРНА ТО сигнал ЛОНГ",
        }
    ],
    "aggregate": {"grid": [-1.0, "…", 1.0], "mu": [0.0, "…", 0.0], "centroid": 0.31, "nodes": 201},
    "u_raw": 0.31,
    "kappa": 0.8,
    "u_final": 0.248,
    "narrative_uk": "Спрацювало правило R22 з α = 0.62: ЯКЩО …",
}


@router.get(
    "/{decision_id}/explain",
    summary="Formal derivation of a trading decision",
    description=(
        "Everything the ExplainView needs for one decision: consensus inputs T/R/V with the membership "
        "degree of every term (plus sampled membership curves), fired rules sorted by activation α "
        "(descending) with Ukrainian rule text, the aggregated output fuzzy set μ_agg(u) on the 201-node "
        "grid with its centroid, entropy agreement and κ, the sizer breakdown with `binding_constraint`, "
        "stop/take-profit/liquidation prices and the Ukrainian narrative. μ_agg is recomputed "
        "deterministically with the Mamdani engine of the run's strategy version; `consistency` reports "
        "whether the recomputation matches the stored trace."
    ),
    responses={
        200: {"content": {"application/json": {"example": EXPLAIN_EXAMPLE}}},
        404: {"model": ErrorResponse},
        409: {"model": ErrorResponse},
    },
)
async def explain(
    decision_id: Annotated[int, Path(ge=1)],
    services: ServicesDep,
    _: Annotated[Principal, Depends(require(Permission.DECISION_READ))],
    mf_points: Annotated[int, Query(ge=11, le=1001, description="Samples per membership curve.")] = 101,
) -> dict[str, Any]:
    async with services.uow() as repos:
        row = await repos.decisions.get(decision_id)
        if row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="decision not found")
        run = await repos.runs.get(row.run_id)
        strategy = None
        if run is not None and run.strategy_id is not None:
            strategy = await repos.strategies.get(run.strategy_id)
    cfg = (
        services.detectors_cfg
        if services.detectors_cfg is not None
        else load_yaml("detectors", services.settings.config_dir)
    )
    try:
        return explain_decision(row, run=run, strategy=strategy, detectors_cfg=cfg, mf_points=mf_points)
    except ExplainError as e:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(e)) from e
