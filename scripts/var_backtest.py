"""Бектест моделей VaR (брифінг §5.13, §14 2.7): історичний vs параметричний VaR₉₅/CVaR₉₅ + тест Купця.

Найменування: var_backtest.py
Призначення: на доходностях портфеля обраної конфігурації (OOS-фолди walk-forward і/або повне вікно; або крива
    збереженого прогону з БД за run_id) — ковзні історичні VaR₉₅/CVaR₉₅ на вікні W = 500 проти параметричного
    z·σ·√h·E; підрахунок пробоїв прогнозу на крок уперед (VaR_t — лише з r[t−W:t]), LR Купця і рішення при
    χ²₁(0.95) = 3.841; гістограма доходностей з лініями VaR/CVaR; висновок про товсті хвости — з вимірів.
    Пласка книга дає багато нульових доходностей (розподіл вироджений), тому все рахується ДВІЧІ: на всіх
    барах і на барах «у позиції» (позиція на закритті t−1 або t ненульова, або капітал змінився в барі).
Автор: Андрій Жук, 2026.

Запуск (димовий): uv run python scripts/var_backtest.py --fixture --smoke --window 200
                  uv run python scripts/var_backtest.py --run-id 89619416-720b-4880-b4e4-26d2c74bca68
Повний (наступна хвиля): uv run python scripts/var_backtest.py --db --symbol BTCUSDT --workers 4 \
    --params @<файл обраної точки> --out docs/report_tables/raw/var_BTCUSDT
Вивід (--out): var_backtest.{json,csv,md}, var_series.npz (ряди для plot_var/plot_equity),
    var_hist_<символ або run_id>_<scope>.png, var_backtest.timing.json.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from typing import Any
from uuid import UUID

import numpy as np

from fuzzhelm.backtest import experiments_analysis as ea
from fuzzhelm.risk.kupiec import CHI2_1_95

EXPERIMENT = "var_backtest"
SUBSET_TITLES = {"all": "усі бари", "active": "бари в позиції"}


def scope_series_compute(args: argparse.Namespace, argv: list[str]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Прогони обраної конфігурації → ряди по областях (конкатенація OOS-фолдів / повне вікно)."""
    run = ea.run_variant_experiment(
        args, EXPERIMENT, argv, lambda cfg: [ea.Variant("chosen", "обрана конфігурація", cfg)])
    series: dict[str, Any] = {}
    for scope in run.scopes:
        res = run.results["chosen"][scope]
        bounds, acc = [], 0
        for r in res:
            bounds.append(acc)
            acc += int(r["n_eval_bars"])
        ret = np.concatenate([r["series"]["returns"] for r in res])
        # позиції по фолдах: у кожного фолду n+1 точок; маска рахується пофолдово і конкатенується
        mask = np.concatenate([ea.active_mask(r["series"]["returns"], r["series"]["position"]) for r in res])
        eq_chain = res[0]["costs"]["equity_start"] * np.concatenate(([1.0], np.cumprod(1.0 + ret)))
        ts = np.concatenate([res[0]["series"]["ts_ns"][:1]] + [r["series"]["ts_ns"][1:] for r in res])
        state = np.concatenate([res[0]["series"]["state"][:1]] + [r["series"]["state"][1:] for r in res])
        pos = np.concatenate([res[0]["series"]["position"][:1]] + [r["series"]["position"][1:] for r in res])
        series[scope] = {"returns": ret, "active": mask, "equity": eq_chain, "ts_ns": ts, "state": state,
                         "position": pos, "boundaries": np.asarray(bounds, dtype=np.int64),
                         "equity0": res[0]["costs"]["equity_start"]}
    ref: dict[str, Any] = {s: run.summaries[s]["chosen"] for s in run.scopes}
    if "full" in run.scopes:
        r0 = run.results["chosen"]["full"][0]
        ref["full_engine_metrics"] = {**r0["metrics"], **r0["extras"]}
        ref["full_equity_hash"] = r0["equity_hash"]
    ref["oos_folds"] = [{"segment": r["meta"]["segment"], **r["metrics"], "psr": r["extras"].get("psr"),
                         "equity_hash": r["equity_hash"]} for r in run.results["chosen"].get("oos", [])]
    meta = {"passport": run.passport, "reference_metrics": ref, "wall_s": run.wall_s, "runs": run.n_runs,
            "config_hash": run.variants[0].config.config_hash}
    return series, meta


