"""RiskGuard — ланцюг ризик-перевірок: кожне правило → вердикт → журнал → композиція → дозволена кількість.

Найменування: risk/guard.py
Призначення: єдина точка, через яку намір сайзера перетворюється на дозволену цільову позицію.
Автор: Андрій Жук, 2026.

Порядок оцінювання:
  1. кожне з правил (6 лімітів §5.11) + вбудований гейт режиму (RiskModeGate: COOLDOWN — reduce-only,
     HALTED або спрацьований kill-switch — flatten-all) перевіряє контекст;
  2. КОЖЕН RuleVerdict пишеться в RiskJournal (rule, verdict, factor, observed, limit);
  3. V = compose(вердикти) — порядок правил не впливає (комутативний моноїд, verdict.py);
  4. дозволений приріст = floor_to_step(exposure(V, increase)); цільова позиція = знак·(base + приріст).
Зменшення/закриття проходить завжди: вердикти гейтять лише приріст. Якщо будь-яке правило подало сигнал
HALT або режим HALTED/kill-switch — дозволена ціль 0 для цього інструмента і flatten_all=True для рушія.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from fuzzhelm.core.digest import to_canonical
from fuzzhelm.core.enums import RiskState
from fuzzhelm.core.money import D0, D1, dec, floor_qty
from fuzzhelm.risk.config import RiskConfig, load_risk_config
from fuzzhelm.risk.context import RiskContext
from fuzzhelm.risk.journal import RiskJournal
from fuzzhelm.risk.killswitch import KillSwitch
from fuzzhelm.risk.rules.liquidation_buffer import LiquidationBufferGuard
from fuzzhelm.risk.rules.max_daily_loss import MaxDailyLoss
from fuzzhelm.risk.rules.max_drawdown_halt import MaxDrawdownHalt
from fuzzhelm.risk.rules.max_gross_leverage import MaxGrossLeverage
from fuzzhelm.risk.rules.max_position_notional import MaxPositionNotional
from fuzzhelm.risk.rules.stale_data import StaleDataGuard
from fuzzhelm.risk.state import SEVERITY
from fuzzhelm.risk.verdict import ALLOW, VETO, RiskRule, RuleVerdict, Verdict, compose, exposure


class RiskModeGate:
    """Гейт режиму автомата: COOLDOWN — reduce-only, HALTED / kill-switch — жодного приросту + flatten-all.

    observed = рівень тяжкості стану (NORMAL 0, WARNING 1, COOLDOWN 2, HALTED 3; kill-switch → 3),
    limit = 1 (найвищий рівень, у якому приріст експозиції ще дозволений).
    """

    name = "risk_mode"
    limit = D1

    def __init__(self, killswitch: KillSwitch | None = None) -> None:
        self.killswitch = killswitch

    def check(self, ctx: RiskContext) -> RuleVerdict:
        tripped = self.killswitch is not None and self.killswitch.is_tripped
        state = RiskState.HALTED if tripped else ctx.risk_state
        level = dec(SEVERITY[state])
        blocked = level > self.limit
        payload: dict[str, object] = {
            "state": ctx.risk_state.value, "kill_switch": tripped,
            "reduce_only": blocked, "flatten_all": state is RiskState.HALTED,
            "increase_qty": ctx.increase_qty,
        }
        verdict = VETO if blocked and ctx.increase_qty > 0 else ALLOW
        return RuleVerdict(self.name, verdict, level, self.limit, payload)


@dataclass(frozen=True, slots=True)
class GuardResult:
    verdict: Verdict                         # compose(усіх вердиктів)
    records: tuple[RuleVerdict, ...]         # у порядку оцінювання
    approved_qty: Decimal                    # дозволена ЦІЛЬОВА позиція зі знаком
    requested_qty: Decimal                   # запитана цільова позиція зі знаком
    current_qty: Decimal
    increase_requested: Decimal
    increase_approved: Decimal
    flatten_all: bool                        # HALTED / kill-switch / сигнал HALT: закрити всі позиції
    halt: bool                               # хоч одне правило подало сигнал HALT

    @property
    def order_qty(self) -> Decimal:
        """Зміна позиції, яку треба надіслати (зі знаком): approved − current."""
        return self.approved_qty - self.current_qty

    def to_dict(self) -> dict[str, Any]:
        """Канонічна (Decimal → рядок) розкладка для DecisionTrace.risk / ExplainView."""
        out = to_canonical({
            "verdict": self.verdict.kind, "factor": self.verdict.factor_dec,
            "approved_qty": self.approved_qty, "requested_qty": self.requested_qty,
            "current_qty": self.current_qty, "increase_requested": self.increase_requested,
            "increase_approved": self.increase_approved, "flatten_all": self.flatten_all,
            "halt": self.halt,
            "records": [{"rule": r.rule, "verdict": r.verdict.kind, "factor": r.verdict.factor_dec,
                         "observed": r.observed, "limit": r.limit} for r in self.records],
        })
        assert isinstance(out, dict)
        return out


class RiskGuard:
    def __init__(self, rules: Sequence[RiskRule], journal: RiskJournal | None = None, *,
                 killswitch: KillSwitch | None = None, mode_gate: bool = True) -> None:
        names = [r.name for r in rules]
        if len(set(names)) != len(names):
            raise ValueError(f"duplicate rule names in the chain: {names}")
        chain: list[RiskRule] = list(rules)
        if mode_gate:
            chain.append(RiskModeGate(killswitch))
        self.rules: tuple[RiskRule, ...] = tuple(chain)
        self.journal = journal if journal is not None else RiskJournal()
        self.killswitch = killswitch

    @classmethod
    def from_config(cls, cfg: RiskConfig | None = None, journal: RiskJournal | None = None, *,
                    killswitch: KillSwitch | None = None) -> RiskGuard:
        return cls(default_rules(cfg or load_risk_config()), journal, killswitch=killswitch)

    def evaluate(self, ctx: RiskContext) -> GuardResult:
        records: list[RuleVerdict] = []
        for rule in self.rules:
            rv = rule.check(ctx)
            self.journal.record_rule(ctx.ts_ns, ctx.instrument, rv)
            records.append(rv)
        verdict = compose(r.verdict for r in records)
        increase = ctx.increase_qty
        approved_inc = exposure(verdict, increase)
        if ctx.step_size is not None:
            approved_inc = floor_qty(approved_inc, ctx.step_size)
        halt = any(r.halt for r in records)
        tripped = self.killswitch is not None and self.killswitch.is_tripped
        if halt and self.killswitch is not None:
            self.killswitch.trip("max_drawdown_halt", ctx.ts_ns)
            tripped = True
        flatten = halt or tripped or ctx.risk_state is RiskState.HALTED
        if flatten:
            approved = D0
            approved_inc = D0
        else:
            approved = (ctx.base_qty + approved_inc) * ctx.target_side
            if approved == 0:
                approved = D0                # без «−0» у журналі
        return GuardResult(verdict=verdict, records=tuple(records), approved_qty=approved,
                           requested_qty=ctx.target_qty, current_qty=ctx.current_qty,
                           increase_requested=increase, increase_approved=approved_inc,
                           flatten_all=flatten, halt=halt)


def default_rules(cfg: RiskConfig) -> list[RiskRule]:
    """Шість лімітів §5.11. Порядок на вердикт не впливає (комутативність compose), лише на порядок записів
    у журналі / ExplainView: StaleDataGuard — першим, бо скор якості даних — ПЕРШИЙ вхід ланцюга (§15),
    далі — у порядку config/risk_limits.yaml."""
    lim = cfg.limits
    return [
        StaleDataGuard.from_config(lim.stale_data),
        MaxPositionNotional.from_config(lim.max_position_notional),
        MaxGrossLeverage.from_config(lim.max_gross_leverage),
        MaxDailyLoss.from_config(lim.max_daily_loss),
        MaxDrawdownHalt.from_config(lim.max_drawdown_halt),
        LiquidationBufferGuard.from_config(lim.liquidation_buffer),
    ]
