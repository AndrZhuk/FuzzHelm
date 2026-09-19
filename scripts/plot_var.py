"""Рисунок «гістограма доходностей з лініями VaR₉₅/CVaR₉₅» (брифінг §5.13, §14 підрозділ 2.7).

Найменування: plot_var.py
Призначення: гістограма одно-барових доходностей портфеля (логарифмічна вісь частот: пік нулів пласкої книги
    інакше сховав би хвости) з вертикалями −VaR₉₅ (історичний), −CVaR₉₅ (історичний) і −VaR₉₅ (параметричний
    z·σ) — окремо для всіх барів і для барів у позиції. Джерела: каталог виводу scripts/var_backtest.py
    (var_series.npz, --results) або прогін обраної конфігурації, порахований тут же (--db / --fixture).
Автор: Андрій Жук, 2026.

Запуск (димовий): uv run python scripts/plot_var.py --results artifacts/tmp/var_backtest
                  uv run python scripts/plot_var.py --fixture --smoke
Повний (наступна хвиля): uv run python scripts/plot_var.py --results docs/report_tables/raw/var_BTCUSDT \
    --out docs/figures
Вивід (--out): var_hist_<символ або run_id>_<область>.png, plot_var_<символ>.{json,md}.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

import numpy as np

from fuzzhelm.backtest import experiments_analysis as ea

EXPERIMENT = "plot_var"
SUBSETS = (("all", "усі бари"), ("active", "бари в позиції"))


def series_from_results(d: Path) -> dict[str, dict[str, Any]]:
    z = np.load(d / "var_series.npz")
    out: dict[str, dict[str, Any]] = {}
    for key in z.files:
        scope, name = key.split("__", 1)
        out.setdefault(scope, {})[name] = z[key]
    return out


def series_computed(args: argparse.Namespace,
                    argv: list[str]) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    run = ea.run_variant_experiment(args, EXPERIMENT, argv,
                                    lambda cfg: [ea.Variant("chosen", "обрана конфігурація", cfg)])
    out: dict[str, dict[str, Any]] = {}
    for scope in run.scopes:
        res = run.results["chosen"][scope]
        out[scope] = {
            "returns": np.concatenate([r["series"]["returns"] for r in res]),
            "active": np.concatenate([ea.active_mask(r["series"]["returns"], r["series"]["position"])
                                      for r in res]),
            "equity0": np.asarray(res[0]["costs"]["equity_start"]),
        }
    return out, run.passport


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Return histogram with VaR95/CVaR95 lines (all bars and "
                                             "in-position bars).")
    ea.add_common_args(ap, EXPERIMENT)
    ea.add_variant_args(ap)
    ap.add_argument("--results", type=Path, default=None, help="output directory of var_backtest.py")
    ap.add_argument("--alpha", type=float, default=ea.VAR_ALPHA)
    args = ap.parse_args(argv)
    full_argv = [sys.argv[0], *(sys.argv[1:] if argv is None else argv)]
    t0 = time.perf_counter()
    seed = args.seed if args.seed is not None else ea.profile_seed()
    if args.results:
        series = series_from_results(args.results)
        src = args.results / "var_backtest.json"
        vp = json.loads(src.read_text(encoding="utf-8")).get("passport", {}) if src.exists() else {}
        tag = vp.get("symbol") or (f"run_{str(vp['run_id'])[:8]}" if vp.get("run_id") else args.results.name)
        pp = ea.passport(EXPERIMENT, full_argv, seed=seed, extra={
            "results": str(args.results), "source_passport": vp,
            **{k: vp[k] for k in ("dataset_hash", "source", "n_bars", "symbol", "config_hashes") if k in vp}})
    else:
        series, pp = series_computed(args, full_argv)
        tag = pp.get("symbol", "chosen")
    summary: dict[str, Any] = {}
    figs: list[Path] = []
    rows = []
    for scope, s in series.items():
        r_all = np.asarray(s["returns"], dtype=np.float64)
        panels = []
        for subset, title in SUBSETS:
            r = r_all if subset == "all" else r_all[np.asarray(s["active"], dtype=bool)]
            b = ea.var_block(r, window=ea.VAR_WINDOW, alpha=args.alpha)
            st = b.get("static") or {}
            lines = {"VaR₉₅ іст.": st.get("hist_var"), "CVaR₉₅ іст.": st.get("hist_cvar"),
                     "VaR₉₅ парам.": st.get("param_var")}
            zs = b.get("zero_share")
            ztxt = f", r = 0: {100 * zs:.1f} %" if zs is not None else ""
            panels.append({"title": f"{scope}: {title} (n = {b['n']}{ztxt})", "returns": r, "lines": lines})
            summary[f"{scope}/{subset}"] = {"n": b["n"], "zero_share": zs, **lines}
            rows.append([scope, title, b["n"], zs, *lines.values()])
        fig = ea.plot_var_histogram(args.out / f"var_hist_{tag}_{scope}.png", panels,
                                    title=f"Розподіл доходностей за бар і VaR₉₅/CVaR₉₅ ({scope})")
        figs.append(fig)
    md = "\n".join([
        "# Гістограми доходностей з VaR₉₅/CVaR₉₅", "",
        "Згенеровано `scripts/plot_var.py`. Автор: Андрій Жук, 2026. Оцінки на всій вибірці (опис розподілу; "
        "бектест прогнозу і тест Купця — у виводі `scripts/var_backtest.py`).", "",
        ea.passport_md(pp), "",
        ea.md_table(["область", "підмножина", "n", "частка r = 0", "VaR₉₅ іст.", "CVaR₉₅ іст.",
                     "VaR₉₅ парам."], rows), "",
        *[f"Рисунок: `{f}`." for f in figs], ""])
    result = {"experiment": EXPERIMENT, "passport": pp, "summary": summary, "figures": [str(f) for f in figs]}
    paths = ea.write_outputs(args.out, f"{EXPERIMENT}_{tag}", result=result, md=md)
    print(f"wall {time.perf_counter() - t0:.1f} s")
    print("wrote " + ", ".join(str(p) for p in [*paths, *figs]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
