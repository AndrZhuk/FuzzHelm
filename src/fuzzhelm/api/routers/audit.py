"""Маршрут журналу аудиту: GET /audit — лише для ролей auditor і admin.

Найменування: api/routers/audit.py
Призначення: доказова база дій (зміна лімітів, стратегій, зняття kill-switch, входи) зі станом до/після.
Додано понад §8.1 брифінгу: без нього роль auditor не мала б власного призначення (docs/security.md §2).
Журнал лише дописується (REVOKE UPDATE, DELETE для fuzzhelm_app, ревізія 0003).
Автор: Андрій Жук, 2026.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query

from fuzzhelm.api.auth import Permission, Principal
from fuzzhelm.api.deps import ServicesDep, require
from fuzzhelm.api.schemas import AuditOut
from fuzzhelm.api.views import audit_out

router = APIRouter(prefix="/audit", tags=["audit"])


@router.get(
    "",
    response_model=list[AuditOut],
    summary="Audit log (auditor, admin)",
    description="Append-only log of user actions with JSON state before/after, user id and IP; newest "
    "first. Filter by `action` (e.g. `risk.limits.update`), `target` or `user_id`.",
)
async def list_audit(
    services: ServicesDep,
    _: Annotated[Principal, Depends(require(Permission.AUDIT_READ))],
    action: Annotated[str | None, Query(max_length=64)] = None,
    target: Annotated[str | None, Query(max_length=200)] = None,
    user_id: Annotated[int | None, Query(ge=1)] = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
) -> list[dict[str, Any]]:
    async with services.uow() as repos:
        rows = await repos.audit.list(target=target, action=action, user_id=user_id, limit=limit)
    return [audit_out(r) for r in rows]
