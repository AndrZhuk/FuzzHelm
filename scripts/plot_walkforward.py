"""Рисунки walk-forward: парні стовпчики IS vs OOS по фолдах, конкатенована OOS-крива, Мамдані vs лінійне.

Найменування: scripts/plot_walkforward.py
Призначення: рисунки підрозділу 2.11 і сцени 3:20–4:10 захисту (брифінг §14, §15) з виводу
    scripts/run_walkforward.py (JSON + CSV кривої); matplotlib Agg, лише 2D, українські підписи, 150 dpi.
Автор: Андрій Жук, 2026.

Запуск:  uv run python scripts/plot_walkforward.py --symbol BTCUSDT --engine mamdani [--in JSON] [--out DIR]
         (--smoke — брати вивід швидкої перевірки з artifacts/tmp/exp_search/walkforward)
Виводи:  walkforward_<SYM>_<engine>_folds.png, walkforward_<SYM>_<engine>_oos_equity.png,
         walkforward_<SYM>_engines.png (якщо є вивід --engine both).
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
INK, INK_2, MUTED, GRID, AXIS, SURFACE = "#0b0b0b", "#52514e", "#898781", "#e1e0d9", "#c3c2b7", "#fcfcfb"
S1, S2 = "#2a78d6", "#eb6834"          # категоріальні слоти 1–2 (перевірено validate_palette.js)


def num(x: Any) -> float:
    """Числа з JSON виводу (NaN/Infinity записані рядками — json_safe)."""
    return float(x) if x is not None else float("nan")


def style(ax: Any) -> None:
    ax.set_facecolor(SURFACE)
    ax.grid(axis="y", color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    ax.tick_params(colors=INK_2, labelsize=8, color=AXIS)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(AXIS)


def paired_bars(ax: Any, labels: list[str], a: list[float], b: list[float], names: tuple[str, str],
                ylabel: str) -> None:
    x = np.arange(len(labels))
    w = 0.3
    ax.bar(x - w / 2, a, w, color=S1, edgecolor=SURFACE, linewidth=1.5, label=names[0], zorder=3)
    ax.bar(x + w / 2, b, w, color=S2, edgecolor=SURFACE, linewidth=1.5, label=names[1], zorder=3)
    ax.axhline(0.0, color=AXIS, linewidth=1.0, zorder=2)
    ax.set_xticks(x, labels)
    ax.set_xlabel("Фолд walk-forward", color=INK_2, fontsize=9)
    ax.set_ylabel(ylabel, color=INK_2, fontsize=9)
    style(ax)


def default_input(args: argparse.Namespace, stem: str) -> Path:
    base = ROOT / "artifacts" / ("tmp/exp_search" if args.smoke else "exp_search") / "walkforward"
    return base / f"{stem}.json"


def plot_folds(d: dict[str, Any], out: Path) -> None:
    folds = d["folds"]
    labels = [f"{f['fold'] + 1}\nкл. {f['selected_cell']}" for f in folds]
    fig, axes = plt.subplots(1, 2, figsize=(11.0, 4.2), layout="constrained")
    fig.patch.set_facecolor(SURFACE)
    paired_bars(axes[0], labels, [num(f["is"]["sharpe"]) for f in folds],
                [num(f["oos"]["sharpe"]) for f in folds],
                ("IS (підбір параметрів)", "OOS (перевірка)"), "Коефіцієнт Шарпа (річний)")
    paired_bars(axes[1], labels, [100 * num(f["is"]["max_drawdown"]) for f in folds],
                [100 * num(f["oos"]["max_drawdown"]) for f in folds],
                ("IS (підбір параметрів)", "OOS (перевірка)"), "Максимальна просадка, %")
    sym = d["provenance"]["dataset"]["symbol"]
    fig.suptitle(f"Walk-forward {sym}, рушій {d['engine']}: обрана на IS клітинка сітки, IS проти OOS",
                 color=INK, fontsize=11, x=0.01, ha="left")
    handles, names = axes[0].get_legend_handles_labels()
    fig.legend(handles, names, loc="outside upper right", ncols=2, frameon=False, fontsize=8,
               labelcolor=INK_2)
    fig.savefig(out, dpi=150, facecolor=SURFACE)
    plt.close(fig)


def plot_equity(d: dict[str, Any], csv_path: Path, out: Path) -> None:
    ts, fold, eq = [], [], []
    with csv_path.open(encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            ts.append(int(row["close_ns"]))
            fold.append(int(row["fold"]))
            eq.append(float(row["equity"]))
    t = mdates.date2num([datetime.fromtimestamp(x / 1e9, tz=UTC) for x in ts])  # type: ignore[no-untyped-call]
    fig, ax = plt.subplots(figsize=(11.0, 3.8), layout="constrained")
    fig.patch.set_facecolor(SURFACE)
    ax.plot(t, eq, color=S1, linewidth=1.6, solid_joinstyle="round", zorder=3)
    ax.axhline(eq[0], color=AXIS, linewidth=1.0, zorder=2)
    starts = [i for i in range(1, len(fold)) if fold[i] != fold[i - 1]]
    for i in [0, *starts]:
        if i:
            ax.axvline(t[i], color=GRID, linewidth=1.0, zorder=1)
        ax.text(t[i], 1.01, f"фолд {fold[i] + 1}", transform=ax.get_xaxis_transform(), fontsize=7.5,
                color=MUTED)
    c = d["concat_oos"]
    ax.set_title(f"Конкатенований OOS ({d['engine']}): дохідність {100 * num(c['total_return']):+.2f} %, "
                 f"MaxDD {100 * num(c['max_drawdown']):.2f} %, Шарп {num(c['sharpe']):.2f}, "
                 f"PSR {num(c['psr']):.3g}",
                 color=INK, fontsize=10, loc="left", pad=14)
    ax.set_xlabel("Час закриття бару, UTC", color=INK_2, fontsize=9)
    ax.set_ylabel("Капітал ланцюга OOS, USDT", color=INK_2, fontsize=9)
    loc = mdates.AutoDateLocator()  # type: ignore[no-untyped-call]
    ax.xaxis.set_major_locator(loc)
    ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(loc))  # type: ignore[no-untyped-call]
    style(ax)
    fig.savefig(out, dpi=150, facecolor=SURFACE)
    plt.close(fig)


def plot_engines(d: dict[str, Any], out: Path) -> None:
    engs = d["engines"]
    if len(engs) != 2:
        return
    n = len(engs[0]["folds"])
    fig, ax = plt.subplots(figsize=(7.5, 4.0), layout="constrained")
    fig.patch.set_facecolor(SURFACE)
    paired_bars(ax, [str(k + 1) for k in range(n)], [num(f["oos"]["sharpe"]) for f in engs[0]["folds"]],
                [num(f["oos"]["sharpe"]) for f in engs[1]["folds"]],
                (f"{engs[0]['engine']} (Мамдані)" if engs[0]["engine"] == "mamdani" else engs[0]["engine"],
                 f"{engs[1]['engine']} (лінійне голосування)" if engs[1]["engine"] == "linear"
                 else engs[1]["engine"]), "Коефіцієнт Шарпа OOS (річний)")
    fig.legend(*ax.get_legend_handles_labels(), loc="outside lower center", ncols=2, frameon=False,
               fontsize=8, labelcolor=INK_2)
    ax.set_title("Ті самі фолди, той самий вибір з фронту: OOS обраної клітинки", color=INK, fontsize=10,
                 loc="left")
    fig.savefig(out, dpi=150, facecolor=SURFACE)
    plt.close(fig)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Walk-forward figures from scripts/run_walkforward.py output")
    ap.add_argument("--in", dest="inp", default=None, help="walkforward_<SYM>_<engine>.json")
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--engine", choices=("mamdani", "linear"), default="mamdani")
    ap.add_argument("--smoke", action="store_true", help="read the smoke output under artifacts/tmp")
    ap.add_argument("--out", default=None, help="output directory (default: next to the input)")
    args = ap.parse_args(argv)
    plt.rcParams.update({"font.family": "DejaVu Sans"})
    stem = f"walkforward_{args.symbol}_{args.engine}"
    inp = Path(args.inp) if args.inp else default_input(args, stem)
    d = json.loads(inp.read_text(encoding="utf-8"))
    stem = inp.stem
    out = Path(args.out) if args.out else inp.parent
    out.mkdir(parents=True, exist_ok=True)
    made = [out / f"{stem}_folds.png", out / f"{stem}_oos_equity.png"]
    plot_folds(d, made[0])
    plot_equity(d, inp.parent / f"{stem}_oos_equity.csv", made[1])
    eng = inp.parent / f"walkforward_{d['provenance']['dataset']['symbol']}_engines.json"
    if eng.exists():
        e = json.loads(eng.read_text(encoding="utf-8"))
        # лише порівняння з ТОГО САМОГО прогону (--engine both): та сама команда, час і набір — інакше це
        # залишок попереднього запуску в тій самій теці, і рисунок змішав би два прогони
        same = all(e["provenance"].get(k) == d["provenance"].get(k) for k in ("command", "created_utc"))
        ds_e, ds_d = e["provenance"]["dataset"], d["provenance"]["dataset"]
        same = same and ds_e["dataset_hash"] == ds_d["dataset_hash"]
        if same:
            made.append(out / f"walkforward_{d['provenance']['dataset']['symbol']}_engines.png")
            plot_engines(e, made[-1])
        else:
            print(f"skipped {eng.name}: it comes from another run than {inp.name}")
    for m in made:
        print(f"wrote {m}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
