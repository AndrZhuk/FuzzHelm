"""Пошукові експерименти фази 7: walk-forward з вибором з фронту Парето, сітка, PSR/DSR, Амдал, чутливість.

Найменування: backtest/experiments_search.py
Призначення: інструментарій обчислювального експерименту (брифінг §5.15, §5.16, §12 фаза 7, §14 п. 2.11):
    * правило вибору робочої точки з недомінованого фронту (SR↑, MaxDD↓, Turnover↓): ε-обмеження, не argmax;
    * конкатенація OOS-сегментів walk-forward у одну криву і її метрики (Sharpe, Sortino, MaxDD, Calmar, PSR);
    * DSR з ФАКТИЧНИМ числом оцінених клітинок N і вибірковою дисперсією їхніх Шарпів (Bailey–López de Prado);
    * підгонка послідовної частки f закону Амдала і розклад накладних витрат пулу процесів;
    * одновимірна (one-at-a-time) чутливість до 8 параметрів і таблиця «торнадо»;
    * чисті воркери ProcessPoolExecutor над numpy-масивами (без БД) і оркестрація для скриптів
      scripts/{run_walkforward,run_grid,bench_amdahl,sensitivity}.py.
Автор: Андрій Жук, 2026.

Запис у БД тут відсутній: скрипти пишуть паспорти через workers.persist. Єдина функція, що читає БД, —
load_dataset (ліниво, лише в головному процесі скрипта); воркери отримують масиви і конфіг як dict.
Числа статистики — float/numpy; Decimal-капітал перетворюється лише features.convert.to_float (межа типів).
"""

from __future__ import annotations

import argparse
import copy
import dataclasses
import math
import os
import shlex
import statistics
import subprocess
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from statistics import NormalDist
from typing import Any

import numpy as np
from numpy.typing import NDArray

from fuzzhelm.backtest.dataset import HASH_COLUMNS, Dataset
from fuzzhelm.backtest.engine import GRID_KEYS, BacktestConfig, run_backtest
from fuzzhelm.backtest.manifest import dataset_hash
from fuzzhelm.backtest.metrics import (
    EULER_GAMMA,
    compute_metrics,
    moments,
    psr,
    returns_from_equity,
    sharpe_ratio,
)
from fuzzhelm.backtest.parallel import amdahl_bound, amdahl_report, run_parallel
from fuzzhelm.backtest.pareto import pareto_front
from fuzzhelm.backtest.walkforward import Fold, make_folds
from fuzzhelm.features.convert import to_float

CRITERIA: tuple[str, str, str] = ("sharpe", "max_drawdown", "turnover")   # (SR↑, MaxDD↓, Turnover↓)
_N = NormalDist()

SELECTION_RULE = "eps_constraint_max_sr"
SELECTION_RULE_UK = (
    "ε-обмеження на фронті Парето (SR↑, MaxDD↓, Turnover↓): серед недомінованих клітинок з MaxDD ≤ dd_cap "
    "обирається клітинка з найбільшим Шарпом; рівність → менший оборот → менший індекс сітки. Якщо жодна "
    "клітинка фронту не вкладається в dd_cap — клітинка фронту з найменшою MaxDD (рівність → більший Шарп → "
    "менший оборот → менший індекс). dd_cap за замовчуванням = state_machine.cool_enter (просадка, з якої "
    "автомат ризику переходить у COOLDOWN): робоча точка, що вже на IS доходить до COOLDOWN, неприйнятна."
)

# метрики конкатенованої OOS-кривої (підмножина 17 метрик, що має сенс для ланцюга сегментів + PSR)
CONCAT_KEYS: tuple[str, ...] = (
    "total_return", "cagr", "ann_vol", "sharpe", "sortino", "max_drawdown", "calmar", "ulcer_index",
    "turnover", "n_trades", "sr_period", "skew", "kurt", "psr", "n_obs", "n_segments",
)
# поля результату сегмента, що не є числами-метриками (не пишуться в run_metric)
NON_METRIC_KEYS = frozenset({"equity", "equity_ts", "equity_hash", "config_hash", "pid", "final_state"})


def _nan_to(x: float, default: float) -> float:
    return default if math.isnan(x) else x


# ====================================================================== вибір робочої точки з фронту


@dataclass(frozen=True, slots=True)
class FrontChoice:
    """Робоча точка, обрана з фронту: індекс клітинки, сам фронт, допустима частина фронту, правило."""

    index: int
    front: tuple[int, ...]
    feasible: tuple[int, ...]          # клітинки фронту з MaxDD ≤ dd_cap
    dd_cap: float
    rule: str                          # "eps_constraint" | "fallback_min_dd"

    def to_dict(self) -> dict[str, Any]:
        return {"index": self.index, "front": list(self.front), "feasible": list(self.feasible),
                "dd_cap": self.dd_cap, "rule": self.rule}


def front_of(metrics: Sequence[Mapping[str, Any]], keys: Sequence[str] = CRITERIA) -> list[int]:
    """Недомінований фронт (SR↑, MaxDD↓, Turnover↓) у порядку клітинок; NaN — найгірше значення."""
    return pareto_front([tuple(m[k] + 0.0 for k in keys) for m in metrics])


def choose_from_front(metrics: Sequence[Mapping[str, Any]], *, dd_cap: float,
                      keys: Sequence[str] = CRITERIA) -> FrontChoice:
    """Правило вибору робочої точки (SELECTION_RULE_UK): метод ε-обмеження багатокритеріальної оптимізації.

    max SR за умови MaxDD ≤ dd_cap. Розв'язок цієї задачі завжди недомінований (точка, що його домінує, теж
    допустима і не гірша за SR), тож пошук серед фронту еквівалентний пошуку серед усіх клітинок.
    """
    if not metrics:
        raise ValueError("no cells to choose from")
    if not dd_cap > 0:
        raise ValueError("dd_cap must be > 0")
    k_sr, k_dd, k_to = keys
    front = front_of(metrics, keys)

    def sr(i: int) -> float:
        return _nan_to(metrics[i][k_sr] + 0.0, -math.inf)

    def dd(i: int) -> float:
        return _nan_to(metrics[i][k_dd] + 0.0, math.inf)

    def to(i: int) -> float:
        return _nan_to(metrics[i][k_to] + 0.0, math.inf)

    feasible = [i for i in front if dd(i) <= dd_cap]
    if feasible:
        best = min(feasible, key=lambda i: (-sr(i), to(i), i))
        rule = "eps_constraint"
    else:
        best = min(front, key=lambda i: (dd(i), -sr(i), to(i), i))
        rule = "fallback_min_dd"
    return FrontChoice(index=best, front=tuple(front), feasible=tuple(feasible), dd_cap=dd_cap, rule=rule)


