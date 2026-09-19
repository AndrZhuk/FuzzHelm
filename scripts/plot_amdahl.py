"""Рисунок S(p) проти межі Амдала з підігнаним f і розклад часу T(p) на старт пулу, обчислення і залишок.

Найменування: scripts/plot_amdahl.py
Призначення: рисунок «S(p) проти Амдала» фази 7 і підрозділу 2.11 (брифінг §5.16, §12, §14) з виводу
    scripts/bench_amdahl.py; matplotlib Agg, лише 2D, українські підписи, 150 dpi.
Автор: Андрій Жук, 2026.

Запуск:  uv run python scripts/plot_amdahl.py --symbol BTCUSDT [--in JSON] [--out DIR] [--smoke]
Вивід:   amdahl_<SYM>.png поруч із входом (або в --out). Якщо замір зроблено на завантаженій машині
         (bench_amdahl --force), це написано в заголовку рисунка.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
INK, INK_2, MUTED, GRID, AXIS, SURFACE = "#0b0b0b", "#52514e", "#898781", "#e1e0d9", "#c3c2b7", "#fcfcfb"
S1, S2, S3 = "#2a78d6", "#eb6834", "#1baf7a"     # категоріальні слоти 1–3 (validate_palette.js: PASS)


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
    ap = argparse.ArgumentParser(description="S(p) vs Amdahl figure from scripts/bench_amdahl.py output")
    ap.add_argument("--in", dest="inp", default=None, help="amdahl_<SYM>.json")
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--smoke", action="store_true", help="read the smoke output under artifacts/tmp")
    ap.add_argument("--out", default=None, help="output directory (default: next to the input)")
    args = ap.parse_args(argv)
    plt.rcParams.update({"font.family": "DejaVu Sans"})
    if args.inp:
        inp = Path(args.inp)
    else:
        base = ROOT / "artifacts" / ("tmp/exp_search" if args.smoke else "exp_search") / "amdahl"
        inp = base / f"amdahl_{args.symbol}.json"
    d = json.loads(inp.read_text(encoding="utf-8"))
    out = Path(args.out) if args.out else inp.parent
    out.mkdir(parents=True, exist_ok=True)
    fit = d["amdahl"]
    ps = [int(p) for p in fit["workers"]]
    f = num(fit["serial_fraction"])
    t1 = num(d["t_median"]["1"])
    fig, (ax, bx) = plt.subplots(1, 2, figsize=(12.0, 4.6), layout="constrained",
                                 gridspec_kw={"width_ratios": [1.15, 1.0]})
    fig.patch.set_facecolor(SURFACE)
    grid_p = np.linspace(1, max(ps), 200)
    ax.plot(grid_p, grid_p, color=MUTED, linewidth=1.0, label="ідеал S = p", zorder=2)
    ax.plot(grid_p, 1.0 / (f + (1.0 - f) / grid_p), color=S2, linewidth=2.0,
            label=f"межа Амдала, f = {f:.3f} (НК)", zorder=3)
    fs = num(d["serial_fraction_structural"])
    ax.plot(grid_p, 1.0 / (fs + (1.0 - fs) / grid_p), color=S3, linewidth=2.0,
            label=f"межа за «структурним» f = {fs:.3f}", zorder=3)
    for p in ps:
        reps = [t1 / num(x) for x in d["times_all"][str(p)]]
        ax.scatter([p] * len(reps), reps, s=14, color=S1, alpha=0.45, edgecolors="none", zorder=4)
    sp = [num(fit["speedup"][str(p)]) for p in ps]
    ax.plot(ps, sp, color=S1, linewidth=2.0, marker="o", markersize=7, markeredgecolor=SURFACE,
            markeredgewidth=2.0, label="виміряно S(p) = T(1)/T(p), медіана", zorder=5)
    ax.annotate(f"S({ps[-1]}) = {sp[-1]:.2f}", (ps[-1], sp[-1]), textcoords="offset points", xytext=(6, -14),
                ha="left", fontsize=8, color=INK)
    ax.set_xlim(0.8, max(ps) + 0.6)
    ax.set_xticks(ps)
    ax.set_xlabel("Кількість процесів пулу p", color=INK_2, fontsize=9)
    ax.set_ylabel("Прискорення S(p)", color=INK_2, fontsize=9)
    ax.legend(frameon=False, fontsize=8, labelcolor=INK_2, loc="upper left")
    style(ax)

    ov = d["overhead"]
    x = np.arange(len(ps))
    start = np.array([num(ov[str(p)]["startup_s"]) for p in ps])
    wall = np.array([num(ov[str(p)]["wall_s"]) for p in ps])
    start = np.minimum(start, wall)
    comp = np.minimum(np.array([num(ov[str(p)]["makespan_s"]) for p in ps]), wall - start)
    rest = np.maximum(wall - start - comp, 0.0)
    w = 0.5
    bx.bar(x, start, w, color=S3, edgecolor=SURFACE, linewidth=1.5, label="старт пулу + імпорти", zorder=3)
    bx.bar(x, comp, w, bottom=start, color=S1, edgecolor=SURFACE, linewidth=1.5,
           label="обчислення (makespan найзайнятішого процесу)", zorder=3)
    bx.bar(x, rest, w, bottom=start + comp, color=S2, edgecolor=SURFACE, linewidth=1.5,
           label="залишок: IPC, pickle, результати, планування", zorder=3)
    for i, tw in enumerate(wall):
        bx.text(x[i], tw, f"{tw:.3g} с", ha="center", va="bottom", fontsize=8, color=INK_2)
    bx.set_xticks(x, [str(p) for p in ps])
    bx.set_xlabel("Кількість процесів пулу p", color=INK_2, fontsize=9)
    bx.set_ylabel("Час T(p), с (повтор з медіанним T)", color=INK_2, fontsize=9)
    fig.legend(*bx.get_legend_handles_labels(), loc="outside lower right", ncols=3, frameon=False, fontsize=8,
               labelcolor=INK_2)
    style(bx)
    m = d["machine"]
    cpu = m.get("machdep.cpu.brand_string") or m.get("cpu_model") or "CPU"
    title = (f"Пул воркерів сітки: задач {d['tasks']} × {d['bars_per_task']} барів, {cpu}, "
             f"load на старті {num(d['load']['start'][0]):.2f}")
    if d["load"]["busy_machine"]:
        title += " — ЗАВАНТАЖЕНА МАШИНА, не для звіту"
    fig.suptitle(title, color=INK, fontsize=10.5, x=0.01, ha="left")
    target = out / f"{inp.stem}.png"
    fig.savefig(target, dpi=150, facecolor=SURFACE)
    plt.close(fig)
    print(f"wrote {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
