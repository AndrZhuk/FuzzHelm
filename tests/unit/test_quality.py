"""Група D брифінгу (§10), «Якість даних»: інваріанти свічки, AHP-ваги, скор Q, MLP-автокодувальник.

Найменування: tests/unit/test_quality.py
Призначення: перевірити інваріанти на СПРАВЖНІХ даних (fixtures/rest/binance_klines.json.gz — 3000
закритих 1m-барів BTCUSDT), формули §5.17 — на контрольних значеннях, AHP — на матриці з
config/dq_weights.yaml. Property-тест test_dq_score_in_unit_interval — у tests/property/.
Автор: Андрій Жук, 2026.
"""

from __future__ import annotations

import gzip
import math
import re
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any

import numpy as np
import orjson
import pytest
import yaml
from tests.helpers.quality_models import fixture_bars, trained_autoencoder

from fuzzhelm.core.dto import BookLevel, BookSnapshot, Candle, Instrument, Trade
from fuzzhelm.core.enums import Src, Venue
from fuzzhelm.features.convert import Bar, bar_from_candle
from fuzzhelm.ingest.normalize import kline_uid, normalize_exchange_info, normalize_rest_klines, trade_uid
from fuzzhelm.quality import invariants as inv
from fuzzhelm.quality.ahp import SAATY_RI, ahp_weights, load_matrix, write_weights_yaml
from fuzzhelm.quality.anomaly_eval import (
    DEFAULT_MAGNITUDES,
    evaluate_injections,
    feature_drift,
    plan_injections,
    structurally_valid,
)
from fuzzhelm.quality.anomaly_mlp import (
    ANOMALY_KINDS,
    FEATURE_NAMES,
    AnomalyAutoencoder,
    AnomalyFeatureExtractor,
    AnomalyScorer,
    feature_matrix,
    inject_anomaly,
)
from fuzzhelm.quality.dq_score import (
    DqAccumulator,
    DqInputs,
    completeness,
    continuity,
    dq_score,
    load_dq_weights,
    merged_length_ns,
    timeliness,
    validity,
)
from fuzzhelm.quality.health import PipelineHealth

ROOT = Path(__file__).resolve().parents[2]
REST = ROOT / "fixtures" / "rest"
ROWS: list[list[Any]] = orjson.loads(gzip.decompress((REST / "binance_klines.json.gz").read_bytes()))
NOW_MS = ROWS[-1][6] + 1_000
INSTR: Instrument = normalize_exchange_info(orjson.loads((REST / "exchange_info.json").read_bytes()),
                                            ["BTCUSDT"])["BTCUSDT"]
CANDLES = normalize_rest_klines(ROWS, INSTR, NOW_MS * 1_000_000, server_time_ms=NOW_MS)
BARS: list[Bar] = [bar_from_candle(c) for c in CANDLES]
SEED = 20260918
MIN_NS = 60_000_000_000


def _candle(**kw: Any) -> Candle:
    base: dict[str, Any] = dict(
        instrument="BTC-USDT-PERP", venue=Venue.BINANCE_USDM, tf="1m",
        open_time_ns=ROWS[0][0] * 1_000_000, close_time_ns=ROWS[0][0] * 1_000_000 + MIN_NS - 1_000_000,
        o=Decimal("81015.40"), h=Decimal("81061.10"), l=Decimal("81015.40"), c=Decimal("81058.70"),
        volume=Decimal("75.128"), quote_volume=Decimal("6088272.65380"), trades_count=1791,
        is_closed=True, src=Src.WS, ts_event_ns=ROWS[0][6] * 1_000_000, ts_ingest_ns=ROWS[0][6] * 1_000_000,
        event_uid=kline_uid(Venue.BINANCE_USDM, "BTC-USDT-PERP", "1m", ROWS[0][0] * 1_000_000))
    base.update(kw)
    return Candle(**base)


# ================================================================ інваріанти