def default_dd_cap(cfg: BacktestConfig) -> float:
    """dd_cap за замовчуванням = cool_enter автомата ризику (config/risk_limits.yaml)."""
    return to_float(cfg.risk_config().state_machine.cool_enter)


# ====================================================================== сітка і фолди


def subset_indices(n_total: int, n: int | None) -> list[int]:
    """n рівномірно розкиданих індексів з [0, n_total) (детерміновано; перший і останній — завжди)."""
    if n is None or n >= n_total:
        return list(range(n_total))
    if n < 1:
        raise ValueError("subset size must be >= 1")
    if n == 1:
        return [0]
    idx = np.linspace(0, n_total - 1, n).round().astype(np.int64)
    return sorted({i.item() for i in idx})


def smoke_folds(n_bars: int, *, embargo_bars: int, warmup: int, k: int = 2) -> list[Fold]:
    """Геометрія фолдів для швидкої перевірки на малому наборі: embargo — справжнє (2·max_lookback),
    OOS = крок = n/8, IS = решта; IS після вирізання embargo має бути довшим за прогрів."""
    oos = n_bars // 8
    is_bars = n_bars - oos * k
    if is_bars - embargo_bars <= warmup or oos < 2:
        raise ValueError(f"{n_bars} bars are too few for {k} smoke folds with embargo {embargo_bars} "
                         f"and warm-up {warmup}")
    return make_folds(n_bars, is_bars, oos, oos, embargo_bars, k)


def oos_segment(fold: Fold, warmup: int) -> tuple[int, int]:
    """(start, eval_start) OOS-прогону: прогрів W барів перед OOS без торгівлі — усередині embargo (W ≤ E)."""
    start = fold.oos_start - warmup
    if start < fold.is_end:
        raise ValueError(f"fold {fold.index}: embargo {fold.embargo_bars} bars is shorter than the warm-up "
                         f"{warmup} bars — OOS warm-up would read IS bars")
    return start, warmup


# ====================================================================== конкатенація OOS


def chain_equity(curves: Sequence[NDArray[np.float64]],
                 e0: float | None = None) -> tuple[NDArray[np.float64], list[float]]:
    """Ланцюг сегментів: прості доходності кожного сегмента поспіль, від e0 (дефолт — перша точка першого).

    Кожен OOS-прогін стартує зі свіжого капіталу, тож склеюються ДОХОДНОСТІ, а не рівні: крива має
    1 + Σ(len_k − 1) точок. Повертає також капітал ланцюга на початку кожного сегмента (C_k) — множник
    для грошових величин сегмента (номінал угод) у масштабі ланцюга.
    """
    if not curves:
        raise ValueError("no segments to chain")
    rs = [returns_from_equity(np.asarray(c, dtype=np.float64)) for c in curves]
    start = np.asarray(curves[0], dtype=np.float64)[0].item() if e0 is None else e0 + 0.0
    growth = np.concatenate([np.ones(1), np.cumprod(1.0 + np.concatenate(rs))])
    eq = start * growth
    starts: list[float] = []
    pos = 0
    for r in rs:
        starts.append(eq[pos].item())
        pos += r.size
    return eq, starts


def concat_oos_metrics(segments: Sequence[Mapping[str, Any]], periods_per_year: float) -> dict[str, float]:
    """Метрики конкатенованої OOS-кривої. Сегмент: {"equity": float[], "traded_notional", "n_trades"}.

    Оборот ланцюга: Σ_k N_k·C_k/E_k(0) / mean(E) · P/n — номінал сегмента k перемасштабовано з його стартового
    капіталу E_k(0) на капітал ланцюга C_k. Угодні метрики (PF, expectancy, win rate) не рахуються: угоди
    сегментів мають різний масштаб капіталу. PSR — з γ₃/γ₄ конкатенованих доходностей (SR* = 0).
    """
    curves = [np.asarray(s["equity"], dtype=np.float64) for s in segments]
    eq, starts = chain_equity(curves)
    notional = math.fsum(s["traded_notional"] * c0 / cur[0].item()
                         for s, c0, cur in zip(segments, starts, curves, strict=True))
    m = compute_metrics(eq, [], periods_per_year, traded_notional=notional)
    r = returns_from_equity(eq)
    g3, g4 = moments(r)
    sr = sharpe_ratio(r, 1.0)
    try:
        p = psr(sr, int(r.size), g3, g4) if r.size >= 2 else math.nan
    except ValueError:
        p = math.nan
    out = {k: m[k] for k in CONCAT_KEYS if k in m}
    out.update(n_trades=math.fsum(s["n_trades"] for s in segments), sr_period=sr, skew=g3, kurt=g4, psr=p,
               n_obs=r.size + 0.0, n_segments=len(segments) + 0.0)
    return {k: out[k] for k in CONCAT_KEYS}


# ====================================================================== PSR / DSR з фактичним N


def sr0_expected_max(trial_srs: Sequence[float], n_trials: int | None = None) -> float:
    """SR₀ = √Var(SR_i)·[(1−γ_E)·Φ⁻¹(1−1/N) + γ_E·Φ⁻¹(1−1/(N·e))] (§5.15).

    N — фактичне число оцінених клітинок (дефолт len(trial_srs)); Var — вибіркова (ddof = 1) по СКІНЧЕННИХ
    Шарпах (NaN/inf відкидаються з дисперсії, але не з N: випробування відбулось). N < 2 → 0.
    """
    s = np.asarray(trial_srs, dtype=np.float64)
    fin = s[np.isfinite(s)]
    n = int(s.size) if n_trials is None else n_trials
    if n < 2 or fin.size < 2:
        return 0.0
    sd = math.sqrt(np.var(fin, ddof=1).item())
    return sd * ((1.0 - EULER_GAMMA) * _N.inv_cdf(1.0 - 1.0 / n)
                 + EULER_GAMMA * _N.inv_cdf(1.0 - 1.0 / (n * math.e)))


