"""Поверхня керування u(T, R) при фіксованому V і аудит монотонності за T.

Найменування: fuzzy/surface.py
Призначення: дані для рисунка поверхні керування (scripts/plot_control_surface.py, V = 0.2/0.5/0.9)
    і сканування монотонності рушія за трендовим входом T (брифінг §3, питання 2).
Автор: Андрій Жук, 2026.

Результат аудиту для робочої конфігурації (w ≡ 1) див. docs/deviations.d/fuzzy.md: локальна
монотонність Мамдані max-min + центроїд НЕ виконується; виконується «груба» — для T₂ ≥ T₁ + 1.0.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import numpy.typing as npt

from fuzzhelm.fuzzy.base import InferenceEngine

FloatArray = npt.NDArray[np.float64]


def evaluate_grid(engine: InferenceEngine, T: FloatArray, R: FloatArray, V: float | FloatArray) -> FloatArray:
    """u_raw у точках (T, R, V) з broadcast; векторизований шлях, якщо рушій має infer_batch."""
    batch = getattr(engine, "infer_batch", None)
    if batch is not None:
        return np.asarray(batch(T, R, V), dtype=np.float64)
    t, r, v = np.broadcast_arrays(np.asarray(T, dtype=np.float64), np.asarray(R, dtype=np.float64),
                                  np.asarray(V, dtype=np.float64))
    out = np.empty(t.shape, dtype=np.float64)
    for idx in np.ndindex(t.shape):
        out[idx] = engine.infer(float(t[idx]), float(r[idx]), float(v[idx])).u_raw
    return out


def control_surface(engine: InferenceEngine, V: float, n: int = 41,
                    t_range: tuple[float, float] = (-1.0, 1.0),
                    r_range: tuple[float, float] = (-1.0, 1.0)) -> tuple[FloatArray, FloatArray, FloatArray]:
    """(T_grid, R_grid, U) форми (n, n): рядки — R, стовпці — T (обидва зростають).

    U[i, j] = u(T_grid[i, j], R_grid[i, j], V) = u(T_j, R_i, V).
    """
    if n < 2:
        raise ValueError("n must be ≥ 2")
    t = np.linspace(t_range[0], t_range[1], n)
    r = np.linspace(r_range[0], r_range[1], n)
    T_grid, R_grid = np.meshgrid(t, r)
    return T_grid, R_grid, evaluate_grid(engine, T_grid, R_grid, V)


@dataclass(frozen=True, slots=True)
class MonotonicityReport:
    """Підсумок сканування u(T) на сітці T × R × V.

    decreasing_steps — кількість сусідніх кроків T, де u спадає більш ніж на tol;
    max_reversal = max (max_{T'≤T} u(T') − u(T)) і де він досягається (T1 < T2, R, V, u1, u2);
    min_gap_slack = min (min_{T2 ≥ T1+gap} u(T2) − u(T1)) — «груба» монотонність при ≥ 0.
    """

    n_T: int
    n_R: int
    n_V: int
    tol: float
    decreasing_steps: int
    total_steps: int
    max_reversal: float
    reversal_at: tuple[float, float, float, float, float, float]
    gap: float
    min_gap_slack: float
    gap_slack_at: tuple[float, float, float, float]


def monotonicity_scan(engine: InferenceEngine, *, n_T: int = 401, n_R: int = 81, n_V: int = 41,
                      gap: float = 1.0, tol: float = 1e-9,
                      v_range: tuple[float, float] = (0.0, 1.0)) -> MonotonicityReport:
    """Сканування монотонності u за T при фіксованих (R, V) на рівномірній сітці."""
    ts = np.linspace(-1.0, 1.0, n_T)
    rs = np.linspace(-1.0, 1.0, n_R)
    vs = np.linspace(v_range[0], v_range[1], n_V)
    step = ts[1] - ts[0]
    k = max(1, int(np.ceil(gap / step - 1e-9)))
    dec = 0
    total = 0
    best_rev = -np.inf
    rev_at: tuple[float, float, float, float, float, float] = (0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
    best_slack = np.inf
    slack_at: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)
    for v in vs:
        T_grid, R_grid = np.meshgrid(ts, rs)
        U = evaluate_grid(engine, T_grid, R_grid, float(v))           # (n_R, n_T)
        d = np.diff(U, axis=1)
        dec += int(np.count_nonzero(d < -tol))
        total += d.size
        rev = np.maximum.accumulate(U, axis=1) - U
        i, j = np.unravel_index(int(np.argmax(rev)), rev.shape)
        if rev[i, j] > best_rev:
            j1 = int(np.argmax(U[i, : j + 1]))
            best_rev = float(rev[i, j])
            rev_at = (float(ts[j1]), float(ts[j]), float(rs[i]), float(v), float(U[i, j1]), float(U[i, j]))
        if k < n_T:
            suf = np.minimum.accumulate(U[:, ::-1], axis=1)[:, ::-1]
            slack = suf[:, k:] - U[:, :-k]
            a, b = np.unravel_index(int(np.argmin(slack)), slack.shape)
            if slack[a, b] < best_slack:
                best_slack = float(slack[a, b])
                # T₂ — фактичний argmin u на [T₁ + gap; 1], а не лише ліва межа цього відрізка
                j2 = int(b) + k + int(np.argmin(U[a, int(b) + k:]))
                slack_at = (float(ts[b]), float(ts[j2]), float(rs[a]), float(v))
    return MonotonicityReport(n_T=n_T, n_R=n_R, n_V=n_V, tol=tol, decreasing_steps=dec, total_steps=total,
                              max_reversal=max(0.0, best_rev), reversal_at=rev_at, gap=float(k * step),
                              min_gap_slack=best_slack, gap_slack_at=slack_at)
