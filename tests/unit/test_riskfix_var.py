"""RF-03: ковзні VaR₉₅/CVaR₉₅ у точках кривої капіталу (вихід рушія і рядки equity_point), W = 500, §5.13.

Найменування: tests/unit/test_riskfix_var.py
Автор: Андрій Жук, 2026.

Конвенція: VaR_t = E_t · VaR̂₉₅(r_{t−499..t}) у грошах (додатне = збиток), нижній емпіричний квантиль
risk.var; None, поки дохідностей < 500; пакетний (рушій) і покроковий (live) шляхи дають однакові числа.
"""

from __future__ import annotations

from decimal import Decimal
from functools import cache
from uuid import UUID

import numpy as np
import pytest
from tests.helpers.engine_scripted import base_config, fixture

from fuzzhelm.backtest.engine import BacktestResult, run_backtest
from fuzzhelm.risk.var import (
    RollingVarCvar,
    historical_var_cvar,
    returns_from_equity,
    rolling_var_cvar,
    var_cvar_money,
)
from fuzzhelm.workers.persist import VAR_MIN_OBS, VAR_WINDOW, RollingVar, plan_backtest

D = Decimal
SEED = 20260918


@cache
def fixture_run() -> BacktestResult:
    """3000 реальних барів BTCUSDT, дефолтна стратегія, трасування угод (є точки кривої)."""
    return run_backtest(fixture(), base_config().with_params(record_traces="trades"), seed=SEED)


def _curve(n: int, seed: int, vol: float = 0.002) -> list[Decimal]:
    rng = np.random.default_rng(seed)
    e, out = D("10000"), [D("10000")]
    for z in rng.standard_normal(n - 1):
        e = (e * (D(1) + D(f"{z * vol:.12f}"))).quantize(D("1E-9"))
        out.append(e)
    return out


def test_engine_equity_points_carry_money_var_cvar_from_the_500th_return() -> None:
    res = fixture_run()
    pts = res.equity_points
    assert len(pts) == len(fixture()) == 3000 and len(res.trades) > 0
    assert (VAR_WINDOW, VAR_MIN_OBS) == (500, 500)
    assert all(p.var95 is None and p.cvar95 is None for p in pts[:500])        # < 500 дохідностей
    defined = pts[500:]
    assert all(p.var95 is not None and p.cvar95 is not None for p in defined)
    # на цій кривій (реальні бари, стоп-лосси, пласкі ділянки): CVaR ≥ VaR ≥ 0 у кожній точці
    assert all(p.cvar95 >= p.var95 >= 0 for p in defined)                      # type: ignore[operator]
    assert any(p.var95 > 0 for p in defined)                                   # type: ignore[operator]
    # гроші = E_t · (оцінка risk.var на вікні r_{t−499..t}, що закінчується на t включно)
    r = returns_from_equity([p.equity for p in pts])
    for t in (500, 501, 1234, 2047, 2999):
        ref = historical_var_cvar(r[t - 500:t], 0.05, window=None)
        v, c, e = pts[t].var95, pts[t].cvar95, pts[t].equity
        assert v is not None and c is not None
        assert v / e == D(repr(ref.var))                          # VaR — сама порядкова статистика
        assert abs((c / e) - D(repr(ref.cvar))) <= D("1e-15") * max(D(1), abs(D(repr(ref.cvar))))


def test_persisted_rows_equal_engine_output_and_live_path_gives_identical_numbers() -> None:
    res = fixture_run()
    plan = plan_backtest(res, run_id=UUID(int=7), instrument_id=1)
    assert [(p.var95, p.cvar95) for p in plan.equity] == [(p.var95, p.cvar95) for p in res.equity_points]
    live = RollingVar()
    assert [live.update(p.equity) for p in res.equity_points] == \
        [(p.var95, p.cvar95) for p in res.equity_points]
    # явне інше вікно — перерахунок, а не числа рушія
    other = plan_backtest(res, run_id=UUID(int=7), instrument_id=1, var_window=250, var_min_obs=250)
    assert other.equity[250].var95 is not None and plan.equity[250].var95 is None


@pytest.mark.parametrize(("n", "chunk", "min_obs"), [(1200, 4096, None), (1200, 7, None), (900, 64, 20),
                                                     (499, 4096, None), (501, 1, None)])
def test_rolling_var_cvar_batch_and_incremental_are_identical(n: int, chunk: int,
                                                              min_obs: int | None) -> None:
    eq = _curve(n, seed=n + chunk)
    var, cvar = rolling_var_cvar(returns_from_equity(eq), 500, 0.05, min_obs=min_obs, chunk=chunk)
    lo = 500 if min_obs is None else min_obs
    assert var.shape == cvar.shape == (n,)
    assert np.isnan(var[:lo]).all() and np.isnan(cvar[:lo]).all()
    assert not np.isnan(var[lo:]).any() and (cvar[lo:] >= var[lo:]).all()   # CVaR ≥ VaR — тотожність, у float
    money_v, money_c = var_cvar_money(eq, min_obs=min_obs)
    step = RollingVarCvar(min_obs=min_obs)
    for t, e in enumerate(eq):
        got = step.update(e)
        assert got == (money_v[t], money_c[t])                                # покроково ≡ пакетно, до біта
        if t < lo:
            assert got == (None, None)
        else:
            assert got[0] == D(repr(var[t].item())) * e and got[1] == D(repr(cvar[t].item())) * e


def test_var_is_not_clipped_on_an_all_gain_window() -> None:
    """R-04: VaR ≥ 0 — не тотожність. На вікні з самих виграшів квантиль збитку від'ємний; у equity_point
    пишеться як є (не 0), тотожністю лишається CVaR ≥ VaR."""
    eq = [D(10_000) + D(i) for i in range(620)]                                # щобару +1 USDT
    var, cvar = var_cvar_money(eq)
    assert var[499] is None and var[500] is not None and cvar[500] is not None
    assert all(v is not None and c is not None and c >= v and v < 0 for v, c in zip(var[500:], cvar[500:],
                                                                                    strict=True))


def test_rolling_var_arguments_are_validated() -> None:
    r = np.zeros(10)
    for bad in (lambda: rolling_var_cvar(r, 500, 0.05, min_obs=600),     # min_obs > W — не має сенсу
                lambda: rolling_var_cvar(r, 0),
                lambda: rolling_var_cvar(r, 500, 1.0),
                lambda: rolling_var_cvar(r, 500, 0.05, chunk=0),
                lambda: rolling_var_cvar(np.array([np.nan])),
                lambda: RollingVarCvar(500, 0.05, min_obs=501),
                lambda: RollingVarCvar(500, 0.0)):
        with pytest.raises(ValueError):
            bad()
    var, cvar = rolling_var_cvar([], 500)
    assert var.shape == (1,) and np.isnan(var[0]) and np.isnan(cvar[0])
    assert var_cvar_money([]) == ([], [])
