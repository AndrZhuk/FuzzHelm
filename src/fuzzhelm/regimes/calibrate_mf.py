"""Калібрування функцій належності з даних: T — перцентилі, V — центроїди KMeans(k=3).

Найменування: regimes/calibrate_mf.py
Призначення: бібліотека для scripts/calibrate_mf.py (хвиля 2: БД → конвеєр → детектори → консенсус T).
             Тут — лише чисті обчислення над МАСИВАМИ і запис membership.yaml.
Автор: Андрій Жук, 2026.

Брифінг §5.5:
  T: точки зламу = перцентилі {8, 25, 50, 75, 92} емпіричного розподілу T на IS-вікні,
     STRONG_DOWN = trap(−1, −1, p8, p25), WEAK_DOWN = tri(p8, p25, p50), NEUTRAL = tri(p25, p50, p75),
     WEAK_UP = tri(p50, p75, p92), STRONG_UP = trap(p75, p92, 1, 1) — сусідні терми мають спільні
     відрізки, тож умова Руспіні Σμ = 1 виконується для будь-яких строго зростаючих точок;
     `t_symmetric=True` (хвиля 2, DATA-06) — ті самі перцентилі симетризованої вибірки T ∪ −T, щоб
     непарна симетрія u(−T, −R, V) = −u(T, R, V) бази правил не ламалась дрейфом IS-вікна;
  V: центри трьох гаусіан = відсортовані координати «перцентиль волатильності» центроїдів
     KMeans(k=3) на векторі (vol_pct, нормований нахил EMA, z-score обсягу);
     σ_i — за правилом `sigma_rule`:
       "nearest" (буквально §5.5, за замовчуванням): σ_i = 0.5·відстань до НАЙБЛИЖЧОГО сусіднього центру;
       "cover" (хвиля 2, docs/deviations.d/data.md DATA-05): σ_i = 0.5·max(d⁻_i, d⁺_i) над центрами,
         доповненими дзеркальними «привидами» 2·lo − m_1 і 2·hi − m_k. Гарантує покриття без мертвих зон:
         max_i μ_i(x) ≥ e^{−1/2} ≈ 0.6065 на всьому [lo, hi] (доведення — у `v_sigmas_cover`). На реальному
         IS-вікні BTCUSDT правило "nearest" дає max μ = 0.10 при V = 1 (крайні центри далеко від меж).
k для звіту обирається за силуетом (k ∈ 2..6); якщо силует віддає перевагу k ≠ 3, це чесно
повідомляється (`k_best`), але V все одно має три терми (LO/MID/HI) з k = 3.
"""

from __future__ import annotations

import json
import math
import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import numpy as np
import numpy.typing as npt
import yaml

from fuzzhelm.features.pipeline import REL_FLOOR, Features
from fuzzhelm.regimes.cluster import fit_regimes

T_PERCENTILES: tuple[int, ...] = (8, 25, 50, 75, 92)
T_TERMS: tuple[str, ...] = ("STRONG_DOWN", "WEAK_DOWN", "NEUTRAL", "WEAK_UP", "STRONG_UP")
V_TERMS: tuple[str, ...] = ("LO", "MID", "HI")
V_K = 3
FEATURE_COLUMNS: tuple[str, ...] = ("vol_pct", "ema_slope_norm", "volume_z")


def features_to_arrays(feats: Sequence[Features], horizon: int = 5
                       ) -> tuple[npt.NDArray[np.float64], npt.NDArray[np.float64], npt.NDArray[np.float64]]:
    """Features конвеєра → (vol_pct, нормований нахил EMA g_t, z-score обсягу); непрогріте → NaN.

    g_t = (e_t − e_{t−h})/(h·max(ATR_t, REL_FLOOR·|e_t|)) — та сама величина (з тією самою підлогою
    знаменника), що й EmaSlope.features["g"]; `horizon` має дорівнювати FeatureParams.ema_horizon.
    """
    n = len(feats)
    vol = np.full(n, np.nan)
    slope = np.full(n, np.nan)
    vz = np.full(n, np.nan)
    for i, f in enumerate(feats):
        if f.vol_rank is not None:
            vol[i] = f.vol_rank
        if f.ema is not None and f.ema_lag is not None and f.atr is not None:
            den = horizon * max(f.atr, REL_FLOOR * abs(f.ema))
            if den > 0.0:
                slope[i] = (f.ema - f.ema_lag) / den
        if f.vol_z is not None:
            vz[i] = f.vol_z
    return vol, slope, vz


