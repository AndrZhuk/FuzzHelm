"""Група J брифінгу (§10): автомат ризик-станів, шість лімітів, RiskGuard, журнал risk_event, kill-switch.

Найменування: tests/unit/test_risk.py
Автор: Андрій Жук, 2026.
"""

from __future__ import annotations

import itertools
from datetime import UTC, datetime
from decimal import Decimal
from fractions import Fraction
from uuid import UUID

import pytest
import time_machine

from fuzzhelm.core.clock import NS_PER_MIN, NS_PER_SEC, ManualClock
from fuzzhelm.core.enums import RiskState, Role, Side, VerdictKind
from fuzzhelm.core.errors import ConfigValidationError, PermissionDeniedError
from fuzzhelm.core.journal import EventJournal, assert_chain
from fuzzhelm.risk.config import StateMachineCfg, load_risk_config
from fuzzhelm.risk.context import EquityTracker, RiskContext
from fuzzhelm.risk.guard import RiskGuard, default_rules
from fuzzhelm.risk.journal import AuditRecord, RiskJournal
from fuzzhelm.risk.killswitch import KillSwitch
from fuzzhelm.risk.margin import (
    dtl_atr,
    liq_price_long,
    reduced_max_leverage,
    side_liq_price,
    signed_dtl_atr,
)
from fuzzhelm.risk.rules.liquidation_buffer import LiquidationBufferGuard
from fuzzhelm.risk.rules.max_daily_loss import MaxDailyLoss
from fuzzhelm.risk.rules.max_drawdown_halt import MaxDrawdownHalt
from fuzzhelm.risk.rules.max_gross_leverage import MaxGrossLeverage
from fuzzhelm.risk.rules.max_position_notional import MaxPositionNotional
from fuzzhelm.risk.rules.stale_data import StaleDataGuard
from fuzzhelm.risk.state import (
    SEVERITY,
    TRANSITIONS,
    RiskEvent,
    RiskObservation,
    RiskStateMachine,
    Transition,
)
from fuzzhelm.risk.verdict import ALLOW, VETO, RiskRule, Verdict, compose, exposure, shrink

D = Decimal
T0 = int(datetime(2026, 9, 18, tzinfo=UTC).timestamp()) * NS_PER_SEC   # 2026-09-18 00:00:00 UTC


def ctx(**kw: object) -> RiskContext:
    base: dict[str, object] = {
        "ts_ns": T0 + 60 * NS_PER_MIN, "instrument": "BTC-USDT-PERP", "price": D(100),
        "equity": D(10_000), "current_qty": D(0), "target_qty": D(10), "atr": D("0.5"),
        "stop_distance": D(1),
    }
    base.update(kw)
    return RiskContext(**base)  # type: ignore[arg-type]


class Driver:
    """Подає автомату бари з капіталом, що дає задану просадку від піку 100."""

    def __init__(self, fsm: RiskStateMachine) -> None:
        self.fsm = fsm
        self.t = T0
        self.last: Transition | None = None

    def bar(self, dd: str | Decimal, vol_ratio: float | None = None) -> RiskState:
        self.t += NS_PER_MIN
        self.last = self.fsm.update(RiskObservation(self.t, D(100) * (1 - D(dd)), vol_ratio))
        return self.fsm.state

    def bars(self, n: int, dd: str | Decimal) -> RiskState:
        for _ in range(n):
            self.bar(dd)
        return self.fsm.state


# Ізоляція порогів просадки: денні пороги (−2%/−3% за добу) відсунуто, щоб внутрішньодобове падіння
# перевіряло саме DD-переходи. Взаємодію з денними порогами перевіряє окремий тест із реальним конфігом.
DD_ONLY = StateMachineCfg(cool_daily_loss=D("0.90"), halt_daily_loss=D("0.95"))


def fresh(cfg: StateMachineCfg = DD_ONLY) -> Driver:
    d = Driver(RiskStateMachine(cfg))
    d.bar("0")
    return d


# ---------------------------------------------------------------- автомат станів: таблиця 4×6

_ORDER = [RiskState.NORMAL, RiskState.WARNING, RiskState.COOLDOWN, RiskState.HALTED]


def _oracle(state: RiskState, event: RiskEvent) -> RiskState:
    """Незалежне прочитання §5.12: ескалація — одразу, повернення — на рівень нижче, HALTED засувний."""
    if state is RiskState.HALTED:
        return RiskState.COOLDOWN if event is RiskEvent.ADMIN_RELEASE else RiskState.HALTED
    lvl = _ORDER.index(state)
    if event is RiskEvent.HALT_BREACH:
        return RiskState.HALTED
    if event is RiskEvent.COOL_BREACH:
        return _ORDER[max(lvl, 2)]
    if event is RiskEvent.WARN_BREACH:
        return _ORDER[max(lvl, 1)]
    if event is RiskEvent.RECOVERY:
        return _ORDER[max(lvl - 1, 0)]
    return state                                    # STEADY; ADMIN_RELEASE поза HALTED — no-op


@pytest.mark.parametrize(("state", "event"), list(itertools.product(RiskState, RiskEvent)),
                         ids=str)
