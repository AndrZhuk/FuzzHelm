"""Чисті помічники аналітичних експериментів (backtest/experiments_analysis.py): пробої VaR, підмножини барів,
зведення фолдів, варіанти ablation, будівники таблиць, розбір брифінгу. Швидкі (< 1 с разом).

Автор: Андрій Жук, 2026.
"""

from __future__ import annotations

import json
import math
from decimal import Decimal
from pathlib import Path
from statistics import NormalDist
from typing import Any

import numpy as np
import pytest

from fuzzhelm.backtest import experiments_analysis as ea
from fuzzhelm.backtest.dataset import load_fixture_dataset
from fuzzhelm.backtest.metrics import dsr, expected_max_sr
from fuzzhelm.backtest.walkforward import make_folds
from fuzzhelm.config import ROOT
from fuzzhelm.detectors.registry import DETECTOR_NAMES
from fuzzhelm.risk.kupiec import kupiec_pof
from fuzzhelm.risk.var import historical_var_cvar, rolling_var_breaches

# ---------------------------------------------------------------- VaR: пробої, параметричний прогноз


def test_count_breaches_is_strict_and_shape_checked() -> None:
    realized = np.array([-0.02, -0.01, 0.0, 0.005])
    var = np.array([0.01, 0.01, 0.0, 0.0])
    assert ea.count_breaches(realized, var) == 1          # −0.01 < −0.01 — ні; 0 < −0 — ні
    with pytest.raises(ValueError):
        ea.count_breaches(realized, var[:3])


def test_rolling_parametric_var_matches_naive_one_step_ahead_loop() -> None:
    r = np.random.default_rng(7).standard_t(3, size=90) * 1e-3
    w, alpha = 20, 0.05
    got = ea.rolling_parametric_var(r, w, alpha, chunk=7)
    z = NormalDist().inv_cdf(1 - alpha)
    naive = np.array([z * np.std(r[t - w:t], ddof=1) for t in range(w, r.size)])
    np.testing.assert_allclose(got, naive, rtol=0, atol=1e-15)
    # та сама вирівнюваність, що й історичний прогноз risk.var: елемент j ↔ r[W + j]
    assert got.shape == rolling_var_breaches(r, w, alpha).var_series.shape
    assert ea.rolling_parametric_var(r[:w], w, alpha).size == 0


def test_active_mask_keeps_exposed_bars_and_intrabar_trades_only() -> None:
    # точки кривої: позиція на закритті; r_t — між точками t−1 і t
    pos = np.array([0, 0, 1, 1, 0, 0, 0, 0])
    r = np.array([0.0, 0.001, -0.002, 0.0005, 0.0, -0.0004, 0.0])
    #            flat  entry   held    exit   flat  вхід+стоп в одному барі  flat
    mask = ea.active_mask(r, pos)
    assert mask.tolist() == [False, True, True, True, False, True, False]
    with pytest.raises(ValueError):
        ea.active_mask(r, pos[:-1])


def test_var_block_breaches_match_naive_rolling_loops_and_kupiec() -> None:
    rng = np.random.default_rng(3)
    r = np.where(rng.random(400) < 0.8, 0.0, rng.normal(0, 1e-3, 400))     # багато нулів пласкої книги
    w, alpha = 50, 0.05
    b = ea.var_block(r, window=w, alpha=alpha)
    z = NormalDist().inv_cdf(1 - alpha)
    hist = sum(1 for t in range(w, r.size) if r[t] < -historical_var_cvar(r[t - w:t], alpha, window=None).var)
    par = sum(1 for t in range(w, r.size) if r[t] < -z * np.std(r[t - w:t], ddof=1))
    roll = b["rolling"]
    assert (roll["hist"]["breaches"], roll["param"]["breaches"]) == (hist, par)
    assert roll["hist"]["n"] == roll["param"]["n"] == r.size - w
    k = kupiec_pof(hist, r.size - w, p=alpha)
    assert roll["hist"]["lr"] == k.lr and roll["hist"]["reject"] is k.reject
    assert b["zero_share"] == pytest.approx(np.mean(r == 0))
    st = b["static"]
    assert st["hist_cvar"] >= st["hist_var"]                             # інваріант risk.var


