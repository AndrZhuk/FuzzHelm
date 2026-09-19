"""Поверхня керування u(T, R) рушія Мамдані при V = 0.2 / 0.5 / 0.9 + таблиця правил + аудит монотонності.

Найменування: scripts/plot_control_surface.py
Призначення: артефакти фази 4 для підрозділу 2.6 і Додатка А звіту (2D-карти замість 3D у браузері,
    брифінг §11): docs/figures/fuzzy_control_surface.png, docs/figures/fuzzy_rules_table.md,
    docs/figures/fuzzy_monotonicity.md (виміряні числа монотонності для поточної конфігурації).
Автор: Андрій Жук, 2026.

Хвиля 2 (docs/deviations.d/data.md, DATA-07): числа монотонності для ПОТОЧНОЇ (каліброваної) конфігурації
уточнюються оптимізатором (Нелдер–Мід з найкращих точок сітки), а поріг кроку «грубої» монотонності
шукається бісекцією — сіткові оцінки занижують реверс і завищують запас (FZ-01).

Незалежна перехресна перевірка (хвиля 2, data): глобальна диференціальна еволюція (scipy, `--de-seeds`
сідів) + полірування Нелдером–Мідом для тих самих величин і для кроків навколо g* — локальний оптимізатор
із сіткових стартів міг би пропустити гірший мінімум.

Запуск:  uv run python scripts/plot_control_surface.py [--n 201] [--skip-scan] [--no-refine] [--de-seeds 6]
"""

from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path

import matplotlib
import numpy as np

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.colors import LinearSegmentedColormap
from scipy.optimize import differential_evolution, minimize

from fuzzhelm.fuzzy.linear import LinearVoteEngine
from fuzzhelm.fuzzy.mamdani import MamdaniEngine
from fuzzhelm.fuzzy.membership import load_membership
from fuzzhelm.fuzzy.rules import format_rule_table_md, load_rulebase
from fuzzhelm.fuzzy.surface import control_surface, evaluate_grid, monotonicity_scan

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


# ------------------------------------------------------------------ уточнення оптимізатором (хвиля 2)

REFINE_N_T, REFINE_N_R, REFINE_N_V = 401, 81, 41       # сітка стартів (окрема від звітної 801×161×81)
N_STARTS = 24
ENTER = 0.25                                           # поріг входу гістерезису (sizing.HysteresisGate)


def _u_tensor(engine: MamdaniEngine, ts: np.ndarray, rs: np.ndarray, vs: np.ndarray) -> np.ndarray:
    T, R = np.meshgrid(ts, rs)
    return np.stack([evaluate_grid(engine, T, R, float(v)) for v in vs])       # (n_V, n_R, n_T)


def _min_slack_rows(U: np.ndarray, k: int) -> np.ndarray:
    """Для кожного рядка (V, R): min_{j} (min_{j' ≥ j+k} u_j' − u_j); k — крок у вузлах сітки T."""
    suf = np.minimum.accumulate(U[..., ::-1], axis=-1)[..., ::-1]
    return (suf[..., k:] - U[..., :-k]).min(axis=-1)


def _nelder_mead(fun: Callable[[np.ndarray], float], starts: Sequence[np.ndarray]
                 ) -> tuple[float, np.ndarray]:
    best_f, best_x = np.inf, np.asarray(starts[0], dtype=np.float64)
    for x0 in starts:
        res = minimize(fun, np.asarray(x0, dtype=np.float64), method="Nelder-Mead",
                       options={"xatol": 1e-10, "fatol": 1e-13, "maxiter": 6000, "maxfev": 12000})
        if res.fun < best_f:
            best_f, best_x = float(res.fun), np.asarray(res.x, dtype=np.float64)
    return best_f, best_x


def _clip(x: float, lo: float, hi: float) -> float:
    return min(hi, max(lo, float(x)))