def t_terms(breakpoints: Sequence[float]) -> dict[str, dict[str, Any]]:
    p8, p25, p50, p75, p92 = (float(x) for x in breakpoints)
    return {
        "STRONG_DOWN": {"type": "trap", "points": [-1.0, -1.0, p8, p25]},
        "WEAK_DOWN": {"type": "tri", "points": [p8, p25, p50]},
        "NEUTRAL": {"type": "tri", "points": [p25, p50, p75]},
        "WEAK_UP": {"type": "tri", "points": [p50, p75, p92]},
        "STRONG_UP": {"type": "trap", "points": [p75, p92, 1.0, 1.0]},
    }


def v_sigmas(centres: Sequence[float]) -> list[float]:
    """σ_i = 0.5·min(|m_i − m_{i−1}|, |m_{i+1} − m_i|) для відсортованих центрів."""
    m = [float(x) for x in centres]
    out: list[float] = []
    for i in range(len(m)):
        gaps = []
        if i > 0:
            gaps.append(m[i] - m[i - 1])
        if i + 1 < len(m):
            gaps.append(m[i + 1] - m[i])
        out.append(0.5 * min(gaps))
    return out


SIGMA_RULES: tuple[str, ...] = ("nearest", "cover")


def v_sigmas_cover(centres: Sequence[float], lo: float = 0.0, hi: float = 1.0) -> list[float]:
    """σ_i = 0.5·max(d⁻_i, d⁺_i) для відсортованих центрів у [lo, hi], доповнених дзеркальними привидами
    g_0 = 2·lo − m_1 і g_{k+1} = 2·hi − m_k (межа діапазону відбиває крайній центр).

    Теорема (покриття без мертвих зон): max_i μ_i(x) ≥ e^{−1/2} для всіх x ∈ [lo, hi].
    Доведення: x лежить між сусідніми точками c_j ≤ x ≤ c_{j+1} розширеної послідовності (d = c_{j+1} − c_j).
    Якщо обидві — справжні центри, ближчий із них віддалений від x не більш ніж на d/2, а його σ ≥ d/2
    (σ — половина БІЛЬШОГО з двох суміжних проміжків, один із яких — d), тож μ ≥ exp(−(d/2)²/(2(d/2)²)) =
    e^{−1/2}. Якщо c_j = g_0 (x ∈ [lo, m_1], бо середина [g_0, m_1] — це lo), ближча справжня точка — m_1 на
    відстані ≤ m_1 − lo = d/2, і σ_1 ≥ d/2 — те саме; правий край симетрично. ∎
    """
    m = sorted(float(x) for x in centres)
    if not m:
        return []
    ext = [2.0 * lo - m[0], *m, 2.0 * hi - m[-1]]
    return [0.5 * max(ext[i] - ext[i - 1], ext[i + 1] - ext[i]) for i in range(1, len(ext) - 1)]


def coverage_min(centres: Sequence[float], sigmas: Sequence[float], lo: float = 0.0, hi: float = 1.0,
                 n: int = 2001) -> tuple[float, float]:
    """(min_x max_i μ_i(x), x у точці мінімуму) на рівномірній сітці n точок — перевірка «мертвих зон»."""
    x = np.linspace(lo, hi, n)
    m = np.asarray(centres, dtype=np.float64)[:, None]
    s = np.asarray(sigmas, dtype=np.float64)[:, None]
    cover = np.exp(-((x[None, :] - m) ** 2) / (2.0 * s * s)).max(axis=0)
    i = int(np.argmin(cover))
    return float(cover[i]), float(x[i])


def t_symmetric_breakpoints(t: npt.ArrayLike) -> list[float]:
    """Перцентилі {8,25,50,75,92} СИМЕТРИЗОВАНОГО розподілу T (вибірка T ∪ −T), точно антисиметричні.

    Для S = T ∪ (−T): F_S(x) = ½ + ½·F_|T|(x) при x ≥ 0, тож q-й перцентиль S (q > 50) — це (2q − 100)-й
    перцентиль |T|: p75 → медіана |T|, p92 → 84-й перцентиль |T|, p50 = 0. Рахуємо через |T|, щоб
    b(−q) = −b(q) виконувалося побітово (база правил і R — непарно-симетричні, DATA-06).
    """
    a = np.abs(np.asarray(t, dtype=np.float64).ravel())
    a50, a84 = (float(x) for x in np.percentile(a, [2 * 75 - 100, 2 * 92 - 100]))
    return [-a84, -a50, 0.0, a50, a84]