def scope_series_db(args: argparse.Namespace, argv: list[str]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Крива збереженого прогону (лише читання) від бару першого можливого рішення (прогрів — пласкі бари)."""
    t0 = time.perf_counter()
    data = asyncio.run(ea.load_db_run(UUID(args.run_id), args.database_url))
    run, curve = data["run"], data["curve"]
    s = ea.series_from_curve(curve)
    # перша точка вікна оцінки рушія: decide_from = n_points − 1 − n_obs (run_metric), інакше прогрів − 1
    skip, skip_src = ea.first_decision_index(len(curve), data["metrics"] or {},
                                             ea.base_config().resolved_warmup() - 1)
    eq = s["equity"][skip:]
    pos = s["position"][skip:]
    ret = eq[1:] / eq[:-1] - 1.0
    series = {"run": {"returns": ret, "active": ea.active_mask(ret, pos), "equity": eq,
                      "ts_ns": s["ts_ns"][skip:], "state": ea.state_codes(s["state_names"][skip:]),
                      "position": pos, "boundaries": np.zeros(1, dtype=np.int64), "equity0": eq[0].item()}}
    pp = ea.passport(EXPERIMENT, argv, seed=run.seed,
                     extra={"run_id": str(run.id), "run_kind": run.kind, "run_status": run.status,
                            "run_git_sha": run.git_sha, "dataset_hash": run.dataset_hash.hex(),
                            "config_hashes": {"run": run.config_hash.hex()},
                            "equity_hash": run.equity_hash.hex() if run.equity_hash else None,
                            "skip_bars": skip, "skip_source": skip_src, "n_points": len(curve)})
    print(f"command: {pp['command']}")
    print(f"run {run.id} ({run.kind}/{run.status}), {len(curve)} equity points, skip {skip} warm-up bars "
          f"({skip_src})")
    meta = {"passport": pp, "reference_metrics": {"run_metrics": data["metrics"]},
            "wall_s": time.perf_counter() - t0, "runs": 0, "config_hash": run.config_hash.hex()}
    return series, meta


def render(p: dict[str, Any], blocks: list[dict[str, Any]], conclusions: dict[str, list[str]],
           figures: dict[str, str], window: int, alpha: float, w_active: int) -> str:
    lines = [
        "# VaR₉₅/CVaR₉₅: історичний проти параметричного, бектест прогнозу і тест Купця (§5.13)",
        "",
        "Згенеровано `scripts/var_backtest.py`. Автор: Андрій Жук, 2026. Числа — лише з цього прогону.",
        "",
        ea.passport_md(p),
        "",
        "Визначення: r_t = E_t/E_(t−1) − 1 (1 бар = 1 хв); VaR₉₅ і CVaR₉₅ — `risk.var` (нижня емпірична "
        "оцінка квантиля, m = ⌊α·n⌋); параметричний VaR = z₀.₉₅·σ̂·√h, h = 1 бар, середнє = 0 (§5.13); "
        "прогноз на "
        f"крок уперед: VaR_t лише з r[t−W:t], W = {window}"
        + ("" if window == ea.VAR_WINDOW else f" (**≠ W = {ea.VAR_WINDOW} брифінгу**)")
        + ("" if w_active == window else f"; для барів у позиції W = {w_active} (**≠ W = {ea.VAR_WINDOW} "
           "брифінгу: барів у позиції замало для W = 500 — задеклароване відхилення**)")
        + f"; пробій — r_t < −VaR_t; Купець: LR проти χ²₁(0.95) = {CHI2_1_95:.3f}, p = {alpha}. "
        "«Бари в позиції» — позиція на закритті t−1 або t ненульова, або капітал змінився в барі (пласка "
        "книга без виконань дає r = 0 точно). OOS-фолди конкатеновано (вікно W на межі фолду бачить минулий "
        "фолд — це минуле, не майбутнє). Гроші — при E₀ сегмента.",
        "",
        *ea.selection_caveat(p),
    ]
    heads = ["область", "підмножина", "n", "частка r = 0", "асиметрія", "куртозис", "VaR₉₅ іст.",
             "CVaR₉₅ іст.", "VaR₉₅ парам.", "CVaR/VaR парам.", "перевищ. іст. (вибірка)",
             "перевищ. парам. (вибірка)"]
    rows = []
    for b in blocks:
        st, mom = b.get("static"), b.get("moments") or {}
        rows.append([b["scope"], SUBSET_TITLES[b["subset"]], b["n"], b.get("zero_share"), mom.get("skew"),
                     mom.get("kurt"), st and st["hist_var"], st and st["hist_cvar"], st and st["param_var"],
                     st and st["cvar_over_param_var"], st and st["hist_exceed_rate"],
                     st and st["param_exceed_rate"]])
    lines += ["## Опис розподілу і оцінки на всій вибірці (частки капіталу)", "", ea.md_table(heads, rows),
              ""]
    heads = ["область", "підмножина", "метод", "прогнозів", "пробоїв", "очікувано", "частка", "LR Купця",
             "рішення (χ²₁ = 3.841)", "медіана VaR", "частка VaR ≤ 0"]
    rows = []
    for b in blocks:
        roll = b.get("rolling")
        if not roll:
            rows.append([b["scope"], SUBSET_TITLES[b["subset"]], "—", b["n"], None, None, None, None,
                         f"неможливо: {b.get('reason', '')}", None, None])
            continue
        for meth, name in (("hist", "історичний"), ("param", "параметричний")):
            k = roll[meth]
            rows.append([b["scope"], SUBSET_TITLES[b["subset"]], name, k["n"], k["breaches"], k["expected"],
                         k["rate"], k["lr"], "ВІДКИНУТО" if k["reject"] else "не відкинуто", k["var_median"],
                         k["var_zero_share"]])
    lines += ["## Бектест прогнозу на крок уперед і тест Купця", "", ea.md_table(heads, rows), ""]
    lines += ["## Висновок (побудовано з чисел вище)", ""]
    for key, ls in conclusions.items():
        lines += [f"**{key}.**", "", *[f"* {x}" for x in ls], ""]
    for scope, fig in figures.items():
        lines += [f"Рисунок ({scope}): `{fig}`.", ""]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Historical vs parametric VaR95/CVaR95 with a Kupiec POF "
                                             "backtest on all bars and on in-position bars (brief §5.13).")
    ea.add_common_args(ap, EXPERIMENT)
    ea.add_variant_args(ap)
    ap.add_argument("--run-id", default=None, help="use the equity curve of a stored run (read-only DB)")
    ap.add_argument("--window", type=int, default=ea.VAR_WINDOW, help="VaR window W (brief: 500)")
    ap.add_argument("--window-active", type=int, default=None,
                    help="VaR window for the in-position subset (default: --window; a smaller value is a "
                         "declared deviation from W = 500, reported as such)")
    ap.add_argument("--alpha", type=float, default=ea.VAR_ALPHA)
    args = ap.parse_args(argv)
    full_argv = [sys.argv[0], *(sys.argv[1:] if argv is None else argv)]
    t0 = time.perf_counter()
    if args.run_id:
        series, meta = scope_series_db(args, full_argv)
    else:
        if args.selection != "fixed":
            ap.error("var_backtest evaluates one chosen configuration (--selection fixed)")
        series, meta = scope_series_compute(args, full_argv)
    p = meta["passport"]
    w_active = args.window_active or args.window
    p["var_window"], p["var_window_active"], p["var_alpha"] = args.window, w_active, args.alpha
    blocks: list[dict[str, Any]] = []
    conclusions: dict[str, list[str]] = {}
    figures: dict[str, str] = {}
    for scope, s in series.items():
        e0 = float(s["equity0"])
        for subset, r in (("all", s["returns"]), ("active", s["returns"][s["active"]])):
            b = ea.var_block(r, window=args.window if subset == "all" else w_active, alpha=args.alpha,
                             equity=e0)
            b.update({"scope": scope, "subset": subset})
            blocks.append(b)
            name = SUBSET_TITLES[subset]
            conclusions[f"{scope}, {name}"] = ea.fat_tail_conclusion(b, name)
        panels = []
        for b in blocks[-2:]:
            st = b.get("static") or {}
            r = s["returns"] if b["subset"] == "all" else s["returns"][s["active"]]
            zs = b.get("zero_share")
            ztxt = f", r = 0: {100 * zs:.1f} %" if zs is not None else ""
            panels.append({"title": f"{scope}: {SUBSET_TITLES[b['subset']]} (n = {b['n']}{ztxt})",
                           "returns": r,
                           "lines": {"VaR₉₅ іст.": st.get("hist_var"), "CVaR₉₅ іст.": st.get("hist_cvar"),
                                     "VaR₉₅ парам.": st.get("param_var")}})
        tag = p.get("symbol") or f"run_{str(p.get('run_id', ''))[:8]}"
        fig = ea.plot_var_histogram(args.out / f"var_hist_{tag}_{scope}.png", panels,
                                    title=f"Розподіл доходностей за бар і VaR₉₅/CVaR₉₅ ({scope})")
        figures[scope] = str(fig)
    md = render(p, blocks, conclusions, figures, args.window, args.alpha, w_active)
    rows = []
    for b in blocks:
        base = {"scope": b["scope"], "subset": b["subset"], "n": b["n"], "zero_share": b.get("zero_share"),
                **{f"m_{k}": v for k, v in (b.get("moments") or {}).items()},
                **{f"s_{k}": v for k, v in (b.get("static") or {}).items()}}
        for meth, k in (b.get("rolling") or {}).items():
            base.update({f"{meth}_{kk}": vv for kk, vv in k.items()})
        rows.append(base)
    result = {"experiment": EXPERIMENT, "passport": p, "window": args.window, "alpha": args.alpha,
              "blocks": blocks, "conclusion": conclusions, "reference_metrics": meta["reference_metrics"],
              "config_hash": meta["config_hash"], "figures": figures}
    paths = ea.write_outputs(args.out, EXPERIMENT, result=result, md=md, rows=rows)
    npz = args.out / "var_series.npz"
    arrays: dict[str, Any] = {f"{scope}__{k}": np.asarray(v) for scope, s in series.items()
                              for k, v in s.items()}
    np.savez_compressed(npz, **arrays)
    paths.append(npz)
    timing = {"wall_s": time.perf_counter() - t0, "runs": meta["runs"], "workers": args.workers}
    (args.out / f"{EXPERIMENT}.timing.json").write_text(json.dumps(timing) + "\n", encoding="utf-8")
    for b in blocks:
        roll = b.get("rolling")
        tail = ("; ".join(f"{m}: {k['breaches']}/{k['n']} LR {k['lr']:.3f} "
                          f"{'REJECT' if k['reject'] else 'ok'}" for m, k in roll.items())
                if roll else b.get("reason", ""))
        zs = b.get("zero_share", float("nan"))
        print(f"[{b['scope']}/{b['subset']}] n {b['n']}, zero share {zs:.4f}; {tail}")
    print(f"wall {timing['wall_s']:.1f} s")
    print("wrote " + ", ".join(str(x) for x in [*paths, *figures.values()]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
