"""Обчислювальний експеримент «ablation» (брифінг §11 — LinearVoteEngine як базова лінія; §14 2.6, 2.11).

Найменування: ablation.py
Призначення: внесок кожного компонента ядра на OOS-вікнах walk-forward (і/або повному вікні):
    leave-one-detector-out (6 варіантів; без VolRegime вхід V фіксовано нейтральним V = 0.5 —
    decision.aggregator.V_DEFAULT, сподівання перцентильного рангу), Мамдані проти LinearVoteEngine, κ-гасіння
    ввімкнено/вимкнено (κ ≡ 1 через κ_min = 1), гістерезис увімкнено/вимкнено (u_exit = u_enter) → таблиця
    і різниці до базової лінії. Решта конфігурації — та сама (одна змінна за раз).
Автор: Андрій Жук, 2026.

Запуск (димовий): uv run python scripts/ablation.py --fixture --smoke --workers 2
Повний (наступна хвиля): uv run python scripts/ablation.py --db --symbol BTCUSDT --workers 8 \
    --out docs/report_tables/raw/ablation_BTCUSDT
Вивід (--out): ablation.{json,csv,md}, ablation_folds.{json,csv,md}, ablation.timing.json.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from fuzzhelm.backtest import experiments_analysis as ea

EXPERIMENT = "ablation"
DELTA_KEYS = ("sharpe", "total_return", "max_drawdown", "turnover", "n_trades", "fees")


def deltas(run: ea.VariantRun, scope: str) -> list[dict[str, Any]]:
    base = run.summaries[scope]["baseline"]
    out = []
    for v in run.variants:
        s = run.summaries[scope][v.key]
        out.append({"variant": v.key, "label": v.label, "scope": scope,
                    **{f"d_{k}": s[k] - base[k] for k in DELTA_KEYS}})
    return out


def render(run: ea.VariantRun, v_mu: dict[str, float]) -> tuple[str, dict[str, Any]]:
    d_all: dict[str, Any] = {}
    mu = ", ".join(f"μ_{k}({ea.V_NEUTRAL}) = {x:.4f}" for k, x in v_mu.items())
    lines = [
        "# Ablation ядра рішень: детектори, Мамдані проти лінійного голосування, κ, гістерезис",
        "",
        "Згенеровано `scripts/ablation.py`. Автор: Андрій Жук, 2026. Числа — лише з цього прогону.",
        "",
        ea.passport_md(run.passport),
        "",
        "Протокол: базова конфігурація (профіль `backtest`"
        + (f" + `--params {json.dumps(run.passport['params'])}`" if run.passport["params"] else "")
        + ") і 9 варіантів, у кожному змінено рівно одне: вилучено один із 6 детекторів; `engine = linear` "
        "(u = 0.5·T + 0.5·R); κ ≡ 1 (`agreement.kappa_min = 1`); без гістерезису (`u_exit = u_enter`). "
        f"Фолди — `{run.layout}` ({len(run.folds)} шт.), OOS-сегмент = [oos_start − W, oos_end), "
        f"W = {run.passport['warmup_bars']}; зведення OOS — зчеплена крива. Вибір параметрів: "
        + ("фіксовані для всіх варіантів і фолдів." if run.selection == "fixed"
           else f"сітка з {len(run.cells or [])} клітинок на IS кожного фолду для кожного варіанта окремо, "
                + ea.is_rule_text(run.passport) + ".")
        + (" Контур ризику ПОСЛАБЛЕНО (`--risk-loop relaxed`)." if run.passport["risk_loop"] == "relaxed"
           else ""),
        "",
        *ea.selection_caveat(run.passport),
        f"Без VolRegime вхід V ≡ {ea.V_NEUTRAL} (нейтральне значення `decision.aggregator.V_DEFAULT`: "
        f"сподівання перцентильного рангу за відсутності інформації); за каліброваними МФ це {mu} — база "
        "правил бачить суміш політик MID/HI/LO, а не одну.",
        "",
    ]
    for scope in run.scopes:
        d = deltas(run, scope)
        d_all[scope] = d
        lines += [f"## {ea.SCOPE_TITLES[scope]}", "", ea.summary_table(run, scope), "",
                  "Різниця до базової лінії (варіант − базова):", "",
                  ea.md_table(["варіант", "ΔШарп", "Δдохідність", "ΔMaxDD", "Δоборот", "Δугод",
                               "Δкомісії, USDT"],
                              [[r["label"], *[r[f"d_{k}"] for k in DELTA_KEYS]] for r in d]),
                  ""]
        s = run.summaries[scope]
        lines.append(f"Мамдані проти лінійного голосування ({ea.SCOPE_TITLES[scope]}): Шарп "
                     f"{ea.fmt(s['baseline']['sharpe'])} проти {ea.fmt(s['linear']['sharpe'])}, дохідність "
                     f"{ea.fmt(s['baseline']['total_return'])} проти {ea.fmt(s['linear']['total_return'])}, "
                     f"угод {s['baseline']['n_trades']} проти {s['linear']['n_trades']}.")
        lines.append("")
    if "oos" in run.scopes:
        lines += ["## По фолдах (OOS)", "", ea.folds_table(run, "oos"), ""]
    if run.chosen:
        lines += ["## Параметри, вибрані на IS", "", ea.chosen_table(run), ""]
    return "\n".join(lines), d_all


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Ablation: leave-one-detector-out, Mamdani vs linear, "
                                             "kappa on/off, hysteresis on/off on walk-forward OOS folds.")
    ea.add_common_args(ap, EXPERIMENT)
    ea.add_variant_args(ap)
    args = ap.parse_args(argv)
    full_argv = [sys.argv[0], *(sys.argv[1:] if argv is None else argv)]
    run = ea.run_variant_experiment(args, EXPERIMENT, full_argv, ea.ablation_variants)
    v_mu = ea.neutral_v_memberships(run.variants[0].config)
    md, d_all = render(run, v_mu)
    rows = ea.summary_rows(run)
    result = {"experiment": EXPERIMENT, "passport": run.passport, "summary": rows, "deltas": d_all,
              "neutral_v": {"v": ea.V_NEUTRAL, "memberships": v_mu}, "chosen": run.chosen,
              "choice_info": run.choice_info}
    paths = ea.write_outputs(args.out, EXPERIMENT, result=result, md=md, rows=rows)
    fold_rows = ea.fold_rows(run.results, {v.key: v.label for v in run.variants})
    paths += ea.write_outputs(args.out, f"{EXPERIMENT}_folds",
                              result={"experiment": EXPERIMENT, "passport": run.passport, "folds": fold_rows},
                              md=ea.folds_table(run, "oos") if "oos" in run.scopes else "", rows=fold_rows)
    timing = {"wall_s": run.wall_s, "runs": run.n_runs, "workers": args.workers, "bars": len(run.dataset)}
    (args.out / f"{EXPERIMENT}.timing.json").write_text(json.dumps(timing) + "\n", encoding="utf-8")
    for scope in run.scopes:
        print(f"[{scope}]")
        for v in run.variants:
            s = run.summaries[scope][v.key]
            print(f"  {v.key:<22} sharpe {s['sharpe']:>10.4g}  return {s['total_return']:>10.5f}  "
                  f"maxdd {s['max_drawdown']:.5f}  trades {s['n_trades']:>5}  fees {s['fees']:>9.2f}")
    print(f"wall {run.wall_s:.1f} s, {run.n_runs} engine runs, workers={args.workers}")
    print("wrote " + ", ".join(str(p) for p in paths))
    return 0


if __name__ == "__main__":
    sys.exit(main())