def refine_reversal(engine: MamdaniEngine, U: np.ndarray, ts: np.ndarray, rs: np.ndarray, vs: np.ndarray
                    ) -> tuple[float, tuple[float, float, float, float]]:
    """sup_{T₁ ≤ T₂} u(T₁) − u(T₂): старти — N_STARTS найгірших рядків сітки, далі Нелдер–Мід."""
    rev = np.maximum.accumulate(U, axis=-1) - U
    flat = np.argsort(rev.max(axis=-1).ravel())[::-1][:N_STARTS]
    starts = []
    for idx in flat:
        iv, ir = np.unravel_index(int(idx), rev.shape[:2])
        j2 = int(np.argmax(rev[iv, ir]))
        j1 = int(np.argmax(U[iv, ir, : j2 + 1]))
        starts.append(np.array([ts[j1], ts[j2] - ts[j1], rs[ir], vs[iv]]))

    def point(x: np.ndarray) -> tuple[float, float, float, float]:
        t1 = _clip(x[0], -1.0, 1.0)
        return t1, _clip(t1 + abs(x[1]), -1.0, 1.0), _clip(x[2], -1.0, 1.0), _clip(x[3], 0.0, 1.0)

    def fun(x: np.ndarray) -> float:
        t1, t2, r, v = point(x)
        return -(engine.infer_u(t1, r, v) - engine.infer_u(t2, r, v))

    f, x = _nelder_mead(fun, starts)
    return -f, point(x)


def refine_margin(engine: MamdaniEngine, U: np.ndarray, ts: np.ndarray, rs: np.ndarray, vs: np.ndarray,
                  gap: float) -> tuple[float, tuple[float, float, float, float]]:
    """inf_{T₂ ≥ T₁ + gap} u(T₂) − u(T₁): старти — найгірші рядки сітки, далі Нелдер–Мід."""
    step = float(ts[1] - ts[0])
    k = max(1, int(np.ceil(gap / step - 1e-9)))
    suf = np.minimum.accumulate(U[..., ::-1], axis=-1)[..., ::-1]
    slack = suf[..., k:] - U[..., :-k]
    flat = np.argsort(slack.min(axis=-1).ravel())[:N_STARTS]
    starts = []
    for idx in flat:
        iv, ir = np.unravel_index(int(idx), slack.shape[:2])
        j1 = int(np.argmin(slack[iv, ir]))
        j2 = j1 + k + int(np.argmin(U[iv, ir, j1 + k:]))
        starts.append(np.array([ts[j1], max(0.0, ts[j2] - ts[j1] - gap), rs[ir], vs[iv]]))

    def point(x: np.ndarray) -> tuple[float, float, float, float]:
        t1 = _clip(x[0], -1.0, 1.0 - gap)
        return t1, _clip(t1 + gap + abs(x[1]), -1.0, 1.0), _clip(x[2], -1.0, 1.0), _clip(x[3], 0.0, 1.0)

    def fun(x: np.ndarray) -> float:
        t1, t2, r, v = point(x)
        return engine.infer_u(t2, r, v) - engine.infer_u(t1, r, v)

    f, x = _nelder_mead(fun, starts)
    return f, point(x)


def gap_threshold(engine: MamdaniEngine, U: np.ndarray, ts: np.ndarray, rs: np.ndarray, vs: np.ndarray,
                  lo: float = 0.5, hi: float = 2.0, iters: int = 14) -> tuple[float, float]:
    """Найменший крок g, з якого уточнений інфімум запасу ≥ 0 (бісекція; запас не спадає за g).
    Повертає (g, запас при g)."""
    if refine_margin(engine, U, ts, rs, vs, hi)[0] < 0:
        return float("inf"), float("nan")
    for _ in range(iters):
        mid = 0.5 * (lo + hi)
        if refine_margin(engine, U, ts, rs, vs, mid)[0] >= 0:
            hi = mid
        else:
            lo = mid
    return hi, refine_margin(engine, U, ts, rs, vs, hi)[0]


