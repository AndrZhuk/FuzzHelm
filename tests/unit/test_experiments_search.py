"""Фаза 7 (exp_search): правило вибору з фронту Парето, DSR з фактичним N, підгонка Амдала, чутливість,
конкатенація OOS — чисті функції backtest.experiments_search (швидкі; прогін рушія — лише в тесті `slow`)."""

from __future__ import annotations

import math
import subprocess
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from fuzzhelm.backtest import experiments_search as xs
from fuzzhelm.backtest.dataset import load_fixture_dataset
from fuzzhelm.backtest.engine import BacktestConfig
from fuzzhelm.backtest.experiments_search import (
    SENS_NAMES,
    SensParam,
    amdahl_fit,
    apply_param,
    base_param_values,
    chain_equity,
    choose_from_front,
    commit_refusal,
    concat_oos_metrics,
    deflated_sharpe_report,
    fmt,
    git_state,
    json_safe,
    oos_segment,
    overhead_breakdown,
    sensitivity_space,
    significance_statement_uk,
    smoke_folds,
    sr0_expected_max,
    subset_indices,
    tornado_rows,
    walkforward_search,
)
from fuzzhelm.backtest.metrics import expected_max_sr
from fuzzhelm.backtest.parallel import amdahl_bound
from fuzzhelm.backtest.runner import run_cell


def _m(sr: float, dd: float, to: float) -> dict[str, float]:
    return {"sharpe": sr, "max_drawdown": dd, "turnover": to}


# ---------------------------------------------------------------- правило вибору з фронту


def test_front_rule_picks_max_sharpe_within_dd_cap_not_global_argmax() -> None:
    cells = [
        _m(3.0, 0.10, 100.0),   # 0: argmax SR, але MaxDD > cap
        _m(2.0, 0.05, 200.0),   # 1: домінований клітинкою 2 (той самий SR і DD, більший оборот)
        _m(2.0, 0.05, 150.0),   # 2: найкращий SR серед допустимих
        _m(1.0, 0.02, 50.0),    # 3: фронт (найменші DD і оборот)
        _m(0.5, 0.06, 300.0),   # 4: домінований клітинкою 2
    ]
    ch = choose_from_front(cells, dd_cap=0.08)
    assert ch.front == (0, 2, 3)
    assert ch.feasible == (2, 3)
    assert ch.index == 2 and ch.rule == "eps_constraint"
    assert max(range(len(cells)), key=lambda i: cells[i]["sharpe"]) == 0   # argmax SR обрав би іншу


def test_front_rule_tie_on_sharpe_goes_to_lower_turnover_then_index() -> None:
    cells = [_m(2.0, 0.03, 200.0), _m(2.0, 0.05, 100.0), _m(1.0, 0.01, 10.0)]
    ch = choose_from_front(cells, dd_cap=0.08)
    assert ch.front == (0, 1, 2) and ch.index == 1
    same = [_m(2.0, 0.03, 100.0), _m(2.0, 0.03, 100.0)]          # однакові точки — обидві на фронті
    assert choose_from_front(same, dd_cap=0.08).index == 0


def test_front_rule_falls_back_to_min_drawdown_when_nothing_fits_the_cap() -> None:
    cells = [_m(-1.0, 0.12, 50.0), _m(-3.0, 0.09, 80.0), _m(-2.0, 0.11, 10.0)]
    ch = choose_from_front(cells, dd_cap=0.08)
    assert ch.feasible == () and ch.rule == "fallback_min_dd" and ch.index == 1


def test_front_rule_nan_is_the_worst_value_and_bad_inputs_raise() -> None:
    cells = [_m(math.nan, 0.01, 1.0), _m(-5.0, 0.02, 2.0)]
    assert choose_from_front(cells, dd_cap=0.08).index == 1
    with pytest.raises(ValueError):
        choose_from_front([], dd_cap=0.08)
    with pytest.raises(ValueError):
        choose_from_front(cells, dd_cap=0.0)


# ---------------------------------------------------------------- PSR / DSR з фактичним N


# Ручний розрахунок (N = 5): mean = 0.002; Σ(x − mean)² = 0.00148; Var(ddof=1) = 0.00037; sd = 0.0192353841;
# Φ⁻¹(1 − 1/5) = 0.8416212336; Φ⁻¹(1 − 1/(5e)) = 1.4496656592;
# SR₀ = sd·(0.4228·0.8416212336 + 0.5772·1.4496656592) = 0.0229398204.
# Обрана точка: ŜR = 0.03 за період, n = 1000, γ₃ = −0.5, γ₄ = 6 → знаменник √(1 + 0.015 + 1.25·0.0009);
# DSR = Φ((0.03 − SR₀)·√999/знам.) = Φ(0.2213731) = 0.5875990; PSR = Φ(0.03·√999/знам.) = 0.8265592.
TRIALS = [-0.02, -0.01, 0.0, 0.01, 0.03]


