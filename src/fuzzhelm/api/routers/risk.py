"""Маршрути ризик-контуру: стан, журнал вердиктів, ліміти (admin) і зняття kill-switch (admin).

Найменування: api/routers/risk.py
Призначення: GET /risk/state, GET /risk/events, GET /risk/limits — читання; PUT /risk/limits — зміна
config/risk_limits.yaml (валідація risk.config, атомарний запис, audit_log before/after);
POST /risk/killswitch/release — команда воркеру зняти HALTED (audit_log + NOTIFY fuzzhelm_control +
Telegram-нотифікація, якщо налаштовано).
Автор: Андрій Жук, 2026.

Зняття HALTED робить власник автомата — торговий воркер (RiskStateMachine.release(Role.ADMIN)), а не API:
у процесі API автомата немає. API лише автентифікує адміністратора, фіксує запит в audit_log (незмінний,
REVOKE UPDATE/DELETE) і передає команду каналом fuzzhelm_control; воркер при старті дочитує пропущені
команди з audit_log (action = risk.killswitch.release, id > останнього обробленого).
"""

from __future__ import annotations

from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse

from fuzzhelm.api.auth import Permission, Principal
from fuzzhelm.api.deps import ServicesDep, client_ip, require
from fuzzhelm.api.limits import (
    LimitsConflictError,
    config_json,
    diff_paths,
    leading_comments,
    render_limits_yaml,
)
from fuzzhelm.api.live import CONTROL_CHANNEL, LIVE_CHANNEL
from fuzzhelm.api.schemas import (
    INT64_MAX,
    ErrorResponse,
    KillSwitchReleaseAccepted,
    KillSwitchReleaseIn,
    RiskEventOut,
    RiskLimitsChanged,
    RiskLimitsIn,
    RiskLimitsOut,
    RiskStateOut,
)
from fuzzhelm.api.services import resolve_live_run
from fuzzhelm.api.views import dstr, fnum, risk_event_out
from fuzzhelm.core.enums import RiskState
from fuzzhelm.core.errors import ConfigValidationError
from fuzzhelm.risk.config import load_risk_config

router = APIRouter(prefix="/risk", tags=["risk"])

RiskRead = Annotated[Principal, Depends(require(Permission.RISK_READ))]
RELEASE_ACTION = "risk.killswitch.release"
LIMITS_ACTION = "risk.limits.update"
LIMITS_TARGET = "config/risk_limits.yaml"


@router.get(
    "/state",
    response_model=RiskStateOut,
    summary="Current risk state",
    description="State of the risk state machine (NORMAL/WARNING/COOLDOWN/HALTED) of a run — by default "
    "the latest live run (paper/replay/testnet): the last `risk_state` transition, the latest "
    "equity point (drawdown), κ_mode from the current limits and the last kill-switch "
    "release request.",
)
async def risk_state(
    services: ServicesDep,
    _: RiskRead,
    run_id: Annotated[UUID | None, Query()] = None,
) -> dict[str, Any]:
    cfg = services.limits.read().config
    async with services.uow() as repos:
        run = await resolve_live_run(repos, run_id)
        if run_id is not None and run is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="run not found")
        last_tr = None
        eq = None
        if run is not None:
            transitions = await repos.risk.transitions(run.id)
            last_tr = transitions[-1] if transitions else None
            curve = await repos.equity.curve(run.id)
            eq = curve[-1] if curve else None
        releases = await repos.audit.list(action=RELEASE_ACTION, limit=1)
    if last_tr is not None and last_tr.state_to:
        state, source = last_tr.state_to, "transition"
    elif eq is not None and eq.risk_state:
        state, source = eq.risk_state, "equity_point"
    else:
        state, source = RiskState.NORMAL.value, "default"
    kappa = cfg.state_machine.kappa_mode.get(RiskState(state)) if state in RiskState.__members__ else None
    return {
        "run_id": None if run is None else run.id,
        "state": state,
        "state_source": source,
        "kappa_mode": fnum(kappa) if kappa is not None else 0.0,
        "last_transition": None if last_tr is None else risk_event_out(last_tr),
        "equity": None if eq is None else dstr(eq.equity),
        "drawdown": None if eq is None else fnum(eq.drawdown),
        "equity_ts_ns": None if eq is None else eq.ts_ns,
        "last_release_request": None
        if not releases
        else {
            "audit_id": releases[0].id,
            "ts_ns": releases[0].ts_ns,
            "user_id": releases[0].user_id,
            "after": releases[0].after_json,
        },
    }