def _polish(fun: Callable[[np.ndarray], float], x0: np.ndarray) -> tuple[float, np.ndarray]:
    res = minimize(fun, x0, method="Nelder-Mead",
                   options={"xatol": 1e-11, "fatol": 1e-14, "maxiter": 8000, "maxfev": 16000})
    return float(res.fun), np.asarray(res.x, dtype=np.float64)


def de_search(fun: Callable[[np.ndarray], float], bounds: Sequence[tuple[float, float]], seeds: int
              ) -> tuple[float, np.ndarray]:
    """min fun: диференціальна еволюція (seed = 0..seeds−1) + полірування Нелдером–Мідом; найкраще з усіх.
    Обмеження (T₁ ≤ T₂, межі діапазонів) тримає сама параметризація `fun` (abs/clip), тож полірування
    без меж їх не порушує."""
    best_f, best_x = np.inf, np.zeros(len(bounds))
    for sd in range(seeds):
        r = differential_evolution(fun, bounds, seed=sd, tol=1e-12, maxiter=400, popsize=20, polish=False)
        f, x = _polish(fun, np.asarray(r.x, dtype=np.float64))
        if min(f, float(r.fun)) < best_f:
            best_f, best_x = (f, x) if f <= r.fun else (float(r.fun), np.asarray(r.x, dtype=np.float64))
    return best_f, best_x


def de_crosscheck(engine: MamdaniEngine, seeds: int, gaps: Sequence[float]
                  ) -> list[tuple[str, float, tuple[float, float, float, float]]]:
    """Ті самі величини, що й refine_*, але глобальним пошуком: sup реверсу і inf запасу для кожного кроку."""
    u = engine.infer_u

    def rev_point(x: np.ndarray) -> tuple[float, float, float, float]:
        t1 = _clip(x[0], -1.0, 1.0)
        return t1, _clip(t1 + abs(x[1]), -1.0, 1.0), _clip(x[2], -1.0, 1.0), _clip(x[3], 0.0, 1.0)

    def rev(x: np.ndarray) -> float:
        t1, t2, r, v = rev_point(x)
        return -(u(t1, r, v) - u(t2, r, v))

    out: list[tuple[str, float, tuple[float, float, float, float]]] = []
    f, x = de_search(rev, [(-1.0, 1.0), (0.0, 2.0), (-1.0, 1.0), (0.0, 1.0)], seeds)
    out.append(("sup локального реверсу u(T₁) − u(T₂), T₁ ≤ T₂", -f, rev_point(x)))
    for gap in gaps:
        def m_point(x: np.ndarray, g: float = gap) -> tuple[float, float, float, float]:
            t1 = _clip(x[0], -1.0, 1.0 - g)
            return t1, _clip(t1 + g + abs(x[1]), -1.0, 1.0), _clip(x[2], -1.0, 1.0), _clip(x[3], 0.0, 1.0)

        def marg(x: np.ndarray, pt: Callable[[np.ndarray], tuple[float, float, float, float]] = m_point
                 ) -> float:
            t1, t2, r, v = pt(x)
            return u(t2, r, v) - u(t1, r, v)

        f, x = de_search(marg, [(-1.0, 1.0 - gap), (0.0, 2.0), (-1.0, 1.0), (0.0, 1.0)], seeds)
        out.append((f"inf запасу «грубої» монотонності, крок {gap:.4f}", f, m_point(x)))
    return out


def hysteresis_rows(U: np.ndarray, enter: float = ENTER) -> tuple[int, int]:
    """Рядки (V, R), у яких рішення «u ≥ enter» немонотонне за T (1 → 0 при зростанні T)."""
    on = np.greater_equal(U, enter)
    bad = np.any(on[..., :-1] & ~on[..., 1:], axis=-1)
    return int(bad.sum()), int(bad.size)