def test_var_block_short_sample_reports_reason_instead_of_numbers() -> None:
    b = ea.var_block(np.linspace(-1e-3, 1e-3, 30), window=50)
    assert b["rolling"] is None and "window 50" in b["reason"]
    assert b["static"] is not None                                        # опис вибірки лишається
    assert ea.var_block(np.array([0.001]), window=5)["static"] is None


def test_fat_tail_conclusion_is_built_from_measured_numbers() -> None:
    r = np.random.default_rng(11).standard_t(2.5, size=3000) * 1e-3
    b = ea.var_block(r, window=500)
    text = " ".join(ea.fat_tail_conclusion(b, "тест"))
    assert "товщі" in text and str(b["n"]) in text and "пробоїв" in text
    assert f"{b['moments']['kurt']:.4g}" in text


# ---------------------------------------------------------------- зведення фолдів і воркер


def _fake(returns: list[float], pos: list[int], *, trades: int, notional: float,
          fees: float) -> dict[str, Any]:
    n = len(returns)
    return {"series": {"returns": np.array(returns), "position": np.array(pos, dtype=np.int8),
                       "state": np.zeros(n + 1, dtype=np.int8)},
            "n_eval_bars": n, "days": n / 1440,
            "metrics": {"sharpe": 1.0, "n_trades": trades, "turnover": math.nan},
            "costs": {"fees": fees, "funding": 0.0, "slippage_cost": 0.0, "gross_pnl": 0.0,
                      "n_fills": 2 * trades, "equity_start": 10_000.0, "traded_notional": notional},
            "extras": {}, "halted": False}


def test_summarize_chains_fold_returns_and_rescales_turnover_to_the_chain() -> None:
    a = _fake([0.01, -0.02], [0, 1, 0], trades=1, notional=5_000.0, fees=4.0)
    b = _fake([0.03], [0, 0], trades=2, notional=8_000.0, fees=6.0)
    s = ea.summarize([a, b], periods_per_year=100)
    assert s["total_return"] == pytest.approx(1.01 * 0.98 * 1.03 - 1)
    assert s["max_drawdown"] == pytest.approx(1 - 0.98)                  # пік 1.01 → 1.01·0.98
    assert s["n_trades"] == 3 and s["fees"] == 10.0
    # compute_metrics на зчепленій кривій: номінал фолду b перемасштабовано на капітал ланцюга на його старті
    curve = np.array([1.0, 1.01, 1.01 * 0.98, 1.01 * 0.98 * 1.03])
    chain_notional = 5_000.0 * 1.0 + 8_000.0 * curve[2]                    # у E₀ = 10 000: /10 000 нижче
    assert s["turnover"] == pytest.approx(chain_notional / (10_000.0 * curve.mean()) * 100 / 3)
    assert s["exposure"] == pytest.approx(1 / 5)
    assert s["fees_per_day_pct"] == pytest.approx(100 * 10 / (10_000 * 3 / 1440))


def test_segment_task_single_segment_summary_equals_engine_metrics() -> None:
    ds = load_fixture_dataset().slice(0, 900)
    cfg = ea.base_config()
    plan = ea.TaskPlan()
    plan.add(ds, ea.full_segment(len(ds)), cfg, 5, series=True, meta={"variant": "x"}, payloads={})
    (res,) = plan.run(0, 5)
    s, m = ea.summarize([res]), res["metrics"]
    assert s["sharpe"] == pytest.approx(m["sharpe"], rel=1e-12)
    assert s["total_return"] == pytest.approx(m["total_return"], rel=1e-9, abs=1e-12)
    assert s["max_drawdown"] == pytest.approx(m["max_drawdown"], rel=1e-9, abs=1e-12)
    assert s["turnover"] == pytest.approx(m["turnover"], rel=1e-9)             # та сама формула, що в рушії
    assert s["exposure"] == pytest.approx(m["exposure"], rel=1e-12)
    ser = res["series"]
    assert ser["returns"].size == res["n_eval_bars"] == ser["position"].size - 1 == ser["u_final"].size - 1
    # розворот — одна заявка на дві межі угод, тож виконань ≥ угод (а не 2·угод)
    assert res["costs"]["n_fills"] >= m["n_trades"] > 0 and res["costs"]["fees"] > 0
    assert res["config_hash"] == cfg.config_hash and res["dataset_hash"] == ds.dataset_hash
    assert sum(res["state_share"].values()) == pytest.approx(1.0)