def test_risk_fsm_transition_table_is_total(state: RiskState, event: RiskEvent) -> None:
    assert len(RiskState) == 4 and len(RiskEvent) == 6 and len(TRANSITIONS) == 24
    assert (state, event) in TRANSITIONS                         # клітинка задана явно
    target = TRANSITIONS[(state, event)]
    assert isinstance(target, RiskState)
    assert target is _oracle(state, event)
    kappa = load_risk_config().state_machine.kappa_mode
    if event in (RiskEvent.HALT_BREACH, RiskEvent.COOL_BREACH, RiskEvent.WARN_BREACH):
        assert kappa[target] <= kappa[state]                     # подія-порушення ніколи не збільшує κ_mode
    if state is RiskState.HALTED and event is not RiskEvent.ADMIN_RELEASE:
        assert target is RiskState.HALTED                        # засувка: лише ручне зняття
    assert abs(SEVERITY[target] - SEVERITY[state]) <= 1 or event in (
        RiskEvent.HALT_BREACH, RiskEvent.COOL_BREACH)            # стрибки через рівень — лише вгору


def test_fsm_escalation_path_and_kappa_mode() -> None:
    d = fresh()
    fsm = d.fsm
    assert fsm.state is RiskState.NORMAL and fsm.kappa_mode == D("1.0")
    assert d.bar("0.0399") is RiskState.NORMAL
    assert d.bar("0.04") is RiskState.WARNING and fsm.kappa_mode == D("0.5")
    assert d.bar("0.08") is RiskState.COOLDOWN and fsm.kappa_mode == D("0.25") and fsm.reduce_only
    assert d.bar("0.12") is RiskState.HALTED and fsm.kappa_mode == D("0.0") and fsm.flatten_all
    assert fsm.kappa_mode_float == 0.0
    assert [t.event for t in fsm.transitions] == [RiskEvent.WARN_BREACH, RiskEvent.COOL_BREACH,
                                                  RiskEvent.HALT_BREACH]
    assert fsm.killswitch.is_tripped


def test_fsm_warning_on_volatility_ratio_and_gap_jumps_levels() -> None:
    d = fresh(load_risk_config().state_machine)
    assert d.bar("0", vol_ratio=1.6) is RiskState.NORMAL          # строго «> 1.6»
    assert d.bar("0", vol_ratio=1.61) is RiskState.WARNING
    g = fresh()
    assert g.bar("0.09") is RiskState.COOLDOWN                    # геп: одразу на рівень тяжкості
    assert g.last is not None and g.last.state_from is RiskState.NORMAL


def test_hysteresis_blocks_recovery_inside_band() -> None:
    d = fresh()
    assert d.bar("0.04") is RiskState.WARNING
    # сцена демо: просадка впала до 3.1% — повернення НЕ відбувається, бо поріг виходу 2.5%
    assert d.bars(100, "0.031") is RiskState.WARNING
    assert d.fsm.dwell_bars >= 15                                 # витримка вже не заважає — лише смуга
    assert d.bar("0.025") is RiskState.NORMAL
    c = fresh()
    assert c.bar("0.08") is RiskState.COOLDOWN
    assert c.bars(100, "0.06") is RiskState.COOLDOWN              # смуга (0.05; 0.08)
    assert c.bars(50, "0.045") is RiskState.WARNING               # DD ≤ 0.05 — вихід на рівень нижче
    last = c.fsm.transitions[-1]
    assert last.state_from is RiskState.COOLDOWN and last.event is RiskEvent.RECOVERY


def test_dwell_time_blocks_premature_recovery() -> None:
    d = fresh()
    d.bar("0.05")
    assert d.fsm.state is RiskState.WARNING
    for k in range(1, 15):
        assert d.bar("0") is RiskState.WARNING, f"recovered after only {k} bars"
    assert d.bar("0") is RiskState.NORMAL
    assert d.last is not None and d.last.dwell_bars == 15 and d.last.event is RiskEvent.RECOVERY
    c = fresh()
    c.bar("0.10")
    for _ in range(29):
        assert c.bar("0") is RiskState.COOLDOWN
    assert c.bar("0") is RiskState.WARNING
    assert c.last is not None and c.last.dwell_bars == 30


def test_halted_requires_manual_release_by_admin() -> None:
    audits: list[AuditRecord] = []
    clock = ManualClock(T0)
    fsm = RiskStateMachine(load_risk_config().state_machine, clock=clock, on_audit=audits.append)
    d = Driver(fsm)
    d.bar("0")
    assert d.bar("0.12") is RiskState.HALTED
    assert d.bars(500, "0") is RiskState.HALTED          # повне відновлення капіталу не знімає засувку
    for role in (Role.OPERATOR, Role.ANALYST, Role.AUDITOR, "operator"):
        with pytest.raises(PermissionDeniedError):
            fsm.release(role)
    assert fsm.state is RiskState.HALTED and fsm.killswitch.is_tripped
    assert audits == []
    clock.set(d.t + 30 * NS_PER_SEC)
    tr = fsm.release(Role.ADMIN, actor="root")
    assert tr is not None and tr.state_from is RiskState.HALTED and tr.state_to is RiskState.COOLDOWN
    assert tr.event is RiskEvent.ADMIN_RELEASE
    assert fsm.state is RiskState.COOLDOWN and not fsm.killswitch.is_tripped
    risk_audit = [a for a in audits if a.action == "risk.release"]
    assert len(risk_audit) == 1
    a = risk_audit[0]
    assert a.actor_role is Role.ADMIN and a.actor == "root" and a.ts_ns == d.t + 30 * NS_PER_SEC
    assert a.before["state"] == "HALTED" and a.after["state"] == "COOLDOWN"
    assert a.to_row()["before_json"]["state"] == "HALTED"
    assert any(x.action == "killswitch.release" for x in audits)
    assert fsm.release(Role.ADMIN) is None                        # поза HALTED — no-op без аудиту
    assert len([x for x in audits if x.action == "risk.release"]) == 1


