"""Рисунок Парето-фронту сітки: три 2D-проєкції (SR, MaxDD, оборот) з фронтом, межею dd_cap і обраною точкою.

Найменування: scripts/plot_pareto.py
Призначення: рисунок підрозділу 2.11 і сцени 3:20–4:10 захисту («робочу точку обрано з недомінованої множини
    за трьома критеріями, а не argmax Sharpe», брифінг §5.16, §15) з виводу scripts/run_grid.py;
    matplotlib Agg, лише 2D (проєкції замість 3D), українські підписи, 150 dpi.
Автор: Андрій Жук, 2026.

Запуск:  uv run python scripts/plot_pareto.py --symbol BTCUSDT --engine mamdani [--in JSON] [--out DIR]
Вивід:   grid_<SYM>_<engine>_pareto.png поруч із входом (або в --out).
Фронт тривимірний, тож у кожній 2D-проєкції частина точок фронту виглядає «домінованою» — вони недоміновані
за третім критерієм; це підписано на рисунку.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

ROOT = Path(__file__).resolve().parents[1]
INK, INK_2, MUTED, GRID, AXIS, SURFACE = "#0b0b0b", "#52514e", "#898781", "#e1e0d9", "#c3c2b7", "#fcfcfb"
S1, S2 = "#2a78d6", "#eb6834"


def num(x: Any) -> float:
    return float(x) if x is not None else float("nan")


def style(ax: Any) -> None:
    ax.set_facecolor(SURFACE)
    ax.grid(color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    ax.tick_params(colors=INK_2, labelsize=8, color=AXIS)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(AXIS)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Pareto-front projections from scripts/run_grid.py output")
    ap.add_argument("--in", dest="inp", default=None, help="grid_<SYM>_<engine>.json")
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--engine", choices=("mamdani", "linear"), default="mamdani")
    ap.add_argument("--smoke", action="store_true", help="read the smoke output under artifacts/tmp")
    ap.add_argument("--out", default=None, help="output directory (default: next to the input)")
    args = ap.parse_args(argv)
    plt.rcParams.update({"font.family": "DejaVu Sans"})
    if args.inp:
        inp = Path(args.inp)
    else:
        base = ROOT / "artifacts" / ("tmp/exp_search" if args.smoke else "exp_search") / "grid"
        inp = base / f"grid_{args.symbol}_{args.engine}.json"
    d = json.loads(inp.read_text(encoding="utf-8"))
    out = Path(args.out) if args.out else inp.parent
    out.mkdir(parents=True, exist_ok=True)
    pre = d["front_space"]
    cells = d["cells"]
    sr = [num(c[f"{pre}_sharpe"]) for c in cells]
    dd = [100 * num(c[f"{pre}_max_drawdown"]) for c in cells]
    to = [num(c[f"{pre}_turnover"]) for c in cells]
    on = [bool(c[f"pareto_{pre}"]) for c in cells]
    sel = [bool(c["selected"]) for c in cells]
    cap = 100 * num(d["dd_cap"])
    where = "конкатенований OOS" if pre == "oos" else "усе вікно"
    lab = {"sr": f"Коефіцієнт Шарпа ({where}, річний)", "dd": f"Максимальна просадка ({where}), %",
           "to": f"Оборот ({where}), капіталів/рік"}
    pairs = [(to, sr, "to", "sr"), (dd, sr, "dd", "sr"), (to, dd, "to", "dd")]
    fig, axes = plt.subplots(1, 3, figsize=(14.0, 4.6), layout="constrained")
    fig.patch.set_facecolor(SURFACE)
    log_to = min(to) > 0 and max(to) / min(to) > 20 if all(math.isfinite(x) for x in to) else False
    for ax, (xs, ys, kx, ky) in zip(axes, pairs, strict=True):
        rest = [i for i in range(len(cells)) if not on[i]]
        front = [i for i in range(len(cells)) if on[i] and not sel[i]]
        chosen = [i for i in range(len(cells)) if sel[i]]
        ax.scatter([xs[i] for i in rest], [ys[i] for i in rest], s=22, color=MUTED, alpha=0.55,
                   edgecolors=SURFACE, linewidths=0.8, label="домінована клітинка", zorder=2)
        ax.scatter([xs[i] for i in front], [ys[i] for i in front], s=34, color=S1, edgecolors=SURFACE,
                   linewidths=1.2, label="фронт Парето (SR↑, MaxDD↓, оборот↓)", zorder=3)
        ax.scatter([xs[i] for i in chosen], [ys[i] for i in chosen], s=90, color=S2, edgecolors=SURFACE,
                   linewidths=2.0, label="обрана робоча точка", zorder=4)
        for i in chosen:
            ax.annotate(f"клітинка {cells[i]['cell']}", (xs[i], ys[i]), textcoords="offset points",
                        xytext=(8, 6), fontsize=8, color=INK)
        # межа dd_cap — лінією, лише якщо вона поруч із даними (інакше стиснула б хмару точок) — тоді текстом
        cap_near = cap <= 1.3 * max(x for x in dd if math.isfinite(x))
        if kx == "dd" and cap_near:
            ax.axvline(cap, color=INK_2, linewidth=0.8, zorder=1)
            ax.text(cap, 1.01, f"dd_cap = {cap:g} %", transform=ax.get_xaxis_transform(), fontsize=7.5,
                    color=INK_2, ha="center")
        if ky == "dd" and cap_near:
            ax.axhline(cap, color=INK_2, linewidth=0.8, zorder=1)
            ax.text(1.0, cap, f" dd_cap = {cap:g} %", transform=ax.get_yaxis_transform(), fontsize=7.5,
                    color=INK_2, va="bottom", ha="right")
        if "dd" in (kx, ky) and not cap_near:
            ax.set_title(f"dd_cap = {cap:g} % — поза межами осі: усі клітинки в межі", loc="right",
                         fontsize=7.5, color=INK_2)
        if kx == "to" and log_to:
            ax.set_xscale("log")
        ax.set_xlabel(lab[kx], color=INK_2, fontsize=9)
        ax.set_ylabel(lab[ky], color=INK_2, fontsize=9)
        style(ax)
    ch = d["choice"]
    params = ", ".join(f"{k} = {v}" for k, v in ch["params"].items())
    fig.suptitle(f"Сітка {len(cells)} клітинок, {d['provenance']['dataset']['symbol']} "
                 f"({d['provenance']['engine']}): на фронті {len(ch['front'])}; "
                 f"обрано клітинку {ch['index']} "
                 f"({params})", color=INK, fontsize=10.5, x=0.01, ha="left")
    fig.legend(*axes[0].get_legend_handles_labels(), loc="outside lower left", ncols=3, frameon=False,
               fontsize=8, labelcolor=INK_2)
    fig.text(0.99, 0.005, "фронт тривимірний: у 2D-проєкції точка фронту може здаватись домінованою",
             ha="right", va="bottom", fontsize=7, color=MUTED)
    target = out / f"{inp.stem}_pareto.png"
    fig.savefig(target, dpi=150, facecolor=SURFACE)
    plt.close(fig)
    print(f"wrote {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