# ---------------------------------------------------------------- сегменти й варіанти


def test_oos_segments_put_warmup_inside_embargo() -> None:
    folds = make_folds(5000, 2000, 600, 600, 1046, 3)
    segs = ea.oos_segments(folds, 523)
    for f, s in zip(folds, segs, strict=True):
        assert s.start == f.oos_start - 523 >= f.is_end and s.stop == f.oos_end and s.eval_start == 523
    with pytest.raises(ValueError, match="embargo"):
        ea.oos_segments(make_folds(5000, 2000, 600, 600, 100, 3), 523)
    assert [s.fold for s in ea.is_segments(folds, 523)] == [0, 1, 2]


def test_smoke_folds_fit_the_fixture() -> None:
    folds = ea.smoke_folds(3000, 523, 523)
    assert len(folds) == 2 and folds[-1].oos_end <= 3000
    assert all(s.start >= f.is_end for f, s in zip(folds, ea.oos_segments(folds, 523), strict=True))


def test_ablation_variants_change_exactly_one_component_each() -> None:
    base = ea.base_config()
    vs = ea.ablation_variants(base)
    assert [v.key for v in vs] == ["baseline", *[f"no_{d}" for d in DETECTOR_NAMES], "linear", "kappa_off",
                                   "hysteresis_off"]
    assert len({v.config.config_hash for v in vs}) == len(vs)
    assert len({v.config.resolved_warmup() for v in vs}) == 1                   # сегменти спільні
    by = {v.key: v for v in vs}
    for d in DETECTOR_NAMES:
        assert set(by[f"no_{d}"].config.detectors or ()) == set(DETECTOR_NAMES) - {d}
    assert "V ≡ 0.5" in by["no_vol_regime"].note
    assert by["linear"].config.engine == "linear"
    assert by["kappa_off"].config.resolved_trees()["detectors"]["agreement"]["kappa_min"] == 1.0
    assert by["hysteresis_off"].config.u_exit == by["hysteresis_off"].config.u_enter
    assert by["hysteresis_off"].tie_exit


def test_cell_config_ties_exit_to_cell_enter_when_hysteresis_is_off() -> None:
    v = ea.ablation_variants(ea.base_config())[-1]
    cfg = ea.cell_config(v, {"n_atr": 7, "chi": 1.5, "u_enter": 0.2, "rho_base": 0.0025, "lam": 0.94})
    assert cfg.u_enter == cfg.u_exit == 0.2
    cfg.risk_config()                                            # валідний тригер (exit ≤ enter)


def test_cost_and_hysteresis_variants_and_relaxed_loop() -> None:
    base = ea.base_config()
    assert [v.config.cost_mode for v in ea.cost_variants(base)] == ["zero", "sqrt_impact", "full"]
    w, wo = ea.hysteresis_variants(base)
    assert (w.config.u_exit, wo.config.u_exit) == (base.u_exit, base.u_enter)
    relaxed = ea.apply_risk_loop(base, "relaxed").risk_config()
    assert relaxed.state_machine.halt_enter > Decimal("0.9") and relaxed.limits.max_drawdown_halt.value > 0.9
    with pytest.raises(ValueError):
        ea.apply_risk_loop(base, "off")


def test_select_cells_is_even_and_order_preserving() -> None:
    assert len(ea.select_cells(None)) == 108
    two = ea.select_cells(2)
    assert two[0]["n_atr"] == 7 and two[1]["n_atr"] == 21 and two[1]["lam"] == 0.97


