"""Режими ринку і калібрування МФ: KMeans + силует, V-центри, T-перцентилі, запис membership.yaml.

Найменування: tests/unit/test_regimes.py
Автор: Андрій Жук, 2026.
"""

from __future__ import annotations

import json
import shutil
from itertools import pairwise
from pathlib import Path

import numpy as np
import pytest
import yaml

from fuzzhelm.detectors.ema_slope import EmaSlope
from fuzzhelm.features.convert import Bar
from fuzzhelm.features.pipeline import FeaturePipeline, Features
from fuzzhelm.features.window import BarWindow
from fuzzhelm.regimes.calibrate_mf import (
    T_PERCENTILES,
    T_TERMS,
    V_TERMS,
    calibrate,
    features_to_arrays,
    v_sigmas,
    write_membership_yaml,
)
from fuzzhelm.regimes.cluster import fit_regimes

ROOT = Path(__file__).resolve().parents[2]
SEED = 20260918


def _blobs(centres: list[list[float]], n: int, sd: float, seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return np.vstack([np.asarray(c) + sd * rng.standard_normal((n, len(c))) for c in centres])


def _synthetic_inputs(seed: int = 0) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    rng = np.random.default_rng(seed)
    m = [0.15, 0.50, 0.85]
    vol = np.concatenate([np.clip(mi + 0.03 * rng.standard_normal(400), 0, 1) for mi in m])
    slope = np.concatenate([s + 0.1 * rng.standard_normal(400) for s in (-0.2, 0.0, 0.3)])
    vz = np.concatenate([z + 0.3 * rng.standard_normal(400) for z in (-0.5, 0.0, 1.5)])
    t = np.tanh(rng.standard_normal(3000) * 0.8)
    return vol, slope, vz, t


def _mf(spec: dict, x: np.ndarray) -> np.ndarray:
    a, b, c, d = spec["points"] if spec["type"] == "trap" else (*spec["points"][:2], *spec["points"][1:])
    up = np.where(b > a, (x - a) / (b - a if b > a else 1.0), 1.0)
    dn = np.where(d > c, (d - x) / (d - c if d > c else 1.0), 1.0)
    return np.clip(np.minimum(np.minimum(up, dn), 1.0), 0.0, 1.0) * ((x >= a) & (x <= d))


def test_fit_regimes_recovers_three_blobs_and_prefers_k3_by_silhouette() -> None:
    truth = [[0.15, -0.2, -0.5], [0.5, 0.0, 0.0], [0.85, 0.3, 1.5]]
    X = _blobs(truth, 300, 0.03, seed=1)
    res = fit_regimes(X, k_range=range(2, 7), seed=SEED)
    assert res.k == 3
    assert res.silhouette_by_k[3] == max(res.silhouette_by_k.values())
    assert set(res.silhouette_by_k) == {2, 3, 4, 5, 6}
    got = res.centroids[np.argsort(res.centroids[:, 0])]
    np.testing.assert_allclose(got, truth, atol=0.02)
    again = fit_regimes(X, k_range=range(2, 7), seed=SEED)          # детермінізм за seed
    assert again.silhouette_by_k == res.silhouette_by_k
    np.testing.assert_array_equal(again.centroids, res.centroids)
    with pytest.raises(ValueError):
        fit_regimes(np.array([[np.nan, 1.0], [0.0, 1.0], [1.0, 1.0]]), k_range=range(2, 3), seed=0)


def test_calibrate_v_centres_are_sorted_kmeans_centroids_with_half_gap_sigmas() -> None:
    vol, slope, vz, t = _synthetic_inputs()
    res = calibrate(vol, slope, vz, t, seed=SEED, run_id="unit")
    centres = res["V"]["centres"]
    assert centres == sorted(centres)
    np.testing.assert_allclose(centres, [0.15, 0.50, 0.85], atol=0.02)
    # незалежно: той самий KMeans(k=3), відсортований за першою координатою
    ref = fit_regimes(np.column_stack([vol, slope, vz]), k_range=(2, 3, 4, 5, 6), seed=SEED)
    np.testing.assert_allclose(centres, np.sort(ref.centroids_by_k[3][:, 0]), rtol=0, atol=1e-12)
    m = centres
    expected = [0.5 * (m[1] - m[0]), 0.5 * min(m[1] - m[0], m[2] - m[1]), 0.5 * (m[2] - m[1])]
    assert res["V"]["sigmas"] == pytest.approx(expected, abs=1e-15)
    assert list(res["V"]["terms"]) == list(V_TERMS)
    assert res["V"]["terms"]["MID"] == {"type": "gauss", "m": m[1], "sigma": expected[1]}
    sil = res["silhouette_by_k"]
    assert set(sil) == {2, 3, 4, 5, 6}
    assert res["k_best"] == max(sil, key=lambda k: (sil[k], -k))          # правило вибору k
    assert res["k_best_is_3"] == (res["k_best"] == 3)
    assert any("silhouette prefers" in w for w in res["warnings"]) == (res["k_best"] != 3)
    json.dumps(res)                                                   # JSON-сумісний
    assert v_sigmas([0.17, 0.51, 0.88]) == pytest.approx([0.17, 0.17, 0.185])   # приклад брифінгу §5.5


def test_calibrate_t_breakpoints_are_percentiles_and_partition_is_ruspini() -> None:
    vol, slope, vz, t = _synthetic_inputs(3)
    res = calibrate(vol, slope, vz, t, seed=SEED)
    bp = res["T"]["breakpoints"]
    assert bp == [float(x) for x in np.percentile(t, T_PERCENTILES)]
    assert all(b > a for a, b in pairwise(bp))
    terms = res["T"]["terms"]
    assert list(terms) == list(T_TERMS)
    assert terms["STRONG_DOWN"]["points"] == [-1.0, -1.0, bp[0], bp[1]]
    assert terms["NEUTRAL"]["points"] == [bp[1], bp[2], bp[3]]
    assert terms["STRONG_UP"]["points"] == [bp[3], bp[4], 1.0, 1.0]
    x = np.linspace(-1.0, 1.0, 2001)
    total = sum(_mf(spec, x) for spec in terms.values())
    np.testing.assert_allclose(total, 1.0, atol=1e-12)


def test_calibrate_reports_silhouette_preference_honestly() -> None:
    rng = np.random.default_rng(9)
    vol = np.concatenate([0.2 + 0.02 * rng.standard_normal(500), 0.8 + 0.02 * rng.standard_normal(500)])
    slope = 0.05 * rng.standard_normal(1000)
    vz = 0.1 * rng.standard_normal(1000)
    res = calibrate(vol, slope, vz, np.linspace(-0.9, 0.9, 101), seed=SEED)
    assert res["k_best"] == 2 and res["k_best_is_3"] is False
    assert any("silhouette prefers k=2" in w for w in res["warnings"])
    assert len(res["V"]["terms"]) == 3                               # V все одно має три терми


def test_calibrate_repairs_degenerate_t_distribution_and_drops_warmup_nans() -> None:
    vol, slope, vz, _ = _synthetic_inputs()
    vol = vol.copy()
    vol[:50] = np.nan                                                 # непрогріті рядки
    t = np.zeros(500)
    t[:10] = np.nan
    res = calibrate(vol, slope, vz, t, seed=SEED)
    bp = res["T"]["breakpoints"]
    assert all(b - a >= 1e-3 - 1e-15 for a, b in pairwise(bp))
    assert res["T"]["raw_breakpoints"] == [0.0] * 5
    assert res["T"]["n"] == 490 and res["n_samples"] == 1200 - 50
    assert any("adjusted" in w for w in res["warnings"])


def test_write_membership_yaml_sets_provisional_false_and_preserves_structure(tmp_path: Path) -> None:
    src = ROOT / "config" / "membership.yaml"
    dst = tmp_path / "membership.yaml"
    shutil.copy(src, dst)
    before = yaml.safe_load(src.read_text(encoding="utf-8"))
    vol, slope, vz, t = _synthetic_inputs()
    res = calibrate(vol, slope, vz, t, seed=SEED, run_id="run-2026-09-18-a")
    write_membership_yaml(res, dst)
    text = dst.read_text(encoding="utf-8")
    after = yaml.safe_load(text)
    v = after["variables"]["V"]
    assert v["provisional"] is False
    assert v["source_run_id"] == "run-2026-09-18-a" == after["variables"]["T"]["source_run_id"]
    assert v["silhouette"] == pytest.approx(res["V"]["silhouette"], abs=1e-6)
    for name in V_TERMS:
        assert v["terms"][name]["m"] == pytest.approx(res["V"]["terms"][name]["m"], abs=1e-6)
        assert v["terms"][name]["sigma"] == pytest.approx(res["V"]["terms"][name]["sigma"], abs=1e-6)
    for name in T_TERMS:
        np.testing.assert_allclose(after["variables"]["T"]["terms"][name]["points"],
                                   res["T"]["terms"][name]["points"], atol=1e-6)
    # схема та сама: ті самі ключі на всіх рівнях; R, U, defuzz — без змін
    assert set(after) == set(before)
    for var in ("T", "R", "V", "U"):
        assert set(after["variables"][var]) == set(before["variables"][var])
        assert list(after["variables"][var]["terms"]) == list(before["variables"][var]["terms"])
    for var in ("R", "U"):
        assert after["variables"][var] == before["variables"][var]
    assert after["defuzz"] == before["defuzz"]
    assert "# Силует за k:" in text and "provisional: false" in text
    assert not list(tmp_path.glob("*.tmp"))


def test_write_membership_yaml_keeps_hostile_run_id_and_warnings_inside_comments(tmp_path: Path) -> None:
    dst = tmp_path / "membership.yaml"
    shutil.copy(ROOT / "config" / "membership.yaml", dst)
    vol, slope, vz, t = _synthetic_inputs()
    res = calibrate(vol, slope, vz, t, seed=SEED)
    res["warnings"] = [*res["warnings"], "line one\ninjected_key: 1", "x\nbroken: [unclosed"]
    res["silhouette_by_k"] = {**res["silhouette_by_k"], 6: float("nan")}     # вироджений k — лише коментар
    run_id = "yes\nversion: 99"
    write_membership_yaml(res, dst, source_run_id=run_id)
    after = yaml.safe_load(dst.read_text(encoding="utf-8"))
    assert set(after) == {"version", "variables", "defuzz"}                 # нічого не «витекло» з коментарів
    assert after["version"] == 3
    assert after["variables"]["V"]["provisional"] is False
    assert after["variables"]["V"]["source_run_id"] == run_id == after["variables"]["T"]["source_run_id"]
    write_membership_yaml(res, dst, source_run_id="yes")                    # YAML-булеве слово → рядок
    assert yaml.safe_load(dst.read_text(encoding="utf-8"))["variables"]["V"]["source_run_id"] == "yes"


def test_features_to_arrays_slope_equals_ema_slope_g() -> None:
    doc = json.loads((ROOT / "fixtures" / "golden" / "source_klines_btcusdt_1m.json").read_text())
    bars = [Bar(t_ns=int(r[0]), o=float(r[1]), h=float(r[2]), l=float(r[3]), c=float(r[4]), v=float(r[5]))
            for r in doc["klines"][:700]]
    pipe = FeaturePipeline()
    w = BarWindow(4)
    det = EmaSlope()
    feats = []
    gs = []
    for b in bars:
        f = pipe.update(b)
        w.append(b, f)
        feats.append(f)
        gs.append(det.compute(w).features.get("g", np.nan))
    vol, slope, vz = features_to_arrays(feats)
    ok = np.isfinite(slope)
    assert ok.sum() == 700 - 25
    np.testing.assert_allclose(slope[ok], np.asarray(gs)[ok], rtol=1e-12)
    assert np.isnan(vol[:522]).all() and np.isfinite(vol[522:]).all()
    assert np.all((vol[522:] > 0) & (vol[522:] <= 1))
    assert np.isfinite(vz[59:]).all()
    # вироджений ATR = 0 (пласкі бари): та сама підлога знаменника, що й у EmaSlope
    f0 = Features(ema=100.0, ema_lag=99.9, atr=0.0, ema_r2=1.0)
    w0 = BarWindow(1)
    w0.append(bars[0], f0)
    _, s0, _ = features_to_arrays([f0])
    assert s0[0] == det.compute(w0).features["g"]