def test_halted_release_rebases_peak_so_latch_does_not_retrip() -> None:
    d = fresh(load_risk_config().state_machine)
    d.bar("0.13")
    assert d.fsm.state is RiskState.HALTED
    d.fsm.release(Role.ADMIN)
    assert d.bar("0.13") is RiskState.COOLDOWN          # той самий капітал 87: DD = 0 від нового піку
    snap = d.fsm.snapshot
    assert snap is not None and snap.drawdown == 0 and snap.peak == D(87)


def test_halted_by_daily_loss_and_external_killswitch_trip() -> None:
    fsm = RiskStateMachine(StateMachineCfg())
    fsm.update(RiskObservation(T0 + NS_PER_MIN, D(100)))
    fsm.update(RiskObservation(T0 + 2 * NS_PER_MIN, D(97)))       # PnL_day = −3% ⇒ HALTED
    assert fsm.state is RiskState.HALTED
    other = RiskStateMachine(StateMachineCfg())
    other.update(RiskObservation(T0 + NS_PER_MIN, D(100)))
    other.killswitch.trip("manual panic", T0 + NS_PER_MIN)
    other.update(RiskObservation(T0 + 2 * NS_PER_MIN, D(100)))
    assert other.state is RiskState.HALTED
    other.killswitch.release(Role.ADMIN)                           # зняли засувку напряму
    tr = other.update(RiskObservation(T0 + 3 * NS_PER_MIN, D(100)))
    assert other.state is RiskState.COOLDOWN and tr is not None and tr.actor == "kill_switch"


def test_fsm_transitions_are_journaled_as_risk_event_rows() -> None:
    """Проводка on_transition → RiskJournal.record_transition → хеш-ланцюг EventJournal (рядок risk_event
    з state_from/state_to/dwell_bars) — саме так рушій пише переходи автомата в аудит."""
    run_id = UUID(int=11)
    events = EventJournal(run_id)
    journal = RiskJournal(run_id, event_journal=events)
    fsm = RiskStateMachine(DD_ONLY, on_transition=journal.record_transition)
    d = Driver(fsm)
    d.bar("0")
    d.bar("0", vol_ratio=1.7)                                      # NORMAL → WARNING за волатильністю
    d.bar("0.12")                                                  # WARNING → HALTED
    recs = journal.by_rule("risk_state")
    assert [(r.state_from, r.state_to) for r in recs] == [
        (RiskState.NORMAL, RiskState.WARNING), (RiskState.WARNING, RiskState.HALTED)]
    row = recs[0].to_row()
    assert row["state_from"] == "NORMAL" and row["state_to"] == "WARNING" and row["dwell_bars"] == 2
    assert row["verdict"] is None and row["payload"]["event"] == "WARN_BREACH"
    assert row["payload"]["vol_ratio"] == "1.7"
    assert recs[1].observed == D("0.12")
    assert [e.kind for e in events.entries] == ["risk_event"] * 2
    assert_chain(events.entries)


def test_observation_rejects_non_finite_vol_ratio_before_any_state_change() -> None:
    """inf раніше змінював стан автомата і лише потім падав у журналі переходу (перехід без запису);
    NaN мовчки не спрацьовував тригер. Тепер обидва відкидаються на вході; «невідоме» — None."""
    journal = RiskJournal()
    fsm = RiskStateMachine(DD_ONLY, on_transition=journal.record_transition)
    fsm.update(RiskObservation(T0 + NS_PER_MIN, D(100)))
    for bad in (float("inf"), float("nan"), -0.5):
        with pytest.raises(ValueError, match="vol_ratio"):
            RiskObservation(T0 + 2 * NS_PER_MIN, D(100), bad)
    assert fsm.state is RiskState.NORMAL and len(journal) == 0
    assert RiskObservation(T0, D(100), None).vol_ratio is None


