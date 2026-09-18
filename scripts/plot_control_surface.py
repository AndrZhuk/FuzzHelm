"""Поверхня керування u(T, R) рушія Мамдані при V = 0.2 / 0.5 / 0.9 + таблиця правил + аудит монотонності.

Найменування: scripts/plot_control_surface.py
Призначення: артефакти фази 4 для підрозділу 2.6 і Додатка А звіту (2D-карти замість 3D у браузері,
    брифінг §11): docs/figures/fuzzy_control_surface.png, docs/figures/fuzzy_rules_table.md,
    docs/figures/fuzzy_monotonicity.md (виміряні числа монотонності для поточної конфігурації).
Автор: Андрій Жук, 2026.

Запуск:  uv run python scripts/plot_control_surface.py [--n 201] [--skip-scan]
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.colors import LinearSegmentedColormap

from fuzzhelm.fuzzy.linear import LinearVoteEngine
from fuzzhelm.fuzzy.mamdani import MamdaniEngine
from fuzzhelm.fuzzy.membership import load_membership
from fuzzhelm.fuzzy.rules import format_rule_table_md, load_rulebase
from fuzzhelm.fuzzy.surface import control_surface, monotonicity_scan

ROOT = Path(__file__).resolve().parents[1]
FIG_DIR = ROOT / "docs" / "figures"
V_LEVELS = (0.2, 0.5, 0.9)
THRESHOLDS = (-0.25, -0.12, 0.12, 0.25)       # пороги гістерезису HysteresisGate(enter, exit)

INK, INK_2, MUTED, AXIS, SURFACE = "#0b0b0b", "#52514e", "#898781", "#c3c2b7", "#fcfcfb"
# Розбіжна шкала: полюси синій (short) ↔ червоний (long), нейтральна сіра середина (u = 0 — HOLD)
DIVERGING = LinearSegmentedColormap.from_list(
    "fuzzhelm_div", ["#104281", "#2a78d6", "#9ec5f4", "#f0efec", "#f4b9b5", "#e34948", "#9e2b2b"])


def _rel(p: Path) -> str:
    return str(p.relative_to(ROOT)) if p.is_relative_to(ROOT) else str(p)


def plot_surface(engine: MamdaniEngine, n: int, out: Path) -> None:
    cfg = engine.membership
    plt.rcParams.update({"font.family": "DejaVu Sans", "figure.facecolor": SURFACE})
    fig, axes = plt.subplots(1, 3, figsize=(13.5, 5.0), sharey=True, layout="constrained")
    mesh = None
    for ax, v in zip(axes, V_LEVELS, strict=True):
        T, R, U = control_surface(engine, v, n=n)
        mesh = ax.pcolormesh(T, R, U, cmap=DIVERGING, vmin=-1.0, vmax=1.0, shading="auto",
                             rasterized=True)
        cs = ax.contour(T, R, U, levels=list(THRESHOLDS), colors=INK, linewidths=0.6)
        ax.clabel(cs, fmt="%+.2f", fontsize=6.5, inline=True)
        ax.contour(T, R, U, levels=[0.0], colors=MUTED, linewidths=0.6)
        mu_v = cfg.V.fuzzify(v)
        mu_txt = " · ".join(f"{k} {val:.2f}" for k, val in mu_v.items())
        ax.set_title(f"V = {v}", loc="left", fontsize=10, color=INK, pad=16)
        ax.text(0.0, 1.015, f"μ_V: {mu_txt}", transform=ax.transAxes, fontsize=7.5, color=MUTED)
        ax.set_xlabel("T (тренд)", color=INK_2, fontsize=9)
        ax.set_aspect("equal")
        ax.tick_params(colors=INK_2, labelsize=8, color=AXIS)
        for side in ax.spines.values():
            side.set_color(AXIS)
    axes[0].set_ylabel("R (реверсія: < 0 тиск продажу, > 0 тиск купівлі)", color=INK_2, fontsize=9)
    assert mesh is not None
    cbar = fig.colorbar(mesh, ax=axes, shrink=0.78, aspect=28, pad=0.01)
    cbar.set_label("u_raw (−1 short … +1 long)", color=INK_2, fontsize=9)
    cbar.ax.tick_params(labelsize=8, colors=INK_2)
    cbar.outline.set_edgecolor(AXIS)
    fig.suptitle("Поверхня керування u(T, R) рушія Мамдані (45 правил, центроїд, трапеції, 201 вузол); "
                 "ізолінії — пороги гістерезису ±0.12 / ±0.25",
                 x=0.01, ha="left", fontsize=10.5, color=INK)
    fig.get_layout_engine().set(h_pad=0.04, w_pad=0.04)
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=200, bbox_inches="tight")
    print(f"wrote {_rel(out)}")


def write_rules_table(engine: MamdaniEngine, out: Path) -> None:
    rb, cfg = engine.rulebase, engine.membership
    counts: dict[str, int] = {}
    for r in rb:
        counts[r.consequent] = counts.get(r.consequent, 0) + 1
    lines = [
        "# База правил Мамдані — таблиця для Додатка А",
        "",
        "Згенеровано `scripts/plot_control_surface.py` з `config/rules_mamdani.yaml` "
        f"({len(rb)} правил, усі w = 1.0). Рядки — терм R, стовпці — терм T, у клітинці — наслідок U (id).",
        "",
        "Політики: **LO** — домінує реверсія: c = clip(2r + trunc(t/2), −2, 2); "
        "**MID** — тренд перемагає у конфлікті: c = t (або r при t = 0), c = t + r при згоді і |t| = 1; "
        "**HI** — усе до HOLD, c = sign(t) лише при sign(t) = sign(r) ≠ 0.",
        "",
        format_rule_table_md(rb, cfg),
        "Розподіл наслідків: " + ", ".join(f"{k} — {counts.get(k, 0)}" for k in cfg.U.term_names) + ".",
        "",
    ]
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"wrote {_rel(out)}")


def write_monotonicity(engine: MamdaniEngine, out: Path) -> None:
    t0 = time.perf_counter()
    rep = monotonicity_scan(engine, n_T=801, n_R=161, n_V=81, gap=1.0)
    lin = monotonicity_scan(LinearVoteEngine(), n_T=201, n_R=41, n_V=5, gap=1.0)
    dt = time.perf_counter() - t0
    t1, t2, r, v, u1, u2 = rep.reversal_at
    g1, g2, gr, gv = rep.gap_slack_at
    provisional = bool(engine.membership.V.meta.get("provisional"))
    share = 100 * rep.decreasing_steps / rep.total_steps
    lines = [
        "# Аудит монотонності u за T (поточна конфігурація)",
        "",
        f"Сітка T×R×V = {rep.n_T}×{rep.n_R}×{rep.n_V}; V-блок: "
        f"{'ТИМЧАСОВИЙ (provisional)' if provisional else 'калібрований'}; час сканування {dt:.1f} с.",
        "",
        "| Показник | Мамдані (w ≡ 1) | Лінійне голосування |",
        "|---|---|---|",
        f"| кроки T зі спаданням u (> {rep.tol:g}) | {rep.decreasing_steps} з {rep.total_steps} "
        f"({share:.1f}%) | {lin.decreasing_steps} з {lin.total_steps} |",
        f"| max локальний реверс max_{{T'≤T}} u(T') − u(T) | {rep.max_reversal:.5f} | "
        f"{lin.max_reversal:.5f} |",
        f"| min запас «грубої» монотонності (T₂ ≥ T₁ + {rep.gap:.2f}) | {rep.min_gap_slack:.5f} | "
        f"{lin.min_gap_slack:.5f} |",
        "",
        f"Найбільший реверс: T₁ = {t1:.4f}, T₂ = {t2:.4f}, R = {r:.4f}, V = {v:.4f}: "
        f"u(T₁) = {u1:.6f}, u(T₂) = {u2:.6f}.",
        f"Найменший запас «грубої» монотонності: T₁ = {g1:.4f}, T₂ = {g2:.4f}, R = {gr:.4f}, V = {gv:.4f}.",
        "",
        "Це оцінки на сітці: справжній мінімум запасу може бути меншим, а справжній максимум реверсу — "
        "більшим (для МФ зі специфікації уточнення оптимізатором дало інфімум запасу ≈ 0.00723 при "
        "T₁ = 0, T₂ = 1, R ≈ 0.00707, V = 1 і sup реверсу ≈ 0.15902 — див. "
        "tests/property/test_fuzzy_property.py).",
        "",
        "Від'ємний запас означає, що «груба» монотонність для цієї конфігурації порушена — "
        "тоді числа в tests/property/test_fuzzy_property.py (зафіксовані МФ) і висновок звіту "
        "треба переглянути.",
        "",
    ]
    out.write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines))
    print(f"wrote {_rel(out)}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--n", type=int, default=201, help="точок сітки на вісь поверхні")
    ap.add_argument("--skip-scan", action="store_true", help="не запускати аудит монотонності")
    args = ap.parse_args()
    membership = load_membership()
    engine = MamdaniEngine(membership, load_rulebase(None, membership, production=True))
    plot_surface(engine, args.n, FIG_DIR / "fuzzy_control_surface.png")
    write_rules_table(engine, FIG_DIR / "fuzzy_rules_table.md")
    if not args.skip_scan:
        write_monotonicity(engine, FIG_DIR / "fuzzy_monotonicity.md")


if __name__ == "__main__":
    main()