def deflated_sharpe_report(sr: float, n_obs: int, skew: float, kurt: float,
                           trial_srs: Sequence[float], *, threshold: float = 0.95) -> dict[str, Any]:
    """PSR (SR* = 0) і DSR (SR* = SR₀ за N = len(trial_srs)) для обраної точки; Шарпи — ЗА ПЕРІОД.

    `significant` — чи PSR ≥ threshold; `dsr_significant` — чи DSR ≥ threshold. Невизначений PSR/DSR
    (нескінченний ŜR, від'ємний підкореневий вираз) → NaN і significant = False.
    """
    s = np.asarray(trial_srs, dtype=np.float64)
    fin = s[np.isfinite(s)]
    var = np.var(fin, ddof=1).item() if fin.size >= 2 else math.nan
    sr0 = sr0_expected_max(trial_srs)

    def safe(star: float) -> float:
        try:
            return psr(sr, n_obs, skew, kurt, sr_star=star)
        except ValueError:
            return math.nan

    p, d = safe(0.0), safe(sr0)
    return {
        "n_trials": int(s.size), "n_finite": int(fin.size), "var_sr": var,
        "sd_sr": math.sqrt(var) if math.isfinite(var) else math.nan, "sr0": sr0, "sr_selected": sr,
        "n_obs": n_obs, "skew": skew, "kurt": kurt, "psr": p, "dsr": d, "threshold": threshold,
        "significant": bool(p >= threshold), "dsr_significant": bool(d >= threshold),
        "units": "Sharpe per period (1 bar), not annualised",
    }


def significance_statement_uk(rep: Mapping[str, Any], *, what: str) -> str:
    """Пряме текстове твердження для звіту (§5.15: якщо PSR < 0.95 — перевага статистично не встановлена)."""
    thr = rep["threshold"]
    p, d = rep["psr"], rep["dsr"]
    if not rep["significant"]:
        return (f"{what}: PSR = {p:.4g} < {thr} — перевага над нульовим Шарпом статистично НЕ встановлена; "
                f"DSR = {d:.4g} при N = {rep['n_trials']} (SR₀ = {rep['sr0']:.4g} за період).")
    if not rep["dsr_significant"]:
        return (f"{what}: PSR = {p:.4g} ≥ {thr}, але DSR = {d:.4g} < {thr} при N = {rep['n_trials']} — "
                f"з поправкою на множинне тестування перевага статистично НЕ встановлена.")
    return f"{what}: PSR = {p:.4g} і DSR = {d:.4g} ≥ {thr} при N = {rep['n_trials']}."


def psr_statement_uk(p: float, *, what: str, threshold: float = 0.95) -> str:
    """Твердження лише про PSR (SR* = 0) — де множинного вибору немає (конкатенований OOS walk-forward)."""
    if math.isnan(p):
        return f"{what}: PSR не визначений (нульова дисперсія доходностей), перевага НЕ встановлена."
    if p < threshold:
        return f"{what}: PSR = {p:.4g} < {threshold}: перевага над SR = 0 статистично НЕ встановлена."
    return f"{what}: PSR = {p:.4g} ≥ {threshold}."


# ====================================================================== закон Амдала


def amdahl_fit(times: Mapping[int, float]) -> dict[str, Any]:
    """T(p) → S(p) = T(1)/T(p), f за НК на самих S (parallel.fit_serial_fraction), межа, Карп–Флатт."""
    rep = amdahl_report(times)
    ps = list(rep.workers)
    sse = math.fsum((rep.speedup[p] - rep.bound[p]) ** 2 for p in ps)
    f = rep.serial_fraction
    return {
        "workers": ps, "times": {p: rep.times[p] for p in ps}, "speedup": {p: rep.speedup[p] for p in ps},
        "efficiency": {p: rep.speedup[p] / p for p in ps}, "serial_fraction": f,
        "bound": {p: rep.bound[p] for p in ps}, "karp_flatt": dict(rep.karp_flatt), "sse": sse,
        "limit_speedup": math.inf if f == 0 else 1.0 / f,
    }


def amdahl_curve(f: float, ps: Sequence[float]) -> list[float]:
    return [amdahl_bound(p, f) for p in ps]


def overhead_breakdown(*, p: int, wall_s: float, startup_s: float, task_walls: Sequence[float],
                       task_pids: Sequence[int], pickle_s_per_task: float,
                       compute_s_p1: float | None = None) -> dict[str, float]:
    """Розклад часу прогону з p воркерами на виміряні складові (усе в секундах).

    compute — Σ часу задач, виміряного в самих воркерах; ideal = compute/p; makespan — найбільша сумарна
    зайнятість одного процесу (групування за pid); imbalance = makespan − ideal (хвіст розкладу і різна
    швидкість ядер); pickle_serial = n_задач · час pickle.dumps задачі в батьківському процесі (серіалізація
    йде в одному потоці-годувальнику пулу); residual = wall − startup − makespan (IPC, передача результатів,
    планування, частина pickle, не перекрита обчисленнями). cpu_inflation = compute(p)/compute(1) — наскільки
    та сама робота дорожчає при p одночасних процесах (спільні кеші/пам'ять, повільніші E-ядра, частота).
    """
    if len(task_walls) != len(task_pids):
        raise ValueError("task_walls and task_pids must have the same length")
    compute = math.fsum(task_walls)
    busy: dict[int, float] = {}
    for w, pid in zip(task_walls, task_pids, strict=True):
        busy[pid] = busy.get(pid, 0.0) + w
    makespan = max(busy.values()) if busy else 0.0
    ideal = compute / p
    return {
        "p": p + 0.0, "wall_s": wall_s, "startup_s": startup_s, "compute_s": compute,
        "ideal_parallel_s": ideal,
        "makespan_s": makespan, "imbalance_s": makespan - ideal,
        "pickle_serial_s": pickle_s_per_task * len(task_walls),
        "residual_s": wall_s - startup_s - makespan, "processes_used": len(busy) + 0.0,
        "cpu_inflation": math.nan if not compute_s_p1 else compute / compute_s_p1,
    }


# ====================================================================== чутливість (one-at-a-time)


@dataclass(frozen=True, slots=True)
class SensParam:
    """Параметр чутливості: ім'я, позначення, де живе, базове значення і рівні варіації."""

    name: str
    symbol: str
    label_uk: str
    where: str                  # поле BacktestConfig або шлях у конфігураційних деревах
    base: float
    values: tuple[float, ...]   # рівні варіації (без базового), за зростанням
    span: float                 # відносна амплітуда ± (для λ — амплітуда пам'яті 1/(1−λ))
    rationale: str


