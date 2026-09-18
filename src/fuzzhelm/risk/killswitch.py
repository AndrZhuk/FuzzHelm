"""Засувний kill-switch: спрацьовує автоматично або вручну, знімається лише адміністратором.

Найменування: risk/killswitch.py
Призначення: остання лінія ризик-контуру. Поки засувка спрацьована, RiskGuard не дозволяє жодного
приросту експозиції і вимагає закрити все (flatten-all). Повторне trip() не перезаписує першу причину.
Зняття — release(Role.ADMIN) з записом аудиту (стан до/після); будь-яка інша роль → PermissionDeniedError.
Автор: Андрій Жук, 2026.

Час — лише через ін'єктований Clock або явну мітку ts_ns (межа детермінізму).
"""

from __future__ import annotations

from fuzzhelm.core.enums import Role
from fuzzhelm.core.errors import PermissionDeniedError
from fuzzhelm.core.ports import Clock
from fuzzhelm.risk.journal import AuditRecord, AuditSink


class KillSwitch:
    __slots__ = ("_clock", "_on_audit", "_reason", "_trip_count", "_tripped", "_tripped_at_ns")

    def __init__(self, *, clock: Clock | None = None, on_audit: AuditSink | None = None) -> None:
        self._clock = clock
        self._on_audit = on_audit
        self._tripped = False
        self._reason: str | None = None
        self._tripped_at_ns: int | None = None
        self._trip_count = 0

    @property
    def is_tripped(self) -> bool:
        return self._tripped

    @property
    def reason(self) -> str | None:
        return self._reason

    @property
    def tripped_at_ns(self) -> int | None:
        return self._tripped_at_ns

    @property
    def trip_count(self) -> int:
        """Скільки разів засувка переходила з відкритого стану в спрацьований."""
        return self._trip_count

    def _now(self, ts_ns: int | None) -> int:
        if ts_ns is not None:
            return ts_ns
        if self._clock is not None:
            return self._clock.now_ns()
        return self._tripped_at_ns or 0

    def trip(self, reason: str, ts_ns: int | None = None) -> bool:
        """Спрацювати. Повертає True, якщо це нове спрацювання (засувка була відкрита)."""
        if self._tripped:
            return False
        self._tripped = True
        self._reason = reason
        self._tripped_at_ns = self._now(ts_ns)
        self._trip_count += 1
        return True

    def state(self) -> dict[str, object]:
        return {"tripped": self._tripped, "reason": self._reason, "tripped_at_ns": self._tripped_at_ns}

    def release(self, actor_role: Role | str, *, actor: str | None = None,
                ts_ns: int | None = None) -> AuditRecord | None:
        """Зняти засувку. Лише Role.ADMIN; інакше PermissionDeniedError (навіть якщо не спрацьована).

        Повертає запис аудиту або None, якщо засувка не була спрацьована (ідемпотентно, без аудиту).
        """
        if actor_role != Role.ADMIN:
            raise PermissionDeniedError(f"kill-switch release requires role admin, got {actor_role}")
        if not self._tripped:
            return None
        before = self.state()
        ts = self._now(ts_ns)
        self._tripped = False
        self._reason = None
        self._tripped_at_ns = None
        record = AuditRecord(ts_ns=ts, action="killswitch.release", target="kill_switch",
                             actor_role=Role(actor_role), actor=actor, before=before, after=self.state())
        if self._on_audit is not None:
            self._on_audit(record)
        return record
