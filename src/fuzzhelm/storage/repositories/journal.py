"""Репозиторій журналу подій з ланцюгом хешів (append-only).

Найменування: storage/repositories/journal.py
Призначення: зберегти JournalEntry з core.journal у event_journal, прочитати ланцюг назад і
перевірити його (core.journal.verify_chain) — доказ, що записане ніхто не переписав.
Автор: Андрій Жук, 2026.

Payload пишеться в JSONB у канонічній формі `core.digest.to_canonical` (Decimal → рядок без
експоненти, UUID → рядок, Enum → значення). Хеш запису рахувався саме над canonical_json(payload),
а to_canonical ідемпотентна, тому хеш, перерахований з прочитаного payload, збігається з записаним.
Репозиторій лише дописує (INSERT без ON CONFLICT): повтор seq — помилка унікальності, а роль
fuzzhelm_app на рівні СУБД не має UPDATE/DELETE на цю таблицю (ревізія 0003_auth_audit).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import func, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from fuzzhelm.core.digest import to_canonical
from fuzzhelm.core.journal import GENESIS, EventJournal, JournalEntry, verify_chain
from fuzzhelm.storage.models import EventJournalModel, table_of
from fuzzhelm.storage.repositories.common import BufferedSink, chunks

_T = table_of(EventJournalModel)
INSERT_CHUNK = 2_000


@dataclass(frozen=True, slots=True)
class ChainHead:
    """Куди дописувати далі: next_seq і хеш останнього запису (GENESIS для порожнього ланцюга)."""

    run_id: UUID
    next_seq: int
    head: bytes


def entry_row(e: JournalEntry) -> dict[str, Any]:
    return {
        "run_id": e.run_id, "seq": e.seq, "ts_event_ns": e.ts_event_ns, "ts_ingest_ns": e.ts_ingest_ns,
        "kind": e.kind, "payload": to_canonical(e.payload), "prev_hash": e.prev_hash, "hash": e.hash,
    }


def row_entry(m: Any) -> JournalEntry:
    return JournalEntry(
        run_id=m["run_id"], seq=int(m["seq"]), ts_event_ns=int(m["ts_event_ns"]),
        ts_ingest_ns=int(m["ts_ingest_ns"]), kind=m["kind"], payload=m["payload"],
        prev_hash=bytes(m["prev_hash"]), hash=bytes(m["hash"]),
    )


class JournalRepo:
    def __init__(self, session: AsyncSession) -> None:
        self.s = session

    async def append(self, entry: JournalEntry) -> None:
        await self.s.execute(insert(_T).values(**entry_row(entry)))

    async def append_many(self, entries: Sequence[JournalEntry]) -> int:
        """Дописати пачку (напр. `BufferedSink.drain()`); повертає кількість записів."""
        for part in chunks(entries, INSERT_CHUNK):
            await self.s.execute(insert(_T), [entry_row(e) for e in part])
        return len(entries)

    async def read_chain(self, run_id: UUID, *, from_seq: int = 0,
                         limit: int | None = None) -> list[JournalEntry]:
        q = select(_T).where(_T.c.run_id == run_id, _T.c.seq >= from_seq).order_by(_T.c.seq)
        if limit is not None:
            q = q.limit(limit)
        res = await self.s.execute(q)
        return [row_entry(m) for m in res.mappings()]

    async def verify(self, run_id: UUID) -> int | None:
        """seq першого зіпсованого запису або None (ланцюг цілий). Порожній журнал — цілий.

        Пропуск seq теж виявляється: запис після дірки має prev_hash, що не збігається з попереднім.
        """
        entries = await self.read_chain(run_id)
        for expected_seq, e in enumerate(entries):
            if e.seq != expected_seq:
                return expected_seq
        return verify_chain(entries, GENESIS)

    async def head(self, run_id: UUID) -> ChainHead:
        """Стан для продовження журналу після рестарту (head, next_seq для EventJournal)."""
        res = await self.s.execute(
            select(_T.c.seq, _T.c.hash).where(_T.c.run_id == run_id).order_by(_T.c.seq.desc()).limit(1))
        row = res.one_or_none()
        if row is None:
            return ChainHead(run_id, 0, GENESIS)
        return ChainHead(run_id, int(row[0]) + 1, bytes(row[1]))

    async def resume(self, run_id: UUID, sink: BufferedSink[JournalEntry] | None = None) -> EventJournal:
        """EventJournal, що продовжує збережений ланцюг (keep=False: записи йдуть лише в sink)."""
        h = await self.head(run_id)
        return EventJournal(run_id, sink=sink, head=h.head, next_seq=h.next_seq, keep=sink is None)

    async def count(self, run_id: UUID | None = None) -> int:
        q = select(func.count()).select_from(_T)
        if run_id is not None:
            q = q.where(_T.c.run_id == run_id)
        return int((await self.s.execute(q)).scalar_one())

    async def kinds(self, run_id: UUID) -> dict[str, int]:
        res = await self.s.execute(
            select(_T.c.kind, func.count()).where(_T.c.run_id == run_id).group_by(_T.c.kind))
        return {k: int(n) for k, n in res.all()}