# Амплітуди ±20–50 % (для λ — пам'яті EWMA). Обґрунтування — поле rationale і docs/api/exp_search.md.
SENS_SPECS: tuple[tuple[str, str, str, str, float, str], ...] = (
    ("u_enter", "u_enter", "поріг входу гістерезису", "risk_limits.hysteresis.enter", 0.20,
     "поріг тригера Шмітта визначає частоту входів; ±20 % тримає u_exit < u_enter і відповідає осі сітки"),
    ("u_exit", "u_exit", "поріг виходу гістерезису", "risk_limits.hysteresis.exit", 0.25,
     "ширина петлі гістерезису (ціна чатерингу, §5.9); ±25 % лишає u_exit < u_enter"),
    ("chi", "χ_ATR", "множник ATR стопу", "risk_limits.sizing.chi_atr", 0.25,
     "відстань стопу Δ = χ·ATR і q_atr ∝ 1/χ; ±25 % = вісь сітки {1.5, 2.5}"),
    ("rho_base", "ρ_base", "ризик на угоду", "risk_limits.sizing.rho_base", 0.50,
     "масштаб позиції q_atr ∝ ρ; ±50 % = вісь сітки знизу (0.0025) і симетрично зверху"),
    ("lam", "λ_EWMA", "λ EWMA волатильності", "risk_limits.sizing.ewma_lambda", 0.50,
     "λ не масштабний параметр: варіюється пам'ять N = 1/(1−λ) на ±50 % (λ ∈ (0, 1) зберігається)"),
    ("sigma_target", "σ_target", "цільова волатильність", "risk_limits.sizing.sigma_target", 0.50,
     "рівень таргетування волатильності s* = σ_target/σ̂ (§5.8); експертне число, ±50 %"),
    ("kappa_min", "κ_min", "нижня межа κ", "detectors.agreement.kappa_min", 0.40,
     "підлога коефіцієнта довіри κ = κ_min + (1−κ_min)·A^ν (§5.4); ±40 % лишає κ_min ∈ (0, 1)"),
    ("n_atr", "n_ATR", "період ATR", "detectors.features.n_atr", 0.50,
     "період ATR Уайлдера; ±50 % = вісь сітки {7, 21} (цілі значення)"),
)
SENS_NAMES: tuple[str, ...] = tuple(s[0] for s in SENS_SPECS)
DEFAULT_LEVELS: tuple[float, ...] = (-1.0, -0.5, 0.5, 1.0)     # частки амплітуди


def base_param_values(cfg: BacktestConfig) -> dict[str, float]:
    """Базові значення 8 параметрів з конфігурації (дерева вже перекриті полями BacktestConfig)."""
    trees = cfg.resolved_trees()
    sizing = trees["risk_limits"].get("sizing") or {}
    agree = trees["detectors"].get("agreement") or {}
    return {
        "u_enter": _num(cfg.u_enter), "u_exit": _num(cfg.u_exit), "chi": _num(cfg.chi),
        "rho_base": _num(cfg.rho_base), "lam": _num(cfg.lam),
        "sigma_target": _num(sizing.get("sigma_target", 0.20)),
        "kappa_min": _num(agree.get("kappa_min", 0.35)),
        "n_atr": _num(cfg.n_atr),
    }


def _num(x: Any) -> float:
    if x is None:
        raise ValueError("parameter is not resolved")
    out: float = x + 0.0
    return out


def _level_value(name: str, base: float, span: float, level: float) -> float:
    if name == "lam":
        mem = 1.0 / (1.0 - base)
        return round(1.0 - 1.0 / (mem * (1.0 + level * span)), 4)
    if name == "n_atr":
        return round(base * (1.0 + level * span)) + 0.0
    return round(base * (1.0 + level * span), 6)


def sensitivity_space(cfg: BacktestConfig, *, levels: Sequence[float] = DEFAULT_LEVELS,
                      names: Sequence[str] | None = None,
                      spans: Mapping[str, float] | None = None) -> list[SensParam]:
    """Рівні кожного параметра навколо бази конфігурації; перевіряє допустимість (u_exit < u_enter тощо)."""
    base = base_param_values(cfg)
    chosen = SENS_NAMES if names is None else tuple(names)
    bad = set(chosen) - set(SENS_NAMES)
    if bad:
        raise ValueError(f"unknown sensitivity parameters {sorted(bad)}; expected {SENS_NAMES}")
    out: list[SensParam] = []
    for name, symbol, label, where, span0, why in SENS_SPECS:
        if name not in chosen:
            continue
        span = span0 if spans is None or name not in spans else spans[name] + 0.0
        b = base[name]
        vals = sorted({_level_value(name, b, span, lv) for lv in levels} - {b})
        for v in vals:
            _check_point(name, v, base)
        out.append(SensParam(name=name, symbol=symbol, label_uk=label, where=where, base=b,
                             values=tuple(vals), span=span, rationale=why))
    return out


def _check_point(name: str, v: float, base: Mapping[str, float]) -> None:
    ok = {
        "u_enter": base["u_exit"] < v < 1.0, "u_exit": 0.0 < v < base["u_enter"],
        "chi": v > 0, "rho_base": 0.0 < v < 1.0, "lam": 0.0 < v < 1.0, "sigma_target": v > 0,
        "kappa_min": 0.0 <= v <= 1.0, "n_atr": v >= 2 and v == int(v),
    }[name]
    if not ok:
        raise ValueError(f"sensitivity value {name}={v} is outside the admissible range")


def apply_param(cfg: BacktestConfig, name: str, value: float) -> BacktestConfig:
    """Конфігурація з одним зміненим параметром (решта — база). Результат іде в config_hash."""
    if name in ("u_enter", "u_exit", "chi", "rho_base", "lam"):
        return cfg.with_params(**{name: value + 0.0})
    if name == "n_atr":
        return cfg.with_params(n_atr=int(value))
    trees = copy.deepcopy(dict(cfg.trees))
    if name == "sigma_target":
        risk = dict(trees["risk_limits"])
        risk["sizing"] = {**(risk.get("sizing") or {}), "sigma_target": value + 0.0}
        trees["risk_limits"] = risk
    elif name == "kappa_min":
        det = dict(trees["detectors"])
        det["agreement"] = {**(det.get("agreement") or {}), "kappa_min": value + 0.0}
        trees["detectors"] = det
    else:
        raise ValueError(f"unknown sensitivity parameter {name!r}")
    return dataclasses.replace(cfg, trees=trees)


def elasticity_coord(name: str, v: float) -> float:
    """Координата, у якій задано амплітуду параметра: для λ — пам'ять EWMA N = 1/(1−λ), для решти — саме θ.

    Еластичність рахується в тій самій координаті, що й рівні (інакше для λ ±50 % пам'яті давали б
    Δλ/λ ≈ ±4 % і «еластичність», завищену на порядок відносно інших параметрів)."""
    return 1.0 / (1.0 - v) if name == "lam" else v


