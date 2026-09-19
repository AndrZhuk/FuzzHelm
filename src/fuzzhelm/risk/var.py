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

Ковзні VaR/CVaR кривої капіталу (equity_point.var95/cvar95, RF-03 у docs/deviations.d/riskfix.md):
    VaR_t  = E_t · VaR̂₉₅(r_{t−W+1..t}),   CVaR_t = E_t · CVaR̂₉₅(r_{t−W+1..t}),   W = 500
— ГРОШІ (валюта котирування, USDT), додатне = збиток: історичний квантиль одно-барових дохідностей кривої
на вікні, що закінчується на t ВКЛЮЧНО («ризик кривої станом на t»; прогноз для тесту Купця —
rolling_var_breaches, строго на минулому), масштабований поточним капіталом. None/NaN, поки дохідностей
< W (лише повні вікна §5.13). Значення не обрізаються до 0: на вікні з ≥ W − m + 1 додатних дохідностей
VaR_t < 0 (R-04).
"""

from __future__ import annotations

import math
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from statistics import NormalDist

import numpy as np
import numpy.typing as npt

from fuzzhelm.features.convert import to_float
from fuzzhelm.sizing.convert import float_to_decimal_exact

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


# ---------------------------------------------------------------- ковзні VaR/CVaR кривої капіталу


def _tail_stats(rows: FloatArray, m: int) -> tuple[FloatArray, FloatArray]:
    """(VaR, CVaR) кожного рядка 2-D масиву вікон однакової довжини — оцінка historical_var_cvar.

    Єдине ядро для пакетного (rolling_var_cvar) і покрокового (RollingVarCvar) шляхів: ті самі операції над
    тими самими даними рядка ⇒ однакові float-и в обох шляхах, а не «рівні з допуском».
    """
    tail = np.partition(rows, m - 1, axis=1)[:, :m]           # m найменших у кожному рядку
    r_m = tail.max(axis=1)                                     # r_(m)
    var = -r_m
    return var, var + (r_m[:, None] - tail).mean(axis=1)      # CVaR = VaR + середнє перевищення ≥ VaR


def rolling_var_cvar(returns: Sequence[float] | FloatArray, window: int = DEFAULT_WINDOW,
                     alpha: float = DEFAULT_ALPHA, *, min_obs: int | None = None,
                     chunk: int = 4096) -> tuple[FloatArray, FloatArray]:
    """VaR/CVaR (частки капіталу) для кожної точки кривої t = 0..n, n = len(returns): точка t бачить
    дохідності r_1..r_t кривої (r[:t] масиву), з них — останні min(t, W). NaN, поки t < min_obs
    (типово min_obs = W: лише повні вікна, §5.13).

    Повні вікна — векторизовано блоками по `chunk` рядків (np.partition, O(W) на точку в C; матриці N×W у
    пам'яті немає). Розгортання min_obs ≤ t < W (лише якщо min_obs < W) — поштучно historical_var_cvar на
    r[:t] (m = ⌊α·t⌋ < 25: інша, грубіша оцінка — тому типово вимкнене).
    """
    if not 0.0 < alpha < 1.0:
        raise ValueError(f"alpha must be in (0, 1), got {alpha}")
    lo_obs = window if min_obs is None else min_obs
    if window <= 0 or chunk <= 0 or not 0 < lo_obs <= window:
        raise ValueError("need window > 0, chunk > 0 and 0 < min_obs <= window")
    r = _as_array(returns)
    n_pts = r.size + 1
    var = np.full(n_pts, np.nan)
    cvar = np.full(n_pts, np.nan)
    for t in range(lo_obs, min(window, n_pts)):
        res = historical_var_cvar(r[:t], alpha, window=None)
        var[t], cvar[t] = res.var, res.cvar
    if r.size >= window:
        m = tail_count(window, alpha)
        wins = np.lib.stride_tricks.sliding_window_view(r, window)     # рядок k: r[k : k+W] → точка k + W
        for k0 in range(0, wins.shape[0], chunk):
            v, c = _tail_stats(wins[k0:k0 + chunk], m)
            t0 = k0 + window
            var[t0:t0 + v.size] = v
            cvar[t0:t0 + c.size] = c
    return var, cvar


def _money(frac: float, equity: Decimal) -> Decimal | None:
    """Частка капіталу → гроші: E_t · frac (float → Decimal лише через sizing.convert, найкоротший repr)."""
    if not math.isfinite(frac):
        return None
    return float_to_decimal_exact(frac + 0.0) * equity          # + 0.0: −0.0 (VaR = −r_(m), r_(m) = 0) → 0


def var_cvar_money(equity: Sequence[Decimal], window: int = DEFAULT_WINDOW, alpha: float = DEFAULT_ALPHA,
                   *, min_obs: int | None = None) -> tuple[list[Decimal | None], list[Decimal | None]]:
    """VaR₉₅/CVaR₉₅ кожної точки кривої капіталу в ГРОШАХ: E_t · rolling_var_cvar(r)[t]; None до min_obs
    дохідностей (типово W = 500). Конвенцію описано в докстрінгу модуля."""
    if not equity:
        return [], []
    var, cvar = rolling_var_cvar(returns_from_equity(equity), window, alpha, min_obs=min_obs)
    return ([_money(v, e) for v, e in zip(var.tolist(), equity, strict=True)],
            [_money(c, e) for c, e in zip(cvar.tolist(), equity, strict=True)])


class RollingVarCvar:
    """Покрокова (live) версія var_cvar_money: ті самі числа на тій самій кривій (спільне ядро _tail_stats).

    update(E_t) → (VaR_t, CVaR_t) у грошах або (None, None), поки дохідностей < min_obs. Пам'ять — O(W),
    час — O(W) у numpy на бар (1 бар/хв у live).
    """

    def __init__(self, window: int = DEFAULT_WINDOW, alpha: float = DEFAULT_ALPHA, *,
                 min_obs: int | None = None) -> None:
        if not 0.0 < alpha < 1.0:
            raise ValueError(f"alpha must be in (0, 1), got {alpha}")
        lo_obs = window if min_obs is None else min_obs
        if window <= 0 or not 0 < lo_obs <= window:
            raise ValueError("need window > 0 and 0 < min_obs <= window")
        self.window, self.alpha, self.min_obs = window, alpha, lo_obs
        self._m = tail_count(window, alpha)
        self._r: deque[float] = deque(maxlen=window)
        self._prev: Decimal | None = None

    def update(self, equity: Decimal) -> tuple[Decimal | None, Decimal | None]:
        prev, self._prev = self._prev, equity
        if prev is not None:
            if prev <= 0:
                raise ValueError("non-positive equity")
            self._r.append(to_float((equity - prev) / prev))
        n = len(self._r)
        if n < self.min_obs:
            return None, None
        arr = np.fromiter(self._r, dtype=np.float64, count=n)
        if n < self.window:
            res = historical_var_cvar(arr, self.alpha, window=None)
            return _money(res.var, equity), _money(res.cvar, equity)
        v, c = _tail_stats(arr[None, :], self._m)
        return _money(v[0].item(), equity), _money(c[0].item(), equity)