@router.get(
    "/events",
    response_model=list[RiskEventOut],
    summary="Risk verdict journal",
    description="Every rule verdict (ALLOW/SHRINK/VETO with observed value and limit) and every state "
    "transition of a run, newest first. `only=veto` gives the rejection log; "
    "`only=transitions` the state changes. `factor` is exact even where NUMERIC(6,4) rounds.",
)
async def risk_events(
    *,
    services: ServicesDep,
    _: RiskRead,
    run_id: Annotated[UUID | None, Query()] = None,
    rule: Annotated[str | None, Query(max_length=64)] = None,
    only: Annotated[Literal["all", "veto", "transitions"], Query()] = "all",
    since_ns: Annotated[int | None, Query(ge=0, le=INT64_MAX)] = None,
    limit: Annotated[int, Query(ge=1, le=2000)] = 500,
) -> list[dict[str, Any]]:
    async with services.uow() as repos:
        run = await resolve_live_run(repos, run_id)
        if run is None:
            if run_id is not None:
                raise HTTPException(status.HTTP_404_NOT_FOUND, detail="run not found")
            return []
        if only == "veto":
            rows = await repos.risk.vetoes(run.id, limit=limit)
        elif only == "transitions":
            rows = list(reversed(await repos.risk.transitions(run.id)))[:limit]
        else:
            rows = await repos.risk.list_for_run(run.id, since_ns=since_ns, limit=limit, rule=rule)
    return [risk_event_out(r) for r in rows]


@router.get(
    "/limits",
    response_model=RiskLimitsOut,
    summary="Current risk limits",
    description="Validated content of `config/risk_limits.yaml` and its SHA-256 (optimistic lock).",
)
async def get_limits(services: ServicesDep, _: RiskRead) -> dict[str, Any]:
    snap = services.limits.read()
    return {"config": config_json(snap.config), "sha256": snap.sha256}