def _elasticity(m_lo: float, m_hi: float, m_base: float, *, v_lo: float, v_hi: float, v_base: float) -> float:
    """Дугова еластичність на крайніх рівнях: (Δm/|m₀|)/(Δθ/θ₀); |m₀| ≈ 0 → NaN."""
    if not all(math.isfinite(x) for x in (m_lo, m_hi, m_base)) or abs(m_base) < 1e-12 or v_hi == v_lo:
        return math.nan
    return ((m_hi - m_lo) / abs(m_base)) / ((v_hi - v_lo) / v_base)


def tornado_rows(base_metrics: Mapping[str, float], runs: Sequence[Mapping[str, Any]],
                 space: Sequence[SensParam], keys: Sequence[str] = CRITERIA) -> list[dict[str, Any]]:
    """Таблиця «торнадо»: рядок на параметр, за спаданням розмаху першої метрики (NaN — у кінці).

    runs: {"param", "value", "metrics"}; low/high — найменше/найбільше значення параметра. Для метрики k:
    k_base, k_low, k_high, k_d_low = k_low − k_base, k_d_high, k_swing = max − min по всіх рівнях разом із
    базою, k_elasticity — дугова еластичність між крайніми рівнями в координаті elasticity_coord
    (λ — пам'ять EWMA).
    """
    by_param: dict[str, dict[float, Mapping[str, float]]] = {}
    for r in runs:
        by_param.setdefault(r["param"], {})[r["value"] + 0.0] = r["metrics"]
    rows: list[dict[str, Any]] = []
    for sp in space:
        got = by_param.get(sp.name, {})
        if not got:
            continue
        lo, hi = min(got), max(got)
        row: dict[str, Any] = {"param": sp.name, "symbol": sp.symbol, "label_uk": sp.label_uk,
                               "base_value": sp.base, "low_value": lo, "high_value": hi,
                               "values": sorted(got)}
        for k in keys:
            mb = base_metrics[k] + 0.0
            ml, mh = got[lo][k] + 0.0, got[hi][k] + 0.0
            allv = [mb, *(got[v][k] + 0.0 for v in got)]
            fin = [x for x in allv if math.isfinite(x)]
            row.update({f"{k}_base": mb, f"{k}_low": ml, f"{k}_high": mh, f"{k}_d_low": ml - mb,
                        f"{k}_d_high": mh - mb,
                        f"{k}_swing": (max(fin) - min(fin)) if len(fin) == len(allv) else math.nan,
                        f"{k}_elasticity": _elasticity(
                            ml, mh, mb, v_lo=elasticity_coord(sp.name, lo),
                            v_hi=elasticity_coord(sp.name, hi), v_base=elasticity_coord(sp.name, sp.base))})
        rows.append(row)
    k0 = keys[0]
    order = {sp.name: i for i, sp in enumerate(space)}
    rows.sort(key=lambda r: (math.isnan(r[f"{k0}_swing"]), -_nan_to(r[f"{k0}_swing"], 0.0),
                             order[r["param"]]))
    return rows


# ====================================================================== воркери (чисті, без БД)


def segment_task(task: Mapping[str, Any]) -> dict[str, Any]:
    """Точка входу ProcessPoolExecutor: один прогін над масивами сегмента → метрики (+ крива OOS за бажання).

    task: {"arrays": Dataset.to_payload(), "config": BacktestConfig.to_dict(), "params": {…сітка…} | {},
           "seed", "eval_start", "keep_equity": bool, "equity_hash": bool}.
    Повертає 17 метрик + extras рушія + wall_s (час задачі в самому воркері), pid, final_state, halted_at;
    з keep_equity — "equity" (float64, з бару першого рішення) і "equity_ts" (int64, close_ns); з equity_hash
    — SHA-256 кривої (як у паспорті run).
    """
    t0 = time.perf_counter()
    params = dict(task.get("params") or {})
    bad = set(params) - set(GRID_KEYS)
    if bad:
        raise ValueError(f"unknown grid parameters {sorted(bad)}; expected {GRID_KEYS}")
    cfg = BacktestConfig.from_dict(task["config"]).with_params(**params, record_traces="none")
    ds = Dataset.from_payload(task["arrays"])
    want_hash = bool(task.get("equity_hash", False))
    res = run_backtest(ds, cfg, task["seed"], eval_start=task.get("eval_start", 0), git=False,
                       hash_equity=want_hash)
    out: dict[str, Any] = {**res.metrics, **res.extras}
    out["halted_at"] = math.nan if res.halted_at is None else res.halted_at + 0.0
    out["final_state"] = res.final_state.value
    if want_hash:
        out["equity_hash"] = res.equity_hash
    out["config_hash"] = cfg.config_hash
    if task.get("keep_equity", False):
        eq = res.equity[res.eval_start:]
        out["equity"] = np.fromiter((to_float(e) for e in eq), dtype=np.float64, count=len(eq))
        out["equity_ts"] = np.asarray(res.equity_ts[res.eval_start:], dtype=np.int64)
    out["pid"] = os.getpid()
    out["wall_s"] = time.perf_counter() - t0
    return out


def startup_probe(task: Any) -> dict[str, Any]:
    """Діагностична задача заміру старту пулу: розпакування цієї функції імпортує той самий модуль, що й
    segment_task (рушій, numpy, pydantic), тож час пулу з p таких задач ≈ старт процесів + імпорти."""
    return {"task": task, "pid": os.getpid()}


def make_task(payload: Mapping[str, Any], cfg_d: Mapping[str, Any], seed: int, *,
              params: Mapping[str, Any] | None = None, eval_start: int = 0, keep_equity: bool = False,
              equity_hash: bool = False) -> dict[str, Any]:
    return {"arrays": payload, "config": cfg_d, "params": dict(params or {}), "seed": seed,
            "eval_start": eval_start, "keep_equity": keep_equity, "equity_hash": equity_hash}


def strip_arrays(res: Mapping[str, Any]) -> dict[str, Any]:
    """Результат сегмента без масивів кривої (для JSON/CSV і run_metric)."""
    return {k: v for k, v in res.items() if k not in ("equity", "equity_ts")}


def numeric_metrics(res: Mapping[str, Any], prefix: str = "") -> dict[str, float]:
    """Числові поля результату (для run_metric): без масивів, хешів, pid і стану."""
    return {f"{prefix}{k}": v + 0.0 for k, v in res.items()
            if k not in NON_METRIC_KEYS and isinstance(v, int | float) and not isinstance(v, bool)}


# ====================================================================== оркестрація