def test_admin_release_clears_killswitch_tripped_before_fsm_latches() -> None:
    """Засувка спрацювала між барами (ручний trip / сигнал HALT від RiskGuard), автомат ще не в HALTED:
    release(ADMIN) знімає саму засувку, а не мовчить; наступний бар не латчить HALTED."""
    audits: list[AuditRecord] = []
    fsm = RiskStateMachine(DD_ONLY, on_audit=audits.append)
    fsm.update(RiskObservation(T0 + NS_PER_MIN, D(100)))
    fsm.killswitch.trip("manual panic", T0 + NS_PER_MIN)
    with pytest.raises(PermissionDeniedError):
        fsm.release(Role.OPERATOR)
    assert fsm.release(Role.ADMIN, actor="root") is None            # автомат не був у HALTED
    assert not fsm.killswitch.is_tripped
    assert [a.action for a in audits] == ["killswitch.release"]
    fsm.update(RiskObservation(T0 + 2 * NS_PER_MIN, D(100)))
    assert fsm.state is RiskState.NORMAL
    # справжня просадка ≥ 12% після такого зняття знову латчить HALTED (пік не перебазовано)
    fsm.update(RiskObservation(T0 + 3 * NS_PER_MIN, D(88)))
    assert fsm.state is RiskState.HALTED


def test_intraday_crash_hits_daily_limits_before_drawdown_thresholds() -> None:
    """Реальний конфіг: якщо падіння від піку відбувається в межах однієї UTC-доби, PnL_day ≈ −DD, тож
    денні пороги (−2% → COOLDOWN, −3% → HALTED) спрацьовують раніше за DD-пороги 8% і 12%.
    Сцена демо «4% → WARNING, 8% → COOLDOWN, 12% → HALTED» можлива лише, якщо падіння розтягнуте на
    кілька діб (docs/deviations.d/risk.md)."""
    d = fresh(load_risk_config().state_machine)
    assert d.bar("0.019") is RiskState.NORMAL
    assert d.bar("0.02") is RiskState.COOLDOWN                     # PnL_day = −2%, DD лише 2%
    assert d.last is not None and d.last.event is RiskEvent.COOL_BREACH
    assert d.bar("0.03") is RiskState.HALTED                       # PnL_day = −3%, DD лише 3%
    # те саме падіння, розтягнуте на кілька діб, проходить сходинками DD
    slow = Driver(RiskStateMachine(load_risk_config().state_machine))
    path = ["0", "0.015", "0.03", "0.04", "0.055", "0.07", "0.08", "0.095", "0.11", "0.12"]
    states = []
    for dd in path:
        slow.t += 86_400 * NS_PER_SEC - NS_PER_MIN                 # кожен бар — нова UTC-доба
        states.append(slow.bar(dd))
    assert states[3] is RiskState.WARNING and states[6] is RiskState.COOLDOWN
    assert states[-1] is RiskState.HALTED


def test_drawdown_uses_running_peak() -> None:
    tr = EquityTracker()
    tr.update(T0, D(100))
    tr.update(T0 + NS_PER_MIN, D(110))
    snap = tr.update(T0 + 2 * NS_PER_MIN, D("104.5"))
    assert snap.peak == D(110)
    assert snap.drawdown == D("0.05")                              # 1 − 104.5/110, а не 1 − 104.5/100 < 0
    snap = tr.update(T0 + 3 * NS_PER_MIN, D(120))
    assert snap.peak == D(120) and snap.drawdown == 0
    # автомат бачить ту саму просадку: 110 → 105.6 = 4% від піку ⇒ WARNING, хоча капітал вище старту
    fsm = RiskStateMachine(StateMachineCfg())
    for i, e in enumerate(("100", "110", "105.6")):
        fsm.update(RiskObservation(T0 + i * NS_PER_MIN, D(e)))
    assert fsm.state is RiskState.WARNING
    with pytest.raises(ValueError, match="non-decreasing"):
        tr.update(T0, D(100))


# ---------------------------------------------------------------- денний ліміт і UTC-північ


def test_max_daily_loss_resets_at_utc_midnight() -> None:
    """Скидання визначається часом бару (ManualClock), а не настінним годинником: time-machine пересуває
    настінний час у довільну точку — результат не змінюється."""
    rule = MaxDailyLoss(D("0.02"))
    tracker = EquityTracker()
    clock = ManualClock(T0 + NS_PER_MIN)
    day2 = T0 + 86_400 * NS_PER_SEC

    def check_at(t_ns: int, equity: str) -> tuple[VerdictKind, Decimal | None, bool]:
        clock.set(t_ns)
        snap = tracker.update(clock.now_ns(), D(equity))
        rv = rule.check(RiskContext.from_snapshot(
            snap, instrument="BTC-USDT-PERP", price=D(100), current_qty=D(0), target_qty=D(1),
            atr=D(1), stop_distance=D(2)))
        return rv.verdict.kind, rv.observed, bool(rv.payload["reset_applied"])

    for wall in (datetime(2031, 1, 1, 12, 0, tzinfo=UTC), datetime(2026, 9, 18, 23, 59, 30, tzinfo=UTC)):
        with time_machine.travel(wall, tick=False):
            tracker = EquityTracker()
            clock = ManualClock(T0)
            assert check_at(T0 + NS_PER_MIN, "100")[0] is VerdictKind.ALLOW
            assert check_at(day2 - 2 * NS_PER_MIN, "98.5")[0] is VerdictKind.ALLOW       # −1.5%
            kind, observed, _ = check_at(day2 - NS_PER_MIN, "97.9")                     # −2.1%
            assert kind is VerdictKind.VETO and observed == D("-0.021")
            assert rule.limit == D("-0.02")
            # 00:00 UTC наступної доби: E_open = перенесений капітал 97.9 → PnL_day = 0
            kind, observed, _ = check_at(day2, "97.9")
            assert kind is VerdictKind.ALLOW and observed == 0
            assert tracker.snapshot is not None and tracker.snapshot.equity_day_open == D("97.9")
            # нова доба рахує збиток уже від 97.9: −2.1% від 97.9 знову VETO
            kind, _, _ = check_at(day2 + 10 * NS_PER_MIN, str(D("97.9") * D("0.979")))
            assert kind is VerdictKind.VETO
    # контекст, у якому PnL ще від минулої доби, а час бару — вже нова доба: правило скидає сам
    stale = ctx(ts_ns=day2 + NS_PER_MIN, pnl_day=D(-500), equity_day_open=D(10_000), day_start_ns=T0)
    rv = rule.check(stale)
    assert rv.verdict is ALLOW and rv.payload["reset_applied"] is True


