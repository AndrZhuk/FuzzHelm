"""K/L. Аналітика бектесту: walk-forward з embargo, Парето-фронт, сітка 108, паспорт прогону,
паралелізм і Амдал."""

from __future__ import annotations

import math
import os
from decimal import Decimal

import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from fuzzhelm.backtest.grid import DEFAULT_SPACE, load_grid_space, make_grid
from fuzzhelm.backtest.manifest import (
    RunManifest,
    build_manifest,
    config_hash,
    dataset_hash,
    equity_hash,
    read_git_sha,
)
from fuzzhelm.backtest.parallel import (
    amdahl_bound,
    amdahl_report,
    fit_serial_fraction,
    karp_flatt,
    measure_speedup,
    run_parallel,
    task_seed,
    worker_probe,
)
from fuzzhelm.backtest.pareto import dominates, pareto_front
from fuzzhelm.backtest.walkforward import folds_from_profile, make_folds
from fuzzhelm.core.enums import EngineKind, RunKind

DAY = 1440


# ============================================================ walk-forward


@pytest.mark.parametrize("k", [6])
@pytest.mark.parametrize("embargo", [0, 60, 120, 240])
def test_walkforward_embargo_no_overlap(k: int, embargo: int) -> None:
    n = 45 * DAY
    folds = make_folds(n, 15 * DAY, 5 * DAY, 5 * DAY, embargo, k)
    assert len(folds) == k
    for f in folds:
        is_t, emb_t, oos_t = set(f.is_range), set(f.embargo_range), set(f.oos_range)
        assert (is_t | emb_t).isdisjoint(oos_t)                     # (IS ∪ E) ∩ OOS = ∅
        assert max(f.is_range) + embargo <= min(f.oos_range)         # max(IS.t) + E ≤ min(OOS.t)
        assert len(emb_t) == embargo and is_t.isdisjoint(emb_t)
        assert len(oos_t) == 5 * DAY and len(is_t) == 15 * DAY - embargo
        assert f.is_start >= 0 and f.oos_end <= n
    # OOS-вікна не перекриваються і суцільно вкривають останні 30 днів (15 + 6·5 = 45)
    assert [(f.oos_start, f.oos_end) for f in folds] == [
        (15 * DAY + 5 * DAY * i, 20 * DAY + 5 * DAY * i) for i in range(k)
    ]


def test_walkforward_profile_and_validation() -> None:
    folds = folds_from_profile(45 * DAY, max_lookback=60)            # embargo null → 2·max_lookback
    assert len(folds) == 6 and all(f.embargo_bars == 120 for f in folds)
    assert folds[-1].oos_end == 45 * DAY
    end = make_folds(50 * DAY, 15 * DAY, 5 * DAY, 5 * DAY, 10, 6, anchor="end")
    assert end[-1].oos_end == 50 * DAY and end[0].is_start == 5 * DAY
    with pytest.raises(ValueError, match="need"):
        make_folds(44 * DAY, 15 * DAY, 5 * DAY, 5 * DAY, 0, 6)
    with pytest.raises(ValueError, match="shorter"):
        make_folds(45 * DAY, 100, 5, 5, 100, 1)


# ============================================================ Парето


def test_pareto_front_contains_only_nondominated() -> None:
    # (SR ↑, MaxDD ↓, Turnover ↓)
    pts = [
        (1.0, 0.10, 5.0),      # 0: фронт
        (0.8, 0.10, 5.0),      # 1: домінована точкою 0
        (0.5, 0.05, 5.0),      # 2: фронт (найменша просадка серед SR ≥ 0.5)
        (1.0, 0.10, 5.0),      # 3: дубль 0 — однакові точки одна одну не домінують
        (0.4, 0.06, 9.0),      # 4: домінована точкою 2
        (float("nan"), 0.01, 1.0),   # 5: NaN SR — найгірший, але найкращі DD і оборот → фронт
        (1.2, 0.30, 20.0),     # 6: фронт (найбільший SR)
    ]
    front = pareto_front(pts)
    assert front == [0, 2, 3, 5, 6]
    assert dominates(pts[0], pts[1]) and not dominates(pts[0], pts[3])


@settings(max_examples=100)
@given(st.lists(st.tuples(st.floats(-3, 3), st.floats(0, 1), st.floats(0, 50)), min_size=1, max_size=40))
def test_pareto_front_property(points: list[tuple[float, float, float]]) -> None:
    front = pareto_front(points)
    assert front, "непорожня множина має непорожній фронт"
    for i in front:                              # жодна точка фронту не домінована
        assert not any(dominates(p, points[i]) for p in points)
    for j in set(range(len(points))) - set(front):    # кожна інша домінована кимось із фронту
        assert any(dominates(points[i], points[j]) for i in front)


# ============================================================ сітка


def test_grid_has_108_cells_in_deterministic_order() -> None:
    g1, g2 = make_grid(), make_grid()
    assert len(g1) == 3 * 3 * 3 * 2 * 2 == 108
    assert g1 == g2
    assert len({tuple(sorted(c.items())) for c in g1}) == 108          # усі різні
    assert g1[0] == {"n_atr": 7, "chi": 1.5, "u_enter": 0.20, "rho_base": 0.0025, "lam": 0.94}
    assert g1[1]["lam"] == 0.97                                        # остання вісь — найшвидша
    assert g1[-1] == {"n_atr": 21, "chi": 2.5, "u_enter": 0.30, "rho_base": 0.005, "lam": 0.97}
    defaults = {"n_atr": 14, "chi": 2.0, "u_enter": 0.25, "rho_base": 0.005, "lam": 0.94}
    assert defaults in g1                                              # дефолти — одна з клітинок
    assert load_grid_space() == DEFAULT_SPACE                          # grid.yaml простору не задає
    assert make_grid({"b": [1, 2], "a": ["x"]}) == [{"b": 1, "a": "x"}, {"b": 2, "a": "x"}]
    with pytest.raises(ValueError):
        make_grid({"a": []})