def test_parse_params_reads_json_file_with_choice_and_rejects_unknown(tmp_path: Path) -> None:
    f = tmp_path / "grid.json"
    f.write_text(json.dumps({"choice": {"params": {"n_atr": 7, "chi": 2, "u_enter": 0.3}}}), encoding="utf-8")
    assert ea.parse_params(f"@{f}") == {"n_atr": 7, "chi": 2.0, "u_enter": 0.3}
    assert ea.parse_params('{"cooldown_policy": "reduce_only"}') == {"cooldown_policy": "reduce_only"}
    assert ea.parse_params(None) == {}
    with pytest.raises(ValueError, match="unknown"):
        ea.parse_params('{"engine": "linear"}')


# ---------------------------------------------------------------- гістерезис


def test_band_stats_counts_band_share_and_threshold_crossings() -> None:
    u = np.array([np.nan, 0.05, 0.20, 0.26, 0.24, -0.30, 0.13, 0.11])
    s = ea.band_stats(u, 0.25, 0.12)
    assert s["n_decisions"] == 7
    assert s["share_in_band"] == pytest.approx(3 / 7)                       # 0.20, 0.24, 0.13
    assert s["share_above_enter"] == pytest.approx(2 / 7)
    assert s["enter_crossings"] == 4                                        # 0.20→0.26→0.24→−0.30→0.13


def test_churn_cost_reproduces_corrected_brief_arithmetic() -> None:
    assert ea.churn_cost_pct_per_day(0.0004, 2.0, 1.0) == pytest.approx(115.2)   # R-03, а не 1.15 %
    assert ea.churn_cost_pct_per_day(0.0004, 0.1, 0.5) == pytest.approx(115.2 / 40)


# ---------------------------------------------------------------- таблиці, JSON, порівняння


def test_fmt_and_md_table() -> None:
    assert [ea.fmt(x) for x in (None, math.nan, math.inf, -0.0, 12345, 0.5, True)] == [
        "—", "—", "∞", "0", "12 345", "0.5", "так"]
    t = ea.md_table(["a", "b"], [["x|y", 1.5]])
    assert t.splitlines() == ["| a | b |", "|---|---|", "| x\\|y | 1.5 |"]


def test_sanitize_gives_strict_json() -> None:
    obj = {"a": math.nan, "b": -math.inf, "c": np.float64(1.5), "d": np.arange(2), "e": Decimal("0.1")}
    s = ea.sanitize(obj)
    assert s == {"a": None, "b": "-inf", "c": 1.5, "d": [0, 1], "e": "0.1"}
    json.dumps(s, allow_nan=False)


def test_compare_reports_ratio_only_for_two_positive_values() -> None:
    assert ea.compare({"sharpe": 1.0}, {"sharpe": 3.0}, "sharpe")["ratio"] == 3.0
    neg = ea.compare({"sharpe": -2.0}, {"sharpe": 1.0}, "sharpe")
    assert math.isnan(neg["ratio"]) and neg["sign_flip"] and neg["diff"] == 3.0


def test_dsr_for_requires_run_moments_and_two_trials() -> None:
    m = {"sr_period": 0.01, "n_obs": 1000.0, "skew": 0.0, "kurt": 3.0}
    assert ea.dsr_for(m, [0.0])["dsr"] is None
    assert "lack" in ea.dsr_for({"sr_period": 0.01}, [0.0, 0.02])["reason"]
    ok = ea.dsr_for(m, [0.0, 0.02, -0.01])
    assert 0.0 <= ok["dsr"] <= 1.0 and ok["n_trials"] == 3


def test_write_outputs_writes_json_csv_md(tmp_path: Path) -> None:
    paths = ea.write_outputs(tmp_path, "x", result={"v": math.nan}, md="# t", rows=[{"a": 1}, {"b": [1]}])
    assert [p.name for p in paths] == ["x.json", "x.csv", "x.md"]
    assert json.loads((tmp_path / "x.json").read_text(encoding="utf-8")) == {"v": None}
    assert (tmp_path / "x.csv").read_text(encoding="utf-8").splitlines() == ["a,b", "1,", ",[1]"]