def test_dsr_from_list_of_sharpes_matches_hand_computed_value() -> None:
    assert sr0_expected_max(TRIALS) == pytest.approx(0.022939820422976844, abs=1e-12)
    assert sr0_expected_max(TRIALS) == pytest.approx(expected_max_sr(TRIALS), abs=1e-15)
    rep = deflated_sharpe_report(0.03, 1000, -0.5, 6.0, TRIALS)
    assert rep["n_trials"] == 5 and rep["var_sr"] == pytest.approx(0.00037, abs=1e-15)
    assert rep["dsr"] == pytest.approx(0.5875990472361626, abs=1e-9)
    assert rep["psr"] == pytest.approx(0.8265591911991893, abs=1e-9)
    assert rep["significant"] is False and rep["dsr_significant"] is False
    assert "НЕ встановлена" in significance_statement_uk(rep, what="OOS")


def test_dsr_counts_every_evaluated_cell_in_n_but_variance_only_over_finite_sharpes() -> None:
    with_nan = [*TRIALS, math.nan]
    rep = deflated_sharpe_report(0.03, 1000, -0.5, 6.0, with_nan)
    assert rep["n_trials"] == 6 and rep["n_finite"] == 5
    assert rep["var_sr"] == pytest.approx(0.00037, abs=1e-15)
    # більше випробувань → вищий SR₀ → менший DSR
    assert rep["sr0"] > sr0_expected_max(TRIALS) and rep["dsr"] < 0.5875990472361626
    assert sr0_expected_max([0.1]) == 0.0


# ---------------------------------------------------------------- Амдал


def test_amdahl_fit_recovers_known_serial_fraction_from_synthetic_speedups() -> None:
    f = 0.12
    times = {p: 100.0 * (f + (1.0 - f) / p) for p in (1, 2, 4, 8)}
    fit = amdahl_fit(times)
    assert fit["serial_fraction"] == pytest.approx(f, abs=1e-6)
    assert fit["karp_flatt"][8] == pytest.approx(f, abs=1e-9)
    assert fit["speedup"][4] == pytest.approx(amdahl_bound(4, f), abs=1e-12)
    assert fit["limit_speedup"] == pytest.approx(1.0 / f, rel=1e-5)
    noisy = {p: t * (1.0 + 0.01 * (-1) ** p) for p, t in times.items()}
    assert amdahl_fit(noisy)["serial_fraction"] == pytest.approx(f, abs=0.02)


def test_overhead_breakdown_components() -> None:
    ob = overhead_breakdown(p=2, wall_s=10.0, startup_s=1.0, task_walls=[3.0, 3.0, 2.0, 1.0],
                            task_pids=[11, 12, 11, 12], pickle_s_per_task=0.01, compute_s_p1=8.0)
    assert ob["compute_s"] == 9.0 and ob["ideal_parallel_s"] == 4.5
    assert ob["makespan_s"] == 5.0 and ob["imbalance_s"] == 0.5
    assert ob["residual_s"] == pytest.approx(4.0) and ob["pickle_serial_s"] == pytest.approx(0.04)
    assert ob["cpu_inflation"] == pytest.approx(9.0 / 8.0) and ob["processes_used"] == 2.0


# ---------------------------------------------------------------- конкатенація OOS


def test_chain_equity_splices_returns_and_rescales_turnover() -> None:
    seg1 = np.array([100.0, 110.0, 99.0])
    seg2 = np.array([50.0, 55.0])                  # свіжий старт: склеюється доходність +10 %, не рівень
    eq, starts = chain_equity([seg1, seg2])
    assert eq.tolist() == pytest.approx([100.0, 110.0, 99.0, 108.9])
    assert starts == pytest.approx([100.0, 99.0])
    segs = [{"equity": seg1, "traded_notional": 1000.0, "n_trades": 2.0},
            {"equity": seg2, "traded_notional": 500.0, "n_trades": 1.0}]
    m = concat_oos_metrics(segs, periods_per_year=3.0)
    # номінал сегмента 2 у масштабі ланцюга: 500·99/50 = 990
    assert m["turnover"] == pytest.approx((1000.0 + 990.0) / eq.mean() * 3.0 / 3.0)
    assert m["total_return"] == pytest.approx(0.089)
    assert m["n_trades"] == 3.0 and m["n_segments"] == 2.0 and m["n_obs"] == 3.0
    assert m["max_drawdown"] == pytest.approx(0.1)