def test_high_below_close_rejected() -> None:
    good = _candle()
    assert inv.check_candle(good, INSTR) == []
    bad_fields = good.model_dump() | {"h": Decimal("81050.00"), "c": Decimal("81058.70")}   # h < c
    # шар 1: канонічний DTO не дає навіть сконструювати таку свічку (ті самі CHECK, що в таблиці candle)
    with pytest.raises(ValueError, match="inconsistent OHLC"):
        Candle(**bad_fields)
    # шар 2: подія, що обійшла валідацію (рядок БД / model_construct), ловиться інваріантами якості
    bypass = Candle.model_construct(**bad_fields)
    violations = inv.check_candle(bypass, INSTR)
    assert inv.HIGH_BELOW_MAX_OC in violations
    assert inv.LOW_ABOVE_MIN_OC not in violations           # l = 81015.40 ≤ min(o, c) — не порушено
    assert not inv.is_valid_candle(bypass, INSTR)
    # h нижче за close, але вище за open: саме close видає порушення (контроль, що перевірка — max(o,c))
    only_close = Candle.model_construct(**(good.model_dump() | {"h": Decimal("81058.60")}))
    assert inv.check_candle(only_close, INSTR) == [inv.HIGH_BELOW_MAX_OC]


def test_price_not_multiple_of_tick_rejected() -> None:
    assert INSTR.tick_size == Decimal("0.10")
    off = _candle(o=Decimal("81015.45"), l=Decimal("81015.40"))       # 81015.45 / 0.10 не ціле
    assert off.o == Decimal("81015.45")                                # DTO це пропускає: tick не його знання
    assert inv.check_candle(off, INSTR) == [f"{inv.PRICE_OFF_TICK}:o"]
    # усі чотири ціни перевіряються незалежно; обсяг — на кратність step_size = 0.001
    many = _candle(h=Decimal("81061.11"), c=Decimal("81058.75"), volume=Decimal("75.1285"),
                   quote_volume=Decimal("6088272.65380"))
    assert set(inv.check_candle(many, INSTR)) == {f"{inv.PRICE_OFF_TICK}:h", f"{inv.PRICE_OFF_TICK}:c",
                                                  f"{inv.QTY_OFF_STEP}:volume"}
    # без специфікації інструмента кратність не перевіряється (лише структурні інваріанти)
    assert inv.check_candle(off, None) == []
    # і та сама перевірка для угод
    t = Trade(instrument="BTC-USDT-PERP", venue=Venue.BINANCE_USDM, agg_id=1, price=Decimal("81015.45"),
              qty=Decimal("0.005"), is_buyer_maker=True, ts_event_ns=1, ts_ingest_ns=2,
              event_uid=trade_uid(Venue.BINANCE_USDM, "BTC-USDT-PERP", 1))
    assert inv.check_trade(t, INSTR) == [f"{inv.PRICE_OFF_TICK}:price"]


def test_real_klines_satisfy_all_invariants() -> None:
    """Негативний контроль: 3000 справжніх барів Binance не дають жодного хибного порушення."""
    assert len(CANDLES) == 3000
    assert [c.open_time_ns for c in CANDLES if inv.check_candle(c, INSTR)] == []


def test_quote_volume_bounds_and_time_grid() -> None:
    c = _candle()
    # qv ∉ [v·l, v·h] — неможливо для суми pᵢ·qᵢ з pᵢ ∈ [l, h]
    bad_qv = Candle.model_construct(**(c.model_dump() | {"quote_volume": Decimal("1")}))
    assert inv.QUOTE_VOLUME_OUT_OF_RANGE in inv.check_candle(bad_qv)
    shifted = Candle.model_construct(**(c.model_dump() | {"open_time_ns": c.open_time_ns + 1}))
    assert {inv.OPEN_TIME_UNALIGNED, inv.CLOSE_TIME_MISMATCH} <= set(inv.check_candle(shifted))


def test_book_invariants() -> None:
    lv = BookLevel
    ok = BookSnapshot(instrument="BTC-USDT-PERP", venue=Venue.BINANCE_USDM,
                      bids=(lv(price=Decimal("100.0"), qty=Decimal(1)),
                            lv(price=Decimal("99.9"), qty=Decimal(1))),
                      asks=(lv(price=Decimal("100.1"), qty=Decimal(1)),), last_update_id=1, ts_event_ns=1,
                      ts_ingest_ns=1, event_uid="x")
    assert inv.check_book(ok, INSTR) == []
    crossed = ok.model_copy(update={"asks": (lv(price=Decimal("100.0"), qty=Decimal(1)),)})
    assert inv.BOOK_CROSSED in inv.check_book(crossed, INSTR)


# ================================================================ AHP