# ---------------------------------------------------------------- решта лімітів


def test_stale_data_vetoes_on_lag_and_on_low_dq() -> None:
    rule = StaleDataGuard(D("5.0"), D("0.90"))
    t = T0 + NS_PER_MIN
    fresh_ctx = rule.check(ctx(ts_ns=t, last_data_ns=t - 5 * NS_PER_SEC, dq_score=D("0.90")))
    assert fresh_ctx.verdict is ALLOW                               # межі включні: lag = 5 с, Q = 0.90
    lag = rule.check(ctx(ts_ns=t, last_data_ns=t - 5 * NS_PER_SEC - 1_000_000))
    assert lag.verdict is VETO and lag.observed == D("5.001") and lag.limit == D("5.0")
    dq = rule.check(ctx(ts_ns=t, last_data_ns=t, dq_score=D("0.899")))
    assert dq.verdict is VETO and dq.observed == D("0.899") and dq.limit == D("0.90")
    both = rule.check(ctx(ts_ns=t, last_data_ns=t - 60 * NS_PER_SEC, dq_score=D("0.5")))
    assert both.verdict is VETO and both.payload["lag_breached"] and both.payload["dq_breached"]
    # зменшення позиції на застарілих даних дозволене завжди
    reduce_ = rule.check(ctx(ts_ns=t, last_data_ns=t - 60 * NS_PER_SEC, current_qty=D(10), target_qty=D(4)))
    assert reduce_.verdict is ALLOW


def test_max_position_notional_is_margin_fraction_of_equity() -> None:
    rule = MaxPositionNotional(D("0.30"))
    # E = 10 000, L_set = 3: IM = q·P/3 ≤ 3 000 ⇔ номінал ≤ 9 000 ⇔ q ≤ 90
    assert rule.check(ctx(target_qty=D(90))).verdict is ALLOW
    rv = rule.check(ctx(target_qty=D(120)))
    assert rv.verdict.kind is VerdictKind.SHRINK and rv.verdict.factor == Fraction(90, 120)
    assert rv.observed == D(120) * D(100) / D(3) / D(10_000) and rv.limit == D("0.30")
    rv = rule.check(ctx(current_qty=D(60), target_qty=D(120)))    # base 60 лишається, приріст 60 → 30
    assert rv.verdict.factor == Fraction(30, 60)
    assert rule.check(ctx(current_qty=D(95), target_qty=D(120))).verdict is VETO
    assert rule.check(ctx(current_qty=D(150), target_qty=D(120))).verdict is ALLOW   # зменшення
    # ліміт 3.0 на валове плече досяжний лише при L_set ≥ 10 — тоді номінал ≤ 3·E
    assert rule.check(ctx(target_qty=D(300), leverage_setting=D(10))).verdict is ALLOW


def test_max_gross_leverage_shrinks_then_vetoes() -> None:
    rule = MaxGrossLeverage(D("3.0"))
    rv = rule.check(ctx(target_qty=D(400)))                       # 40 000 / 10 000 = 4× > 3×
    assert rv.verdict.factor == Fraction(300, 400) and rv.observed == D(4)
    rv = rule.check(ctx(target_qty=D(100), gross_notional_other=D(25_000)))
    assert rv.verdict.factor == Fraction(50, 100)
    assert rule.check(ctx(target_qty=D(100), gross_notional_other=D(30_000))).verdict is VETO


def test_max_drawdown_halt_signals_halt_even_on_reduction() -> None:
    rule = MaxDrawdownHalt(D("0.12"))
    rv = rule.check(ctx(drawdown=D("0.12")))
    assert rv.verdict is VETO and rv.halt and rv.observed == D("0.12")
    rv = rule.check(ctx(drawdown=D("0.13"), current_qty=D(10), target_qty=D(0)))
    assert rv.verdict is ALLOW and rv.halt
    assert not rule.check(ctx(drawdown=D("0.1199"))).halt