# ============================================================ паспорт прогону


def test_config_hash_canonical_and_sensitive() -> None:
    a = {"k_s": 0.5, "nested": {"x": 1, "y": [0.1, 2]}, "mode": "full"}
    b = {"mode": "full", "nested": {"y": [Decimal("0.1"), 2], "x": 1}, "k_s": Decimal("0.5")}
    assert config_hash(a) == config_hash(b)            # порядок ключів і float↔Decimal не впливають
    assert config_hash(a) != config_hash({**a, "k_s": 0.6})
    assert len(config_hash(a)) == 64


def test_dataset_hash_canonical_form() -> None:
    t = np.arange(5, dtype=np.int64) * 60_000
    c = np.array([1.0, 2.0, -0.0, 4.0, 5.0])
    h = dataset_hash({"t_ns": t, "c": c})
    assert h == dataset_hash({"c": c.copy(), "t_ns": t.astype(np.int32)})    # порядок колонок і ширина int
    assert h == dataset_hash({"t_ns": t, "c": np.abs(c)})                    # −0.0 ≡ 0.0
    c2 = c.copy()
    c2[3] = np.nextafter(4.0, 5.0)                                           # зміна в останньому біті
    assert h != dataset_hash({"t_ns": t, "c": c2})
    with pytest.raises(ValueError):
        dataset_hash({"c": np.array([1.0, np.nan])})


def test_equity_hash_value_based() -> None:
    e1 = [Decimal("10000"), Decimal("10001.5")]
    e2 = [Decimal("10000.00"), Decimal("10001.500")]
    assert equity_hash(e1) == equity_hash(e2)          # однакові значення з різним експонентом
    assert equity_hash(e1) != equity_hash([Decimal("10000"), Decimal("10001.4")])
    assert equity_hash(e1, [1, 2]) != equity_hash(e1, [1, 3])
    assert len(equity_hash(e1)) == 64


def test_build_manifest_identity() -> None:
    cols = {"t_ns": np.arange(3, dtype=np.int64), "c": np.array([1.0, 2.0, 3.0])}
    m = build_manifest(kind="backtest", engine="mamdani", seed=42, config={"a": 1}, dataset=cols,
                       journal_head=bytes(32), equity=[Decimal(1), Decimal(2)], git=False)
    assert isinstance(m, RunManifest)
    assert m.kind == RunKind.BACKTEST and m.engine == EngineKind.MAMDANI
    assert m.journal_head_hash == "00" * 32 and m.git_sha is None
    assert m.identity() == (config_hash({"a": 1}), dataset_hash(cols), 42, "mamdani", None)
    sha, dirty = read_git_sha()
    if sha is not None:                                # у робочому репозиторії є коміт
        assert len(sha) == 40 and all(ch in "0123456789abcdef" for ch in sha)
        assert isinstance(dirty, bool)


# ============================================================ паралелізм і Амдал


def test_run_parallel_order_and_context_independent_of_worker_count() -> None:
    tasks = list(range(6))
    serial = run_parallel(worker_probe, tasks, workers=0, seed=11)
    pooled = run_parallel(worker_probe, tasks, workers=2, seed=11)
    assert [r["task"] for r in serial] == tasks == [r["task"] for r in pooled]     # порядок задач
    assert all(r["prec"] == 38 and r["rounding"] == "ROUND_HALF_EVEN" for r in serial + pooled)
    # навіть глобальний ГВЧ дає ті самі значення: зерно — від (seed, індекс задачі), а не від воркера
    assert [r["rand"] for r in serial] == [r["rand"] for r in pooled]
    assert all(r["pid"] != os.getpid() for r in pooled)
    assert task_seed(11, 0) != task_seed(11, 1) and task_seed(11, 0) == task_seed(11, 0)


def test_amdahl_fit_recovers_known_serial_fraction() -> None:
    f_true, t1 = 0.12, 100.0
    times = {p: t1 * (f_true + (1 - f_true) / p) for p in (1, 2, 4, 8)}   # синтетичний ідеальний Амдал
    rep = amdahl_report(times)
    assert rep.serial_fraction == pytest.approx(f_true, abs=1e-6)
    for p in (2, 4, 8):
        assert rep.speedup[p] == pytest.approx(amdahl_bound(p, f_true), rel=1e-12)
        assert rep.karp_flatt[p] == pytest.approx(f_true, rel=1e-9)
    assert amdahl_bound(8, 0.0) == 8.0 and amdahl_bound(8, 1.0) == 1.0
    # зашумлені виміри: НК-оцінка лежить між мінімальним і максимальним e(p) Карпа–Флатта
    noisy = {1: 100.0, 2: 57.0, 4: 36.0, 8: 25.0}
    f = fit_serial_fraction([1, 2, 4, 8], [100 / noisy[p] for p in (1, 2, 4, 8)])
    kf = [karp_flatt(p, 100 / noisy[p]) for p in (2, 4, 8)]
    assert min(kf) - 1e-6 <= f <= max(kf) + 1e-6
    with pytest.raises(ValueError):
        karp_flatt(1, 1.0)


def test_measure_speedup_uses_injected_timer() -> None:
    ticks = iter([0.0, 10.0, 10.0, 15.0])
    rep = measure_speedup(abs, [], workers=(1, 2), timer=lambda: next(ticks))
    assert rep.times == {1: 10.0, 2: 5.0} and rep.speedup[2] == 2.0
    assert rep.serial_fraction == pytest.approx(0.0, abs=1e-6)
    assert math.isclose(rep.bound[2], 2.0, rel_tol=1e-5)
