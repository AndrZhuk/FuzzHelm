"""Настінний годинник і випадкові ідентифікатори — ПОЗА межею детермінізму.

Найменування: infra/wallclock.py
Автор: Андрій Жук, 2026.

Лише live-воркери, API та скрипти імпортують цей модуль; пакети core, features, detectors,
fuzzy, decision, risk, sizing — ніколи (AST-тест tests/arch/test_determinism_boundary.py).
"""

from __future__ import annotations

import time
import uuid
from uuid import UUID


class SystemClock:
    def now_ns(self) -> int:
        return time.time_ns()


class MonotonicClock:
    """Монотонний годинник для сторожа тиші live-клієнта: не реагує на стрибки NTP (WS-07).
    Не є часом епохи — лише для вимірювання інтервалів."""

    def now_ns(self) -> int:
        return time.monotonic_ns()


class RandomIdGenerator:
    def next_uuid(self) -> UUID:
        return uuid.uuid4()


def new_run_id() -> UUID:
    return uuid.uuid4()