def test_liquidation_guard_reduces_leverage_before_veto() -> None:
    rule = LiquidationBufferGuard(D("6.0"), D("0.20"))
    e, p, atr = D(10_000), D(100), D(6)
    # запит 3× (q = 300) при ATR = 6% ціни: DTL ≈ 5.5 < 6 — порушення
    c = ctx(equity=e, price=p, atr=atr, stop_distance=2 * atr, target_qty=D(300))
    rv = rule.check(c)
    assert rv.observed is not None and rv.observed < 6
    assert rv.verdict.kind is VerdictKind.SHRINK                   # НЕ VETO: плече зменшується
    approved = D(300) * rv.verdict.factor_dec
    liq = liq_price_long(approved, p, e, D("0.005"))
    assert dtl_atr(p, liq, atr) >= D("5.999999999")                # після «ремонту» ліміт DTL ≥ 6 виконується
    l_red = reduced_max_leverage(2 * atr, p, D("0.005"), D("0.20"))
    assert l_red == 1 / (D(12) / (D(100) * D("0.8")) + D("0.005"))
    assert approved * p / e <= l_red                               # і стоп спрацює раніше за ліквідацію
    assert rv.payload["cap_binding"] == "dtl_limit"
    # широкий стоп (5·ATR): вузьким місцем стає саме L'_max із брифінгу
    wide = rule.check(ctx(equity=e, price=p, atr=atr, stop_distance=5 * atr, target_qty=D(300)))
    assert wide.payload["cap_binding"] == "reduced_max_leverage"
    l_wide = reduced_max_leverage(5 * atr, p, D("0.005"), D("0.20"))
    assert wide.payload["q_cap_post"] == l_wide * e / p
    assert wide.verdict.kind is VerdictKind.SHRINK
    # VETO — лише коли навіть незмінна частина позиції вже за межею: приросту нема куди стиснутись
    held = rule.check(ctx(equity=e, price=p, atr=atr, stop_distance=2 * atr,
                          current_qty=D(290), target_qty=D(300)))
    assert held.verdict is VETO
    assert rule.check(ctx(equity=e, price=p, atr=D("0.5"), target_qty=D(300))).verdict is ALLOW   # DTL ≫ 6
    assert rule.check(ctx(equity=e, price=p, atr=atr, current_qty=D(300), target_qty=D(100))).verdict is ALLOW


def test_liquidation_guard_rejects_position_already_beyond_liquidation() -> None:
    """Плече > 1/mmr = 200: P_liq лежить по інший бік від P (миттєва ліквідація). Модуль |P − P_liq| дав би
    фіктивне DTL ≈ 9.8 і ALLOW; правило мусить бачити від'ємну знакову відстань і стискати приріст."""
    rule = LiquidationBufferGuard(D("6.0"), D("0.20"))
    e, p, atr, mmr = D(10_000), D(100), D("0.05"), D("0.005")
    for tgt in (D(1_000_000), D(-1_000_000)):                          # 10 000× лонг і шорт
        side = Side.LONG if tgt > 0 else Side.SHORT
        rv = rule.check(ctx(equity=e, price=p, atr=atr, stop_distance=2 * atr, target_qty=tgt))
        liq = side_liq_price(side, abs(tgt), p, e, mmr)
        assert (liq > p) if side is Side.LONG else (liq < p)           # ціна вже за ліквідацією
        assert rv.observed is not None and rv.observed < 0
        assert rv.verdict.kind is VerdictKind.SHRINK
        approved = abs(tgt) * rv.verdict.factor_dec
        assert signed_dtl_atr(side, p, side_liq_price(side, approved, p, e, mmr), atr) >= D("5.999999999")
    # наскрізно: навіть із лояльним лімітом валового плеча (150×) прийнята позиція тримає DTL ≥ 6
    guard = RiskGuard([LiquidationBufferGuard(), MaxGrossLeverage(D(150))], mode_gate=False)
    res = guard.evaluate(ctx(equity=e, price=p, atr=atr, stop_distance=2 * atr, target_qty=D(100_000)))
    q = res.approved_qty
    assert 0 < q < D(100_000)
    assert signed_dtl_atr(Side.LONG, p, liq_price_long(q, p, e, mmr), atr) >= D("5.999999999")


# ---------------------------------------------------------------- RiskGuard і журнал


def test_every_verdict_written_to_risk_event_with_observed_and_limit() -> None:
    run_id = UUID(int=7)
    events = EventJournal(run_id)
    sink: list[object] = []
    journal = RiskJournal(run_id, sink.append, event_journal=events)
    guard = RiskGuard.from_config(load_risk_config(), journal)
    # денний збиток −2.1% (VETO) і номінал 4× (SHRINK від двох правил) — одна заявка
    c = ctx(target_qty=D(400), pnl_day=D(-210), equity_day_open=D(10_000))
    res = guard.evaluate(c)
    names = [r.name for r in guard.rules]
    assert len(names) == 7 and len(journal) == 7 and len(sink) == 7   # 6 лімітів + гейт режиму
    assert [r.rule for r in journal.records] == names
    for rec, rv in zip(journal.records, res.records, strict=True):
        assert rec.rule == rv.rule and rec.verdict is rv.verdict.kind
        assert rec.factor == rv.verdict.factor_dec
        assert rec.observed is not None and rec.limit_value is not None
        assert rec.observed == rv.observed and rec.limit_value == rv.limit
        row = rec.to_row()
        assert row["run_id"] == str(run_id) and row["verdict"] in ("ALLOW", "SHRINK", "VETO")
        assert isinstance(row["observed"], str) and isinstance(row["limit_value"], str)
    daily = journal.by_rule("max_daily_loss")[0]
    assert daily.verdict is VerdictKind.VETO
    assert daily.observed == D("-0.021") and daily.limit_value == D("-0.02")   # як у сцені демо
    assert journal.by_rule("max_gross_leverage")[0].verdict is VerdictKind.SHRINK
    assert res.verdict is VETO and res.approved_qty == 0
    assert len(journal.vetoes()) == 1
    assert [e.kind for e in events.entries] == ["risk_event"] * 7
    assert_chain(events.entries)                                   # хеш-ланцюг цілий
    guard.evaluate(ctx(target_qty=D(5)))
    assert len(journal) == 14