def _enforce_increasing(bp: list[float], gap: float) -> list[float]:
    """Строго зростаючі точки в (−1, 1) з мінімальним кроком gap (прямий + зворотний прохід)."""
    lo, hi = -1.0 + gap, 1.0 - gap
    x = [min(hi, max(lo, v)) for v in bp]
    for i in range(1, len(x)):
        x[i] = max(x[i], x[i - 1] + gap)
    if x[-1] > hi:
        x[-1] = hi
        for i in range(len(x) - 2, -1, -1):
            x[i] = min(x[i], x[i + 1] - gap)
    return x


def calibrate(vol_pct: npt.ArrayLike, ema_slope_norm: npt.ArrayLike, volume_z: npt.ArrayLike,
              t_series: npt.ArrayLike, *, seed: int, k_range: range | tuple[int, ...] = range(2, 7),
              n_init: int = 10, run_id: str | None = None, silhouette_sample: int | None = 10_000,
              min_gap: float = 1e-3, min_sigma: float = 1e-3, sigma_rule: str = "nearest",
              t_symmetric: bool = False) -> dict[str, Any]:
    """Каліброване T/V + силует для k ∈ k_range. Рядки з NaN (прогрів) відкидаються.

    Повертає JSON-сумісний dict: {"T": {...}, "V": {...}, "silhouette_by_k", "k_best", ...};
    `T.terms` / `V.terms` мають формат секцій membership.yaml.
    """
    if sigma_rule not in SIGMA_RULES:
        raise ValueError(f"sigma_rule must be one of {SIGMA_RULES}, got {sigma_rule!r}")
    v = np.asarray(vol_pct, dtype=np.float64).ravel()
    g = np.asarray(ema_slope_norm, dtype=np.float64).ravel()
    z = np.asarray(volume_z, dtype=np.float64).ravel()
    if not (v.shape == g.shape == z.shape):
        raise ValueError(f"feature arrays differ in length: {v.shape}, {g.shape}, {z.shape}")
    X = np.column_stack([v, g, z])
    mask = np.all(np.isfinite(X), axis=1)
    X = X[mask]
    warnings: list[str] = []
    if X.shape[0] < 10 * max(k_range):
        warnings.append(f"only {X.shape[0]} finite feature rows for k up to {max(k_range)}")

    ks = sorted(set(k_range) | {V_K})
    cr = fit_regimes(X, k_range=tuple(ks), seed=seed, n_init=n_init, silhouette_sample=silhouette_sample)
    c3 = cr.centroids_by_k[V_K]
    order = np.argsort(c3[:, 0], kind="stable")
    c3 = c3[order]
    centres = [float(x) for x in c3[:, 0]]
    sig_nearest = v_sigmas(centres)
    sig_raw = sig_nearest if sigma_rule == "nearest" else v_sigmas_cover(centres)
    sigmas = [max(min_sigma, s) for s in sig_raw]
    if any(s < min_sigma for s in sig_raw):
        warnings.append(f"V centres nearly coincide (σ_raw={sig_raw}); σ floored at {min_sigma}")
    cov, cov_x = coverage_min(centres, sigmas)
    cov_nearest, cov_nearest_x = coverage_min(centres, [max(min_sigma, s) for s in sig_nearest])
    if cov < 0.5:
        warnings.append(f"V terms leave a dead zone: max mu = {cov:.4f} < 0.5 at V = {cov_x:.4f} "
                        f"(sigma_rule={sigma_rule})")
    sil_in_range = {int(k): float(cr.silhouette_by_k[k]) for k in sorted(set(k_range))}
    valid = {k: s for k, s in sil_in_range.items() if not math.isnan(s)}
    k_best = max(valid, key=lambda k: (valid[k], -k)) if valid else V_K
    if k_best != V_K:
        warnings.append(f"silhouette prefers k={k_best} over k={V_K}; V still uses k={V_K} (3 terms)")

    t = np.asarray(t_series, dtype=np.float64).ravel()
    t = t[np.isfinite(t)]
    if t.size == 0:
        raise ValueError("t_series has no finite values")
    raw_bp = [float(x) for x in np.percentile(t, T_PERCENTILES)]
    src_bp = t_symmetric_breakpoints(t) if t_symmetric else raw_bp
    bp = _enforce_increasing(src_bp, min_gap)
    if bp != src_bp:
        warnings.append(f"T percentiles {src_bp} adjusted to strictly increasing {bp} (min_gap={min_gap})")

    return {
        "T": {
            "percentiles": list(T_PERCENTILES),
            "raw_breakpoints": raw_bp,
            "symmetric": t_symmetric,
            "breakpoints": bp,
            "n": int(t.size),
            "terms": t_terms(bp),
        },
        "V": {
            "k_used": V_K,
            "centres": centres,
            "sigmas": sigmas,
            "silhouette": float(cr.silhouette_by_k[V_K]),
            "sigma_rule": sigma_rule,
            "sigmas_nearest": [max(min_sigma, s) for s in sig_nearest],
            "coverage_min": cov, "coverage_min_at": cov_x,
            "coverage_min_nearest": cov_nearest, "coverage_min_nearest_at": cov_nearest_x,
            "terms": {name: {"type": "gauss", "m": m, "sigma": s}
                      for name, m, s in zip(V_TERMS, centres, sigmas, strict=True)},
        },
        "centroids_k3": [[float(x) for x in row] for row in c3],
        "feature_columns": list(FEATURE_COLUMNS),
        "silhouette_by_k": sil_in_range,
        "inertia_by_k": {int(k): float(cr.inertia_by_k[k]) for k in sorted(set(k_range))},
        "k_best": int(k_best),
        "k_best_is_3": k_best == V_K,
        "n_samples": int(X.shape[0]),
        "seed": seed,
        "source_run_id": run_id,
        "warnings": warnings,
    }


