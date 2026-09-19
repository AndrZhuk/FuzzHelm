"""Обчислювальний експеримент «ціна відсутності гістерезису» (брифінг §5.9, §14 підрозділ 2.7).

Найменування: hysteresis_cost.py
Призначення: виміряти оборот і комісії з тригером Шмітта (вхід u_enter, вихід u_exit) і без нього
    (компаратор: u_exit = u_enter) на тих самих OOS-фолдах / повному вікні і зіставити з аналітичним
    твердженням брифінгу «−1.15 %/добу при u ≈ 0.25» (його арифметика — 115.2 % при N = E, R-03). Звітується
    ВИМІРЯНЕ число: комісії за добу у % капіталу, різниця «без − з», частота виконань і розмір позиції, з яких
    та сама аналітика відтворює виміряне; частка рішень у смузі петлі (u_exit ≤ |u| < u_enter).
Автор: Андрій Жук, 2026.

Запуск (димовий): uv run python scripts/hysteresis_cost.py --fixture --smoke --workers 2
Повний (наступна хвиля): uv run python scripts/hysteresis_cost.py --db --symbol BTCUSDT --workers 4 \
    --out docs/report_tables/raw/hysteresis_BTCUSDT   (і те саме з --risk-loop relaxed → …_relaxed)
Вивід (--out): hysteresis_cost.{json,csv,md}, hysteresis_cost.timing.json.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from typing import Any

import numpy as np

from fuzzhelm.backtest import experiments_analysis as ea

EXPERIMENT = "hysteresis_cost"


def measure(run: ea.VariantRun, scope: str, key: str, fee_rate: float) -> dict[str, Any]:
    res = run.results[key][scope]
    s = run.summaries[scope][key]
    bars = sum(int(r["n_eval_bars"]) for r in res)
    fills = sum(int(r["costs"]["n_fills"]) for r in res)
    notional = sum(r["costs"]["traded_notional"] for r in res)
    e0 = res[0]["costs"]["equity_start"] if res else math.nan
    days = s["days"]
    nf = notional / fills / e0 if fills and e0 > 0 else math.nan
    fpb = fills / bars if bars else math.nan
    return {
        "n_trades": s["n_trades"], "n_fills": fills, "fills_per_day": fills / days if days else math.nan,
        "fills_per_bar": fpb, "notional_per_fill_over_equity": nf, "fees": s["fees"],
        "fees_per_day_pct": s["fees_per_day_pct"], "turnover": s["turnover"],
        "total_return": s["total_return"],
        "max_drawdown": s["max_drawdown"], "sharpe": s["sharpe"], "share_halted": s["share_halted"],
        "share_cooldown": s["share_cooldown"], "days": days,
        # та сама аналітика §5.9 з ВИМІРЯНИМИ частотою виконань і розміром (усі виконання — taker)
        "analytic_at_measured_pct": (ea.churn_cost_pct_per_day(fee_rate, fpb, nf)
                                     if math.isfinite(fpb) and math.isfinite(nf) else math.nan),
        # сценарій брифінгу «перевідкриття щобару» (2 виконання/бар) з фактичним розміром позиції
        "brief_scenario_at_measured_size_pct": (ea.churn_cost_pct_per_day(fee_rate, 2.0, nf)
                                                if math.isfinite(nf) else math.nan),
    }


def render(run: ea.VariantRun, meas: dict[str, dict[str, dict[str, Any]]], bands: dict[str, Any],
           fee_rate: float, enter: float, exit_: float) -> str:
    lines = [
        "# Ціна відсутності гістерезису (тригер Шмітта, §5.9): виміряно",
        "",
        "Згенеровано `scripts/hysteresis_cost.py`. Автор: Андрій Жук, 2026. Числа — лише з цього прогону.",
        "",
        ea.passport_md(run.passport),
        "",
        f"Протокол: та сама конфігурація з петлею (вхід |u| ≥ {enter:g}, вихід |u| < {exit_:g}, знаковий — "
        "R-09) "
        f"і без неї (вхід = вихід = {enter:g}); фолди `{run.layout}` ({len(run.folds)} шт.); розмір позиції "
        "фіксується на вході (ENG-04), тож без петлі витрати ростуть лише через додаткові цикли вхід/вихід. "
        f"Комісія taker τ = {fee_rate:g} (усі виконання ринкові)."
        + (" Контур ризику ПОСЛАБЛЕНО (`--risk-loop relaxed`): HALTED/COOLDOWN не обрізають торгівлю, тож "
           "комісії за добу не розбавлені пласкими днями." if run.passport["risk_loop"] == "relaxed" else
           " Контур ризику типовий: засувка HALTED/COOLDOWN обрізає торгівлю, тож «за добу» включає пласкі "
           "дні — див. частку HALTED; для ізоляції ефекту — прогін з `--risk-loop relaxed`."),
        "",
        f"Твердження брифінгу (§5.9): «−{ea.BRIEF_HYSTERESIS_CLAIM_PCT} % капіталу на добу» при u ≈ 0.25; "
        f"його арифметика 1440 × 2 × {fee_rate:g} = {ea.churn_cost_pct_per_day(fee_rate, 2.0, 1.0):.1f} % "
        "на добу за "
        "номіналу N = E (docs/deviations.d/risk.md R-03). Нижче — виміряне.",
        "",
        *ea.selection_caveat(run.passport),
    ]
    rows_spec = [
        ("n_trades", "угод"), ("n_fills", "виконань"), ("fills_per_day", "виконань за добу"),
        ("fills_per_bar", "виконань на бар"), ("notional_per_fill_over_equity", "номінал виконання / E₀"),
        ("fees", "комісії, USDT"), ("fees_per_day_pct", "комісії, % E₀ за добу"),
        ("turnover", "оборот, капіталів/рік"), ("total_return", "дохідність"), ("max_drawdown", "MaxDD"),
        ("sharpe", "Шарп (річн.)"), ("share_halted", "частка барів у HALTED"),
        ("share_cooldown", "частка барів у COOLDOWN"),
        ("analytic_at_measured_pct", "аналітика §5.9 при виміряних частоті й розмірі, %/добу"),
        ("brief_scenario_at_measured_size_pct",
         "сценарій «2 виконання щобару» при виміряному розмірі, %/добу"),
    ]
    for scope in run.scopes:
        w, wo = meas[scope]["with"], meas[scope]["without"]
        lines += [f"## {ea.SCOPE_TITLES[scope]} ({ea.fmt(w['days'])} діб)", "",
                  ea.md_table(["величина", "з гістерезисом", "без гістерезису", "без − з"],
                              [[label, w[k], wo[k], (wo[k] - w[k]) if isinstance(w[k], int | float) else None]
                               for k, label in rows_spec]), ""]
        b = bands[scope]
        d = wo["fees_per_day_pct"] - w["fees_per_day_pct"]
        lines += [
            f"Смуга петлі: {ea.pct(b['share_in_band'])} рішень мають {exit_:g} ≤ |u| < {enter:g} (там петля "
            f"тримає стан, а компаратор — ні), {ea.pct(b['share_above_enter'])} — |u| ≥ {enter:g}; "
            "перетинів порогу "
            f"|u| = {enter:g}: {b['enter_crossings']} ({ea.fmt(b['crossings_per_day'])} за добу).",
            "",
            f"**Виміряна ціна відсутності гістерезису ({ea.SCOPE_TITLES[scope]}): {ea.fmt(d)} % E₀ за добу** "
            f"додаткових комісій (без петлі {ea.fmt(wo['fees_per_day_pct'])} проти "
            f"{ea.fmt(w['fees_per_day_pct'])} %/добу з петлею; твердження брифінгу — "
            f"{ea.BRIEF_HYSTERESIS_CLAIM_PCT} %/добу). Дві виміряні величини, що входять в аналітику §5.9: "
            f"виконань на бар без петлі — {ea.fmt(wo['fills_per_bar'])} (сценарій брифінгу — 2) і номінал "
            f"виконання — {ea.fmt(wo['notional_per_fill_over_equity'])}·E₀ (сценарій брифінгу — 1·E₀).",
            "",
        ]
        if not d > 0:
            lines += ["Без петлі комісій за добу не більше: торгівлю обрізав контур ризику (частки HALTED/"
                      "COOLDOWN вище) або шлях станів розійшовся — ізольований вимір дає прогін з "
                      "`--risk-loop relaxed`.", ""]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Turnover and fees with vs without the Schmitt trigger "
                                             "(u_exit = u_enter), compared with the brief §5.9 claim.")
    ea.add_common_args(ap, EXPERIMENT)
    ea.add_variant_args(ap)
    args = ap.parse_args(argv)
    if args.selection != "fixed":
        ap.error("hysteresis_cost measures one fixed configuration (--selection fixed)")
    full_argv = [sys.argv[0], *(sys.argv[1:] if argv is None else argv)]
    run = ea.run_variant_experiment(args, EXPERIMENT, full_argv, ea.hysteresis_variants)
    cfg = run.variants[0].config
    fee_rate = to_rate(cfg.trees["cost_model"]["taker_fee"])
    hyst = cfg.risk_config().hysteresis      # фактичні пороги прогону (профіль + --params)
    enter, exit_ = to_rate(hyst.enter), to_rate(hyst.exit)
    meas = {s: {k: measure(run, s, k, fee_rate) for k in ("with", "without")} for s in run.scopes}
    bands = {}
    for scope in run.scopes:
        u = np.concatenate([r["series"]["u_final"] for r in run.results["with"][scope]])
        bands[scope] = ea.band_stats(u, enter, exit_)
    md = render(run, meas, bands, fee_rate, enter, exit_)
    rows = [{"scope": s, "variant": k, **meas[s][k]} for s in run.scopes for k in ("with", "without")]
    result = {"experiment": EXPERIMENT, "passport": run.passport, "fee_rate": fee_rate, "u_enter": enter,
              "u_exit": exit_, "brief_claim_pct_per_day": ea.BRIEF_HYSTERESIS_CLAIM_PCT,
              "brief_arithmetic_pct_per_day_at_full_notional": ea.churn_cost_pct_per_day(fee_rate, 2.0, 1.0),
              "measured": meas, "band": bands, "summary": ea.summary_rows(run)}
    paths = ea.write_outputs(args.out, EXPERIMENT, result=result, md=md, rows=rows)
    timing = {"wall_s": run.wall_s, "runs": run.n_runs, "workers": args.workers, "bars": len(run.dataset)}
    (args.out / f"{EXPERIMENT}.timing.json").write_text(json.dumps(timing) + "\n", encoding="utf-8")
    for scope in run.scopes:
        w, wo = meas[scope]["with"], meas[scope]["without"]
        print(f"[{scope}] fees %E0/day with {w['fees_per_day_pct']:.4f}  "
              f"without {wo['fees_per_day_pct']:.4f}  "
              f"delta {wo['fees_per_day_pct'] - w['fees_per_day_pct']:.4f}; fills with {w['n_fills']} "
              f"without {wo['n_fills']}; in-band share {bands[scope]['share_in_band']:.4f}")
    print(f"wall {run.wall_s:.1f} s, {run.n_runs} engine runs, workers={args.workers}")
    print("wrote " + ", ".join(str(p) for p in paths))
    return 0


def to_rate(x: Any) -> float:
    """YAML-число/рядок ставки комісії → float (скрипт поза межею типів; джерело — config/cost_model.yaml)."""
    return float(str(x))


if __name__ == "__main__":
    sys.exit(main())