# ---------------------------------------------------------------- чутливість


@pytest.fixture(scope="module")
def cfg() -> BacktestConfig:
    return BacktestConfig()


def test_sensitivity_space_has_eight_admissible_parameters_around_defaults(cfg: BacktestConfig) -> None:
    base = base_param_values(cfg)
    assert base == pytest.approx({"u_enter": 0.25, "u_exit": 0.12, "chi": 2.0, "rho_base": 0.005, "lam": 0.94,
                                  "sigma_target": 0.20, "kappa_min": 0.35, "n_atr": 14.0})
    space = {sp.name: sp for sp in sensitivity_space(cfg)}
    assert tuple(space) == SENS_NAMES and len(space) == 8
    assert space["n_atr"].values == (7.0, 10.0, 18.0, 21.0)
    assert space["lam"].values == (0.88, 0.92, 0.952, 0.96)            # пам'ять 1/(1−λ) ± 50 %
    assert space["u_enter"].values == pytest.approx((0.2, 0.225, 0.275, 0.3))
    for sp in space.values():
        rel = [abs(v / sp.base - 1.0) for v in sp.values]
        assert max(rel) <= 0.5 + 1e-9 and sp.base not in sp.values
    assert max(space["u_exit"].values) < base["u_enter"]
    with pytest.raises(ValueError):
        sensitivity_space(cfg, names=["u_exit"], spans={"u_exit": 1.5})   # u_exit ≥ u_enter
    with pytest.raises(ValueError):
        sensitivity_space(cfg, names=["nope"])


def test_apply_param_changes_exactly_one_parameter_and_the_config_hash(cfg: BacktestConfig) -> None:
    for name, value in (("sigma_target", 0.3), ("kappa_min", 0.2), ("n_atr", 21.0), ("chi", 2.5)):
        c2 = apply_param(cfg, name, value)
        got = base_param_values(c2)
        assert got[name] == pytest.approx(value)
        assert all(got[k] == pytest.approx(v) for k, v in base_param_values(cfg).items() if k != name)
        assert c2.config_hash != cfg.config_hash
    assert apply_param(cfg, "sigma_target", 0.3).risk_config().sizing.sigma_target == pytest.approx(0.3)
    assert isinstance(apply_param(cfg, "n_atr", 7.0).n_atr, int)


def test_tornado_rows_sorted_by_swing_with_deltas() -> None:
    space = [SensParam("a", "a", "", "", 1.0, (0.5, 1.5), 0.5, ""),
             SensParam("b", "b", "", "", 2.0, (1.0, 3.0), 0.5, ""),
             SensParam("c", "c", "", "", 3.0, (2.0, 4.0), 0.3, "")]
    base = _m(1.0, 0.05, 100.0)
    runs = [{"param": "a", "value": 0.5, "metrics": _m(0.9, 0.05, 100.0)},
            {"param": "a", "value": 1.5, "metrics": _m(1.2, 0.06, 90.0)},
            {"param": "b", "value": 1.0, "metrics": _m(-1.0, 0.02, 50.0)},
            {"param": "b", "value": 3.0, "metrics": _m(2.0, 0.09, 150.0)},
            {"param": "c", "value": 2.0, "metrics": _m(math.nan, 0.05, 100.0)},
            {"param": "c", "value": 4.0, "metrics": _m(1.0, 0.05, 100.0)}]
    rows = tornado_rows(base, runs, space)
    assert [r["param"] for r in rows] == ["b", "a", "c"]           # розмах SR: 3.0, 0.3, NaN (у кінці)
    b = rows[0]
    assert (b["low_value"], b["high_value"]) == (1.0, 3.0)
    assert b["sharpe_d_low"] == -2.0 and b["sharpe_d_high"] == 1.0 and b["sharpe_swing"] == 3.0
    assert b["max_drawdown_swing"] == pytest.approx(0.07)
    assert b["sharpe_elasticity"] == pytest.approx((3.0 / 1.0) / (2.0 / 2.0))
    assert math.isnan(rows[2]["sharpe_swing"])


