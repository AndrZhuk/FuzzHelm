"""Наскрізне калібрування МФ на фікстурі: бари → конвеєр ознак → детектори → консенсус → KMeans/перцентилі
→ membership.yaml, який приймає нечітке ядро.

Найменування: tests/unit/test_calibration_pipeline.py
Призначення: детермінованість (той самий вхід → ті самі числа й той самий id калібрування), відсутність
    заглядання в майбутнє, коректність T-ряду (той самий код рішень), правила σ для V (покриття без мертвих
    зон), симетризовані T-точки і сумісність записаного YAML з fuzzy.load_membership / MamdaniEngine.
Автор: Андрій Жук, 2026.
"""

from __future__ import annotations

import math
import shutil
from itertools import pairwise
from pathlib import Path

import numpy as np
import orjson
import pytest
import yaml

from fuzzhelm import cli
from fuzzhelm.config import load_yaml
from fuzzhelm.decision.aggregator import consensus
from fuzzhelm.detectors.registry import build_detectors
from fuzzhelm.features.convert import Bar
from fuzzhelm.features.pipeline import FeatureParams, FeaturePipeline
from fuzzhelm.features.window import BarWindow
from fuzzhelm.fuzzy.mamdani import MamdaniEngine
from fuzzhelm.fuzzy.membership import load_membership
from fuzzhelm.fuzzy.rules import load_rulebase
from fuzzhelm.regimes.calibrate_mf import (
    T_PERCENTILES,
    calibrate,
    coverage_min,
    t_symmetric_breakpoints,
    v_sigmas,
    v_sigmas_cover,
    write_membership_yaml,
)

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "fixtures" / "golden" / "source_klines_btcusdt_1m.json"
SEED = 20260918
E_HALF = math.exp(-0.5)


@pytest.fixture(scope="module")
def bars() -> list[Bar]:
    return cli.load_fixture_bars(FIXTURE)[0]


@pytest.fixture(scope="module")
def cfg() -> dict:
    return load_yaml("detectors")


@pytest.fixture(scope="module")
def run(bars: list[Bar], cfg: dict) -> cli.CalibrationRun:
    return cli.run_calibration(bars, seed=SEED, detectors_cfg=cfg, dataset=_dataset(bars))


def _dataset(bars: list[Bar]) -> dict:
    return {"source": "fixture", "n": len(bars)}


def test_fixture_bars_are_real_ordered_klines(bars: list[Bar]) -> None:
    doc = orjson.loads(FIXTURE.read_bytes())
    assert len(bars) == len(doc["klines"]) == 2000
    assert all(b.t_ns - a.t_ns == 60_000_000_000 for a, b in pairwise(bars))
    assert bars[0].c == float(doc["klines"][0][4])


def test_series_masks_warmup_and_uses_the_decision_code_for_T(bars: list[Bar], cfg: dict,
                                                               run: cli.CalibrationRun) -> None:
    s = run.series
    pipe = FeaturePipeline(FeatureParams.from_config(cfg))
    warm = s.warmup_bars
    assert warm == pipe.max_lookback == 523                      # VolRegime — найдовший прогрів
    assert np.all(np.isnan(s.T[: warm - 1])) and np.all(np.isfinite(s.T[warm - 1:]))
    for arr in (s.vol_pct, s.ema_slope_norm, s.volume_z):
        assert np.all(np.isnan(arr[: warm - 1]))
    assert s.n_valid == len(bars) - warm + 1
    # незалежний прохід тим самим кодом рішень: T, R, V побітово ті самі
    det, win = build_detectors(cfg), BarWindow(8)
    for i, b in enumerate(bars):
        win.append(b, pipe.update(b))
        if i + 1 >= warm and i % 97 == 0:
            c = consensus([d.compute(win) for d in det])
            assert (s.T[i], s.R[i], s.V[i]) == (c.T, c.R, c.V)
    assert np.all(np.abs(s.T[warm - 1:]) <= 1.0) and np.all((s.V[warm - 1:] >= 0) & (s.V[warm - 1:] <= 1))
    # V консенсусу — це та сама ознака, на якій кластеризуємо (перцентиль σ_P)
    np.testing.assert_array_equal(s.V[warm - 1:], s.vol_pct[warm - 1:])