def write_monotonicity(engine: MamdaniEngine, out: Path, *, refine: bool = True, de_seeds: int = 0,
                       command: str = "uv run python scripts/plot_control_surface.py") -> None:
    t0 = time.perf_counter()
    rep = monotonicity_scan(engine, n_T=801, n_R=161, n_V=81, gap=1.0)
    lin = monotonicity_scan(LinearVoteEngine(), n_T=201, n_R=41, n_V=5, gap=1.0)
    dt = time.perf_counter() - t0
    t1, t2, r, v, u1, u2 = rep.reversal_at
    g1, g2, gr, gv = rep.gap_slack_at
    provisional = bool(engine.membership.V.meta.get("provisional"))
    run_id = engine.membership.V.meta.get("source_run_id")
    share = 100 * rep.decreasing_steps / rep.total_steps
    lines = [
        "# Аудит монотонності u за T (поточна конфігурація)",
        "",
        f"Конфігурація: `config/membership.yaml` (V-блок: "
        f"{'ТИМЧАСОВИЙ (provisional)' if provisional else f'калібрований, source_run_id = {run_id}'}). "
        f"Згенеровано `{command}`.",
        "",
        f"Сітка T×R×V = {rep.n_T}×{rep.n_R}×{rep.n_V}; час сканування {dt:.1f} с.",
        "",
        "| Показник | Мамдані (w ≡ 1) | Лінійне голосування |",
        "|---|---|---|",
        f"| кроки T зі спаданням u (> {rep.tol:g}) | {rep.decreasing_steps} з {rep.total_steps} "
        f"({share:.1f}%) | {lin.decreasing_steps} з {lin.total_steps} |",
        f"| max локальний реверс max_{{T'≤T}} u(T') − u(T) (сітка) | {rep.max_reversal:.5f} | "
        f"{lin.max_reversal:.5f} |",
        f"| min запас «грубої» монотонності (T₂ ≥ T₁ + {rep.gap:.2f}) (сітка) | {rep.min_gap_slack:.5f} | "
        f"{lin.min_gap_slack:.5f} |",
        "",
        f"Найбільший реверс на сітці: T₁ = {t1:.4f}, T₂ = {t2:.4f}, R = {r:.4f}, V = {v:.4f}: "
        f"u(T₁) = {u1:.6f}, u(T₂) = {u2:.6f}.",
        f"Найменший запас «грубої» монотонності на сітці: T₁ = {g1:.4f}, T₂ = {g2:.4f}, R = {gr:.4f}, "
        f"V = {gv:.4f}.",
        "",
    ]
    if refine:
        t0 = time.perf_counter()
        ts = np.linspace(-1.0, 1.0, REFINE_N_T)
        rs = np.linspace(-1.0, 1.0, REFINE_N_R)
        vs = np.linspace(0.0, 1.0, REFINE_N_V)
        U = _u_tensor(engine, ts, rs, vs)
        rev, (a1, a2, ar, av) = refine_reversal(engine, U, ts, rs, vs)
        marg, (b1, b2, br, bv) = refine_margin(engine, U, ts, rs, vs, 1.0)
        g_star, m_star = gap_threshold(engine, U, ts, rs, vs)
        fine = _u_tensor(engine, np.linspace(-1.0, 1.0, 801), np.linspace(-1.0, 1.0, 321),
                         np.linspace(0.0, 1.0, 161))
        bad, rows = hysteresis_rows(fine)
        dt2 = time.perf_counter() - t0
        lines += [
            "## Уточнення оптимізатором (Нелдер–Мід з найгірших точок сітки)",
            "",
            f"Старти — {N_STARTS} найгірших рядків (V, R) сітки {REFINE_N_T}×{REFINE_N_R}×{REFINE_N_V}; "
            f"час {dt2:.1f} с.",
            "",
            "| Показник | Значення | Точка |",
            "|---|---|---|",
            f"| sup локального реверсу u(T₁) − u(T₂), T₁ ≤ T₂ | {rev:.5f} | T₁ = {a1:.5f}, T₂ = {a2:.5f}, "
            f"R = {ar:.5f}, V = {av:.5f} |",
            f"| inf запасу «грубої» монотонності, крок 1.0 | {marg:.5f} | T₁ = {b1:.5f}, T₂ = {b2:.5f}, "
            f"R = {br:.5f}, V = {bv:.5f} |",
            (f"| найменший крок g*, з якого запас ≥ 0 (бісекція, 14 кроків) | {g_star:.4f} | запас при g*: "
             f"{m_star:.2e} |") if np.isfinite(g_star) else
            "| найменший крок g*, з якого запас ≥ 0 (бісекція) | > 2.0 | — |",
            f"| рядки (V, R), де рішення u ≥ {ENTER} немонотонне за T (сітка 801×321×161) | {bad} з {rows} "
            f"({100 * bad / rows:.2f}%) | — |",
            "",
            "Від'ємний інфімум запасу при кроці 1.0 означає, що «груба» монотонність із кроком 1.0 для цієї "
            "конфігурації НЕ виконується; вона виконується лише з кроку g*. Числа в "
            "tests/property/test_fuzzy_property.py стосуються зафіксованих у тесті МФ специфікації "
            "(там інфімум запасу при кроці 1.0 ≈ +0.00723, sup реверсу ≈ 0.15902) і для робочої конфігурації "
            "не переносяться (FZ-01).",
            "",
        ]
    else:
        lines += ["Це оцінки на сітці (без уточнення оптимізатором, `--no-refine`).", ""]
        g_star = float("nan")
    if de_seeds > 0:
        t0 = time.perf_counter()
        gaps = [1.0, *([round(g_star - 0.05, 4), round(g_star, 4)] if np.isfinite(g_star) else [])]
        rows = de_crosscheck(engine, de_seeds, gaps)
        dt3 = time.perf_counter() - t0
        lines += [
            "## Незалежна перевірка глобальним пошуком (диференціальна еволюція + Нелдер–Мід)",
            "",
            f"`scipy.optimize.differential_evolution` (popsize 20, maxiter 400, seed = 0…{de_seeds - 1}) з "
            f"поліруванням Нелдером–Мідом; найкраще з {de_seeds} запусків; час {dt3:.1f} с. Глобальний пошук "
            "не залежить від сіткових стартів; розбіжність з таблицею вище означала б, що локальне уточнення "
            "пропустило гірший мінімум.",
            "",
            "| Показник | Значення | Точка |",
            "|---|---|---|",
            *[f"| {name} | {val:.5f} | T₁ = {p[0]:.5f}, T₂ = {p[1]:.5f}, R = {p[2]:.5f}, V = {p[3]:.5f} |"
              for name, val, p in rows],
            "",
        ]
    out.write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines))
    print(f"wrote {_rel(out)}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--n", type=int, default=201, help="точок сітки на вісь поверхні")
    ap.add_argument("--skip-scan", action="store_true", help="не запускати аудит монотонності")
    ap.add_argument("--no-refine", action="store_true", help="лише сітка, без уточнення оптимізатором")
    ap.add_argument("--de-seeds", type=int, default=6,
                    help="сідів диференціальної еволюції для перехресної перевірки (0 — пропустити)")
    args = ap.parse_args()
    membership = load_membership()
    engine = MamdaniEngine(membership, load_rulebase(None, membership, production=True))
    plot_surface(engine, args.n, FIG_DIR / "fuzzy_control_surface.png")
    write_rules_table(engine, FIG_DIR / "fuzzy_rules_table.md")
    if not args.skip_scan:
        command = " ".join(["uv run python scripts/plot_control_surface.py", *sys.argv[1:]])
        write_monotonicity(engine, FIG_DIR / "fuzzy_monotonicity.md", refine=not args.no_refine,
                           de_seeds=args.de_seeds, command=command)


if __name__ == "__main__":
    main()