def test_tornado_elasticity_of_lambda_is_taken_in_ewma_memory() -> None:
    lam = SensParam("lam", "λ", "", "", 0.94, (0.88, 0.96), 0.5, "")
    runs = [{"param": "lam", "value": 0.88, "metrics": _m(0.5, 0.05, 100.0)},
            {"param": "lam", "value": 0.96, "metrics": _m(1.5, 0.05, 100.0)}]
    row = tornado_rows(_m(1.0, 0.05, 100.0), runs, [lam])[0]
    mem = [1.0 / (1.0 - v) for v in (0.88, 0.96, 0.94)]               # пам'ять 8.33, 25, 16.67 (±50 %)
    assert row["sharpe_elasticity"] == pytest.approx(1.0 / ((mem[1] - mem[0]) / mem[2]))   # = 1.0, не ≈ 12


# ---------------------------------------------------------------- оркестрація walk-forward без рушія


def test_walkforward_search_selects_on_is_only_by_the_front_rule_and_warms_oos_inside_embargo(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """Підмінений run_parallel (рушій не запускається): IS-задачі бачать лише бари IS свого фолду, вибір —
    правилом фронту за IS-метриками (не argmax SR), OOS-задача — обрана клітинка з прогрівом W усередині
    embargo; OOS-результати на вибір не впливають (у них найкраща інша клітинка)."""
    ds = load_fixture_dataset()
    cfg = BacktestConfig.from_profile("backtest")
    w = cfg.resolved_warmup()
    folds = smoke_folds(len(ds), embargo_bars=2 * cfg.feature_params().max_lookback, warmup=w)
    # 0: argmax SR, але DD > cap; 1: правило ε-обмеження; 2: найкраща на OOS
    cells = [{"n_atr": 7, "chi": 1.5, "u_enter": 0.2, "rho_base": 0.005, "lam": 0.94},
             {"n_atr": 14, "chi": 2.0, "u_enter": 0.25, "rho_base": 0.005, "lam": 0.94},
             {"n_atr": 21, "chi": 2.5, "u_enter": 0.3, "rho_base": 0.0025, "lam": 0.97}]
    is_m = [_m(3.0, 0.20, 100.0), _m(1.0, 0.05, 100.0), _m(0.5, 0.02, 50.0)]
    oos_m = [_m(-1.0, 0.10, 100.0), _m(-0.5, 0.05, 100.0), _m(9.0, 0.01, 10.0)]
    calls: list[list[dict[str, Any]]] = []

    def fake_run_parallel(fn: Any, tasks: Any, workers: int, *, seed: int = 0) -> list[dict[str, Any]]:
        tasks = list(tasks)
        calls.append(tasks)
        out = []
        for t in tasks:
            i = cells.index(t["params"])
            if t["keep_equity"]:
                n = len(t["arrays"]["t_ns"]) - t["eval_start"]
                out.append({**oos_m[i], "equity": np.linspace(100.0, 99.0, n), "traded_notional": 10.0,
                            "n_trades": 1.0, "equity_ts": t["arrays"]["t_ns"][t["eval_start"]:],
                            "config_hash": "x", "equity_hash": "y"})
            else:
                out.append({**is_m[i], "config_hash": "x"})
        return out

    monkeypatch.setattr(xs, "run_parallel", fake_run_parallel)
    wf = walkforward_search(ds, cfg, folds=folds, cells=cells, seed=1, workers=2, dd_cap=0.08)
    assert len(calls) == 2                                       # спершу вся IS-сітка, потім OOS обраних
    is_tasks, oos_tasks = calls
    t = ds.t_ns
    assert len(is_tasks) == len(folds) * len(cells)
    for k, f in enumerate(folds):
        for task in is_tasks[k * len(cells):(k + 1) * len(cells)]:
            ts = task["arrays"]["t_ns"]
            assert ts[0] == t[f.is_start] and ts[-1] == t[f.is_end - 1]      # лише IS, без embargo і OOS
            assert task["eval_start"] == 0 and not task["keep_equity"]
        o = oos_tasks[k]
        ts = o["arrays"]["t_ns"]
        assert ts[0] == t[f.oos_start - w] and ts[0] >= t[f.is_end]        # прогрів — лише бари embargo
        assert ts[-1] == t[f.oos_end - 1] and o["eval_start"] == w
        assert o["params"] == cells[1]                            # ε-обмеження, а не argmax SR (0) чи OOS (2)
        fs = wf.folds[k]
        assert fs.choice.index == 1 and fs.choice.rule == "eps_constraint" and fs.choice.front == (0, 1, 2)
        assert fs.oos_run_start == f.oos_start - w and fs.oos_eval_start == w
    assert wf.concat_equity.size == 1 + sum(fs.oos_equity.size - 1 for fs in wf.folds)


# ---------------------------------------------------------------- git: паспорти лише із закоміченого коду


def test_git_state_ignores_outputs_and_docs_but_not_code_and_commit_is_refused_when_dirty(
        tmp_path: Path) -> None:
    def git(*a: str) -> None:
        subprocess.run(["git", "-c", "user.email=t@example.com", "-c", "user.name=t",
                        "-c", "commit.gpgsign=false", *a], cwd=tmp_path, check=True, capture_output=True)

    git("init", "-q")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "m.py").write_text("x = 1\n", encoding="utf-8")
    git("add", ".")
    git("commit", "-q", "-m", "init")
    assert git_state(tmp_path)["dirty"] is False
    (tmp_path / "artifacts" / "exp_search").mkdir(parents=True)          # вивід попереднього скрипта
    (tmp_path / "artifacts" / "exp_search" / "grid.json").write_text("{}", encoding="utf-8")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "note.md").write_text("n", encoding="utf-8")
    gs = git_state(tmp_path)
    assert gs["dirty_any"] is True and gs["dirty"] is False and len(gs["sha"]) == 40
    assert commit_refusal("commit", gs, allow_dirty=False) is None
    (tmp_path / "src" / "m.py").write_text("x = 2\n", encoding="utf-8")   # незакомічена зміна коду
    gs = git_state(tmp_path)
    assert gs["dirty"] is True and any("src/m.py" in ln for ln in gs["dirty_paths"])
    assert "uncommitted" in (commit_refusal("commit", gs, allow_dirty=False) or "")
    assert commit_refusal("commit", gs, allow_dirty=True) is None
    assert commit_refusal("dry-run", gs, allow_dirty=False) is None
    assert commit_refusal("commit", {**gs, "sha": None}, allow_dirty=False) is not None


