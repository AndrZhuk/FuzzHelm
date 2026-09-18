"""Журнал подій з ланцюгом хешів BLAKE2b (append-only).

Найменування: core/journal.py
Автор: Андрій Жук, 2026.

hash_i = BLAKE2b-256( prev_hash_i ‖ canonical_json([seq, ts_event_ns, ts_ingest_ns, kind, payload]) ),
prev_hash_0 = GENESIS (32 нульові байти). Будь-яка зміна payload рве ланцюг рівно на своєму seq.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from fuzzhelm.core.digest import canonical_json
from fuzzhelm.core.errors import JournalIntegrityError

GENESIS = bytes(32)


@dataclass(frozen=True, slots=True)
class JournalEntry:
    run_id: UUID
    seq: int
    ts_event_ns: int
    ts_ingest_ns: int
    kind: str
    payload: dict[str, Any]
    prev_hash: bytes
    hash: bytes


def entry_hash(prev_hash: bytes, seq: int, ts_event_ns: int, ts_ingest_ns: int, kind: str,
               payload: dict[str, Any]) -> bytes:
    h = hashlib.blake2b(digest_size=32)
    h.update(prev_hash)
    h.update(canonical_json([seq, ts_event_ns, ts_ingest_ns, kind, payload]))
    return h.digest()


class EventJournal:
    """Append-only журнал. `sink` (напр. репозиторій БД) отримує кожен запис у момент додавання."""

    def __init__(self, run_id: UUID, sink: Callable[[JournalEntry], None] | None = None,
                 head: bytes = GENESIS, next_seq: int = 0, keep: bool = True) -> None:
        self.run_id = run_id
        self._sink = sink
        self._head = head
        self._seq = next_seq
        self._keep = keep
        self.entries: list[JournalEntry] = []

    @property
    def head(self) -> bytes:
        return self._head

    @property
    def next_seq(self) -> int:
        return self._seq

    def append(self, kind: str, payload: dict[str, Any], ts_event_ns: int, ts_ingest_ns: int) -> JournalEntry:
        h = entry_hash(self._head, self._seq, ts_event_ns, ts_ingest_ns, kind, payload)
        e = JournalEntry(self.run_id, self._seq, ts_event_ns, ts_ingest_ns, kind, payload, self._head, h)
        self._head = h
        self._seq += 1
        if self._keep:
            self.entries.append(e)
        if self._sink is not None:
            self._sink(e)
        return e


def verify_chain(entries: Iterable[JournalEntry], genesis: bytes = GENESIS) -> int | None:
    """Повертає seq першого зіпсованого запису або None, якщо ланцюг цілий."""
    prev = genesis
    for e in entries:
        expected = entry_hash(prev, e.seq, e.ts_event_ns, e.ts_ingest_ns, e.kind, e.payload)
        if e.prev_hash != prev or e.hash != expected:
            return e.seq
        prev = e.hash
    return None


def assert_chain(entries: Iterable[JournalEntry], genesis: bytes = GENESIS) -> None:
    bad = verify_chain(entries, genesis)
    if bad is not None:
        raise JournalIntegrityError(bad)