def test_guard_composes_shrinks_and_floors_to_step() -> None:
    guard = RiskGuard(default_rules(load_risk_config()))
    res = guard.evaluate(ctx(target_qty=D(400), step_size=D("0.001")))
    # MaxPositionNotional: 90/400; MaxGrossLeverage: 300/400 → добуток 0.16875 → 67.5
    assert res.verdict.kind is VerdictKind.SHRINK
    assert res.verdict.factor == Fraction(90, 400) * Fraction(300, 400)
    assert res.increase_approved == D("67.500") and res.approved_qty == D("67.500")
    assert res.approved_qty <= D(90)                               # кожен ліміт окремо виконано
    assert res.order_qty == D("67.500")
    d = res.to_dict()
    assert d["verdict"] == "SHRINK" and d["approved_qty"] == "67.500" and len(d["records"]) == 7
    assert d["records"][0]["rule"] == "stale_data"                 # скор якості — перший вхід ланцюга (§15)
    mpn = next(r for r in d["records"] if r["rule"] == "max_position_notional")
    assert (mpn["verdict"], mpn["limit"]) == ("SHRINK", "0.3")
    assert D(mpn["factor"]) == D("0.225") and abs(D(mpn["observed"]) - D(4) / D(3)) < D("1e-30")


def test_guard_always_allows_reduction_and_flattens_when_halted() -> None:
    guard = RiskGuard(default_rules(load_risk_config()))
    bad = {"drawdown": D("0.11"), "pnl_day": D(-500), "equity_day_open": D(10_000), "dq_score": D("0.1")}
    res = guard.evaluate(ctx(current_qty=D(50), target_qty=D(20), **bad))
    assert res.approved_qty == D(20) and res.increase_requested == 0
    flip = guard.evaluate(ctx(current_qty=D(50), target_qty=D(-20), **bad))
    assert flip.approved_qty == 0                                  # закрити лонг — так, відкрити шорт — ні
    cool = guard.evaluate(ctx(current_qty=D(10), target_qty=D(20), risk_state=RiskState.COOLDOWN))
    assert cool.approved_qty == D(10) and cool.verdict is VETO     # reduce-only
    halted = guard.evaluate(ctx(current_qty=D(10), target_qty=D(20), risk_state=RiskState.HALTED))
    assert halted.flatten_all and halted.approved_qty == 0 and halted.order_qty == D(-10)
    ks = KillSwitch()
    g2 = RiskGuard(default_rules(load_risk_config()), killswitch=ks)
    res = g2.evaluate(ctx(drawdown=D("0.125"), current_qty=D(10), target_qty=D(10)))
    assert res.halt and res.flatten_all and ks.is_tripped and ks.reason == "max_drawdown_halt"
    res = g2.evaluate(ctx(target_qty=D(1)))                         # засувка тримає і на «чистому» контексті
    assert res.flatten_all and res.approved_qty == 0


def test_guard_rejects_duplicate_rule_names() -> None:
    with pytest.raises(ValueError, match="duplicate"):
        RiskGuard([MaxDailyLoss(), MaxDailyLoss()])


# ---------------------------------------------------------------- kill-switch, вердикти, конфіг


def test_killswitch_is_latching_and_admin_only() -> None:
    audits: list[AuditRecord] = []
    ks = KillSwitch(clock=ManualClock(T0), on_audit=audits.append)
    assert ks.trip("first") is True and ks.trip("second") is False
    assert ks.reason == "first" and ks.tripped_at_ns == T0 and ks.trip_count == 1
    with pytest.raises(PermissionDeniedError):
        ks.release(Role.OPERATOR)
    assert ks.is_tripped
    rec = ks.release(Role.ADMIN, actor="root")
    assert rec is not None and rec.before["tripped"] is True and rec.after["tripped"] is False
    assert audits == [rec] and not ks.is_tripped
    assert ks.release(Role.ADMIN) is None
    with pytest.raises(PermissionDeniedError):
        ks.release(Role.ANALYST)