# ------------------------------------------------------------------ запис membership.yaml

_DEFAULT_R = {
    "SELL_PRESSURE": {"type": "trap", "points": [-1.0, -1.0, -0.45, 0.0]},
    "NO_PRESSURE": {"type": "tri", "points": [-0.45, 0.0, 0.45]},
    "BUY_PRESSURE": {"type": "trap", "points": [0.0, 0.45, 1.0, 1.0]},
}
_DEFAULT_U = {
    "STRONG_SHORT": {"type": "trap", "points": [-1.0, -1.0, -0.80, -0.45]},
    "SHORT": {"type": "tri", "points": [-0.80, -0.40, 0.0]},
    "HOLD": {"type": "tri", "points": [-0.40, 0.0, 0.40]},
    "LONG": {"type": "tri", "points": [0.0, 0.40, 0.80]},
    "STRONG_LONG": {"type": "trap", "points": [0.45, 0.80, 1.0, 1.0]},
}


def _num(x: Any) -> str:
    if isinstance(x, bool) or x is None:
        return "null" if x is None else ("true" if x else "false")
    if isinstance(x, int):
        return str(x)
    f = float(x)
    if not math.isfinite(f):
        raise ValueError(f"non-finite number {f} cannot go into membership.yaml")
    s = f"{f:.6f}".rstrip("0")
    s = s + "0" if s.endswith(".") else s
    return "0.0" if s in ("-0.0", "-0") else s


def _scalar(x: Any) -> str:
    if isinstance(x, str):
        # голий рядок лише якщо YAML прочитає його назад тим самим рядком ("yes"/"null"/"123" — ні)
        bare = x.replace("_", "").isalnum() and yaml.safe_load(x) == x
        return x if bare else json.dumps(x, ensure_ascii=False)
    if isinstance(x, list | tuple):
        return "[" + ", ".join(_scalar(v) for v in x) + "]"
    return _num(x)


def _flow(d: Mapping[str, Any]) -> str:
    return "{" + ", ".join(f"{k}: {_scalar(v)}" for k, v in d.items()) + "}"


def _terms_block(terms: Mapping[str, Mapping[str, Any]], indent: str = "      ") -> list[str]:
    width = max(len(k) for k in terms) + 1
    return [f"{indent}{(name + ':').ljust(width + 1)}{_flow(spec)}" for name, spec in terms.items()]


def _run_id(x: str | None) -> str:
    return "null" if x is None else json.dumps(str(x), ensure_ascii=False)


def _comment(text: str) -> str:
    """Один рядок YAML-коментаря: перенос рядка в run_id/попередженні не має «вийти» з коментаря в дані."""
    return "# " + " ".join(str(text).splitlines())


def _sigma_rule_text(V: Mapping[str, Any]) -> str:
    if V.get("sigma_rule", "nearest") == "nearest":
        return "σ_i = 0.5·d до найближчого центру"
    return ("σ_i = 0.5·max(d⁻, d⁺) з дзеркальними привидами на межах [0, 1] (правило cover: покриття ≥ e^−½; "
            f"буквальне §5.5 «до найближчого» дало б σ = {_scalar(V.get('sigmas_nearest', []))} і "
            f"max μ = {_num(V.get('coverage_min_nearest', float('nan')))} при V = "
            f"{_num(V.get('coverage_min_nearest_at', float('nan')))})")