def test_series_is_causal_future_bars_do_not_change_the_past(bars: list[Bar], cfg: dict) -> None:
    full = cli.calibration_series(bars, cfg)
    part = cli.calibration_series(bars[:1500], cfg)
    np.testing.assert_array_equal(full.T[:1500], part.T)
    np.testing.assert_array_equal(full.vol_pct[:1500], part.vol_pct)
    with pytest.raises(ValueError, match="strictly increasing"):
        cli.calibration_series([bars[1], bars[0]], cfg)


def _close(a: object, b: object) -> bool:
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(_close(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_close(x, y) for x, y in zip(a, b, strict=True))
    if isinstance(a, float) and isinstance(b, float):
        return a == b or math.isclose(a, b, rel_tol=1e-12, abs_tol=1e-15)
    return a == b


def test_calibration_is_deterministic_and_id_tracks_data_and_seed(tmp_path: Path, bars: list[Bar], cfg: dict,
                                                                  run: cli.CalibrationRun) -> None:
    again = cli.run_calibration(bars, seed=SEED, detectors_cfg=cfg, dataset=_dataset(bars))
    assert again.run_id == run.run_id
    np.testing.assert_array_equal(again.series.T, run.series.T)          # T-ряд — побітово
    # KMeans (sklearn, OpenMP-редукції) відтворюється до ~1e−15 відносно, а не побітово (DATA-08);
    # записаний YAML (6 знаків) — побітово однаковий
    assert _close(again.result, run.result)
    a, b = tmp_path / "a.yaml", tmp_path / "b.yaml"
    for p, r in ((a, run), (b, again)):
        shutil.copy(ROOT / "config" / "membership.yaml", p)
        write_membership_yaml(r.result, p, source_run_id=r.run_id)
    assert a.read_bytes() == b.read_bytes()
    assert run.run_id.startswith("cal-") and len(run.run_id) == 4 + 16
    assert run.result["source_run_id"] == run.run_id
    other_seed = cli.manifest_id({**run.manifest, "seed": SEED + 1})
    other_data = cli.manifest_id({**run.manifest, "dataset": {"source": "fixture", "n": len(bars) - 1}})
    assert len({run.run_id, other_seed, other_data}) == 3


def test_result_uses_percentiles_kmeans_and_cover_sigmas(run: cli.CalibrationRun) -> None:
    r, s = run.result, run.series
    t = s.T[np.isfinite(s.T)]
    assert r["T"]["n"] == t.size and r["T"]["symmetric"] is True
    assert r["T"]["raw_breakpoints"] == [float(x) for x in np.percentile(t, T_PERCENTILES)]
    assert r["T"]["breakpoints"] == t_symmetric_breakpoints(t)
    V = r["V"]
    assert V["centres"] == sorted(V["centres"]) and all(0.0 <= m <= 1.0 for m in V["centres"])
    assert V["centres"] == [row[0] for row in r["centroids_k3"]]
    assert V["sigma_rule"] == "cover" and V["sigmas"] == pytest.approx(v_sigmas_cover(V["centres"]))
    assert V["sigmas_nearest"] == pytest.approx(v_sigmas(V["centres"]))
    assert V["coverage_min"] >= E_HALF - 1e-12
    assert set(r["silhouette_by_k"]) == {2, 3, 4, 5, 6} and r["n_samples"] == s.n_valid


def test_symmetric_t_breakpoints_are_exact_percentiles_of_t_and_minus_t() -> None:
    rng = np.random.default_rng(3)
    t = np.clip(rng.normal(0.1, 0.5, 4001), -1, 1)               # навмисно зсунутий розподіл
    bp = t_symmetric_breakpoints(t)
    assert bp == [-bp[4], -bp[3], 0.0, bp[3], bp[4]]              # побітова антисиметрія
    assert all(b > a for a, b in pairwise(bp))
    pooled = np.concatenate([t, -t])
    # від перцентилів вибірки T ∪ −T відрізняються лише схемою інтерполяції (скінченна вибірка)
    np.testing.assert_allclose(bp, np.percentile(pooled, T_PERCENTILES), atol=2e-3)
    assert np.percentile(t, 50) != pytest.approx(0.0, abs=1e-3)   # сирі — несиметричні


def test_cover_sigmas_guarantee_no_dead_zones_where_nearest_fails() -> None:
    rng = np.random.default_rng(7)
    worst = 1.0
    for _ in range(300):
        m = np.sort(rng.uniform(0.0, 1.0, 3))
        if np.min(np.diff(m)) < 1e-3:
            continue
        cov, _ = coverage_min(m, v_sigmas_cover(m), n=4001)
        worst = min(worst, cov)
    assert worst >= E_HALF - 1e-9                                  # теорема з докстрінга v_sigmas_cover
    # центри, отримані на реальному IS-вікні: «до найближчого» лишає мертву зону біля V = 1
    m = [0.211367, 0.546153, 0.765761]
    cov_near, at = coverage_min(m, v_sigmas(m))
    assert cov_near < 0.5 and at == 1.0
    assert coverage_min(m, v_sigmas_cover(m))[0] >= E_HALF - 1e-12
    assert v_sigmas_cover([0.17, 0.51, 0.88]) == pytest.approx([0.17, 0.185, 0.185])   # приклад брифінгу
    with pytest.raises(ValueError, match="sigma_rule"):
        calibrate([0.1] * 30, [0.0] * 30, [0.0] * 30, [0.0] * 30, seed=0, sigma_rule="widest")


def test_written_membership_loads_into_the_engine_and_keeps_partitions(tmp_path: Path,
                                                                       run: cli.CalibrationRun) -> None:
    dst = tmp_path / "membership.yaml"
    shutil.copy(ROOT / "config" / "membership.yaml", dst)
    write_membership_yaml(run.result, dst, source_run_id=run.run_id)
    doc = yaml.safe_load(dst.read_text(encoding="utf-8"))
    assert doc["variables"]["V"]["provisional"] is False
    assert doc["variables"]["T"]["source_run_id"] == run.run_id
    assert "<<TBD" not in dst.read_text(encoding="utf-8")
    mc = load_membership(dst)
    x = np.linspace(-1.0, 1.0, 2001)
    np.testing.assert_allclose(mc.T.evaluate(x).sum(axis=0), 1.0, atol=1e-12)      # Руспіні для T
    v = np.linspace(0.0, 1.0, 2001)
    assert mc.V.evaluate(v).max(axis=0).min() >= 0.5                               # без мертвих зон
    eng = MamdaniEngine(mc, load_rulebase(None, mc))
    u = eng.infer_u(0.4, -0.2, 0.3)
    assert -1.0 <= u <= 1.0
    # симетричні T-терми ⇒ непарна симетрія рушія зберігається
    for t, r, vv in [(0.4, -0.2, 0.3), (0.9, 0.1, 0.8), (-0.3, 0.6, 0.55)]:
        assert eng.infer_u(-t, -r, vv) == pytest.approx(-eng.infer_u(t, r, vv), abs=1e-12)


def test_cli_calibrate_from_fixture_writes_all_artifacts(tmp_path: Path, capsys: pytest.CaptureFixture[str],
                                                         run: cli.CalibrationRun) -> None:
    mem = tmp_path / "membership.yaml"
    shutil.copy(ROOT / "config" / "membership.yaml", mem)
    paths = {"--membership": mem, "--manifest": tmp_path / "m.json", "--report": tmp_path / "r.md",
             "--figure": tmp_path / "f.png"}
    argv = ["calibrate", "--from-fixture", str(FIXTURE), "--seed", str(SEED)]
    for k, v in paths.items():
        argv += [k, str(v)]
    assert cli.main(argv) == 0
    assert "calibration cal-" in capsys.readouterr().out
    manifest = orjson.loads(paths["--manifest"].read_bytes())
    written = yaml.safe_load(mem.read_text(encoding="utf-8"))
    assert manifest["id"] == written["variables"]["V"]["source_run_id"]
    assert manifest["dataset"]["sha256"] and manifest["result"]["V"]["sigma_rule"] == "cover"
    report = paths["--report"].read_text(encoding="utf-8")
    assert manifest["id"] in report and "Силует обирає" in report and "мертві зони" in report
    assert paths["--figure"].stat().st_size > 10_000