def test_ahp_weights_sum_to_one_and_cr_below_0_1() -> None:
    m = load_matrix()
    res = ahp_weights(m)
    assert res.n == 4 and res.ri == SAATY_RI[4] == 0.90
    assert math.isclose(sum(res.weights), 1.0, abs_tol=1e-12)
    assert all(w > 0 for w in res.weights)
    assert res.cr < 0.1 and res.consistent
    # визначення: A·w = λ_max·w (власний вектор), CI = (λ−n)/(n−1), CR = CI/RI
    a = np.asarray(m)
    np.testing.assert_allclose(a @ np.asarray(res.weights), res.lambda_max * np.asarray(res.weights),
                               rtol=1e-10)
    assert math.isclose(res.ci, (res.lambda_max - 4) / 3, rel_tol=1e-12)
    assert math.isclose(res.cr, res.ci / 0.90, rel_tol=1e-12)
    # незалежний метод (степенева ітерація) дає той самий вектор
    alt = ahp_weights(m, method="power")
    np.testing.assert_allclose(alt.weights, res.weights, atol=1e-12)
    assert math.isclose(alt.lambda_max, res.lambda_max, rel_tol=1e-10)
    # рядки 3 і 4 матриці однакові ⇒ w₃ = w₄ за побудовою; очікування брифінгу (…, 0.20, 0.18) з цією
    # матрицею недосяжне — фактичні ваги вписані в config/dq_weights.yaml (див. deviations.d)
    assert math.isclose(res.weights[2], res.weights[3], rel_tol=1e-12)
    assert res.weights[0] > res.weights[1] > res.weights[2]
    cfg = yaml.safe_load((ROOT / "config" / "dq_weights.yaml").read_text(encoding="utf-8"))
    np.testing.assert_allclose(cfg["weights"], res.weights, atol=5e-7)
    assert math.isclose(cfg["consistency_ratio"], res.cr, abs_tol=5e-7)


def test_ahp_consistent_matrix_recovers_weights_and_inconsistent_is_flagged() -> None:
    w = np.array([0.5, 0.3, 0.2])
    exact = ahp_weights(w[:, None] / w[None, :])                 # a_ij = w_i/w_j — ідеально узгоджена
    np.testing.assert_allclose(exact.weights, w, atol=1e-12)
    assert abs(exact.lambda_max - 3) < 1e-12 and abs(exact.cr) < 1e-12
    cyclic = [[1, 9, 1 / 9], [1 / 9, 1, 9], [9, 1 / 9, 1]]       # A≫B≫C≫A — неузгоджено
    assert ahp_weights(cyclic).cr > 0.1
    with pytest.raises(ValueError, match="reciprocal"):
        ahp_weights([[1, 2], [2, 1]])
    with pytest.raises(ValueError, match="square"):
        ahp_weights([[1, 2, 3]])


def test_write_weights_yaml_keeps_rest_of_file(tmp_path: Path) -> None:
    src = (ROOT / "config" / "dq_weights.yaml").read_text(encoding="utf-8")
    p = tmp_path / "dq_weights.yaml"
    blank = re.sub(r"(?m)^weights:.*$", "weights: null", src)
    blank = re.sub(r"(?m)^consistency_ratio:.*$", "consistency_ratio: null", blank)
    p.write_text(blank, encoding="utf-8")
    assert yaml.safe_load(blank)["weights"] is None
    res = ahp_weights(load_matrix())
    out = write_weights_yaml(res, p)
    parsed = yaml.safe_load(out)
    assert parsed["ahp_matrix"] == yaml.safe_load(src)["ahp_matrix"] and parsed["tau0_ms"] == 1000
    np.testing.assert_allclose(parsed["weights"], res.weights, atol=5e-7)
    assert out.splitlines()[0] == src.splitlines()[0]            # коментарі на місці


# ================================================================ Q


