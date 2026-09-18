"""Метрики бектесту (17), імовірнісний (PSR) і дефльований (DSR) коефіцієнти Шарпа.

Найменування: backtest/metrics.py
Призначення: звітні числа прогону (таблиця run_metric) і статистична перевірка переваги стратегії.
Автор: Андрій Жук, 2026.

Формули (брифінг §5.15), r_t = E_t/E_{t−1} − 1, P — періодів на рік:
    Sharpe  = √P·(mean r − r_f)/std r                   (std — вибіркове, ddof = 1)
    Sortino = √P·(mean r − r_f)/√(mean min(r,0)²)        (нижнє відхилення відносно 0, як у брифінгу)
    MaxDD   = max_t(1 − E_t/max_{τ≤t}E_τ);   Calmar = CAGR/MaxDD;   Ulcer = √(mean DD_t²) (частки, не %)
    PF      = Σприбутків/|Σзбитків|;         Expectancy = p·avg_win − (1−p)·avg_loss
    PSR     = Φ( (ŜR − SR*)·√(n−1) / √(1 − γ₃·ŜR + ((γ₄−1)/4)·ŜR²) )
    DSR     = PSR з SR* = SR₀ = √Var(SR_i)·[(1−γ_E)·Φ⁻¹(1−1/N) + γ_E·Φ⁻¹(1−1/(N·e))],  γ_E = 0.5772

У PSR/DSR ŜR — Шарп ЗА ПЕРІОД (не річний), γ₃ — асиметрія, γ₄ — куртозис (не надлишковий, нормальний = 3),
моменти — популяційні (ddof = 0). Φ, Φ⁻¹ — statistics.NormalDist.

Ділення на нуль: a/0 = 0, якщо a = 0, інакше ±inf (напр. PF без збиткових угод = inf).
Статистика — float/numpy (float-домен дозволений для звітних метрик); Decimal-капітал перетворюється
лише через features.convert.to_float.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from decimal import Decimal
from statistics import NormalDist
from typing import Any

import numpy as np
from numpy.typing import NDArray

from fuzzhelm.features.convert import to_float

NumSeq = Sequence[Decimal] | Sequence[float] | Sequence[int] | NDArray[Any]
EULER_GAMMA = 0.5772          # γ_E як у брифінгу (0.5772156649… з округленням до 4 знаків)
_N = NormalDist()

METRIC_NAMES: tuple[str, ...] = (
    "total_return",      # 1  E_T/E_0 − 1
    "cagr",              # 2  (E_T/E_0)^(P/n) − 1
    "ann_vol",           # 3  std(r)·√P
    "sharpe",            # 4
    "sortino",           # 5
    "max_drawdown",      # 6  частка ∈ [0, 1]
    "calmar",            # 7  CAGR/MaxDD
    "ulcer_index",       # 8  √(mean DD²)
    "profit_factor",     # 9  Σwins/|Σlosses|
    "expectancy",        # 10 p·avg_win − (1−p)·avg_loss  (у валюті рахунку на угоду)
    "win_rate",          # 11 p = n_wins/n_trades
    "avg_win",           # 12 середній прибуток прибуткової угоди (> 0)
    "avg_loss",          # 13 середній збиток збиткової угоди (модуль, > 0)
    "n_trades",          # 14
    "turnover",          # 15 річний оборот у капіталах: Σ|N|/mean(E)·P/n
    "exposure",          # 16 частка періодів з ненульовою позицією (NaN, якщо позиції не передано)
    "tail_ratio",        # 17 |q95(r)|/|q05(r)|
)


# ---------------------------------------------------------------- перетворення входів


def _to_float_array(values: NumSeq) -> NDArray[np.float64]:
    if isinstance(values, np.ndarray):
        return np.asarray(values, dtype=np.float64)
    seq: Sequence[Decimal | float | int] = values
    out = np.empty(len(seq), dtype=np.float64)
    for i, v in enumerate(seq):
        out[i] = _num(v)
    return out


def _num(x: Decimal | float | int) -> float:
    return to_float(x) if isinstance(x, Decimal) else x + 0.0


def _py(x: Any) -> float:
    """numpy-скаляр або число → Python float (без float(<expr>): межа типів backtest)."""
    out: float = x.item() if isinstance(x, np.generic) else x + 0.0
    return out


def _ratio(a: float, b: float) -> float:
    if b == 0:
        return 0.0 if a == 0 else math.copysign(math.inf, a)
    return a / b


def _equity_array(equity: NumSeq) -> NDArray[np.float64]:
    """Крива капіталу як float-масив; NaN/inf — помилка даних (інакше тихо «отруїли» б усі метрики)."""
    e = _to_float_array(equity)
    if not np.isfinite(e).all():
        raise ValueError("equity curve contains NaN or inf")
    return e


def returns_from_equity(equity: NumSeq) -> NDArray[np.float64]:
    """Прості доходності r_t = E_t/E_{t−1} − 1 (довжина n−1)."""
    e = _equity_array(equity)
    if e.size < 2:
        return np.empty(0, dtype=np.float64)
    if np.any(e[:-1] <= 0):
        raise ValueError("equity must stay > 0 to define returns")
    return e[1:] / e[:-1] - 1.0


def drawdown_series(equity: NumSeq) -> NDArray[np.float64]:
    """DD_t = 1 − E_t/max_{τ≤t} E_τ (потрібен додатний початковий капітал)."""
    e = _equity_array(equity)
    if e.size == 0:
        return e
    if e[0] <= 0:
        raise ValueError("initial equity must be > 0 to define drawdown")
    peak = np.maximum.accumulate(e)
    return 1.0 - e / peak


# ---------------------------------------------------------------- окремі метрики


def sharpe_ratio(r: NDArray[np.float64], periods_per_year: float, rf: float = 0.0) -> float:
    if r.size < 2:
        return 0.0
    return math.sqrt(periods_per_year) * _ratio(np.mean(r).item() - rf, np.std(r, ddof=1).item())


def sortino_ratio(r: NDArray[np.float64], periods_per_year: float, rf: float = 0.0) -> float:
    if r.size == 0:
        return 0.0
    downside = math.sqrt(np.mean(np.minimum(r, 0.0) ** 2).item())
    return math.sqrt(periods_per_year) * _ratio(np.mean(r).item() - rf, downside)


def max_drawdown(equity: NumSeq) -> float:
    dd = drawdown_series(equity)
    return dd.max().item() if dd.size else 0.0


def ulcer_index(equity: NumSeq) -> float:
    dd = drawdown_series(equity)
    return math.sqrt(np.mean(dd**2).item()) if dd.size else 0.0


def moments(r: NDArray[np.float64]) -> tuple[float, float]:
    """(γ₃, γ₄) — популяційні асиметрія і куртозис (нормальний розподіл: 0 і 3). Стала серія → (0, 3)."""
    if r.size < 2:
        return 0.0, 3.0
    d = r - r.mean()
    m2 = np.mean(d**2).item()
    if m2 == 0:
        return 0.0, 3.0
    return np.mean(d**3).item() / m2**1.5, np.mean(d**4).item() / m2**2


# ---------------------------------------------------------------- 17 метрик


def _trade_pnls(trades: Sequence[Any]) -> NDArray[np.float64]:
    out = np.empty(len(trades), dtype=np.float64)
    for i, t in enumerate(trades):
        out[i] = _num(t.pnl if hasattr(t, "pnl") else t)
    return out


def _trades_notional(trades: Sequence[Any]) -> float | None:
    total = 0.0
    for t in trades:
        if not (hasattr(t, "entry_notional") and hasattr(t, "exit_notional")):
            return None
        total += _num(t.entry_notional) + _num(t.exit_notional)
    return total


def compute_metrics(
    equity: NumSeq,
    trades: Sequence[Any],
    periods_per_year: float,
    *,
    rf: float = 0.0,
    positions: NumSeq | None = None,
    traded_notional: Decimal | float | None = None,
) -> dict[str, float]:
    """Рівно 17 метрик (ключі — METRIC_NAMES, у цьому порядку).

    equity — крива капіталу по періодах (Decimal або float); trades — ClosedTrade (поле pnl,
    для обороту — entry_notional/exit_notional) або просто числа PnL; positions — підписана позиція
    на кожній точці кривої (для exposure); traded_notional — Σ|номіналу| усіх виконань (інакше з trades).
    """
    if periods_per_year <= 0:
        raise ValueError("periods_per_year must be > 0")
    e = _equity_array(equity)
    if e.size == 0:
        raise ValueError("equity curve is empty")
    r = returns_from_equity(e)
    n = r.size
    growth = e[-1] / e[0] if e[0] > 0 else 0.0
    total_return = growth - 1.0
    if n == 0:
        cagr = 0.0
    elif growth <= 0:
        cagr = -1.0
    else:
        try:
            # exp(ln g · P/n) − 1: на коротких вибірках з великим P річна екстраполяція вибухає → inf
            cagr = math.expm1(math.log(growth) * periods_per_year / n)
        except OverflowError:
            cagr = math.inf
    ann_vol = np.std(r, ddof=1).item() * math.sqrt(periods_per_year) if n >= 2 else 0.0
    mdd = max_drawdown(e)

    pnl = _trade_pnls(trades)
    wins, losses = pnl[pnl > 0], pnl[pnl < 0]
    n_trades = int(pnl.size)
    win_rate = wins.size / n_trades if n_trades else 0.0
    avg_win = wins.mean().item() if wins.size else 0.0
    avg_loss = -losses.mean().item() if losses.size else 0.0

    notional = _num(traded_notional) if traded_notional is not None else _trades_notional(trades)
    mean_eq = e.mean().item()
    turnover = math.nan if notional is None or n == 0 else _ratio(notional, mean_eq) * periods_per_year / n

    if positions is None:
        exposure = math.nan
    else:
        pos = _to_float_array(positions)
        exposure = np.mean(pos != 0.0).item() if pos.size else 0.0

    if n:
        q95, q05 = np.percentile(r, [95.0, 5.0])
        tail = _ratio(abs(q95.item()), abs(q05.item()))
    else:
        tail = 0.0

    values = (
        total_return, cagr, ann_vol, sharpe_ratio(r, periods_per_year, rf),
        sortino_ratio(r, periods_per_year, rf), mdd, _ratio(cagr, mdd), ulcer_index(e),
        _ratio(wins.sum().item(), -losses.sum().item()),
        win_rate * avg_win - (1.0 - win_rate) * avg_loss,
        win_rate, avg_win, avg_loss, n_trades + 0.0, turnover, exposure, tail,
    )
    return dict(zip(METRIC_NAMES, (_py(v) for v in values), strict=True))


# ---------------------------------------------------------------- PSR / DSR


def psr(sr: float, n: int, skew: float = 0.0, kurt: float = 3.0, sr_star: float = 0.0) -> float:
    """Імовірнісний коефіцієнт Шарпа (Bailey & López de Prado, 2012). `sr` — Шарп за період."""
    if n < 2:
        raise ValueError("PSR needs at least 2 observations")
    if not (math.isfinite(sr) and math.isfinite(skew) and math.isfinite(kurt)):
        # ŜR = ±inf (нульова дисперсія доходностей) — PSR невизначений, а не NaN
        raise ValueError(f"PSR undefined for non-finite inputs (sr={sr}, skew={skew}, kurt={kurt})")
    var = 1.0 - skew * sr + (kurt - 1.0) / 4.0 * sr * sr
    if var <= 0:
        raise ValueError(f"non-positive PSR variance term {var} (skew={skew}, kurt={kurt}, sr={sr})")
    return _N.cdf((sr - sr_star) * math.sqrt(n - 1) / math.sqrt(var))


def psr_from_returns(r: Sequence[float] | NDArray[Any], sr_star: float = 0.0) -> float:
    """PSR з сирих доходностей: ŜR = mean/std(ddof=1), γ₃/γ₄ — популяційні."""
    arr = np.asarray(r, dtype=np.float64)
    sd = np.std(arr, ddof=1).item() if arr.size >= 2 else 0.0
    sr = _ratio(np.mean(arr).item(), sd) if arr.size else 0.0
    g3, g4 = moments(arr)
    return psr(sr, int(arr.size), g3, g4, sr_star)


def expected_max_sr(trial_srs: Sequence[float] | NDArray[Any]) -> float:
    """SR₀ = √Var(SR_i)·[(1−γ_E)·Φ⁻¹(1−1/N) + γ_E·Φ⁻¹(1−1/(N·e))]; N < 2 → 0 (немає множинного тестування)."""
    s = np.asarray(trial_srs, dtype=np.float64)
    n_trials = int(s.size)
    if n_trials < 2:
        return 0.0
    sd = math.sqrt(np.var(s, ddof=1).item())
    return sd * ((1.0 - EULER_GAMMA) * _N.inv_cdf(1.0 - 1.0 / n_trials)
                 + EULER_GAMMA * _N.inv_cdf(1.0 - 1.0 / (n_trials * math.e)))


def dsr(sr: float, n: int, skew: float, kurt: float, trial_srs: Sequence[float] | NDArray[Any]) -> float:
    """Дефльований коефіцієнт Шарпа: PSR з SR* = SR₀ за фактичними N прогонами сітки.

    Усі Шарпи (sr і trial_srs) — в одних одиницях: за період, не річні.
    """
    return psr(sr, n, skew, kurt, sr_star=expected_max_sr(trial_srs))
