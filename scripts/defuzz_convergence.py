"""Дослідження збіжності дефазифікації центроїдом за кроком сітки (три схеми інтегрування).

Найменування: scripts/defuzz_convergence.py
Призначення: таблиця похибок для Δ = 0.02 / 0.01 / 0.002 / 0.001 відносно точного центроїда,
    порядок збіжності (формула брифінгу §5.7 і робастний МНК-нахил), робочий критерій
    |u(0.01) − u(0.001)| < 1e−3. Друкує markdown і пише docs/figures/fuzzy_defuzz_convergence.md
    та docs/figures/fuzzy_defuzz_convergence.png.
Автор: Андрій Жук, 2026.

Запуск:  uv run python scripts/defuzz_convergence.py [--n-inputs 60] [--seed 20260918]
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

from fuzzhelm.fuzzy.defuzz import convergence_study
from fuzzhelm.fuzzy.mamdani import default_engine

ROOT = Path(__file__).resolve().parents[1]
FIG_DIR = ROOT / "docs" / "figures"
SCHEME_UK = {"rect": "прямокутники", "trapezoid": "трапеції", "simpson": "Сімпсон"}
SERIES = {"rect": "#2a78d6", "trapezoid": "#eb6834", "simpson": "#1baf7a"}
INK, INK_2, GRID, AXIS, SURFACE = "#0b0b0b", "#52514e", "#e1e0d9", "#c3c2b7", "#fcfcfb"


def to_markdown(study: dict[str, Any], seed: int) -> str:
    lines = [
        "# Збіжність дефазифікації центроїдом за кроком сітки",
        "",
        f"Рушій: Мамдані, робоча база 45 правил; {study['n_inputs']} входів "
        "(T, R, V) ~ U([−1;1]×[−1;1]×[0;1]), "
        f"seed = {seed}. Еталон u*: `{study['reference']}` — точне інтегрування кусково-лінійної μ_agg "
        "за всіма точками зламу (вузли МФ, перетини рівнів обрізання з ребрами, перетини ребер).",
        "",
        "p (брифінг) = log₂(|u_Δ − u_{Δ/2}| / |u_{Δ/2} − u_{Δ/4}|) — медіана та [Q1; Q3] за входами; "
        "розкид великий, бо злами μ_agg лежать у невузлових точках. Робастний порядок — МНК-нахил "
        "log RMS(u_Δ − u*) від log Δ за Δ = 0.04 … 0.0003125 (8 половинних кроків).",
        "",
        "| Схема | Δ | вузлів | max\\|u_Δ − u*\\| | RMS(u_Δ − u*) | p (брифінг): медіана [Q1; Q3] |",
        "|---|---|---|---|---|---|",
    ]
    for scheme, data in study["schemes"].items():
        for row in data["rows"]:
            lines.append(
                f"| {SCHEME_UK.get(scheme, scheme)} | {row['delta']:g} | {row['nodes']} | "
                f"{row['max_abs_err']:.3e} | {row['rms_err']:.3e} | "
                f"{row['p_brief_median']:.2f} [{row['p_brief_q1']:.2f}; {row['p_brief_q3']:.2f}] |")
    lines += [
        "",
        "| Схема | порядок (МНК-нахил) | max\\|u(0.01) − u(0.001)\\| | критерій < 1e−3 |",
        "|---|---|---|---|",
    ]
    for scheme, data in study["schemes"].items():
        crit = data["criterion_max_diff"]
        lines.append(f"| {SCHEME_UK.get(scheme, scheme)} | {data['fitted_order']:.3f} | {crit:.3e} | "
                     f"{'так' if crit < 1e-3 else 'НІ'} |")
    lines += [
        "",
        "Висновок: трапеції — другий порядок; прямокутники без крайових поправок — перший (і не "
        "проходять робочий критерій); Сімпсон також лише ≈ 2-го порядку, бо μ_agg має злами "
        "(лише C⁰), а не 4-го, як для гладких функцій. Робоча схема — трапеції на 201 вузлі.",
        "",
    ]
    return "\n".join(lines)


def plot(study: dict[str, Any], out: Path) -> None:
    plt.rcParams.update({"font.family": "DejaVu Sans", "figure.facecolor": SURFACE})
    fig, ax = plt.subplots(figsize=(7.2, 4.6))
    ax.set_facecolor(SURFACE)
    for scheme, data in study["schemes"].items():
        d = np.array(data["fit_deltas"])
        e = np.array(data["fit_rms_err"])
        label = f"{SCHEME_UK.get(scheme, scheme)}, p ≈ {data['fitted_order']:.2f}"
        ax.loglog(d, e, color=SERIES[scheme], linewidth=1.6, marker="o", markersize=4.5,
                  markeredgecolor=SURFACE, markeredgewidth=1.2, label=label)
        # прямий підпис на правому кінці: тут серії розходяться найдалі (на лівому — злипаються)
        ax.annotate(label, xy=(d[-1], e[-1]), xytext=(7, 0), textcoords="offset points", va="center",
                    fontsize=7.5, color=INK)
    ax.invert_xaxis()
    ax.set_xlabel("крок сітки Δ", color=INK_2, fontsize=9)
    ax.set_ylabel("RMS похибки центроїда", color=INK_2, fontsize=9)
    ax.set_title("Збіжність дефазифікації за кроком сітки (відносно точного центроїда)", loc="left",
                 fontsize=10, color=INK)
    ax.grid(True, which="major", color=GRID, linewidth=0.6)
    ax.tick_params(colors=INK_2, labelsize=8, color=AXIS)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(AXIS)
    ax.legend(fontsize=8, frameon=False, labelcolor=INK_2, loc="lower left")
    ax.set_xlim(0.06, 0.00004)
    fig.tight_layout()
    fig.savefig(out, dpi=200)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--n-inputs", type=int, default=60)
    ap.add_argument("--seed", type=int, default=20260918)
    args = ap.parse_args()
    rng = np.random.default_rng(args.seed)
    pts = rng.uniform((-1.0, -1.0, 0.0), (1.0, 1.0, 1.0), size=(args.n_inputs, 3))
    inputs = [(float(t), float(r), float(v)) for t, r, v in pts]
    study = convergence_study(default_engine(), inputs)
    md = to_markdown(study, args.seed)
    print(md)
    FIG_DIR.mkdir(parents=True, exist_ok=True)
    (FIG_DIR / "fuzzy_defuzz_convergence.md").write_text(md, encoding="utf-8")
    plot(study, FIG_DIR / "fuzzy_defuzz_convergence.png")
    print("wrote docs/figures/fuzzy_defuzz_convergence.md, docs/figures/fuzzy_defuzz_convergence.png")


if __name__ == "__main__":
    main()
