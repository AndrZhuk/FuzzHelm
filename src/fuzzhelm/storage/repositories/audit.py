"""Репозиторій журналу аудиту дій (audit_log) зі станом до і після.

Найменування: storage/repositories/audit.py
Призначення: хто (user_id, ip), що (action), над чим (target), before_json/after_json — напр. зміна
лімітів ризику адміністратором. Лише дописування: роль fuzzhelm_app не має UPDATE/DELETE (0003).
Автор: Андрій Жук, 2026.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any, Protocol

from sqlalchemy import func, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from fuzzhelm.storage.models import AuditLogModel, table_of
from fuzzhelm.storage.repositories.common import from_mapping, ns_to_dt_opt

_T = table_of(AuditLogModel)


class AuditRecordLike(Protocol):
    """Структурний тип risk.journal.AuditRecord."""

    ts_ns: int
    action: str
    target: str
    actor_role: Any
    actor: str | None
    before: Mapping[str, Any]
    after: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class AuditRow:
    id: int
    ts_ns: int | None
    user_id: int | None
    action: str | None
    target: str | None
    before_json: dict[str, Any] | None
    after_json: dict[str, Any] | None
    ip: str | None


class AuditRepo:
    def __init__(self, session: AsyncSession) -> None:
        self.s = session

    async def append(self, action: str, target: str, *, before: Mapping[str, Any] | None = None,
                     after: Mapping[str, Any] | None = None, user_id: int | None = None,
                     ip: str | None = None, ts_ns: int | None = None) -> int:
        """Дописати подію аудиту; ts = ts_ns або now() СУБД. Decimal у before/after → рядок без експоненти."""
        res = await self.s.execute(insert(_T).values(
            ts=ns_to_dt_opt(ts_ns) if ts_ns is not None else func.now(), user_id=user_id, action=action,
            target=target, before_json=None if before is None else dict(before),
            after_json=None if after is None else dict(after), ip=ip,
        ).returning(_T.c.id))
        return int(res.scalar_one())

    async def append_record(self, rec: AuditRecordLike, *, user_id: int | None = None,
                            ip: str | None = None) -> int:
        """З risk.journal.AuditRecord: роль і логін актора додаються в after_json під ключем _actor."""
        after = dict(rec.after)
        role = getattr(rec.actor_role, "value", rec.actor_role)
        after.setdefault("_actor", {"role": role, "login": rec.actor})
        return await self.append(rec.action, rec.target, before=rec.before, after=after,
                                 user_id=user_id, ip=ip, ts_ns=rec.ts_ns)

    async def list(self, *, target: str | None = None, action: str | None = None, user_id: int | None = None,
                   limit: int = 200) -> list[AuditRow]:
        q = select(_T)
        if target is not None:
            q = q.where(_T.c.target == target)
        if action is not None:
            q = q.where(_T.c.action == action)
        if user_id is not None:
            q = q.where(_T.c.user_id == user_id)
        res = await self.s.execute(q.order_by(_T.c.id.desc()).limit(limit))
        return [_row(m) for m in res.mappings()]


def _row(m: Mapping[Any, Any]) -> AuditRow:
    # INET повертається як ipaddress.IPv4Address/IPv6Address — у рядку API/звіту потрібен текст
    row = from_mapping(AuditRow, m)
    return row if row.ip is None or isinstance(row.ip, str) else replace(row, ip=str(row.ip))
