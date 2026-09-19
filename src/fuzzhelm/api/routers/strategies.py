"""Маршрути стратегій — правила як дані (брифінг §7): CRUD з валідацією YAML і версіонуванням.

Найменування: api/routers/strategies.py
Призначення: оператор змінює базу правил і МФ через UI, а не програміст комітом. Кожна зміна — НОВА
незмінна версія (на старі посилаються паспорти прогонів); ідентичний набір текстів не дублюється (409).
Невалідний YAML → 422 з точним шляхом до поля (`rules[3].if.T`), тими самими завантажувачами, що й у рушія.
Кожна зміна пишеться в audit_log зі станом до/після.
Автор: Андрій Жук, 2026.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, status
from fastapi.responses import JSONResponse

from fuzzhelm.api.auth import Permission, Principal
from fuzzhelm.api.deps import ServicesDep, client_ip, require
from fuzzhelm.api.live import LIVE_CHANNEL
from fuzzhelm.api.schemas import (
    INT32_MAX,
    ErrorResponse,
    StrategyDetailOut,
    StrategyIn,
    StrategySummaryOut,
    StrategyUpdateIn,
    StrategyVersionOut,
)
from fuzzhelm.api.validation import StrategyValidationError, validate_strategy_texts
from fuzzhelm.api.views import strategy_detail, strategy_meta
from fuzzhelm.storage.repositories import StrategyConflictError, StrategyRow

router = APIRouter(prefix="/strategies", tags=["strategies"])

NamePath = Annotated[str, Path(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")]
_ERRORS: dict[int | str, dict[str, Any]] = {
    409: {"model": ErrorResponse, "description": "Identical rule set already stored / name conflict."},
    422: {"model": ErrorResponse, "description": "Invalid YAML; `detail[0].path` is the precise field path."},
}


def validation_response(err: StrategyValidationError) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, content={"detail": [err.as_item()]}
    )


def _conflict(e: StrategyConflictError) -> HTTPException:
    return HTTPException(
        status.HTTP_409_CONFLICT,
        detail=f"identical rules already stored as {e.name!r} v{e.version} (id={e.existing_id})",
    )


@router.get(
    "",
    response_model=list[StrategySummaryOut],
    summary="List strategies",
    description="Strategy names with the number of versions, latest and active version.",
)
async def list_strategies(
    services: ServicesDep,
    _: Annotated[Principal, Depends(require(Permission.STRATEGY_READ))],
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    async with services.uow() as repos:
        for name in await repos.strategies.list_names():
            versions = await repos.strategies.list_versions(name)
            active = next((v.version for v in versions if v.is_active), None)
            out.append(
                {
                    "name": name,
                    "versions": len(versions),
                    "latest_version": max(v.version for v in versions),
                    "active_version": active,
                }
            )
    return out


@router.get(
    "/{name}",
    response_model=list[StrategyVersionOut],
    summary="Versions of a strategy",
    description="All immutable versions of the strategy (metadata only).",
    responses={404: {"model": ErrorResponse}},
)
async def list_versions(
    name: NamePath,
    services: ServicesDep,
    _: Annotated[Principal, Depends(require(Permission.STRATEGY_READ))],
) -> list[dict[str, Any]]:
    async with services.uow() as repos:
        versions = await repos.strategies.list_versions(name)
    if not versions:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="strategy not found")
    return [strategy_meta(v) for v in versions]


@router.get(
    "/{name}/versions/{version}",
    response_model=StrategyDetailOut,
    summary="One strategy version with YAML texts",
    description="Full rule base and membership YAML of one immutable version (as stored, verbatim).",
    responses={404: {"model": ErrorResponse}},
)
async def get_version(
    name: NamePath,
    version: Annotated[int, Path(ge=1, le=INT32_MAX)],
    services: ServicesDep,
    _: Annotated[Principal, Depends(require(Permission.STRATEGY_READ))],
) -> dict[str, Any]:
    async with services.uow() as repos:
        row = await repos.strategies.get_version(name, version)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="strategy version not found")
    return strategy_detail(row)


async def _store_version(
    request: Request,
    services: ServicesDep,
    principal: Principal,
    *,
    name: str,
    rules_yaml: str,
    membership_yaml: str,
    activate: bool,
    must_exist: bool,
) -> dict[str, Any]:
    validated = validate_strategy_texts(rules_yaml, membership_yaml)
    n_rules = len(validated.rulebase)
    async with services.uow() as repos:
        previous: StrategyRow | None = await repos.strategies.latest(name)
        if must_exist and previous is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="strategy not found; use POST /strategies")
        if not must_exist and previous is not None:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail=f"strategy {name!r} exists; use PUT /strategies/{name} for a new version",
            )
        active_before = await repos.strategies.get_active(name)
        try:
            row = await repos.strategies.create_version(
                name, rules_yaml, membership_yaml, created_by=principal.login, activate=activate
            )
        except StrategyConflictError as e:
            raise _conflict(e) from e
        after = strategy_meta(row, n_rules)
        before = (
            None
            if previous is None
            else {
                "latest": strategy_meta(previous),
                "active_version": None if active_before is None else active_before.version,
            }
        )
        await repos.audit.append(
            "strategy.create" if not must_exist else "strategy.update",
            f"strategy/{name}",
            before=before,
            after=after,
            user_id=principal.uid,
            ip=client_ip(request),
        )
        await repos.notify(
            LIVE_CHANNEL,
            "strategy",
            {
                "name": name,
                "version": row.version,
                "id": row.id,
                "active": bool(row.is_active),
                "actor": principal.login,
            },
        )
    return {**strategy_detail(row), "n_rules": n_rules}


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=StrategyDetailOut,
    responses=_ERRORS,
    summary="Create a strategy (version 1)",
    description="Validates `membership_yaml` and `rules_yaml` with the production loaders (exactly 45 rules, "
    "every antecedent combination once, all terms defined, all weights 1.0) and stores version 1. "
    "YAML anchors/aliases are rejected. On error: 422 with the precise path, e.g. "
    '`{"detail": [{"loc": ["body", "rules_yaml"], "path": "rules[3].if.T", ...}]}`.',
)
async def create_strategy(
    body: StrategyIn,
    request: Request,
    services: ServicesDep,
    principal: Annotated[Principal, Depends(require(Permission.STRATEGY_WRITE))],
) -> Any:
    try:
        return await _store_version(
            request,
            services,
            principal,
            name=body.name,
            rules_yaml=body.rules_yaml,
            membership_yaml=body.membership_yaml,
            activate=body.activate,
            must_exist=False,
        )
    except StrategyValidationError as e:
        return validation_response(e)


@router.put(
    "/{name}",
    response_model=StrategyDetailOut,
    responses={404: {"model": ErrorResponse}, **_ERRORS},
    summary="Create a new version of a strategy",
    description="Same validation as POST. Existing versions are never modified: the change becomes version "
    "`max(version) + 1`; `activate: true` makes it the active one.",
)
async def update_strategy(
    name: NamePath,
    body: StrategyUpdateIn,
    request: Request,
    services: ServicesDep,
    principal: Annotated[Principal, Depends(require(Permission.STRATEGY_WRITE))],
) -> Any:
    try:
        return await _store_version(
            request,
            services,
            principal,
            name=name,
            rules_yaml=body.rules_yaml,
            membership_yaml=body.membership_yaml,
            activate=body.activate,
            must_exist=True,
        )
    except StrategyValidationError as e:
        return validation_response(e)


@router.post(
    "/{name}/activate",
    response_model=StrategyVersionOut,
    responses={404: {"model": ErrorResponse}},
    summary="Activate a strategy version",
    description="Makes `version` the active one (other versions of the same name become inactive).",
)
async def activate_version(
    name: NamePath,
    request: Request,
    services: ServicesDep,
    principal: Annotated[Principal, Depends(require(Permission.STRATEGY_WRITE))],
    version: Annotated[int, Query(ge=1, le=INT32_MAX)],
) -> dict[str, Any]:
    async with services.uow() as repos:
        row = await repos.strategies.get_version(name, version)
        if row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="strategy version not found")
        before = await repos.strategies.get_active(name)
        activated = await repos.strategies.activate(row.id)
        await repos.audit.append(
            "strategy.activate",
            f"strategy/{name}",
            before={"active_version": None if before is None else before.version},
            after={"active_version": activated.version, "id": activated.id},
            user_id=principal.uid,
            ip=client_ip(request),
        )
        await repos.notify(
            LIVE_CHANNEL,
            "strategy",
            {
                "name": name,
                "version": activated.version,
                "id": activated.id,
                "active": True,
                "actor": principal.login,
            },
        )
    return strategy_meta(activated)
