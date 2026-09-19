"""Рисунок «торнадо» чутливості: зміна Шарпа, просадки і обороту при низькому і високому значенні параметра.

Найменування: scripts/plot_sensitivity.py
Призначення: рисунок таблиці чутливості до 8 параметрів (брифінг §12 фаза 7, §14 п. 2.11, §15) з виводу
    scripts/sensitivity.py; matplotlib Agg, лише 2D, українські підписи, 150 dpi.
Автор: Андрій Жук, 2026.

Запуск:  uv run python scripts/plot_sensitivity.py --symbol BTCUSDT --target oos [--in JSON] [--out DIR]
Вивід:   sensitivity_<SYM>_<engine>_<target>.png поруч із входом (або в --out). Параметри впорядковано за
         розмахом Шарпа (найчутливіший — угорі); стовпчики — Δ метрики від бази при найменшому (слот 1) і
         найбільшому (слот 2) значенні параметра.
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
S1, S2 = "#2a78d6", "#eb6834"
PANELS = (("sharpe", "Δ коефіцієнта Шарпа (річного)", 1.0),
          ("max_drawdown", "Δ максимальної просадки, п.п.", 100.0),
          ("turnover", "Δ обороту, капіталів/рік", 1.0))


def num(x: Any) -> float:
    return float(x) if x is not None else float("nan")


def short(x: float) -> str:
    return f"{x:g}"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Tornado figure from scripts/sensitivity.py output")
    ap.add_argument("--in", dest="inp", default=None, help="sensitivity_<SYM>_<engine>_<target>.json")
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--engine", choices=("mamdani", "linear"), default="mamdani")
    ap.add_argument("--target", choices=("oos", "full"), default="oos")
    ap.add_argument("--smoke", action="store_true", help="read the smoke output under artifacts/tmp")
    ap.add_argument("--out", default=None, help="output directory (default: next to the input)")
    args = ap.parse_args(argv)
    plt.rcParams.update({"font.family": "DejaVu Sans"})
    if args.inp:
        inp = Path(args.inp)
    else:
        base = ROOT / "artifacts" / ("tmp/exp_search" if args.smoke else "exp_search") / "sensitivity"
        inp = base / f"sensitivity_{args.symbol}_{args.engine}_{args.target}.json"
    d = json.loads(inp.read_text(encoding="utf-8"))
    out = Path(args.out) if args.out else inp.parent
    out.mkdir(parents=True, exist_ok=True)
    rows = d["tornado"]
    n = len(rows)
    y = np.arange(n)[::-1]                        # найчутливіший — угорі
    labels = [f"{r['symbol']}\n{short(num(r['low_value']))} · {short(num(r['base_value']))} · "
              f"{short(num(r['high_value']))}" for r in rows]
    fig, axes = plt.subplots(1, 3, figsize=(13.5, 0.62 * n + 2.0), layout="constrained", sharey=True)
    fig.patch.set_facecolor(SURFACE)
    h = 0.36
    for ax, (key, xlabel, scale) in zip(axes, PANELS, strict=True):
        lo = [scale * num(r[f"{key}_d_low"]) for r in rows]
        hi = [scale * num(r[f"{key}_d_high"]) for r in rows]
        ax.barh(y + h / 2, lo, h, color=S1, edgecolor=SURFACE, linewidth=1.5,
                label="найменше значення параметра", zorder=3)
        ax.barh(y - h / 2, hi, h, color=S2, edgecolor=SURFACE, linewidth=1.5,
                label="найбільше значення параметра", zorder=3)
        ax.axvline(0.0, color=AXIS, linewidth=1.0, zorder=2)
        ax.set_xlabel(xlabel, color=INK_2, fontsize=9)
        ax.set_facecolor(SURFACE)
        ax.grid(axis="x", color=GRID, linewidth=0.8)
        ax.set_axisbelow(True)
        ax.tick_params(colors=INK_2, labelsize=8, color=AXIS)
        ax.tick_params(axis="y", left=False)
        for side in ("top", "right", "left"):
            ax.spines[side].set_visible(False)
        ax.spines["bottom"].set_color(AXIS)
        base_v = scale * num(rows[0][f"{key}_base"]) if rows else float("nan")
        ax.set_title(f"база: {base_v:.4g}", loc="left", fontsize=8, color=INK_2)
    axes[0].set_yticks(y, labels)
    axes[0].set_ylabel("Параметр (низьке · база · високе)", color=INK_2, fontsize=9)
    fig.legend(*axes[0].get_legend_handles_labels(), loc="outside lower left", ncols=2, frameon=False,
               fontsize=8, labelcolor=INK_2)
    tgt = "конкатенований OOS фолдів" if d["target"] == "oos" else "усе вікно"
    p = d["provenance"]
    fig.suptitle(f"Чутливість (один параметр за раз), {p['dataset']['symbol']}, {p['engine']}, ціль: {tgt}; "
                 f"упорядковано за розмахом Шарпа", color=INK, fontsize=10.5, x=0.01, ha="left")
    target = out / f"{inp.stem}.png"
    fig.savefig(target, dpi=150, facecolor=SURFACE)
    plt.close(fig)
    print(f"wrote {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
