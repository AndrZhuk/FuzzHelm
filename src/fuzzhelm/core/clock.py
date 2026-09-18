"""Детерміністичні реалізації портів Clock та IdGenerator.

Найменування: core/clock.py
Автор: Андрій Жук, 2026.

Системний (настінний) годинник і uuid4 живуть поза межею детермінізму — у `fuzzhelm.infra.wallclock`.
"""

from __future__ import annotations

import hashlib
from uuid import UUID

NS_PER_SEC = 1_000_000_000
NS_PER_MIN = 60 * NS_PER_SEC
NS_PER_DAY = 86_400 * NS_PER_SEC


class FixedClock:
    """Годинник, що стоїть на місці (тести)."""

    def __init__(self, t_ns: int) -> None:
        self._t = t_ns

    def now_ns(self) -> int:
        return self._t


class ManualClock:
    """Годинник, який рухає рушій (бектест/реплей): час = час поточної події."""

    def __init__(self, t_ns: int = 0) -> None:
        self._t = t_ns

    def now_ns(self) -> int:
        return self._t

    def set(self, t_ns: int) -> None:
        if t_ns < self._t:
            raise ValueError(f"clock cannot go backwards: {t_ns} < {self._t}")
        self._t = t_ns

    def advance(self, dt_ns: int) -> None:
        self.set(self._t + dt_ns)


class SeededIdGenerator:
    """UUID(v4-формат) як BLAKE2b(seed ‖ namespace ‖ counter) — відтворювано між процесами."""

    def __init__(self, seed: int, namespace: bytes = b"") -> None:
        self._seed = seed
        self._ns = namespace
        self._n = 0

    def next_uuid(self) -> UUID:
        h = hashlib.blake2b(digest_size=16)
        h.update(self._seed.to_bytes(16, "big", signed=True))
        h.update(self._ns)
        h.update(self._n.to_bytes(8, "big"))
        self._n += 1
        return UUID(bytes=h.digest(), version=4)


def utc_day_start_ns(t_ns: int) -> int:
    return t_ns - (t_ns % NS_PER_DAY)