def test_perfect_hour_scores_one() -> None:
    """«Ідеальна» година: 60 з 60 кошиків, 0 невалідних і аномалій, 0 с прогалин, лаг p95 = 0 мс.

    Лише за нульового лагу timeliness = exp(0) = 1; будь-який реальний лаг > 0 дає Q < 1 (перевірено нижче).
    """
    w = load_dq_weights()
    perfect = DqInputs(expected_buckets=60, observed_buckets=60, total_count=60, invalid_count=0,
                       anomaly_count=0, gap_seconds=0.0, lag_p95_ms=0.0)
    s = dq_score(perfect, w)
    assert (s.completeness, s.validity, s.timeliness, s.continuity) == (1.0, 1.0, 1.0, 1.0)
    assert math.isclose(s.score, 1.0, abs_tol=1e-12)
    # і кожна окрема вада робить години неідеальною рівно на свою вагу×дефект
    lag = dq_score(DqInputs(60, 60, 60, lag_p95_ms=150.0), w)
    assert math.isclose(lag.score, 1.0 - w[2] * (1.0 - math.exp(-0.15)), rel_tol=1e-12)
    miss = dq_score(DqInputs(60, 57, 57), w)
    assert math.isclose(miss.score, 1.0 - w[0] * 3 / 60, rel_tol=1e-12)
    gap = dq_score(DqInputs(60, 60, 60, gap_seconds=180.0), w)
    assert math.isclose(gap.score, 1.0 - w[3] * 180 / 3600, rel_tol=1e-12)
    bad = dq_score(DqInputs(60, 60, 60, invalid_count=1, anomaly_count=2), w)
    assert math.isclose(bad.score, 1.0 - w[1] * 3 / 60, rel_tol=1e-12)


def test_timeliness_decays_exponentially() -> None:
    tau = 1000.0
    assert timeliness(0.0) == 1.0
    assert math.isclose(timeliness(tau), math.exp(-1), rel_tol=1e-15)
    assert math.isclose(timeliness(tau * math.log(2)), 0.5, rel_tol=1e-12)     # «напіврозпад» τ₀·ln2
    lags = np.linspace(0.0, 5000.0, 51)
    t = np.array([timeliness(x) for x in lags])
    assert np.all(np.diff(t) < 0)                                              # строго спадає
    # лінійність логарифма: ln T(lag) = −lag/τ₀ (саме експонента, а не, напр., гіпербола 1/(1+lag))
    np.testing.assert_allclose(np.log(t), -lags / tau, rtol=0, atol=1e-12)
    # функціональне рівняння експоненти: T(a + b) = T(a)·T(b)
    for a, b in [(100.0, 250.0), (1000.0, 1000.0), (37.5, 4200.0)]:
        assert math.isclose(timeliness(a + b), timeliness(a) * timeliness(b), rel_tol=1e-12)
    assert timeliness(-50.0) == 1.0                    # від'ємний лаг (зсув годинника) не «кращий» за 0
    assert math.isclose(timeliness(500.0, tau0_ms=500.0), math.exp(-1), rel_tol=1e-15)


def test_dq_component_conventions() -> None:
    assert completeness(0, 0) == 1.0 and completeness(61, 60) == 1.0 and completeness(30, 60) == 0.5
    assert validity(0, 0, 0) == 1.0 and validity(80, 30, 100) == 0.0
    assert continuity(3600.0) == 0.0 and continuity(7200.0) == 0.0 and continuity(0.0) == 1.0
    with pytest.raises(ValueError):
        dq_score(DqInputs(60, 60, 60), (0.5, 0.5, 0.5, 0.5))
    with pytest.raises(ValueError):
        dq_score(DqInputs(60, 60, 60), (1.2, -0.2, 0.0, 0.0))
    # NaN не стає «ідеальною» компонентою (max(0.0, nan) у Python — 0.0): лише явна помилка
    with pytest.raises(ValueError, match="NaN"):
        timeliness(math.nan)
    with pytest.raises(ValueError, match="NaN"):
        continuity(math.nan)
    with pytest.raises(ValueError, match="NaN"):
        dq_score(DqInputs(60, 60, 60, gap_seconds=math.nan), load_dq_weights())
    assert timeliness(math.inf) == 0.0 and continuity(math.inf) == 0.0


def test_dq_accumulator_merges_overlapping_gaps() -> None:
    assert merged_length_ns([(0, 10), (5, 20), (30, 40), (40, 45)]) == 35
    acc = DqAccumulator(window_start_ns=0, window_ns=3_600 * 10**9)
    for m in range(58):
        acc.add_bucket(m * MIN_NS)
        acc.add_checked()
    acc.add_checked(invalid=True)
    acc.add_checked(invalid=True, anomaly=True)           # невалідна свічка аномалією не рахується двічі
    acc.add_gap(58 * MIN_NS, 60 * MIN_NS)
    acc.add_gap(59 * MIN_NS, 61 * MIN_NS)                 # перекриття + вихід за межу години
    for x in (100.0, 110.0, 120.0):
        acc.add_lag_ms(x)
    i = acc.inputs(expected_buckets=60)
    assert (i.observed_buckets, i.total_count, i.invalid_count, i.anomaly_count) == (58, 60, 2, 0)
    assert i.gap_seconds == 120.0
    assert math.isclose(i.lag_p95_ms, 119.0)