@dataclass(frozen=True)
class FoldSearch:
    """Один фолд: IS-метрики всіх клітинок, IS-фронт, вибір за правилом, OOS обраної клітинки."""

    fold: Fold
    is_metrics: list[dict[str, Any]]
    choice: FrontChoice
    params: dict[str, Any]
    is_selected: dict[str, Any]
    oos_selected: dict[str, Any]           # без масивів
    oos_equity: NDArray[np.float64]        # з бару першого OOS-рішення
    oos_equity_ts: NDArray[np.int64]
    oos_run_start: int                     # перший бар OOS-прогону в наборі (= oos_start − прогрів)
    oos_eval_start: int                    # прогрів усередині OOS-прогону


@dataclass(frozen=True)
class WalkForwardSearch:
    engine: str
    cells: list[dict[str, Any]]
    cell_ids: list[int]                    # індекси клітинок у повній сітці
    folds: list[FoldSearch]
    seed: int
    config_hash: str                       # базова конфігурація (без параметрів сітки)
    warmup: int
    dd_cap: float
    concat: dict[str, float]
    concat_equity: NDArray[np.float64]
    concat_fold: NDArray[np.int64]         # номер фолду кожної точки ланцюга (перша точка — фолд 0)
    concat_ts: NDArray[np.int64]
    wall_s: dict[str, float]

    def fold_rows(self) -> list[dict[str, Any]]:
        """Плаский рядок на фолд: межі, обрана клітинка, IS vs OOS (SR, MaxDD, оборот, дохідність, угоди)."""
        rows: list[dict[str, Any]] = []
        for fs in self.folds:
            f = fs.fold
            row: dict[str, Any] = {
                "fold": f.index, "is_start": f.is_start, "is_end": f.is_end, "embargo_bars": f.embargo_bars,
                "oos_start": f.oos_start, "oos_end": f.oos_end, "cell": self.cell_ids[fs.choice.index],
                "rule": fs.choice.rule, "front_size": len(fs.choice.front),
                "feasible": len(fs.choice.feasible),
                **{f"param_{k}": v for k, v in fs.params.items()},
            }
            for side, m in (("is", fs.is_selected), ("oos", fs.oos_selected)):
                for k in ("sharpe", "sortino", "max_drawdown", "turnover", "total_return", "n_trades", "psr",
                          "halted"):
                    row[f"{side}_{k}"] = m[k]
            rows.append(row)
        return rows


def walkforward_search(dataset: Dataset, cfg: BacktestConfig, *, folds: Sequence[Fold],
                       cells: Sequence[Mapping[str, Any]], seed: int, workers: int, dd_cap: float,
                       cell_ids: Sequence[int] | None = None) -> WalkForwardSearch:
    """Walk-forward: на IS кожного фолду — усі клітинки (чисті воркери), фронт і вибір правилом
    choose_from_front; на OOS — обрана клітинка з прогрівом на попередніх барах без торгівлі."""
    cells_l = [dict(c) for c in cells]
    ids = list(range(len(cells_l))) if cell_ids is None else list(cell_ids)
    warmup = cfg.resolved_warmup()
    segments = [oos_segment(f, warmup) for f in folds]         # embargo ≥ прогріву — до будь-якого прогону
    for f in folds:
        if f.is_end - f.is_start <= warmup:
            raise ValueError(f"fold {f.index}: IS window {f.is_end - f.is_start} bars <= warm-up {warmup}")
    cfg_d = cfg.to_dict()
    t0 = time.perf_counter()
    is_tasks: list[dict[str, Any]] = []
    for f in folds:
        payload = dataset.slice(f.is_start, f.is_end).to_payload()
        is_tasks += [make_task(payload, cfg_d, seed, params=c) for c in cells_l]
    is_res = run_parallel(segment_task, is_tasks, workers, seed=seed)
    t_is = time.perf_counter() - t0
    n = len(cells_l)
    per_fold = [[strip_arrays(r) for r in is_res[k * n:(k + 1) * n]] for k in range(len(folds))]
    choices = [choose_from_front(m, dd_cap=dd_cap) for m in per_fold]

    t1 = time.perf_counter()
    oos_tasks = [make_task(dataset.slice(start, f.oos_end).to_payload(), cfg_d, seed,
                           params=cells_l[ch.index], eval_start=ev, keep_equity=True, equity_hash=True)
                 for f, ch, (start, ev) in zip(folds, choices, segments, strict=True)]
    oos_res = run_parallel(segment_task, oos_tasks, workers, seed=seed)
    t_oos = time.perf_counter() - t1

    reports = [
        FoldSearch(fold=f, is_metrics=per_fold[k], choice=ch, params=dict(cells_l[ch.index]),
                   is_selected=per_fold[k][ch.index], oos_selected=strip_arrays(oos_res[k]),
                   oos_equity=oos_res[k]["equity"], oos_equity_ts=oos_res[k]["equity_ts"],
                   oos_run_start=segments[k][0], oos_eval_start=segments[k][1])
        for k, (f, ch) in enumerate(zip(folds, choices, strict=True))
    ]
    ppy = dataset.bars_per_year
    concat = concat_oos_metrics(oos_res, ppy)
    eq, _ = chain_equity([r.oos_equity for r in reports])
    fold_of = np.concatenate([np.zeros(1, dtype=np.int64)]
                             + [np.full(r.oos_equity.size - 1, r.fold.index, dtype=np.int64)
                                for r in reports])
    ts = np.concatenate([reports[0].oos_equity_ts[:1]] + [r.oos_equity_ts[1:] for r in reports])
    return WalkForwardSearch(
        engine=str(cfg.engine), cells=cells_l, cell_ids=ids, folds=reports, seed=seed,
        config_hash=cfg.config_hash, warmup=warmup, dd_cap=dd_cap, concat=concat, concat_equity=eq,
        concat_fold=fold_of, concat_ts=ts, wall_s={"is_grid": t_is, "oos": t_oos},
    )


def oos_tasks_all_cells(dataset: Dataset, cfg: BacktestConfig, *, folds: Sequence[Fold],
                        cells: Sequence[Mapping[str, Any]], seed: int) -> list[dict[str, Any]]:
    """OOS-задачі кожної клітинки на кожному фолді (порядок: фолд, потім клітинка) з кривими для ланцюга."""
    warmup = cfg.resolved_warmup()
    cfg_d = cfg.to_dict()
    tasks: list[dict[str, Any]] = []
    for f in folds:
        start, ev = oos_segment(f, warmup)
        payload = dataset.slice(start, f.oos_end).to_payload()
        tasks += [make_task(payload, cfg_d, seed, params=c, eval_start=ev, keep_equity=True) for c in cells]
    return tasks


