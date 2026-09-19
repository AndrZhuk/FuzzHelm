"""Генератор statechart автомата ризик-станів з ФАКТИЧНОЇ таблиці переходів коду.

Найменування: docs/diagrams/gen_risk_fsm.py
Призначення: побудувати docs/diagrams/risk_fsm.puml безпосередньо з
    fuzzhelm.risk.state.TRANSITIONS (тотальна таблиця 4×6) і config/risk_limits.yaml (пороги,
    витримки, κ_mode, cooldown_policy), щоб діаграма не могла розійтися з кодом.
Автор: Андрій Жук, 2026.

Запуск із кореня репозиторію (лише читає код і конфігурацію, у src/ нічого не пише):
    PYTHONDONTWRITEBYTECODE=1 .venv/bin/python docs/diagrams/gen_risk_fsm.py
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path

from fuzzhelm.core.enums import RiskState
from fuzzhelm.risk.config import load_risk_config
from fuzzhelm.risk.state import TRANSITIONS, RiskEvent

OUT = Path(__file__).with_name("risk_fsm.puml")

# умови подій — дослівно з класифікатора RiskStateMachine.classify (risk/state.py); числа — з конфігурації
EVENT_GUARD = {
    RiskEvent.HALT_BREACH: "kill-switch ∨ DD ≥ {halt_enter} ∨ PnL_day ≤ −{halt_daily_loss}·E_open",
    RiskEvent.COOL_BREACH: "DD ≥ {cool_enter} ∨ PnL_day ≤ −{cool_daily_loss}·E_open",
    RiskEvent.WARN_BREACH: "DD ≥ {warn_enter} ∨ σ_ann/σ_base > {warn_vol_ratio}",
    RiskEvent.ADMIN_RELEASE: "release(Role.ADMIN) або KillSwitch.release(Role.ADMIN)\\n"
                             "(тоді — на наступному барі); пік і E_open перебазовано",
}
# У WARNING класифікатор перевіряє WARN_BREACH раніше за RECOVERY, тож повернення в NORMAL блокує і σ-тригер
# (R-08): при DD ≤ warn_exit < warn_enter ефективна умова — ще й ¬(σ_ann/σ_base > warn_vol_ratio).
RECOVERY_GUARD = {
    RiskState.WARNING: "DD ≤ {warn_exit} ∧ dwell ≥ {warn_dwell} ∧ ¬(σ_ann/σ_base > {warn_vol_ratio})",
    RiskState.COOLDOWN: "DD ≤ {cool_exit} ∧ dwell ≥ {cool_dwell}",
}


def main() -> None:
    sm = load_risk_config().state_machine
    p = {k: getattr(sm, k) for k in ("halt_enter", "halt_daily_loss", "cool_enter", "cool_daily_loss",
                                     "warn_enter", "warn_vol_ratio", "warn_exit", "warn_dwell",
                                     "cool_exit", "cool_dwell")}
    kappa = {s: sm.kappa_mode[s] for s in RiskState}
    policy = sm.cooldown_policy.value

    # групуємо переходи за (from, to); петлі (self-loop) рахуємо окремо — вони лише для таблиці
    edges: dict[tuple[RiskState, RiskState], list[RiskEvent]] = defaultdict(list)
    loops: dict[RiskState, list[RiskEvent]] = defaultdict(list)
    for (src, ev), dst in TRANSITIONS.items():
        (loops[src] if src is dst else edges[(src, dst)]).append(ev)

    lines = [
        "@startuml risk_fsm",
        "' ЗГЕНЕРОВАНО docs/diagrams/gen_risk_fsm.py з fuzzhelm.risk.state.TRANSITIONS",
        f"' ({len(TRANSITIONS)} клітинок) і config/risk_limits.yaml. Не редагувати вручну.",
        "title Автомат ризик-станів (risk/state.py, RiskStateMachine)",
        "hide empty description",
        "skinparam defaultFontName Helvetica",
        "skinparam state {\n  BackgroundColor<<latch>> #FDE2E2\n}",
    ]
    for s in RiskState:
        stereo = " <<latch>>" if s is RiskState.HALTED else ""
        lines.append(f'state {s.value}{stereo} : κ_mode = {kappa[s]}')
    lines.append(f"[*] --> {RiskState.NORMAL.value}")
    for (src, dst), evs in sorted(edges.items(), key=lambda kv: (kv[0][0].value, kv[0][1].value)):
        labels = []
        for ev in evs:
            if ev is RiskEvent.RECOVERY:
                guard = RECOVERY_GUARD[src].format(**p)
            else:
                guard = EVENT_GUARD[ev].format(**p)
            labels.append(f"{ev.value}\\n[{guard}]")
        lines.append(f"{src.value} --> {dst.value} : " + "\\n".join(labels))
    self_rows = [f"    {s.value}: " + ", ".join(e.value for e in loops[s]) for s in RiskState if loops[s]]
    lines += [
        "note right of COOLDOWN",
        f"  cooldown_policy = {policy} (config/risk_limits.yaml, RF-01):",
        "  scaled_entries — новий вхід дозволено, розмір × κ_mode = 0.25;",
        "    наявну позицію не збільшувати;",
        "  reduce_only — буквально §5.12: жодного приросту;",
        "    пласка книга при DD > cool_exit — поглинаючий стан (ENG-14).",
        "  Політику застосовує гейт risk_mode (RiskModeGate),",
        "  таблиця переходів від неї не залежить.",
        "end note",
        "note left of HALTED",
        "  засувний: виходу за даними немає,",
        "  лише ADMIN_RELEASE — RiskStateMachine.release(Role.ADMIN)",
        "  (API: POST /risk/killswitch/release → команда воркеру) або",
        "  KillSwitch.release(Role.ADMIN), після чого update() наступного бару",
        "  синхронізує автомат; інша роль → PermissionDeniedError;",
        "  вхід у HALTED спрацьовує kill-switch, flatten-all",
        "end note",
        "legend bottom",
        "  Класифікація подій за пріоритетом: HALT_BREACH → COOL_BREACH → (COOLDOWN: RECOVERY|STEADY)",
        "  → WARN_BREACH → (WARNING: RECOVERY) → STEADY. У COOLDOWN подія WARN_BREACH не генерується.",
        "  Умова на ребрі діє лише тоді, коли жодна подія вищого пріоритету не спрацювала: напр. RECOVERY",
        f"  вимагає ще й PnL_day > −{p['cool_daily_loss']}·E_open (інакше COOL_BREACH).",
        "  dwell — барів у поточному стані (скидається лише при зміні стану).",
        f"  Петлі (стан не змінюється) — {sum(len(v) for v in loops.values())} клітинок:",
        *self_rows,
        "endlegend",
        "@enduml",
    ]
    OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({len(edges)} edges, {sum(len(v) for v in loops.values())} self-loops)")


if __name__ == "__main__":
    main()