# ---------------------------------------------------------------- брифінг: групи тестів, трасування


def test_parse_brief_groups_a_to_n_with_verbatim_names() -> None:
    groups = ea.parse_brief_test_groups((ROOT / "docs" / "BRIEF.md").read_text(encoding="utf-8"))
    assert [g.letter for g in groups] == list("ABCDEFGHIJKLMN")
    by = {g.letter: g for g in groups}
    assert by["A"].declared == 3 and "test_no_wallclock_in_core" in by["A"].names
    assert "test_u_nondecreasing_in_trend_input" in by["G"].names and by["G"].declared == 17
    assert "test_kupiec_rejects_at_20_breaches_of_500" in by["J"].names
    assert all(len(set(g.names)) == len(g.names) for g in groups)


def test_test_group_rows_count_parametrized_items_and_file_affinity() -> None:
    groups = [ea.BriefGroup("A", "a", 2, ("test_x", "test_missing")), ea.BriefGroup("B", "b", 1, ("test_y",))]
    ids = ["tests/u/t1.py::test_x[1]", "tests/u/t1.py::test_x[2]", "tests/u/t1.py::test_extra",
           "tests/u/t2.py::TestC::test_y", "tests/u/t3.py::test_other"]
    a, b = ea.test_group_rows(groups, ids)
    got = (a["named_present"], a["named_items"], a["missing"], a["file_items"])
    assert got == (1, 2, ["test_missing"], 3)
    assert (b["named_present"], b["file_items"], b["files"]) == (1, 1, ["tests/u/t2.py"])


def test_extract_md_tables_by_heading_and_deviation_titles() -> None:
    text = "# T\n\n## A\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n## B розділ\n\n| c |\n|---|\n| 3 |\n"
    assert ea.extract_md_tables(text, "B розділ") == ["| c |\n|---|\n| 3 |"]
    assert len(ea.extract_md_tables(text)) == 2
    devs = ea.parse_deviation_titles("## X-01. Перше\n\n## Нотатка\n## RF-02a. Друге\n", "f.md")
    assert [(d["id"], d["title"]) for d in devs] == [("X-01", "Перше"), ("RF-02a", "Друге")]


def test_traceability_rows_cover_the_eleven_fragments_with_existing_modules() -> None:
    assert [r[0] for r in ea.TRACE_ROWS] == list(range(1, 12))
    assert set(ea.TRACE_BRIEF_CLAIMS) == set(range(1, 12))
    missing = [m for _, _, mods, _ in ea.TRACE_ROWS for m in mods if not (ROOT / m).exists()]
    assert missing == []


# ---------------------------------------------------------------- вибір на фронті, DSR, походження параметрів


def _cells(*rows: tuple[float, float, float]) -> list[dict[str, float]]:
    return [{"sharpe": sr, "max_drawdown": dd, "turnover": to} for sr, dd, to in rows]


def test_select_on_front_is_eps_constraint_not_argmax_sharpe() -> None:
    m = _cells((2.0, 0.10, 5.0),      # глобальний argmax SR, але MaxDD > cap
               (1.5, 0.05, 5.0),      # найкращий SR серед допустимих
               (1.5, 0.05, 3.0),      # той самий SR і DD, менший оборот → він
               (0.5, 0.20, 9.0))      # домінована
    ch = ea.select_on_front(m, dd_cap=0.08)
    assert ch["index"] == 2 and ch["rule"] == "eps_constraint"
    assert 3 not in ch["front"] and set(ch["feasible"]) == {2}          # №1 домінована №2 (менший оборот)
    assert ea.select_is_cell(m, "sharpe", 0.08)["index"] == 0          # runner.select_cell — argmax SR
    fb = ea.select_on_front(m, dd_cap=0.01)                   # жодна не вкладається → min MaxDD фронту
    assert fb["index"] == 2 and fb["rule"] == "fallback_min_dd"
    nan = ea.select_on_front(_cells((math.nan, 0.01, 1.0), (0.1, 0.02, 1.0)), dd_cap=0.08)
    assert nan["index"] == 1                                            # NaN-Шарп — найгірший
    with pytest.raises(ValueError):
        ea.select_is_cell(m, "median", 0.08)


