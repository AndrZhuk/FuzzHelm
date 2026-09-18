"""Історичні VaR₉₅ / CVaR₉₅ на вікні W = 500 і параметричний VaR (звітні метрики, НЕ блокують ордери).

Найменування: risk/var.py
Автор: Андрій Жук, 2026.

    r^p_t = ΔE_t / E_{t−1}
    VaR_α  = −Q̂_α({r}),                  Q̂_α = r_(m) — m-та порядкова статистика (зростання)
    CVaR_α = −(1/m)·Σ_{i≤m} r_(i),        m = max(1, ⌊α·n⌋)
    VaR_param = z_{1−α}·σ_p·√h·E,         z через statistics.NormalDist

Вибір оцінювача квантиля. «Quantile_0.05» у брифінгу не фіксує схему інтерполяції; тут квантиль —
НИЖНЯ емпірична оцінка r_(m) з тим самим m, що й у CVaR. Тоді CVaR — середнє m найменших значень,
а VaR — найбільше з них, тому CVaR ≥ VaR для БУДЬ-якої вибірки (а не лише в середньому). Для n = 500
це збігається з numpy.quantile(method="lower"): ⌊(n−1)·α⌋ + 1 = 25 = ⌊n·α⌋.
Зауваження: VaR ≥ 0 НЕ є тотожністю — якщо у вікні ≥ n − m + 1 додатних дохідностей (r_(m) > 0), VaR < 0
(docs/deviations.d/risk.md). Тотожністю є лише CVaR ≥ VaR.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from statistics import NormalDist

import numpy as np
import numpy.typing as npt

from fuzzhelm.features.convert import to_float

DEFAULT_WINDOW = 500
DEFAULT_ALPHA = 0.05
_EPS_COUNT = 1e-9                  # захист ⌊α·n⌋ від 0.57·100 = 56.999… у float

FloatArray = npt.NDArray[np.float64]


@dataclass(frozen=True, slots=True)
class VarResult:
    var: float              # у частках капіталу (додатне = збиток)
    cvar: float
    m: int                  # скільки найгірших спостережень у хвості
    n: int                  # розмір вікна, на якому пораховано


def tail_count(n: int, alpha: float) -> int:
    """m = max(1, ⌊α·n⌋)."""
    if n <= 0:
        raise ValueError("empty sample")
    return max(1, math.floor(alpha * n + _EPS_COUNT))


def _as_array(returns: Sequence[float] | FloatArray) -> FloatArray:
    arr = np.asarray(returns, dtype=np.float64)
    if arr.ndim != 1:
        raise ValueError("returns must be one-dimensional")
    if not np.all(np.isfinite(arr)):
        raise ValueError("returns contain NaN/inf")
    return arr


def historical_var_cvar(returns: Sequence[float] | FloatArray, alpha: float = DEFAULT_ALPHA,
                        window: int | None = DEFAULT_WINDOW) -> VarResult:
    """VaR/CVaR на останніх `window` дохідностях (None — на всіх)."""
    if not 0.0 < alpha < 1.0:
        raise ValueError(f"alpha must be in (0, 1), got {alpha}")
    arr = _as_array(returns)
    if window is not None:
        if window <= 0:
            raise ValueError("window must be > 0")
        arr = arr[-window:]
    n = arr.size
    m = tail_count(n, alpha)
    tail = np.partition(arr, m - 1)[:m]          # m найменших (невпорядковано), tail.max() = r_(m)
    r_m = tail.max()
    var: float = -r_m.item()
    # CVaR = VaR + середнє перевищення хвоста над VaR. Математично це −mean(tail), але така форма
    # зберігає CVaR ≥ VaR і в арифметиці float: кожне r_(m) − r_(i) ≥ 0 обчислюється точно-монотонно,
    # тоді як −mean(tail) при однакових значеннях може на 1 ulp опинитися нижче за VaR.
    excess: float = (r_m - tail).mean().item()
    return VarResult(var=var, cvar=var + excess, m=m, n=n)


def parametric_var(sigma: float, *, equity: float = 1.0, h: float = 1.0,
                   alpha: float = DEFAULT_ALPHA) -> float:
    """VaR = z_{1−α}·σ·√h·E (нормальне наближення; на товстих хвостах занижує ризик)."""
    if sigma < 0 or h < 0:
        raise ValueError("sigma and h must be >= 0")
    return NormalDist().inv_cdf(1.0 - alpha) * sigma * math.sqrt(h) * equity


def returns_from_equity(equity: Sequence[Decimal]) -> FloatArray:
    """r_t = (E_t − E_{t−1}) / E_{t−1}; ділення — у Decimal, у float — через єдину точку конвертації."""
    out = np.empty(max(0, len(equity) - 1), dtype=np.float64)
    for i in range(1, len(equity)):
        prev = equity[i - 1]
        if prev <= 0:
            raise ValueError(f"non-positive equity at index {i - 1}")
        out[i - 1] = to_float((equity[i] - prev) / prev)
    return out


@dataclass(frozen=True, slots=True)
class VarBacktest:
    breaches: int
    n: int
    var_series: FloatArray          # VaR_t, оцінений лише за r[t−W : t] (без зазирання вперед)


def rolling_var_breaches(returns: Sequence[float] | FloatArray, window: int = DEFAULT_WINDOW,
                         alpha: float = DEFAULT_ALPHA, chunk: int = 4096) -> VarBacktest:
    """Ковзний історичний VaR і лічильник пробоїв r_t < −VaR_t (вхід для kupiec_pof(breaches, n)).

    Для кожного t ≥ W оцінка будується строго на минулому вікні r[t−W:t]. Векторизовано блоками,
    щоб не тримати в пам'яті матрицю (n−W)×W.
    """
    if not 0.0 < alpha < 1.0:
        raise ValueError(f"alpha must be in (0, 1), got {alpha}")
    if window <= 0:
        raise ValueError("window must be > 0")
    arr = _as_array(returns)
    n = arr.size - window
    if n <= 0:
        return VarBacktest(0, 0, np.empty(0, dtype=np.float64))
    m = tail_count(window, alpha)
    windows = np.lib.stride_tricks.sliding_window_view(arr[:-1], window)   # рядок t−W: r[t−W..t−1]
    var = np.empty(n, dtype=np.float64)
    for lo in range(0, n, chunk):
        block = np.partition(windows[lo:lo + chunk], m - 1, axis=1)
        var[lo:lo + chunk] = -block[:, m - 1]
    realized = arr[window:]
    breaches = int(np.count_nonzero(realized < -var))
    return VarBacktest(breaches, n, var)