def concat_by_cell(oos_results: Sequence[Mapping[str, Any]], n_cells: int, n_folds: int,
                   periods_per_year: float) -> list[dict[str, float]]:
    """Результати oos_tasks_all_cells (фолд-major) → метрики конкатенованого OOS кожної клітинки."""
    if len(oos_results) != n_cells * n_folds:
        raise ValueError("unexpected number of OOS results")
    return [concat_oos_metrics([oos_results[k * n_cells + i] for k in range(n_folds)], periods_per_year)
            for i in range(n_cells)]


# ====================================================================== завантаження набору і провенанс


def load_dataset(source: str, symbol: str, *, fixture_path: str | None = None) -> Dataset:
    """db — 45-денне вікно data/dataset_window.json з PostgreSQL (backtest.runner.load_db_window: хеш свічок
    звіряється, фандинг з data/); fixture — fixtures/rest/binance_klines.json.gz (3000 барів BTCUSDT)."""
    if source == "db":
        import asyncio  # noqa: PLC0415

        from fuzzhelm.backtest.runner import load_db_window  # noqa: PLC0415

        return asyncio.run(load_db_window(symbol))
    if source == "fixture":
        from fuzzhelm.backtest.dataset import (  # noqa: PLC0415
            DEFAULT_KLINES,
            load_exchange_instrument,
            load_klines_json,
        )

        if symbol != "BTCUSDT" and fixture_path is None:
            raise ValueError("the default kline fixture holds BTCUSDT only")
        return load_klines_json(fixture_path or DEFAULT_KLINES, load_exchange_instrument(symbol))
    raise ValueError(f"unknown source {source!r}")


def add_common_args(ap: argparse.ArgumentParser) -> None:
    """Спільні прапорці скриптів експерименту: джерело, символ, seed, воркери, тека виводу, --smoke."""
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--db", action="store_true",
                     help="45-day window of data/dataset_window.json from PostgreSQL (default source)")
    src.add_argument("--fixture", nargs="?", const="", default=None, metavar="PATH",
                     help="kline fixture instead of the DB (default fixtures/rest/binance_klines.json.gz)")
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--seed", type=int, default=None, help="default: seed of the run profile (20260918)")
    ap.add_argument("--workers", type=int, default=min(8, os.cpu_count() or 1),
                    help="ProcessPoolExecutor size (0 = sequential in-process)")
    ap.add_argument("--out", default=None, help="output directory (default artifacts/exp_search/<script>, "
                                                "with --smoke artifacts/tmp/exp_search/<script>)")
    ap.add_argument("--smoke", action="store_true", help="small end-to-end check (few days/cells/folds)")
    ap.add_argument("--smoke-days", type=float, default=4.0, help="days of the DB window used by --smoke")
    ap.add_argument("--database-url", default=None, help="override FUZZHELM_DATABASE_URL (read and persist)")
    ap.add_argument("--cooldown-policy", choices=("scaled_entries", "reduce_only"), default=None,
                    help="override state_machine.cooldown_policy (default: config/risk_limits.yaml)")


def resolve_dataset(args: argparse.Namespace) -> Dataset:
    """Набір за прапорцями add_common_args; --smoke з БД — перші --smoke-days діб вікна."""
    if args.database_url:
        from fuzzhelm.config import get_settings  # noqa: PLC0415

        os.environ["FUZZHELM_DATABASE_URL"] = args.database_url
        get_settings.cache_clear()
    if args.fixture is not None:
        return load_dataset("fixture", args.symbol, fixture_path=args.fixture or None)
    ds = load_dataset("db", args.symbol)
    return smoke_slice(ds, args.smoke_days) if args.smoke else ds


def resolve_out(args: argparse.Namespace, name: str) -> Path:
    from fuzzhelm.config import ROOT  # noqa: PLC0415

    if args.out:
        out = Path(args.out)
    else:
        out = ROOT / "artifacts" / ("tmp/exp_search" if args.smoke else "exp_search") / name
    out.mkdir(parents=True, exist_ok=True)
    return out


def profile_seed(profile: str) -> int:
    from fuzzhelm.config import load_yaml  # noqa: PLC0415

    return int(load_yaml(f"profiles/{profile}")["seed"])


def smoke_slice(ds: Dataset, days: float) -> Dataset:
    """Перші `days` діб набору (для --smoke на даних з БД)."""
    n = min(len(ds), round(days * 86_400_000_000_000 / ds.tf_ns))
    return ds if n >= len(ds) else ds.slice(0, n)


def candles_hash(ds: Dataset) -> str:
    """Хеш лише свічок (як data/dataset_window.json), без специфікації інструмента і фандингу."""
    return dataset_hash({c: getattr(ds, c) for c in HASH_COLUMNS})


def dataset_info(ds: Dataset) -> dict[str, Any]:
    from datetime import UTC, datetime  # noqa: PLC0415 — лише форматування меж набору

    def utc(ns: int) -> str:
        return datetime.fromtimestamp(ns / 1e9, tz=UTC).strftime("%Y-%m-%dT%H:%MZ")

    return {"source": ds.source, "symbol": ds.instrument.symbol_venue,
            "symbol_canon": ds.instrument.symbol_canon,
            "tf": ds.tf, "bars": len(ds), "first_open_utc": utc(ds.t_ns[0].item()),
            "last_open_utc": utc(ds.t_ns[-1].item()), "dataset_hash": ds.dataset_hash,
            "candles_hash": candles_hash(ds),
            "funding_rates": 0 if ds.funding_t_ns is None else int(ds.funding_t_ns.size)}


def command_line(argv: Sequence[str]) -> str:
    return "uv run python " + " ".join(shlex.quote(a) for a in argv)


# Каталоги, зміни в яких НЕ впливають на числа експерименту: виводи скриптів і документація. Сирий
# `git status --porcelain` (manifest.read_git_sha) бачить їх як «незакомічені зміни», тож перший же скрипт
# наступної хвилі, що пише в artifacts/exp_search/ (не в .gitignore), зробив би «брудними» паспорти всіх
# наступних (XS-11). Код, конфіги, дані і фікстури (src/, scripts/, config/, data/, fixtures/,
# pyproject/uv.lock) рахуються завжди.
GIT_DIRTY_IGNORED: tuple[str, ...] = ("artifacts", "docs")


