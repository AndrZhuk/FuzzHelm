"""Автомат ризик-станів NORMAL → WARNING → COOLDOWN → HALTED з гістерезисом і витримкою (dwell).

Найменування: risk/state.py
Призначення: режим ризику, що масштабує розмір позиції (κ_mode) і вмикає reduce-only / flatten-all (§5.12).
Автор: Андрій Жук, 2026.

    DD_t = 1 − E_t / max_{τ≤t} E_τ
    NORMAL   → WARNING:   DD ≥ 0.04 ∨ σ_ann/σ_base > 1.6
    WARNING  → NORMAL:    DD ≤ 0.025 ∧ dwell ≥ 15 барів
    WARNING  → COOLDOWN:  DD ≥ 0.08 ∨ PnL_day ≤ −0.02·E_open        (reduce-only)
    COOLDOWN → WARNING:   DD ≤ 0.05 ∧ dwell ≥ 30 барів
    *        → HALTED:    DD ≥ 0.12 ∨ PnL_day ≤ −0.03·E_open        (засувний)
    κ_mode = {NORMAL: 1.0, WARNING: 0.5, COOLDOWN: 0.25, HALTED: 0.0}

Шість вхідних подій. Кожне спостереження (бар) класифікується рівно в одну подію відносно поточного
стану — пріоритет згори донизу:
    HALT_BREACH    засувка спрацьована АБО DD ≥ halt_enter АБО PnL_day ≤ −halt_daily·E_open
    COOL_BREACH    DD ≥ cool_enter АБО PnL_day ≤ −cool_daily·E_open
    WARN_BREACH    DD ≥ warn_enter АБО σ_ann/σ_base > warn_vol_ratio  (не генерується в COOLDOWN: там це
                   не ескалація, а смуга гістерезису, в якій працює умова виходу DD ≤ cool_exit)
    RECOVERY       умова виходу поточного стану виконана разом із витримкою (dwell ≥ N)
    STEADY         нічого з наведеного (усередині смуги гістерезису або витримка ще не минула)
    ADMIN_RELEASE  ручне зняття HALTED адміністратором (release(Role.ADMIN))
Таблиця TRANSITIONS — тотальна: 4 стани × 6 подій = 24 клітинки, кожна задана явно.

dwell — кількість барів, прожитих у поточному стані (скидається лише при зміні стану; записується в
risk_event.dwell_bars). Перехід на вищий рівень тяжкості (напр. NORMAL → COOLDOWN на гепі) відбувається
одразу, повернення — лише на один рівень і лише після витримки.

Після ADMIN_RELEASE автомат переходить у COOLDOWN (reduce-only, κ = 0.25), а трекер перебазовує пік і
E_open на поточний капітал — інакше засувка спрацювала б знову на наступному ж барі (пласка книга не
може відіграти історичний пік). Далі — звичайний шлях COOLDOWN →(30 барів)→ WARNING →(15)→ NORMAL.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Final

from fuzzhelm.core.enums import RiskState, Role
from fuzzhelm.core.errors import PermissionDeniedError
from fuzzhelm.core.money import D0
from fuzzhelm.core.ports import Clock
from fuzzhelm.features.convert import to_float
from fuzzhelm.risk.config import StateMachineCfg
from fuzzhelm.risk.context import EquitySnapshot, EquityTracker
from fuzzhelm.risk.journal import AuditRecord, AuditSink
from fuzzhelm.risk.killswitch import KillSwitch
from fuzzhelm.sizing.convert import float_to_decimal_exact


class RiskEvent(StrEnum):
    HALT_BREACH = "HALT_BREACH"
    COOL_BREACH = "COOL_BREACH"
    WARN_BREACH = "WARN_BREACH"
    RECOVERY = "RECOVERY"
    STEADY = "STEADY"
    ADMIN_RELEASE = "ADMIN_RELEASE"


_N, _W, _C, _H = RiskState.NORMAL, RiskState.WARNING, RiskState.COOLDOWN, RiskState.HALTED
_E = RiskEvent

# Тотальна таблиця переходів 4×6 (§5.12). Кожна клітинка — явно, без «інакше».
TRANSITIONS: Final[Mapping[tuple[RiskState, RiskEvent], RiskState]] = MappingProxyType({
    (_N, _E.HALT_BREACH): _H, (_N, _E.COOL_BREACH): _C, (_N, _E.WARN_BREACH): _W,
    (_N, _E.RECOVERY): _N, (_N, _E.STEADY): _N, (_N, _E.ADMIN_RELEASE): _N,

    (_W, _E.HALT_BREACH): _H, (_W, _E.COOL_BREACH): _C, (_W, _E.WARN_BREACH): _W,
    (_W, _E.RECOVERY): _N, (_W, _E.STEADY): _W, (_W, _E.ADMIN_RELEASE): _W,

    (_C, _E.HALT_BREACH): _H, (_C, _E.COOL_BREACH): _C, (_C, _E.WARN_BREACH): _C,
    (_C, _E.RECOVERY): _W, (_C, _E.STEADY): _C, (_C, _E.ADMIN_RELEASE): _C,

    (_H, _E.HALT_BREACH): _H, (_H, _E.COOL_BREACH): _H, (_H, _E.WARN_BREACH): _H,
    (_H, _E.RECOVERY): _H, (_H, _E.STEADY): _H, (_H, _E.ADMIN_RELEASE): _C,
})

SEVERITY: Final[Mapping[RiskState, int]] = MappingProxyType({_N: 0, _W: 1, _C: 2, _H: 3})


@dataclass(frozen=True, slots=True)
class RiskObservation:
    """Спостереження бару: мітка часу (бар/Clock), капітал, відношення σ_ann/σ_base (None — невідоме).

    vol_ratio має бути скінченним і ≥ 0: NaN мовчки не спрацьовував би тригер (NaN > 1.6 — хибно), а inf
    змінив би стан автомата і лише потім упав у журналі переходу (float → Decimal), лишивши перехід без
    запису risk_event. Тому відмова — на вході, до будь-якої зміни стану; «невідоме» — це None.
    """

    ts_ns: int
    equity: Decimal
    vol_ratio: float | None = None

    def __post_init__(self) -> None:
        if self.vol_ratio is not None and not (math.isfinite(self.vol_ratio) and self.vol_ratio >= 0.0):
            raise ValueError(f"vol_ratio must be a finite number >= 0 or None, got {self.vol_ratio!r}")


@dataclass(frozen=True, slots=True)
class Transition:
    ts_ns: int
    state_from: RiskState
    state_to: RiskState
    event: RiskEvent
    dwell_bars: int                  # скільки барів автомат прожив у state_from
    drawdown: Decimal
    day_return: Decimal
    vol_ratio: float | None
    actor: str | None = None

    def payload(self) -> dict[str, Any]:
        """Канонічно-серіалізовний payload для risk_event (float → Decimal лише через sizing.convert)."""
        return {
            "event": self.event.value,
            "drawdown": self.drawdown,
            "day_return": self.day_return,
            "vol_ratio": None if self.vol_ratio is None else float_to_decimal_exact(self.vol_ratio),
        }


class RiskStateMachine:
    """Автомат станів. Стан змінюється лише через update(obs) (подія з даних) і release(role) (ручна)."""

    def __init__(self, cfg: StateMachineCfg | None = None, *, killswitch: KillSwitch | None = None,
                 tracker: EquityTracker | None = None, clock: Clock | None = None,
                 on_transition: Callable[[Transition], None] | None = None,
                 on_audit: AuditSink | None = None) -> None:
        self.cfg = cfg or StateMachineCfg()
        self._clock = clock
        self._on_transition = on_transition
        self._on_audit = on_audit
        self.killswitch = killswitch or KillSwitch(clock=clock, on_audit=on_audit)
        self.tracker = tracker or EquityTracker()
        self._state = RiskState.NORMAL
        self._dwell = 0
        self._last_ts = 0
        self._last_vol: float | None = None
        self.transitions: list[Transition] = []

    # --- стан
    @property
    def state(self) -> RiskState:
        return self._state

    @property
    def kappa_mode(self) -> Decimal:
        return self.cfg.kappa_mode[self._state]

    @property
    def kappa_mode_float(self) -> float:
        """κ_mode для float-домену сайзера (конвертація через єдину точку features.convert)."""
        return to_float(self.kappa_mode)

    @property
    def dwell_bars(self) -> int:
        return self._dwell

    @property
    def snapshot(self) -> EquitySnapshot | None:
        return self.tracker.snapshot

    @property
    def reduce_only(self) -> bool:
        return self._state in (RiskState.COOLDOWN, RiskState.HALTED)

    @property
    def flatten_all(self) -> bool:
        return self._state is RiskState.HALTED

    # --- класифікація і переходи
    def classify(self, snap: EquitySnapshot, vol_ratio: float | None,
                 state: RiskState | None = None, dwell: int | None = None) -> RiskEvent:
        """Одна подія для спостереження відносно стану `state` (за замовчуванням — поточного)."""
        c = self.cfg
        st = self._state if state is None else state
        dw = self._dwell if dwell is None else dwell
        dd, day_ret = snap.drawdown, snap.day_return
        if self.killswitch.is_tripped or dd >= c.halt_enter or day_ret <= -c.halt_daily_loss:
            return RiskEvent.HALT_BREACH
        if st is RiskState.HALTED:
            return RiskEvent.STEADY
        if dd >= c.cool_enter or day_ret <= -c.cool_daily_loss:
            return RiskEvent.COOL_BREACH
        if st is RiskState.COOLDOWN:
            ok = dd <= c.cool_exit and dw >= c.cool_dwell
            return RiskEvent.RECOVERY if ok else RiskEvent.STEADY
        if dd >= c.warn_enter or (vol_ratio is not None and vol_ratio > c.warn_vol_ratio):
            return RiskEvent.WARN_BREACH
        if st is RiskState.WARNING and dd <= c.warn_exit and dw >= c.warn_dwell:
            return RiskEvent.RECOVERY
        return RiskEvent.STEADY

    def update(self, obs: RiskObservation) -> Transition | None:
        """Обробити бар: оновити пік/денний PnL, збільшити dwell, класифікувати, перейти за таблицею."""
        snap = self.tracker.update(obs.ts_ns, obs.equity)
        self._last_ts = obs.ts_ns
        self._last_vol = obs.vol_ratio
        self._dwell += 1
        if self._state is RiskState.HALTED and not self.killswitch.is_tripped:
            # засувку зняли напряму (killswitch.release адміністратором) — синхронізуємо автомат
            return self._release_transition(actor="kill_switch")
        return self._apply(self.classify(snap, obs.vol_ratio), snap, obs.vol_ratio)

    def _apply(self, event: RiskEvent, snap: EquitySnapshot, vol_ratio: float | None,
               actor: str | None = None) -> Transition | None:
        target = TRANSITIONS[(self._state, event)]
        if target is self._state:
            return None
        tr = Transition(ts_ns=snap.ts_ns, state_from=self._state, state_to=target, event=event,
                        dwell_bars=self._dwell, drawdown=snap.drawdown, day_return=snap.day_return,
                        vol_ratio=vol_ratio, actor=actor)
        self._state = target
        self._dwell = 0
        if target is RiskState.HALTED:
            self.killswitch.trip(f"risk_state:{event.value}", snap.ts_ns)
        self.transitions.append(tr)
        if self._on_transition is not None:
            self._on_transition(tr)
        return tr

    def _release_transition(self, actor: str | None) -> Transition | None:
        snap = self.tracker.rebase()
        if snap is None:
            snap = EquitySnapshot(self._last_ts, D0, D0, D0, 0, D0, D0)
        return self._apply(RiskEvent.ADMIN_RELEASE, snap, self._last_vol, actor=actor)

    def release(self, actor_role: Role | str, *, actor: str | None = None) -> Transition | None:
        """Зняти HALTED. Лише Role.ADMIN, інакше PermissionDeniedError. Пише AuditRecord (до/після).

        Поза HALTED автомат не змінюється (None, без запису risk.release). Але якщо засувка вже спрацювала
        (ручний trip або сигнал HALT від RiskGuard), а автомат ще не обробив бар і не перейшов у HALTED,
        зняття адміністратором знімає саму засувку (її аудит пише KillSwitch): інакше команда мовчки
        ігнорувалася б, а наступний бар латчив би HALTED. Пік при цьому НЕ перебазовується — справжня
        просадка ≥ halt_enter знову переведе автомат у HALTED на наступному барі.
        """
        if actor_role != Role.ADMIN:
            raise PermissionDeniedError(f"HALTED release requires role admin, got {actor_role}")
        if self._state is not RiskState.HALTED:
            if self.killswitch.is_tripped:
                ts_pre = self._clock.now_ns() if self._clock is not None else self._last_ts
                self.killswitch.release(actor_role, actor=actor, ts_ns=ts_pre)
            return None
        before = self._audit_state()
        ts = self._clock.now_ns() if self._clock is not None else self._last_ts
        self.killswitch.release(actor_role, actor=actor, ts_ns=ts)
        tr = self._release_transition(actor=actor or str(actor_role))
        if self._on_audit is not None:
            self._on_audit(AuditRecord(ts_ns=ts, action="risk.release", target="risk_state",
                                       actor_role=Role(actor_role), actor=actor,
                                       before=before, after=self._audit_state()))
        return tr

    def _audit_state(self) -> dict[str, Any]:
        snap = self.tracker.snapshot
        return {
            "state": self._state.value,
            "kappa_mode": self.kappa_mode,
            "dwell_bars": self._dwell,
            "peak": None if snap is None else snap.peak,
            "drawdown": None if snap is None else snap.drawdown,
            "killswitch": self.killswitch.state(),
        }