def test_verdict_constructors_validate_factor() -> None:
    for bad in (0, 1, "1.5", D("-0.1"), D(0), D(1)):
        with pytest.raises(ValueError):
            shrink(bad)
    with pytest.raises(TypeError):
        shrink(0.5)  # type: ignore[arg-type]
    assert shrink("0.5").factor == Fraction(1, 2)
    assert compose([]) is ALLOW
    assert compose([shrink("0.5"), ALLOW, shrink("0.2")]).factor == Fraction(1, 10)
    assert str(shrink("0.25")) == "SHRINK(0.25)" and str(ALLOW) == "ALLOW" and str(VETO) == "VETO"
    assert shrink(Fraction(1, 3)).factor_dec == D("0.333333333333333333")   # вниз, не вгору
    for kind, factor in ((VerdictKind.ALLOW, Fraction(1, 2)), (VerdictKind.VETO, Fraction(1, 2))):
        with pytest.raises(ValueError):
            Verdict(kind, factor)
    with pytest.raises(TypeError):
        Verdict(VerdictKind.SHRINK, D("0.5"))  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        shrink(D("NaN"))
    with pytest.raises(TypeError):
        shrink([0.5])  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        exposure(ALLOW, D(-1))
    assert isinstance(MaxDailyLoss(), RiskRule)


def test_risk_limits_yaml_loads_and_invalid_config_reports_field_path() -> None:
    cfg = load_risk_config()
    assert cfg.limits.max_daily_loss.value == D("0.02") and cfg.state_machine.warn_dwell == 15
    assert cfg.limits.liquidation_buffer.b == D("0.2") and cfg.hysteresis.enter == 0.25
    bad = cfg.model_dump(mode="python")
    bad["state_machine"]["warn_exit"] = D("0.05")                  # вихід вище за вхід — петлі немає
    with pytest.raises(ConfigValidationError) as ei:
        load_risk_config(bad)
    assert ei.value.path == "state_machine"
    bad2 = cfg.model_dump(mode="python")
    bad2["limits"]["max_daily_loss"]["value"] = D("1.5")
    with pytest.raises(ConfigValidationError) as ei2:
        load_risk_config(bad2)
    assert ei2.value.path == "limits.max_daily_loss.value"


def test_rules_and_config_reject_invalid_parameters() -> None:
    for make in (lambda: MaxPositionNotional(D(0)), lambda: MaxGrossLeverage(D(0)),
                 lambda: MaxDailyLoss(D(1)), lambda: MaxDrawdownHalt(D(0)),
                 lambda: LiquidationBufferGuard(D(0)), lambda: StaleDataGuard(D(0))):
        with pytest.raises(ValueError):
            make()
    for bad_ctx in ({"price": D(0)}, {"atr": D(-1)}, {"leverage_setting": D(0)},
                    {"gross_notional_other": D(-1)}):
        with pytest.raises(ValueError):
            ctx(**bad_ctx)
    base = load_risk_config().model_dump(mode="python")
    sm_breaks = [
        {"cool_exit": D("0.09")},                       # вихід COOLDOWN вище входу
        {"warn_enter": D("0.06")},                      # смуги не вкладені
        {"cool_enter": D("0.13")},                      # COOLDOWN вище HALTED
        {"cool_daily_loss": D("0.04")},                 # денний COOLDOWN вище денного HALT
        {"kappa_mode": {"NORMAL": D(1), "WARNING": D(1), "COOLDOWN": D("0.25")}},
        {"kappa_mode": {"NORMAL": D(2), "WARNING": D(1), "COOLDOWN": D("0.25"), "HALTED": D(0)}},
        {"kappa_mode": {"NORMAL": D("0.4"), "WARNING": D("0.5"), "COOLDOWN": D("0.25"), "HALTED": D(0)}},
        {"kappa_mode": {"NORMAL": D(1), "WARNING": D("0.5"), "COOLDOWN": D("0.25"), "HALTED": D("0.1")}},
    ]
    for patch in sm_breaks:
        bad = {**base, "state_machine": {**base["state_machine"], **patch}}
        with pytest.raises(ConfigValidationError):
            load_risk_config(bad)
    for section, patch in (("sizing", {"scale_min": 4.0}), ("hysteresis", {"exit": 0.3})):
        bad = {**base, section: {**base[section], **patch}}
        with pytest.raises(ConfigValidationError):
            load_risk_config(bad)
    text = "limits: {}\nstate_machine: {}\n"
    with pytest.raises(ConfigValidationError):
        load_risk_config(text)


def test_rules_veto_increase_on_non_positive_equity() -> None:
    broke = ctx(equity=D(-1), target_qty=D(1))
    for rule in (MaxPositionNotional(), MaxGrossLeverage(), LiquidationBufferGuard()):
        rv = rule.check(broke)
        assert rv.verdict is VETO and rv.observed is None
        assert rule.check(ctx(equity=D(-1), current_qty=D(2), target_qty=D(1))).verdict is ALLOW
    daily = MaxDailyLoss().check(ctx(equity_day_open=D(0), pnl_day=D(0)))
    assert daily.verdict is VETO and daily.observed is None
    assert LiquidationBufferGuard().check(ctx(target_qty=D(0), current_qty=D(3))).observed is None
    assert LiquidationBufferGuard().check(ctx(atr=D(0))).verdict is VETO