# ================================================================ MLP-автокодувальник


def test_anomaly_feature_extractor_warmup_is_exact() -> None:
    ex = AnomalyFeatureExtractor()
    first = next(i + 1 for i, b in enumerate(BARS) if ex.update(b) is not None)
    assert first == ex.warmup == 219
    X, idx = feature_matrix(BARS[:400])
    assert X.shape == (400 - 218, len(FEATURE_NAMES)) == (182, 8) and idx[0] == 218
    assert np.all(np.isfinite(X))


def test_mlp_autoencoder_flags_injected_spike_and_not_normal_bar() -> None:
    train_X, _ = feature_matrix(BARS[:1500])                     # IS-вікно: лише справжні бари
    assert fixture_bars() == tuple(BARS)                          # спільна модель навчена на тих самих барах
    model = trained_autoencoder()                                 # AnomalyAutoencoder(seed=SEED).fit(train_X)
    assert model.converged and model.n_iter <= 500
    assert model._mlp is not None and model._mlp.hidden_layer_sizes == (3,)
    # поріг = q99 власних навчальних скорів: рівно ~1 % навчальних барів вище за нього
    assert math.isclose(float(np.mean(model.train_scores > model.threshold)), 0.01, abs_tol=0.002)

    i = 2000                                                      # поза навчальним вікном
    normal_X, _ = feature_matrix(BARS[: i + 1])
    spiked = list(BARS[: i + 1])
    spiked[i] = inject_anomaly(BARS, i, "price_spike", 0.01)     # +1 % за хвилину при σ₁ₘ ≈ 0.05 %
    spike_X, _ = feature_matrix(spiked)
    s_normal = float(model.score(normal_X[-1])[0])
    s_spike = float(model.score(spike_X[-1])[0])
    assert s_normal <= model.threshold, (s_normal, model.threshold)    # той самий бар без спотворення
    assert s_spike > model.threshold, (s_spike, model.threshold)
    assert not model.is_anomaly(normal_X[-1])[0] and model.is_anomaly(spike_X[-1])[0]
    # на відкладених справжніх барах частка хибних тривог близька до номінальних 1 % (не «все аномалія»)
    X_all, idx = feature_matrix(BARS)
    assert float(np.mean(model.is_anomaly(X_all[idx >= 1500]))) < 0.05
    # детермінізм: свіже навчання з тим самим seed → той самий поріг і скори, що в кешованої моделі
    again = AnomalyAutoencoder(seed=SEED).fit(train_X)
    assert again.threshold == model.threshold
    assert float(again.score(spike_X[-1])[0]) == s_spike


