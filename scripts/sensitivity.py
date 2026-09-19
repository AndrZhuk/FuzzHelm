"""Одновимірна (one-at-a-time) чутливість Sharpe, MaxDD і обороту до 8 параметрів; таблиця «торнадо»; фаза 7.

Найменування: scripts/sensitivity.py
Призначення: брифінг §12 фаза 7 («sensitivity.py (8 параметрів)»), §14 п. 2.11, §15 сцена 3:20–4:10, §16 ФК7:
    як змінюється результат при варіації кожного з 8 параметрів ланцюга «рішення → розмір» навколо бази
    (решта — база): u_enter, u_exit, χ_ATR, ρ_base, λ_EWMA, σ_target, κ_min, n_ATR; рівні — ±половина і ±повна
    амплітуда (±20–50 %, для λ — пам'ять EWMA 1/(1−λ) ± 50 %). Ціль: конкатенований OOS фолдів walk-forward
    (--target oos, типово: ті самі вікна, що й у run_walkforward.py, але без переналаштування) або все вікно
    (--target full).
Автор: Андрій Жук, 2026.

Запуск (повний):  uv run python scripts/sensitivity.py --db --symbol BTCUSDT --target oos --workers 8
Швидка перевірка: uv run python scripts/sensitivity.py --fixture --smoke --workers 2

Вибір 8 параметрів: це неперервні параметри рішення і сайзера, задані експертно (МФ калібровано з даних §5.5,
ліміти ризику — обмеження безпеки, а не налаштування; детектори — окремий ablation; модель витрат — окремий
експеримент §5.14). База — дефолтна конфігурація config/*.yaml (профіль backtest). Обґрунтування кожної
амплітуди — backtest.experiments_search.SENS_SPECS і docs/api/exp_search.md.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from datetime import UTC, datetime
from typing import Any

from fuzzhelm.backtest.engine import BacktestConfig
from fuzzhelm.backtest.experiments_search import (
    CRITERIA,
    DEFAULT_LEVELS,
    SENS_NAMES,
    add_common_args,
    apply_param,
    concat_oos_metrics,
    fmt,
    git_state,
    json_safe,
    make_task,
    md_table,
    oos_segment,
    profile_seed,
    provenance,
    provenance_md,
    resolve_dataset,
    resolve_out,
    segment_task,
    sensitivity_space,
    smoke_folds,
    strip_arrays,
    tornado_rows,
)
from fuzzhelm.backtest.grid import load_grid_space, make_grid
from fuzzhelm.backtest.parallel import run_parallel
from fuzzhelm.backtest.walkforward import folds_from_profile

NAME = "sensitivity"
REPORT_KEYS = ("sharpe", "max_drawdown", "turnover", "total_return", "n_trades", "psr")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="FuzzHelm one-at-a-time sensitivity to 8 parameters (tornado)")
    add_common_args(ap)
    ap.add_argument("--engine", choices=("mamdani", "linear"), default="mamdani")
    ap.add_argument("--target", choices=("oos", "full"), default="oos",
                    help="oos: concatenated OOS of the walk-forward folds; full: the whole window")
    ap.add_argument("--params", default=None, help=f"comma list, subset of {','.join(SENS_NAMES)}")
    ap.add_argument("--base-cell", type=int, default=None,
                    help="use this cell of the 108-cell grid as the base (e.g. run_grid's working point)")
    ap.add_argument("--levels", default=",".join(str(x) for x in DEFAULT_LEVELS),
                    help="levels as fractions of each parameter's span (default -1,-0.5,0.5,1)")
    args = ap.parse_args(argv)
    argv_full = [sys.argv[0], *(sys.argv[1:] if argv is None else argv)]
    names = SENS_NAMES if args.params is None else tuple(x.strip() for x in args.params.split(","))
    if args.smoke and args.params is None:
        names = ("u_enter", "sigma_target", "kappa_min")
    levels = tuple(float(x) for x in args.levels.split(","))
    if args.smoke and args.levels == ",".join(str(x) for x in DEFAULT_LEVELS):
        levels = (-1.0, 1.0)
    seed = profile_seed("backtest") if args.seed is None else args.seed
    gs = git_state()                               # стан коду ДО обчислень (воркери імпортують його з диска)
    out = resolve_out(args, NAME)
    ds = resolve_dataset(args)
    base = BacktestConfig.from_profile("backtest", engine=args.engine, cooldown_policy=args.cooldown_policy)
    if args.base_cell is not None:
        base = base.with_params(**make_grid(load_grid_space())[args.base_cell])
    space = sensitivity_space(base, levels=levels, names=names)
    variants: list[tuple[str | None, float | None, BacktestConfig]] = [(None, None, base)]
    variants += [(sp.name, v, apply_param(base, sp.name, v)) for sp in space for v in sp.values]
    warmup = base.resolved_warmup()
    emb = 2 * base.feature_params().max_lookback
    if args.target == "oos":
        folds = (smoke_folds(len(ds), embargo_bars=emb, warmup=warmup) if args.smoke
                 else folds_from_profile(len(ds), max_lookback=base.feature_params().max_lookback))
        segs = [(oos_segment(f, warmup), f) for f in folds]
        payloads = [ds.slice(start, f.oos_end).to_payload() for (start, _), f in segs]
        tasks = [make_task(pl, cfg.to_dict(), seed, eval_start=ev, keep_equity=True)
                 for _, _, cfg in variants for pl, ((_, ev), _) in zip(payloads, segs, strict=True)]
    else:
        folds = []
        payload = ds.to_payload()
        tasks = [make_task(payload, cfg.to_dict(), seed) for _, _, cfg in variants]
    per = len(folds) if folds else 1
    print(f"dataset {ds.source}: {len(ds)} bars; {len(space)} parameters, {len(variants)} configurations "
          f"x {per} segment(s) = {len(tasks)} tasks; target {args.target}; workers {args.workers}")
    t0 = time.perf_counter()
    res = run_parallel(segment_task, tasks, args.workers, seed=seed)
    wall = time.perf_counter() - t0
    metrics: list[dict[str, float]] = []
    for k in range(len(variants)):
        chunk = res[k * per:(k + 1) * per]
        if folds:
            metrics.append(concat_oos_metrics(chunk, ds.bars_per_year))
        else:
            metrics.append({kk: v for kk, v in strip_arrays(chunk[0]).items() if kk in REPORT_KEYS})
    base_m = metrics[0]
    runs: list[dict[str, Any]] = [{"param": p, "value": v, "config_hash": cfg.config_hash, "metrics": m}
                                  for (p, v, cfg), m in zip(variants[1:], metrics[1:], strict=True)]
    rows = tornado_rows(base_m, runs, space, keys=CRITERIA)

    created = datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    prov = provenance(argv_full, ds, base, seed=seed, workers=args.workers, created_utc=created, git=gs,
                      extra={"script": NAME, "smoke": args.smoke, "target": args.target,
                             "base_cell": args.base_cell,
                             "levels": list(levels),
                             "oos_folds": len(folds), "wall_s": wall})
    stem = f"{NAME}_{ds.instrument.symbol_venue}_{args.engine}_{args.target}"
    result = {"provenance": prov, "target": args.target, "levels": list(levels),
              "space": [{"name": sp.name, "symbol": sp.symbol, "label_uk": sp.label_uk, "where": sp.where,
                         "base": sp.base, "values": list(sp.values), "span": sp.span,
                         "rationale": sp.rationale}
                        for sp in space],
              "base": {"config_hash": base.config_hash, "metrics": base_m}, "runs": runs, "tornado": rows}
    (out / f"{stem}.json").write_text(json.dumps(json_safe(result), ensure_ascii=False, indent=1),
                                      encoding="utf-8")
    with (out / f"{stem}.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["param", "value", "is_base", "config_hash", *REPORT_KEYS])
        w.writerow(["base", "", 1, base.config_hash, *(base_m.get(k, "") for k in REPORT_KEYS)])
        for r in runs:
            w.writerow([r["param"], r["value"], 0, r["config_hash"],
                        *(r["metrics"].get(k, "") for k in REPORT_KEYS)])
    (out / f"{stem}.md").write_text(sens_md(prov, space, base_m, runs, rows, args.target), encoding="utf-8")
    print(f"wall {wall:.1f} s; base sharpe {base_m['sharpe']:.3f}; swing order (sharpe): "
          + ", ".join(f"{r['param']} {r['sharpe_swing']:.3g}" for r in rows))
    print(f"outputs {out / stem}.*")
    return 0


def sens_md(prov: dict[str, Any], space: list[Any], base_m: dict[str, float], runs: list[dict[str, Any]],
            rows: list[dict[str, Any]], target: str) -> str:
    tgt = "конкатенований OOS фолдів walk-forward (параметри фіксовані, без переналаштування)" \
        if target == "oos" else "усе вікно"
    cell = prov.get("base_cell")
    base_txt = "дефолтна конфігурація config/*.yaml" if cell is None else f"клітинка сітки {cell}"
    lines = [f"# Чутливість до {len(space)} параметрів ({prov['dataset']['symbol']}, `{prov['engine']}`)", "",
             f"Згенеровано `scripts/sensitivity.py`. Ціль: {tgt}. Одновимірна схема: змінюється один "
             f"параметр, решта — база ({base_txt}).", "", provenance_md(prov), "",
             "## Параметри і рівні", "",
             md_table(["параметр", "де", "база", "рівні", "амплітуда", "чому так"],
                      [[f"{sp.symbol} ({sp.label_uk})", f"`{sp.where}`", sp.base,
                        ", ".join(fmt(v) for v in sp.values), f"±{sp.span:.0%}", sp.rationale]
                       for sp in space]),
             "", "## Торнадо (упорядковано за розмахом Шарпа)", "",
             md_table(["параметр", "низьке → високе", "SR: низьке / база / високе", "розмах SR",
                       "MaxDD: низьке / база / високе", "розмах MaxDD", "оборот: низьке / база / високе",
                       "еластичність SR"],
                      [[r["symbol"], f"{fmt(r['low_value'])} → {fmt(r['high_value'])}",
                        f"{fmt(r['sharpe_low'])} / {fmt(r['sharpe_base'])} / {fmt(r['sharpe_high'])}",
                        r["sharpe_swing"],
                        f"{fmt(r['max_drawdown_low'])} / {fmt(r['max_drawdown_base'])} / "
                        f"{fmt(r['max_drawdown_high'])}", r["max_drawdown_swing"],
                        f"{fmt(r['turnover_low'])} / {fmt(r['turnover_base'])} / {fmt(r['turnover_high'])}",
                        r["sharpe_elasticity"]] for r in rows]), "",
             "Розмах — max − min метрики по всіх рівнях параметра разом із базою; еластичність — дугова між "
             "крайніми рівнями (ΔSR/|SR₀|)/(Δθ/θ₀), — якщо |SR₀| ≈ 0.", "", "## Усі прогони", "",
             md_table(["параметр", "значення", *REPORT_KEYS],
                      [["база", "—", *(base_m.get(k) for k in REPORT_KEYS)]]
                      + [[r["param"], r["value"], *(r["metrics"].get(k) for k in REPORT_KEYS)]
                         for r in runs]), ""]
    return "\n".join(lines)


if __name__ == "__main__":
    sys.exit(main())
