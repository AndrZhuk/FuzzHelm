"""Рисунок функцій належності чотирьох лінгвістичних змінних (T, R, V, U).

Найменування: scripts/plot_membership.py
Призначення: артефакт фази 4 — 4 панелі МФ для підрозділу 2.6 звіту; читає робочу конфігурацію
    config/membership.yaml (або --membership <шлях>) і пише docs/figures/fuzzy_membership.png.
Автор: Андрій Жук, 2026.

Запуск:  uv run python scripts/plot_membership.py [--membership config/membership.yaml] [--out ...]
"""

from __future__ import annotations

import argparse
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

from fuzzhelm.fuzzy.membership import LinguisticVariable, load_membership

ROOT = Path(__file__).resolve().parents[1]
OUT_DEFAULT = ROOT / "docs" / "figures" / "fuzzy_membership.png"

# Категоріальна палітра (фіксований порядок слотів, перевірена валідатором на CVD/контраст);
# світлі слоти мають контраст < 3:1, тому кожна крива підписана прямо біля піку.
SERIES = ("#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4")
INK, INK_2, MUTED = "#0b0b0b", "#52514e", "#898781"
GRID, AXIS, SURFACE = "#e1e0d9", "#c3c2b7", "#fcfcfb"

TITLES = {
    "T": "T — трендова складова консенсусу",
    "R": "R — реверсійна складова (тиск)",
    "V": "V — режим волатильності (перцентиль σ_P)",
    "U": "U — вихід: торговельний сигнал",
}


def _style_axes(ax: plt.Axes) -> None:
    ax.set_facecolor(SURFACE)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(AXIS)
        ax.spines[side].set_linewidth(0.8)
    ax.grid(True, color=GRID, linewidth=0.6, linestyle="-")
    ax.set_axisbelow(True)
    ax.tick_params(colors=INK_2, labelsize=8, length=3, color=AXIS)


def _panel(ax: plt.Axes, var: LinguisticVariable) -> None:
    lo, hi = var.range
    x = np.linspace(lo, hi, 1001)
    mu = var.evaluate(x)
    for k, (name, mf) in enumerate(var.terms.items()):
        color = SERIES[k % len(SERIES)]
        ax.plot(x, mu[k], color=color, linewidth=1.6, solid_capstyle="round", solid_joinstyle="round",
                label=name)
        c_lo, c_hi = mf.core
        peak = min(max((c_lo + c_hi) / 2.0, lo + 0.06 * (hi - lo)), hi - 0.06 * (hi - lo))
        ax.annotate(name, xy=(peak, 1.0), xytext=(0, 5), textcoords="offset points", ha="center",
                    va="bottom", fontsize=7, color=INK)
    ax.set_xlim(lo, hi)
    ax.set_ylim(0.0, 1.16)
    ax.set_yticks([0.0, 0.5, 1.0])
    ax.set_ylabel("μ", color=INK_2, fontsize=9)
    source = var.meta.get("source")
    note = f"джерело: {source}" if source else "джерело: expert"
    if var.meta.get("provisional"):
        note += " · тимчасові значення до калібрування"
    ax.set_title(TITLES.get(var.name, var.name), loc="left", fontsize=10, color=INK, pad=14)
    ax.text(0.0, 1.02, note, transform=ax.transAxes, fontsize=7.5, color=MUTED, va="bottom")
    ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.12), ncol=len(var.terms), fontsize=7,
              frameon=False, handlelength=1.6, columnspacing=1.0, labelcolor=INK_2)
    _style_axes(ax)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--membership", type=Path, default=None, help="шлях до membership.yaml")
    ap.add_argument("--out", type=Path, default=OUT_DEFAULT)
    args = ap.parse_args()
    cfg = load_membership(args.membership)

    plt.rcParams.update({"font.family": "DejaVu Sans", "figure.facecolor": SURFACE})
    fig, axes = plt.subplots(2, 2, figsize=(11.0, 7.4))
    for ax, name in zip(axes.ravel(), ("T", "R", "V", "U"), strict=True):
        _panel(ax, cfg[name])
    fig.suptitle("Функції належності лінгвістичних змінних нечіткого ядра", x=0.01, ha="left",
                 fontsize=12, color=INK)
    fig.tight_layout(rect=(0.0, 0.0, 1.0, 0.96), h_pad=2.6)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(args.out, dpi=200)
    print(f"wrote {args.out.relative_to(ROOT) if args.out.is_relative_to(ROOT) else args.out}")


if __name__ == "__main__":
    main()