def test_injection_evaluation_is_deterministic_causal_and_uses_only_normal_training_bars() -> None:
    """Процедура scripts/train_anomaly_mlp.py на малих даних: позиції ін'єкцій — лише в held-out, амплітуди в
    заданих межах, «ненормальні» бари вилучаються з навчання, результат відтворний за seed, очевидні аномалії
    (стрибок ціни, застиглий бар) відокремлюються майже ідеально в обох архітектурах."""
    plan = plan_injections(1500, 2400, per_kind=15, rng=np.random.default_rng(SEED))
    assert plan.n_positive() == 15 * len(ANOMALY_KINDS)
    for kind in ANOMALY_KINDS:
        pos, mag = plan.positions[kind], plan.magnitudes[kind]
        assert len(set(pos.tolist())) == 15 and pos.min() >= 1500 and pos.max() < 2400
        lo_m, hi_m = DEFAULT_MAGNITUDES[kind]
        assert np.all((np.abs(mag) >= lo_m) & (np.abs(mag) <= hi_m))
    assert (plan.magnitudes["price_spike"] < 0).any() and (plan.magnitudes["price_spike"] > 0).any()

    normal = [structurally_valid(b) for b in BARS[:2400]]
    assert all(normal)                                              # справжні бари — без порушень
    normal[1000] = False                                            # імітація синтетичного/невалідного бару
    kw: dict[str, Any] = {"train": (0, 1500), "holdout": (1500, 2400), "seed": SEED, "per_kind": 15,
                          "normal": normal}
    r = evaluate_injections(BARS[:2400], **kw)
    warm = AnomalyFeatureExtractor().warmup
    assert (r.train_vectors, r.excluded_train_vectors, r.neg_vectors) == (1500 - (warm - 1) - 1, 1, 900)
    for label in ("8-3-8", "5-3-5"):
        a = r.archs[label]
        assert a.per_kind["price_spike"]["auc"] > 0.95 and a.per_kind["frozen"]["auc"] > 0.95, label
        assert 0.5 < a.auc_all <= 1.0 and 0.0 <= a.fpr_at_q99 < 0.1 and a.converged
    assert r.archs["8-3-8"].n_features == 8 and r.archs["5-3-5"].n_features == 5
    assert set(r.feature_drift) == set(FEATURE_NAMES) and r.sigma_dlogp_holdout > 0
    again = evaluate_injections(BARS[:2400], **kw, architectures={"8-3-8": 8})   # детермінізм за seed
    assert again.archs["8-3-8"].auc_all == r.archs["8-3-8"].auc_all
    assert again.archs["8-3-8"].per_kind == r.archs["8-3-8"].per_kind and set(again.archs) == {"8-3-8"}
    # однакові дані → σ-відношення 1 і частка хвостів ≈ номінальна
    X, _ = feature_matrix(BARS[:1500])
    same = feature_drift(X, X)
    assert all(math.isclose(d["sd_ratio"], 1.0) and d["tail_share"] <= 0.011 for d in same.values())
    with pytest.raises(ValueError):
        evaluate_injections(BARS[:2400], **{**kw, "holdout": (1400, 2400)})      # held-out перекриває train
    with pytest.raises(ValueError):
        evaluate_injections(BARS[:2400], **{**kw, "normal": normal[:10]})
    with pytest.raises(ValueError):
        plan_injections(1500, 1510, per_kind=11, rng=np.random.default_rng(SEED))
    broken = replace(BARS[5], h=BARS[5].l - 1.0)
    assert not structurally_valid(broken) and not structurally_valid(replace(BARS[5], v=-1.0))


def test_streaming_scorer_matches_batch_scores() -> None:
    model = trained_autoencoder()
    sc = AnomalyScorer(model)
    verdicts = [v for b in BARS[:600] if (v := sc.update(b)) is not None]
    X, _ = feature_matrix(BARS[:600])
    np.testing.assert_allclose([v.score for v in verdicts], model.score(X), rtol=1e-12)
    assert all(v.threshold == model.threshold for v in verdicts)
    with pytest.raises(RuntimeError):
        AnomalyAutoencoder().score(X)


def test_injected_anomalies_keep_ohlc_consistent() -> None:
    for kind, mag in [("price_spike", -0.03), ("price_spike", 0.03), ("wick", 0.02), ("volume_burst", 10.0),
                      ("frozen", 0.0)]:
        b = inject_anomaly(BARS, 100, kind, mag)  # type: ignore[arg-type]
        assert b.h >= max(b.o, b.c) and b.l <= min(b.o, b.c) and b.v >= 0


# ================================================================ health


def test_pipeline_health_snapshot() -> None:
    h = PipelineHealth(lag_window=3)
    for k, lag in enumerate((100, 200, 300, 400)):
        h.on_frame("market", 10**9 + k)
        h.on_event("trade", 10**9, 10**9 + lag * 1_000_000)
    h.on_event("trade", 10**9, 10**9 - 5_000_000)      # від'ємний лаг — у p95 як 0
    h.on_gap(1, "OPEN")
    h.on_gap(1, "FILLED")
    h.on_gap(2, "OPEN")
    h.on_disconnect("HEARTBEAT_TIMEOUT", watchdog=True)
    s = h.snapshot()
    assert s.frames == 4 and s.events_by_kind == {"trade": 5}
    assert s.lag_p95_ms is not None and math.isclose(s.lag_p95_ms, float(np.percentile([300, 400, 0], 95)))
    assert s.gaps_opened == 2 and s.gaps_open == 1 and s.gaps_by_status == {"FILLED": 1, "OPEN": 1}
    assert s.reconnects == 1 and s.watchdog_fires == 1
    assert s.as_dict()["frames"] == 4