# ---------------------------------------------------------------- дрібниці


def test_subset_and_smoke_folds_and_formatting() -> None:
    assert subset_indices(108, 4) == [0, 36, 71, 107]
    assert subset_indices(108, None) == list(range(108)) and subset_indices(5, 1) == [0]
    folds = smoke_folds(3000, embargo_bars=1046, warmup=523)
    assert len(folds) == 2 and folds[-1].oos_end <= 3000
    for f in folds:
        start, ev = oos_segment(f, 523)
        assert start >= f.is_end and ev == 523 and f.is_end - f.is_start > 523
    with pytest.raises(ValueError):
        smoke_folds(1500, embargo_bars=1046, warmup=523)
    assert json_safe({"a": [math.nan, math.inf, np.float64(1.5)], 2: np.int64(3)}) == {
        "a": ["NaN", "Infinity", 1.5], "2": 3}
    assert fmt(math.nan) == "—" and fmt(-math.inf) == "−∞" and fmt(0.12) == "0.12" and fmt(64800) == "64 800"


@pytest.mark.slow
def test_walkforward_search_end_to_end_on_fixture_matches_runner_cells() -> None:
    ds = load_fixture_dataset()
    c = BacktestConfig.from_profile("backtest")
    folds = smoke_folds(len(ds), embargo_bars=2 * c.feature_params().max_lookback, warmup=c.resolved_warmup())
    cells = [{"n_atr": 14, "chi": 2.0, "u_enter": 0.2, "rho_base": 0.005, "lam": 0.94},
             {"n_atr": 7, "chi": 2.5, "u_enter": 0.3, "rho_base": 0.0025, "lam": 0.97}]
    wf = walkforward_search(ds, c, folds=folds, cells=cells, seed=7, workers=0, dd_cap=0.08)
    f0 = wf.folds[0]
    ref = run_cell(cells[1], ds.slice(f0.fold.is_start, f0.fold.is_end).to_payload(), 7, config=c.to_dict())
    assert f0.is_metrics[1]["sharpe"] == ref["sharpe"] and f0.is_metrics[1]["turnover"] == ref["turnover"]
    assert wf.concat_equity.size == 1 + sum(fs.oos_equity.size - 1 for fs in wf.folds)
    assert all(fs.oos_equity.size == fs.fold.oos_end - fs.fold.oos_start for fs in wf.folds)
    assert wf.concat_ts.size == wf.concat_equity.size == wf.concat_fold.size