@router.put(
    "/limits",
    response_model=RiskLimitsChanged,
    summary="Change risk limits (admin only)",
    description="Replaces `config/risk_limits.yaml` with the validated document (same schema as the file; "
    "numbers may be JSON numbers or decimal strings). The write is atomic; `expected_sha256` from "
    "GET /risk/limits protects against lost updates (409). The change is written to `audit_log` "
    "with the full configuration before and after, the user and the IP, in the same transaction; "
    "workers are notified on channel `fuzzhelm_control` (kind `risk.limits.changed`).",
    responses={403: {"model": ErrorResponse}, 409: {"model": ErrorResponse}, 422: {"model": ErrorResponse}},
)
async def put_limits(
    body: RiskLimitsIn,
    request: Request,
    services: ServicesDep,
    principal: Annotated[Principal, Depends(require(Permission.RISK_LIMITS_WRITE))],
) -> Any:
    tree: dict[str, Any] = {"limits": body.limits, "state_machine": body.state_machine}
    for key in ("sizing", "hysteresis"):
        if getattr(body, key) is not None:
            tree[key] = getattr(body, key)
    try:
        new_cfg = load_risk_config(tree)
    except ConfigValidationError as e:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content={
                "detail": [
                    {
                        "loc": ["body", *e.path.split(".")],
                        "msg": e.detail,
                        "type": "config_validation",
                        "path": e.path,
                    }
                ]
            },
        )
    old = services.limits.read()
    if body.expected_sha256 is not None and body.expected_sha256 != old.sha256:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="risk limits changed since they were read")
    before, after = config_json(old.config), config_json(new_cfg)
    changed = diff_paths(before, after)
    new_text = render_limits_yaml(new_cfg, leading_comments(old.text))
    written = False
    try:
        async with services.uow() as repos:
            audit_id = await repos.audit.append(
                LIMITS_ACTION,
                LIMITS_TARGET,
                before=before,
                after={**after, "_changed": changed, "_sha256_before": old.sha256},
                user_id=principal.uid,
                ip=client_ip(request),
            )
            snap = services.limits.replace(new_text, expected_sha256=old.sha256)
            written = True
            await repos.notify(
                CONTROL_CHANNEL, "risk.limits.changed", {"sha256": snap.sha256, "audit_id": audit_id}
            )
            await repos.notify(
                LIVE_CHANNEL,
                "audit",
                {
                    "action": LIMITS_ACTION,
                    "audit_id": audit_id,
                    "changed": changed[:20],
                    "actor": principal.login,
                },
            )
    except LimitsConflictError as e:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(e)) from e
    except BaseException:
        # COMMIT аудиту не вдався — повертаємо файл: зміни без сліду в audit_log не лишається
        if written:
            services.limits.restore(old.text)
        raise
    return {"sha256": snap.sha256, "audit_id": audit_id, "changed": changed, "config": after}


@router.post(
    "/killswitch/release",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=KillSwitchReleaseAccepted,
    summary="Release the HALTED latch (admin only)",
    description="Records a release request in `audit_log` (who, why, observed state) and sends it to the "
    "trading worker on channel `fuzzhelm_control`; the worker applies "
    "`RiskStateMachine.release(admin)` (HALTED → COOLDOWN with re-based peak) and writes its own "
    "`risk.release` audit record with the state before/after.",
    responses={403: {"model": ErrorResponse}, 404: {"model": ErrorResponse}},
)
async def release_killswitch(
    body: KillSwitchReleaseIn,
    request: Request,
    services: ServicesDep,
    principal: Annotated[Principal, Depends(require(Permission.KILLSWITCH_RELEASE))],
) -> dict[str, Any]:
    async with services.uow() as repos:
        run = await resolve_live_run(repos, body.run_id)
        if body.run_id is not None and run is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="run not found")
        observed = None
        if run is not None:
            transitions = await repos.risk.transitions(run.id)
            observed = transitions[-1].state_to if transitions else None
        run_ref = None if run is None else str(run.id)
        audit_id = await repos.audit.append(
            RELEASE_ACTION,
            f"risk/killswitch/{run_ref or 'live'}",
            before={"state": observed, "run_id": run_ref},
            after={"command": "release", "status": "requested", "reason": body.reason, "run_id": run_ref},
            user_id=principal.uid,
            ip=client_ip(request),
        )
        await repos.notify(
            CONTROL_CHANNEL,
            "killswitch.release",
            {"audit_id": audit_id, "run_id": run_ref, "actor": principal.login, "role": principal.role.value},
        )
        await repos.notify(
            LIVE_CHANNEL,
            "audit",
            {"action": RELEASE_ACTION, "audit_id": audit_id, "run_id": run_ref, "actor": principal.login},
        )
    if services.notifier is not None and services.notifier.enabled:
        # оператору на телефон — після COMMIT, у фоні: Telegram не затримує відповідь і не валить її
        services.spawn(
            services.notifier.killswitch(
                action="release_requested", reason=body.reason, actor=principal.login, run_id=run_ref
            )
        )
    return {
        "audit_id": audit_id,
        "run_id": None if run is None else run.id,
        "observed_state": observed,
        "status": "requested",
        "channel": CONTROL_CHANNEL,
    }