def test_dsr_counts_every_trial_in_n_but_variance_over_finite_sharpes() -> None:
    fin = [0.01, -0.02, 0.005, 0.0]
    assert ea.sr0_expected_max(fin) == pytest.approx(expected_max_sr(fin), rel=1e-12)
    m = {"sr_period": 0.01, "n_obs": 5000.0, "skew": -0.5, "kurt": 6.0}
    assert ea.dsr_for(m, fin)["dsr"] == pytest.approx(dsr(0.01, 5000, -0.5, 6.0, fin), rel=1e-12)
    with_nan = ea.dsr_for(m, [*fin, None, math.nan])
    assert (with_nan["n_trials"], with_nan["n_finite"]) == (6, 4)
    assert with_nan["sr0"] > ea.sr0_expected_max(fin)                  # більше N → вищий поріг SR₀
    assert ea.dsr_for(m, [0.01, None])["dsr"] is None


def test_pick_trial_group_uses_one_grid_on_the_same_dataset() -> None:
    rows = [{"dataset_hash": "d", "git_sha": "old", "seed": 1, "engine": "mamdani", "sr_period": 0.1,
             "started_at": "2026-09-01"},
            {"dataset_hash": "d", "git_sha": "new", "seed": 1, "engine": "mamdani", "sr_period": 0.2,
             "started_at": "2026-09-20"},
            {"dataset_hash": "d", "git_sha": "new", "seed": 1, "engine": "mamdani", "sr_period": 0.3,
             "started_at": "2026-09-20"},
            {"dataset_hash": "d", "git_sha": "new", "seed": 1, "engine": "linear", "sr_period": 0.9,
             "started_at": "2026-09-21"},
            {"dataset_hash": "other", "git_sha": "new", "seed": 1, "engine": "mamdani", "sr_period": 9.0,
             "started_at": "2026-09-22"}]
    srs, g = ea.pick_trial_group(rows, "d", git_sha="old", engine="mamdani")
    assert srs == [0.1] and g is not None and g["groups"] == 3
    srs, g = ea.pick_trial_group(rows, "d", git_sha="unknown", engine="mamdani")
    assert srs == [0.2, 0.3] and g is not None and g["git_sha"] == "new"   # найновіша сітка того рушія
    assert ea.pick_trial_group(rows, "missing") == ([], None)


def test_params_source_flags_oos_selected_point_and_caveat(tmp_path: Path) -> None:
    f = tmp_path / "grid.json"
    f.write_text(json.dumps({"front_space": "oos", "selection_rule": "eps_constraint_max_sr",
                             "choice": {"index": 7, "rule": "eps_constraint", "dd_cap": 0.08,
                                        "params": {"n_atr": 21}}}), encoding="utf-8")
    src = ea.params_source(f"@{f}")
    assert src["selected_on_oos"] and src["choice_index"] == 7
    assert not ea.params_source('{"n_atr": 21}')["selected_on_oos"]
    assert not ea.params_source(None)["selected_on_oos"]
    assert "OOS" in " ".join(ea.selection_caveat({"selection": "fixed", "params_source": src}))
    assert ea.selection_caveat({"selection": "is_grid", "params_source": src}) == []


def test_first_decision_index_from_stored_n_obs() -> None:
    assert ea.first_decision_index(64_800, {"n_obs": 64_277.0}, 0) == (522, "run_metric.n_obs")
    idx, src = ea.first_decision_index(100, {}, 522)
    assert idx == 99 and "default" in src


def test_passport_separates_code_dirt_from_experiment_outputs() -> None:
    p = ea.passport("x", ["scripts/x.py", "--smoke"], seed=1)
    assert p["command"] == "uv run python scripts/x.py --smoke"
    assert {"git_sha", "git_dirty", "git_dirty_any", "git_dirty_paths"} <= set(p)
    assert not any(ln[3:].startswith(("artifacts/", "docs/")) for ln in p["git_dirty_paths"])


