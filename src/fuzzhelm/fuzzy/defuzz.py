"""Дефазифікація центроїдом як задача чисельного інтегрування.

Найменування: fuzzy/defuzz.py
Призначення: u_raw = ∫u·μ_agg(u)du / ∫μ_agg(u)du на сітці u_j = lo + jΔ трьома схемами
    (прямокутники, трапеції, Сімпсон), точний еталон для кусково-лінійних МФ і дослідження
    збіжності за кроком сітки (брифінг §5.7).
Автор: Андрій Жук, 2026.

Схеми (ваги квадратури w_j, u_raw = Σ w_j u_j μ_j / Σ w_j μ_j):
  * rect      — w_j = Δ для всіх вузлів, тобто u = Σ u_j μ_j / Σ μ_j (як у брифінгу);
  * trapezoid — w_j = Δ, крайні Δ/2;
  * simpson   — w_j = Δ/3·(1, 4, 2, 4, …, 4, 1), потрібна парна кількість інтервалів.
Порожня активація (Σ w_j μ_j = 0) ⇒ u_raw = 0.0 рівно (HOLD, не NaN).

μ_agg = max_k min(β_k, μ_k(u)) — кусково-лінійна (для tri/trap-термів U), але лише неперервна:
у точках зламу (вузли МФ, перетини рівнів обрізання β_k з ребрами МФ, перетини ребер різних
термів) похідна стрибає. Тому очікуваний порядок трапецій — 2, а Сімпсон НЕ дає 4-го порядку:
злам усередині панелі обмежує його тим самим O(Δ²). Прямокутники без крайових поправок — O(Δ),
якщо μ_agg(±1) ≠ 0. Злами лежать у довільних (не вузлових) точках, тому похибка осцилює з
положенням зламу всередині комірки, і оцінка порядку з одного відношення шумна — робастна оцінка
в `convergence_study` — МНК-нахил log(RMS похибки за багатьма входами) від log Δ за послідовністю
половинних кроків відносно точного еталону.
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Sequence
from typing import Any, Protocol

import numpy as np
import numpy.typing as npt

from fuzzhelm.fuzzy.membership import DEFUZZ_SCHEMES, MembershipConfig, MembershipFunction

FloatArray = npt.NDArray[np.float64]
DEFAULT_DELTAS: tuple[float, ...] = (0.02, 0.01, 0.002, 0.001)


def quadrature_weights(nodes: int, scheme: str, lo: float = -1.0, hi: float = 1.0) -> FloatArray:
    """Ваги w_j квадратури на рівномірній сітці з `nodes` вузлами на [lo; hi] (з урахуванням Δ)."""
    if scheme not in DEFUZZ_SCHEMES:
        raise ValueError(f"unknown scheme {scheme!r}; expected one of {DEFUZZ_SCHEMES}")
    if nodes < 3:
        raise ValueError(f"need at least 3 grid nodes, got {nodes}")
    delta = (hi - lo) / (nodes - 1)
    w = np.full(nodes, delta, dtype=np.float64)
    if scheme == "trapezoid":
        w[0] = w[-1] = 0.5 * delta
    elif scheme == "simpson":
        if (nodes - 1) % 2:
            raise ValueError(f"simpson needs an even number of intervals, got {nodes - 1}")
        w[:] = 2.0 * delta / 3.0
        w[1:-1:2] = 4.0 * delta / 3.0
        w[0] = w[-1] = delta / 3.0
    return w


def make_grid(nodes: int, lo: float = -1.0, hi: float = 1.0) -> FloatArray:
    """u_j = lo + jΔ, j = 0..nodes−1, обчислене як c + h·(2j − n)/n (c, h — центр і піврадіус).

    Математично це та сама сітка, але в такій формі вона ТОЧНО антисиметрична відносно c
    (u_{n−j} = −u_j при c = 0) і має точні кінці lo, hi — симетрична μ_agg дає центроїд ≈ 0
    без систематичного зсуву від округлення вузлів.
    """
    if nodes < 2:
        raise ValueError(f"need at least 2 grid nodes, got {nodes}")
    n = nodes - 1
    k = np.arange(-n, n + 1, 2, dtype=np.float64)
    return (lo + hi) / 2.0 + (hi - lo) / 2.0 * (k / n)


def nodes_for_delta(delta: float, lo: float = -1.0, hi: float = 1.0) -> int:
    """Кількість вузлів для кроку Δ; (hi − lo)/Δ мусить бути цілим."""
    n = round((hi - lo) / delta)
    if n < 2 or abs(n * delta - (hi - lo)) > 1e-9 * (hi - lo):
        raise ValueError(f"(hi - lo)/delta must be an integer ≥ 2; got delta={delta}")
    return n + 1


def aggregate(betas: npt.ArrayLike, consequent_mu: FloatArray) -> FloatArray:
    """μ_agg(u_j) = max_k min(β_k, μ_k(u_j)); consequent_mu має форму (n_terms, nodes)."""
    b = np.asarray(betas, dtype=np.float64)
    return np.minimum(b[:, None], consequent_mu).max(axis=0)


def centroid(grid: FloatArray, mu: FloatArray, scheme: str = "trapezoid") -> float:
    """Центроїд μ на рівномірній сітці `grid` схемою `scheme`; порожня активація → 0.0."""
    g = np.asarray(grid, dtype=np.float64)
    m = np.asarray(mu, dtype=np.float64)
    if g.shape != m.shape or g.ndim != 1:
        raise ValueError("grid and mu must be 1-D arrays of equal length")
    w = quadrature_weights(g.size, scheme, float(g[0]), float(g[-1]))
    return centroid_weighted(w, w * g, m, float(g[0]), float(g[-1]))


def centroid_weighted(w: FloatArray, wu: FloatArray, mu: FloatArray, lo: float, hi: float) -> float:
    """Ядро центроїда з попередньо обчисленими вагами w і w·u (швидкий шлях рушія)."""
    den = float(w @ mu)
    if not den > 0.0:
        return 0.0
    u = float(wu @ mu) / den
    return lo if u < lo else hi if u > hi else u


# ------------------------------------------------------------------ точний еталон


def _pl_eval(knots: tuple[float, ...], x: float) -> float:
    """μ trap(a,b,c,d) у точці x (та сама формула, що й Trapezoidal.scalar)."""
    a, b, c, d = knots
    if x < a or x > d:
        return 0.0
    if x < b:
        return (x - a) / (b - a)
    if x <= c:
        return 1.0
    return (d - x) / (d - c)


def exact_centroid(mfs: Sequence[MembershipFunction], betas: Sequence[float],
                   lo: float = -1.0, hi: float = 1.0) -> float:
    """Точний центроїд μ_agg = max_k min(β_k, μ_k) для кусково-лінійних (tri/trap) термів.

    Між сусідніми точками зламу μ_agg лінійна, тому ∫μ і ∫u·μ беруться аналітично:
    на [x0; x1] з μ(x0)=y0, μ(x1)=y1:  ∫μ = h(y0+y1)/2,  ∫u·μ = h·(x0(2y0+y1) + x1(y0+2y1))/6.
    Значення на кінцях інтервалу беруться як лінійна екстраполяція з двох внутрішніх точок —
    це коректно і для вертикальних ребер (a == b), де μ має розрив у вузлі.
    """
    knots_list: list[tuple[float, float, float, float]] = []
    for mf in mfs:
        k = mf.knots
        if k is None or len(k) != 4:
            raise ValueError("exact_centroid supports only piecewise-linear (tri/trap) terms")
        knots_list.append((k[0], k[1], k[2], k[3]))
    active = [(kn, float(b)) for kn, b in zip(knots_list, betas, strict=True) if b > 0.0]
    if not active:
        return 0.0

    def g_all(x: float) -> list[float]:
        return [min(b, _pl_eval(kn, x)) for kn, b in active]

    # 1) злами кожного g_k = min(β_k, μ_k): вузли МФ і точки μ_k = β_k на ребрах
    pts: set[float] = {lo, hi}
    for (a, b_, c, d), beta in active:
        pts.update((a, b_, c, d))
        if b_ > a:
            pts.add(a + beta * (b_ - a))
        if d > c:
            pts.add(d - beta * (d - c))
    xs = sorted(p for p in pts if lo <= p <= hi)
    # 2) попарні перетини g_j = g_k всередині інтервалів, де кожна g лінійна
    extra: set[float] = set()
    for x0, x1 in itertools.pairwise(xs):
        h = x1 - x0
        if h <= 0.0:
            continue
        p, q = x0 + h / 3.0, x0 + 2.0 * h / 3.0
        gp, gq = g_all(p), g_all(q)
        for i in range(len(active)):
            for j in range(i + 1, len(active)):
                # лінійна різниця на інтервалі, екстрапольована на кінці
                dp, dq = gp[i] - gp[j], gq[i] - gq[j]
                d0, d1 = 2.0 * dp - dq, 2.0 * dq - dp
                if (d0 < 0.0 < d1) or (d1 < 0.0 < d0):
                    extra.add(x0 + h * d0 / (d0 - d1))
    xs = sorted(set(xs) | {e for e in extra if lo < e < hi})
    area = 0.0
    moment = 0.0
    for x0, x1 in itertools.pairwise(xs):
        h = x1 - x0
        if h <= 0.0:
            continue
        mp = max(g_all(x0 + h / 3.0))
        mq = max(g_all(x0 + 2.0 * h / 3.0))
        y0, y1 = 2.0 * mp - mq, 2.0 * mq - mp
        area += h * (y0 + y1) / 2.0
        moment += h * (x0 * (2.0 * y0 + y1) + x1 * (y0 + 2.0 * y1)) / 6.0
    if not area > 0.0:
        return 0.0
    return moment / area


# ------------------------------------------------------------------ дослідження збіжності


class StrengthEngine(Protocol):
    """Мінімум від рушія для дослідження збіжності (реалізує MamdaniEngine)."""

    @property
    def membership(self) -> MembershipConfig: ...

    def consequent_strengths(self, T: float, R: float, V: float) -> FloatArray: ...


def _u_on_grid(betas: FloatArray, mu_terms: FloatArray, w: FloatArray, wu: FloatArray,
               bounds: tuple[float, float]) -> float:
    return centroid_weighted(w, wu, aggregate(betas, mu_terms), bounds[0], bounds[1])


def convergence_study(engine: StrengthEngine, inputs: Sequence[tuple[float, float, float]],
                      deltas: Sequence[float] = DEFAULT_DELTAS, *,
                      schemes: Sequence[str] = DEFUZZ_SCHEMES,
                      fit_base_delta: float = 0.04, fit_levels: int = 8) -> dict[str, Any]:
    """Похибки центроїда за кроком сітки для трьох схем.

    Для кожного Δ з `deltas`: max і RMS |u_Δ − u*| за всіма входами (u* — точний еталон
    `exact_centroid`, або трапеції з Δ = 1e−5, якщо терми U не кусково-лінійні) і порядок за
    формулою брифінгу p = log2(|u_Δ − u_{Δ/2}| / |u_{Δ/2} − u_{Δ/4}|) (медіана і квартилі за входами,
    де обидві різниці > 1e−14). `fitted_order` — МНК-нахил log RMS(Δ) від log Δ для
    Δ_k = fit_base_delta / 2^k, k = 0..fit_levels−1 (робастна оцінка). `criterion_max_diff` —
    max |u(0.01) − u(0.001)| (робочий критерій брифінгу: < 1e−3).
    """
    U = engine.membership.U
    lo, hi = U.range
    mfs = list(U.terms.values())
    betas = [np.asarray(engine.consequent_strengths(t, r, v), dtype=np.float64) for t, r, v in inputs]
    if not betas:
        raise ValueError("inputs must be non-empty")
    piecewise_linear = all(mf.knots is not None for mf in mfs)
    if piecewise_linear:
        ref = np.array([exact_centroid(mfs, b.tolist(), lo, hi) for b in betas])
        ref_kind = "exact_piecewise_linear"
    else:  # pragma: no cover - поточна конфігурація U кусково-лінійна
        n_ref = nodes_for_delta(1e-5, lo, hi)
        g = make_grid(n_ref, lo, hi)
        wr = quadrature_weights(n_ref, "trapezoid", lo, hi)
        mu_ref = U.evaluate(g)
        ref = np.array([_u_on_grid(b, mu_ref, wr, wr * g, (lo, hi)) for b in betas])
        ref_kind = "trapezoid_delta_1e-5"

    cache: dict[tuple[int, str], FloatArray] = {}

    def u_all(delta: float, scheme: str) -> FloatArray:
        n = nodes_for_delta(delta, lo, hi)
        key = (n, scheme)
        if key not in cache:
            g = make_grid(n, lo, hi)
            w = quadrature_weights(n, scheme, lo, hi)
            mu_t = U.evaluate(g)
            cache[key] = np.array([_u_on_grid(b, mu_t, w, w * g, (lo, hi)) for b in betas])
        return cache[key]

    out_schemes: dict[str, Any] = {}
    for scheme in schemes:
        rows: list[dict[str, Any]] = []
        for delta in deltas:
            u1, u2, u4 = u_all(delta, scheme), u_all(delta / 2, scheme), u_all(delta / 4, scheme)
            err = np.abs(u1 - ref)
            d12, d24 = np.abs(u1 - u2), np.abs(u2 - u4)
            ok = (d12 > 1e-14) & (d24 > 1e-14)
            p = np.log2(d12[ok] / d24[ok])
            rows.append({
                "delta": float(delta),
                "nodes": nodes_for_delta(delta, lo, hi),
                "max_abs_err": float(err.max()),
                "rms_err": float(math.sqrt(float(np.mean(err * err)))),
                "p_brief_median": float(np.median(p)) if p.size else float("nan"),
                "p_brief_q1": float(np.quantile(p, 0.25)) if p.size else float("nan"),
                "p_brief_q3": float(np.quantile(p, 0.75)) if p.size else float("nan"),
                "p_brief_n": int(p.size),
            })
        fit_d = [fit_base_delta / 2**k for k in range(fit_levels)]
        fit_e = []
        for delta in fit_d:
            e = u_all(delta, scheme) - ref
            fit_e.append(float(math.sqrt(float(np.mean(e * e)))))
        mask = [e > 0.0 for e in fit_e]
        x = np.log([d for d, m in zip(fit_d, mask, strict=True) if m])
        y = np.log([e for e, m in zip(fit_e, mask, strict=True) if m])
        slope = float(np.polyfit(x, y, 1)[0]) if x.size >= 2 else float("nan")
        crit = float(np.max(np.abs(u_all(0.01, scheme) - u_all(0.001, scheme))))
        out_schemes[scheme] = {"rows": rows, "fitted_order": slope, "fit_deltas": fit_d,
                               "fit_rms_err": fit_e, "criterion_max_diff": crit}
    return {"reference": ref_kind, "n_inputs": len(betas), "deltas": [float(d) for d in deltas],
            "schemes": out_schemes}