def write_membership_yaml(result: Mapping[str, Any], path: str | Path, *,
                          source_run_id: str | None = None) -> Path:
    """Переписати membership.yaml за шаблоном: T/V — з `result`, R/U/defuzz — з наявного файлу.

    Структура і ключі — ті самі, що в config/membership.yaml (схему читає fuzzy.membership);
    додаткова діагностика калібрування (силует за k, k_best, сирі перцентилі) іде в коментарі.
    V.provisional := false, T/V.source_run_id := run_id. Запис атомарний (tmp + os.replace).
    """
    p = Path(path)
    existing: dict[str, Any] = {}
    if p.exists():
        loaded = yaml.safe_load(p.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            existing = loaded
    ev: Mapping[str, Any] = existing.get("variables", {}) or {}
    r_terms = (ev.get("R") or {}).get("terms") or _DEFAULT_R
    u_terms = (ev.get("U") or {}).get("terms") or _DEFAULT_U
    defuzz = existing.get("defuzz") or {"scheme": "trapezoid", "grid_nodes": 201}
    version = existing.get("version", 3)
    run_id = source_run_id if source_run_id is not None else result.get("source_run_id")

    T = result["T"]
    V = result["V"]
    sil = result.get("silhouette_by_k", {})
    # силует невизначений (NaN) для виродженого k — лише в коментарі, не в даних
    sil_txt = ", ".join(f"{k}: {_num(s) if math.isfinite(float(s)) else 'n/a'}"
                        for k, s in sorted(sil.items()))
    lines = [
        "# Функції належності лінгвістичних змінних нечіткого ядра (брифінг §5.5).",
        "# Кожне число має джерело: percentile | kmeans | expert.",
        _comment(f"Згенеровано regimes/calibrate_mf.write_membership_yaml: run_id={run_id}, "
                 f"seed={result.get('seed')}, n_samples={result.get('n_samples')}, n_T={T.get('n')}."),
        f"# T: перцентилі {{8,25,50,75,92}} емпіричного T = {_scalar(T.get('raw_breakpoints', []))}"
        + (f"; записано симетризовані (T ∪ −T) = {_scalar(T['breakpoints'])}" if T.get("symmetric") else ""),
        f"# V: KMeans(k=3) центри (vol_pct) = {_scalar(V['centres'])}; {_sigma_rule_text(V)}",
        f"# Силует за k: {sil_txt}; найкращий k за силуетом = {result.get('k_best')} "
        f"(для V використано k = {V.get('k_used', 3)}).",
    ]
    for w in result.get("warnings", []) or []:
        lines.append(_comment(f"УВАГА: {w}"))
    lines += [
        f"version: {_num(version)}",
        "variables:",
        "  T:",
        "    range: [-1.0, 1.0]",
        "    source: percentile          # точки зламу = перцентилі {8,25,50,75,92} емпіричного T"
        " на IS-вікні",
        f"    source_run_id: {_run_id(run_id)}",
        "    terms:",
        *_terms_block(T["terms"]),
        "  R:",
        "    range: [-1.0, 1.0]",
        "    source: expert",
        "    terms:",
        *_terms_block(r_terms),
        "  V:",
        "    range: [0.0, 1.0]",
        "    source: kmeans              # центри = центроїди KMeans(k=3); σ_i = 0.5·d(m_i, m_{i±1})"
        + ("" if V.get("sigma_rule", "nearest") == "nearest" else " — правило cover (див. коментар угорі)"),
        "    provisional: false          # записано калібруванням (regimes/calibrate_mf.py)",
        f"    source_run_id: {_run_id(run_id)}",
        f"    silhouette: {_num(V['silhouette'])}",
        "    terms:",
        *_terms_block(V["terms"]),
        "  U:",
        "    range: [-1.0, 1.0]",
        "    terms:",
        *_terms_block(u_terms),
        "defuzz:",
        f"  scheme: {_scalar(defuzz.get('scheme', 'trapezoid'))}             # rect | trapezoid | simpson",
        f"  grid_nodes: {_num(int(defuzz.get('grid_nodes', 201)))}",
    ]
    text = "\n".join(lines) + "\n"
    parsed = yaml.safe_load(text)  # самоперевірка: записуємо лише валідний YAML
    if not isinstance(parsed, dict) or parsed["variables"]["V"]["provisional"] is not False:
        raise RuntimeError("generated membership.yaml failed self-check")
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, p)
    return p