# ------------------------------------------------------------ evaluate_variants: маршрутизація без прогонів


def test_evaluate_variants_selects_on_is_only_and_runs_the_chosen_cell_out_of_sample(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """Вибір на IS (selection="is_grid"): IS-задачі бачать лише бари [is_start, is_end) свого фолду, клітинка
    обирається за правилом фронту лише з IS-метрик, OOS-задача фолду k біжить саме з обраною клітинкою і з
    прогрівом усередині embargo; повне вікно — з параметрами варіанта. Рушій підмінено: перевіряється
    маршрутизація задач, а не числа бектесту (їх перевіряють тести рушія)."""
    ds = load_fixture_dataset()
    base = ea.base_config()
    variants = [ea.Variant("a", "A", base), ea.Variant("b", "B", base.with_params(cost_mode="zero"))]
    folds = ea.smoke_folds(len(ds), base.feature_params().max_lookback, base.resolved_warmup())
    cells = ea.select_cells(3)
    t = ds.t_ns
    stages: list[list[dict[str, Any]]] = []

    def fake_run_parallel(fn: Any, tasks: list[dict[str, Any]], workers: int, *, seed: int) -> list[Any]:
        stages.append(tasks)
        out = []
        for task in tasks:
            m = task["meta"]
            # на IS фолду k єдина недомінована клітинка — k mod 3 (різні фолди → різний вибір)
            sharpe = 1.0 if m["kind"] == "is" and m["cell"] == m["fold"] % len(cells) else 0.0
            out.append({"meta": m, "metrics": {"sharpe": sharpe, "max_drawdown": 0.01, "turnover": 1.0},
                        "extras": {"n_obs": 1}})
        return out

    monkeypatch.setattr(ea, "run_parallel", fake_run_parallel)
    ev = ea.evaluate_variants(ds, variants, folds=folds, seed=5, scopes=("oos", "full"), selection="is_grid",
                              cells=cells, dd_cap=0.05)
    is_tasks, eval_tasks = stages
    assert len(is_tasks) == len(variants) * len(folds) * len(cells)
    for task in is_tasks:
        f = folds[task["meta"]["fold"]]
        ts = task["arrays"]["t_ns"]
        assert ts[0] == t[f.is_start] and ts[-1] == t[f.is_end - 1]      # лише IS, без embargo і OOS
    for v in variants:
        assert ev["chosen"][v.key] == [cells[k % len(cells)] for k in range(len(folds))]
        assert [c["rule"] for c in ev["choice_info"][v.key]] == ["eps_constraint"] * len(folds)
    warm = base.resolved_warmup()
    by_variant: dict[str, list[dict[str, Any]]] = {}
    for task in eval_tasks:
        by_variant.setdefault(task["meta"]["variant"], []).append(task)
    for v in variants:
        oos = [x for x in by_variant[v.key] if x["meta"]["kind"] == "oos"]
        [full] = [x for x in by_variant[v.key] if x["meta"]["kind"] == "full"]
        for task in oos:
            k = task["meta"]["fold"]
            f, cell = folds[k], cells[k % len(cells)]
            assert {p: task["config"][p] for p in cell} == cell           # OOS — обрана на IS клітинка
            assert task["config"]["cost_mode"] == v.config.cost_mode      # поверх конфігурації варіанта
            ts = task["arrays"]["t_ns"]
            assert ts[0] == t[f.oos_start - warm] >= t[f.is_end] and task["eval_start"] == warm
        assert full["config"] == v.config.to_dict()                       # повне вікно — без клітинки
        assert len(ev["results"][v.key]["oos"]) == len(folds) and len(ev["results"][v.key]["full"]) == 1
    with pytest.raises(ValueError, match="selection"):
        ea.evaluate_variants(ds, variants, folds=folds, seed=5, selection="oos_peek")
    with pytest.raises(ValueError, match="warm-up"):
        ea.evaluate_variants(ds, [variants[0], ea.Variant("w", "W", base.with_params(warmup_bars=600))],
                             folds=folds, seed=5)
