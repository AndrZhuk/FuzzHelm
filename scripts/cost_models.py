"""Обчислювальний експеримент «три моделі витрат» (брифінг §5.14, §14 підрозділ 2.8, §15 сцена 3:20–4:10).

Найменування: cost_models.py
Призначення: та сама конфігурація і ті самі фолди walk-forward під режимами моделі витрат
    zero | sqrt_impact | full → таблиця Шарп / дохідність / MaxDD / оборот / комісії / фандинг / ковзання і
    ВИМІРЯНЕ завищення наївної моделі відносно повної (твердження брифінгу «наївна модель завищує Sharpe у
    кілька разів» перевіряється, а не приймається).
Автор: Андрій Жук, 2026.

Запуск (димовий): uv run python scripts/cost_models.py --fixture --smoke --workers 2
Повний (наступна хвиля): uv run python scripts/cost_models.py --db --symbol BTCUSDT --workers 8 \
    --out docs/report_tables/raw/cost_models_BTCUSDT
Вивід (--out): cost_models.{json,csv,md}, cost_models_folds.csv, cost_models.timing.json.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from typing import Any

from fuzzhelm.backtest import experiments_analysis as ea

EXPERIMENT = "cost_models"


def verdict_lines(cmp: dict[str, Any], naive: str, scope: str) -> list[str]:
    """Текст висновку лише з виміряних чисел: відношення має сенс тільки для двох додатних Шарпів."""
    a, b = cmp["reference"], cmp["other"]
    head = (f"* {ea.SCOPE_TITLES[scope]}: Шарп `full` = {ea.fmt(a)}, `{naive}` = {ea.fmt(b)}; "
            f"різниця {ea.fmt(cmp['diff'])}")
    if not (math.isfinite(a) and math.isfinite(b)):
        return [head + " — Шарп не визначений, порівняння неможливе."]
    if math.isfinite(cmp["ratio"]):
        k = cmp["ratio"]
        word = "ПІДТВЕРДЖЕНО" if k >= 2 else "НЕ підтверджено"
        return [head + f"; відношення {k:.3g}× — твердження «у кілька разів» {word} на цій вибірці."]
    if b <= a:
        return [head + f"; `{naive}` НЕ кращий за `full` — твердження брифінгу на цій вибірці "
                       "не підтверджено."]
    if cmp["sign_flip"]:
        return [head + "; знак Шарпа різний: наївна модель перетворює збиткову стратегію на прибуткову — "
                       "«у k разів» тут не визначене, завищення = різниця."]
    return [head + "; наївна модель дає більший Шарп, але відношення не визначене (Шарп `full` ≤ 0) — "
                   "завищення = різниця."]


def render(run: ea.VariantRun) -> tuple[str, dict[str, Any]]:
    over: dict[str, Any] = {}
    lines = [
        "# Три моделі витрат виконання (§5.14): наскільки наївна модель завищує результат",
        "",
        "Згенеровано `scripts/cost_models.py`. Автор: Андрій Жук, 2026. Числа — лише з цього прогону.",
        "",
        ea.passport_md(run.passport),
        "",
        "Протокол: одна конфігурація (профіль `backtest`"
        + (f" + `--params {json.dumps(run.passport['params'])}`" if run.passport["params"] else "")
        + f"), режими `cost_mode` = zero | sqrt_impact | full; фолди — `{run.layout}` "
        f"({len(run.folds)} шт.), OOS-сегмент = [oos_start − W, oos_end), W = {run.passport['warmup_bars']} "
        "(прогрів усередині embargo); кожен фолд стартує з E₀ і NORMAL; зведення OOS — зчеплена крива "
        "з конкатенованих дохідностей. Вибір параметрів: "
        + ("фіксовані на всіх фолдах." if run.selection == "fixed"
           else f"сітка з {len(run.cells or [])} клітинок на IS кожного фолду ОКРЕМО для кожного режиму "
                "(наївний дослідник і оптимізує на наївній моделі), " + ea.is_rule_text(run.passport)
                + " → OOS.")
        + (" Контур ризику ПОСЛАБЛЕНО (`--risk-loop relaxed`, лише ізоляція ефекту)."
           if run.passport["risk_loop"] == "relaxed" else ""),
        "",
        *ea.selection_caveat(run.passport),
        "`ковзання ≈` — Σ qty·price·slippage_bps/10⁴ (відносно P_ref, спред + √-імпакт + шум); у `zero` — 0.",
        "",
    ]
    for scope in run.scopes:
        s = run.summaries[scope]
        over[scope] = {f"{m}_vs_full": {k: ea.compare(s["full"], s[m], k) for k in ("sharpe", "total_return")}
                       for m in ("zero", "sqrt_impact")}
        lines += [f"## {ea.SCOPE_TITLES[scope]}", "", ea.summary_table(run, scope), ""]
        lines += ["Завищення відносно `full`:", ""]
        for m in ("zero", "sqrt_impact"):
            lines += verdict_lines(over[scope][f"{m}_vs_full"]["sharpe"], m, scope)
        lines += [
            "",
            ea.md_table(["порівняння", "метрика", "full", "наївна", "різниця", "відношення", "зміна знака"],
                        [[f"{m} vs full", k, c["reference"], c["other"], c["diff"], c["ratio"],
                          c["sign_flip"]]
                         for m in ("zero", "sqrt_impact") for k, c in over[scope][f"{m}_vs_full"].items()]),
            "",
        ]
    if "oos" in run.scopes:
        lines += ["## По фолдах (OOS)", "", ea.folds_table(run, "oos"), ""]
    if run.chosen:
        lines += ["## Параметри, вибрані на IS (для кожного режиму окремо)", "", ea.chosen_table(run), ""]
    return "\n".join(lines), over


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Three execution-cost models (zero / sqrt_impact / full) "
                                             "on the same config and walk-forward folds (brief §5.14).")
    ea.add_common_args(ap, EXPERIMENT)
    ea.add_variant_args(ap)
    args = ap.parse_args(argv)
    full_argv = [sys.argv[0], *(sys.argv[1:] if argv is None else argv)]
    run = ea.run_variant_experiment(args, EXPERIMENT, full_argv, ea.cost_variants)
    md, over = render(run)
    rows = ea.summary_rows(run)
    result = {"experiment": EXPERIMENT, "passport": run.passport, "summary": rows, "overstatement": over,
              "chosen": run.chosen, "choice_info": run.choice_info}
    paths = ea.write_outputs(args.out, EXPERIMENT, result=result, md=md, rows=rows)
    fold_rows = ea.fold_rows(run.results, {v.key: v.label for v in run.variants})
    paths += ea.write_outputs(args.out, f"{EXPERIMENT}_folds", result={"experiment": EXPERIMENT,
                                                                      "passport": run.passport,
                                                                      "folds": fold_rows},
                              md=ea.folds_table(run, "oos") if "oos" in run.scopes else "", rows=fold_rows)
    timing = {"wall_s": run.wall_s, "runs": run.n_runs, "workers": args.workers, "bars": len(run.dataset)}
    (args.out / f"{EXPERIMENT}.timing.json").write_text(json.dumps(timing) + "\n", encoding="utf-8")
    for scope in run.scopes:
        print(f"[{scope}]")
        for v in run.variants:
            s = run.summaries[scope][v.key]
            print(f"  {v.key:<12} sharpe {s['sharpe']:>10.4g}  return {s['total_return']:>10.5f}  "
                  f"maxdd {s['max_drawdown']:.5f}  fees {s['fees']:>10.2f}  trades {s['n_trades']}")
    print(f"wall {run.wall_s:.1f} s, {run.n_runs} engine runs, workers={args.workers}")
    print("wrote " + ", ".join(str(p) for p in paths))
    return 0


if __name__ == "__main__":
    sys.exit(main())