def git_state(repo: Path | None = None) -> dict[str, Any]:
    """Стан git для паспортів експерименту: sha HEAD, dirty (зміни поза GIT_DIRTY_IGNORED), dirty_any (сирий
    `git status --porcelain`, як manifest.read_git_sha) і перші 20 рядків змін, що зробили dirty = True."""
    from fuzzhelm.backtest.manifest import read_git_sha  # noqa: PLC0415
    from fuzzhelm.config import ROOT  # noqa: PLC0415

    root = ROOT if repo is None else repo
    sha, dirty_any = read_git_sha(root)
    out: dict[str, Any] = {"sha": sha, "dirty": dirty_any, "dirty_any": dirty_any, "dirty_paths": [],
                           "dirty_ignored": list(GIT_DIRTY_IGNORED)}
    if sha is None:
        return out
    try:
        st = subprocess.run(["git", "status", "--porcelain", "--", ".",
                             *(f":(exclude){p}" for p in GIT_DIRTY_IGNORED)],
                            cwd=root, capture_output=True, text=True, timeout=10, check=True).stdout
    except (OSError, subprocess.SubprocessError):
        return out                        # не вдалось звузити — лишається сирий (суворіший) прапорець
    lines = [ln for ln in st.splitlines() if ln.strip()]
    out.update(dirty=bool(lines), dirty_paths=lines[:20])
    return out


def commit_refusal(persist: str, gs: Mapping[str, Any], *, allow_dirty: bool) -> str | None:
    """Причина відмови писати прогони в БД (--persist commit) з незакоміченого коду, або None.

    Числа звіту мають походити із закоміченого коду (паспорт = git_sha без змін): commit з dirty = True або
    без git_sha — лише з явним --allow-dirty (тоді паспорт чесно несе git_dirty = 1)."""
    if persist != "commit" or allow_dirty:
        return None
    if gs["sha"] is None:
        return "git SHA is unavailable: refusing to commit runs without a code identity (pass --allow-dirty)"
    if gs["dirty"]:
        paths = gs["dirty_paths"]
        shown = "; ".join(paths[:5]) + (" …" if len(paths) > 5 else "")
        outside = ", ".join(p + "/" for p in GIT_DIRTY_IGNORED)
        return (f"uncommitted changes outside {outside} ({shown}): commit the code first so every persisted "
                f"run carries a clean git_sha, or pass --allow-dirty")
    return None


def provenance(argv: Sequence[str], ds: Dataset, cfg: BacktestConfig, *, seed: int, workers: int,
               created_utc: str, extra: Mapping[str, Any] | None = None,
               git: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Паспорт виводу скрипта: точна команда, набір (хеші), конфігурація (хеш), git_sha/dirty, seed.

    git — стан git_state(), знятий ДО обчислень (його ж отримують паспорти БД); None — зняти зараз."""
    gs = git_state() if git is None else git
    return {"command": command_line(argv), "created_utc": created_utc, "git_sha": gs["sha"],
            "git_dirty": gs["dirty"], "git_dirty_any": gs["dirty_any"], "git_dirty_paths": gs["dirty_paths"],
            "git_dirty_ignored": gs["dirty_ignored"],
            "seed": seed, "workers": workers, "dataset": dataset_info(ds), "config_hash": cfg.config_hash,
            "engine": cfg.engine, "cost_mode": cfg.cost_mode, "cooldown_policy": cfg.cooldown_policy,
            "initial_equity": str(cfg.initial_equity), "warmup_bars": cfg.resolved_warmup(),
            **dict(extra or {})}


def provenance_md(prov: Mapping[str, Any]) -> str:
    d = prov["dataset"]
    dirty = {True: " (**незакомічені зміни в коді/конфігах/даних**)", False: "",
             None: " (git недоступний)"}[prov["git_dirty"]]
    if prov["git_dirty"] is False and prov.get("git_dirty_any"):
        dirty = f" (чисто; змінено лише {', '.join(p + '/' for p in prov.get('git_dirty_ignored', []))})"
    lines = [
        "```", prov["command"], "```", "",
        md_table(["поле", "значення"], [
            ["набір", f"{d['source']} · {d['symbol']} {d['tf']} · {d['bars']} барів · "
                      f"{d['first_open_utc']} … {d['last_open_utc']}"],
            ["dataset_hash (свічки + інструмент + фандинг)", f"`{d['dataset_hash']}`"],
            ["хеш лише свічок (як data/dataset_window.json)", f"`{d['candles_hash']}`"],
            ["config_hash (база, без параметрів сітки)", f"`{prov['config_hash']}`"],
            ["git_sha", f"`{prov['git_sha']}`{dirty}"],
            ["рушій / витрати / COOLDOWN",
             f"{prov['engine']} / {prov['cost_mode']} / {prov['cooldown_policy']}"],
            ["seed / воркери / прогрів", f"{prov['seed']} / {prov['workers']} / {prov['warmup_bars']} барів"],
            ["створено (UTC)", prov["created_utc"]],
        ]),
    ]
    return "\n".join(lines)


def fmt(x: Any, digits: int = 4) -> str:
    """Число для markdown: NaN → «—», ±inf → «∞», великі — з пробілами-розділювачами."""
    if x is None:
        return "—"
    if isinstance(x, bool):
        return "так" if x else "ні"
    if isinstance(x, int):
        return f"{x:,}".replace(",", " ")
    if isinstance(x, float):
        if math.isnan(x):
            return "—"
        if math.isinf(x):
            return "∞" if x > 0 else "−∞"
        if x != 0 and (abs(x) >= 1e5 or abs(x) < 1e-4):
            return f"{x:.{digits}g}"
        return f"{x:.{digits}f}".rstrip("0").rstrip(".") if abs(x) < 1000 else f"{x:,.1f}".replace(",", " ")
    return str(x)


def md_table(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> str:
    def cell(v: Any) -> str:
        return (v if isinstance(v, str) else fmt(v)).replace("|", "\\|")

    out = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    out += ["| " + " | ".join(cell(v) for v in r) + " |" for r in rows]
    return "\n".join(out)


def json_safe(obj: Any) -> Any:
    """JSON без нестандартних токенів: NaN → "NaN", ±inf → "Infinity"/"-Infinity" (читає float(...)),
    numpy → Python, ключі словників → рядки."""
    if isinstance(obj, Mapping):
        return {str(k): json_safe(v) for k, v in obj.items()}
    if isinstance(obj, list | tuple):
        return [json_safe(v) for v in obj]
    if isinstance(obj, np.ndarray):
        return [json_safe(v) for v in obj.tolist()]
    if isinstance(obj, np.generic):
        return json_safe(obj.item())
    if isinstance(obj, float) and not math.isfinite(obj):
        return "NaN" if math.isnan(obj) else ("Infinity" if obj > 0 else "-Infinity")
    return obj


def median(xs: Sequence[float]) -> float:
    return statistics.median(xs) if xs else math.nan
