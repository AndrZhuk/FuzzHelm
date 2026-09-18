"""H. Агрегація і κ + DecisionCore, DecisionTrace, narrative_uk (модульні тести).

Найменування: tests/unit/test_decision.py
Автор: Андрій Жук, 2026.

Детектори, вікно і рушій виведення — мінімальні фейки, що реалізують Protocol-и з
detectors/base.py і fuzzy/base.py: справжні модулі пишуться паралельно, а ці тести
перевіряють саме шар рішень.
"""

from __future__ import annotations

import json
import math
import random
import re
import warnings
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from fractions import Fraction
from pathlib import Path

import numpy as np
import pytest

from fuzzhelm.config import load_yaml
from fuzzhelm.core.enums import RejectCode, RiskState, Side, VerdictKind
from fuzzhelm.core.errors import ConfigValidationError
from fuzzhelm.decision.aggregator import Consensus, consensus
from fuzzhelm.decision.agreement import (
    Agreement,
    agreement,
    kappa_from_agreement,
    normalized_entropy,
)
from fuzzhelm.decision.core import DecisionCore, agreement_params_from_config
from fuzzhelm.decision.narrative_uk import narrate, rule_sentence
from fuzzhelm.decision.trace import DecisionTrace, json_safe
from fuzzhelm.detectors.base import Detector, DetectorGroup, DetectorOutput
from fuzzhelm.features.convert import Bar
from fuzzhelm.fuzzy.base import FiredRule, FuzzyResult, InferenceEngine

TREND = DetectorGroup.TREND
REV = DetectorGroup.REVERSION
CTX = DetectorGroup.CONTEXT

DEMO_SENTENCE = (
    "Спрацювало правило R07 з α = 0.62: ЯКЩО тренд СИЛЬНЕ_ЗРОСТАННЯ І реверсія НЕМА_ТИСКУ "
    "І волатильність ПОМІРНА ТО сигнал СИЛЬНИЙ_ЛОНГ."
)


# --- фейки ---------------------------------------------------------------------------------------

def out(name: str, s: float, c: float, group: DetectorGroup = TREND, weight: float = 1.0,
        features: Mapping[str, float] | None = None) -> DetectorOutput:
    return DetectorOutput(name=name, s=s, c=c, features=dict(features or {}), group=group, weight=weight)


def vol(v: float, name: str = "vol_regime") -> DetectorOutput:
    return out(name, 0.0, 1.0, CTX, 1.0, {"V": v})


class FakeWindow:
    def __init__(self, t_ns: int = 1_700_000_000_000_000_000) -> None:
        self._bar = Bar(t_ns=t_ns, o=100.0, h=101.0, l=99.0, c=100.5, v=10.0)

    def bar(self, lag: int = 0) -> Bar:
        assert lag == 0
        return self._bar

    def __len__(self) -> int:
        return 1


@dataclass
class FakeDetector:
    name: str
    group: DetectorGroup
    s: float
    c: float
    weight: float = 1.0
    warmup: int = 0
    features: dict[str, float] = field(default_factory=dict)
    calls: list[object] = field(default_factory=list)

    def compute(self, window: object) -> DetectorOutput:
        self.calls.append(window)
        return DetectorOutput(self.name, self.s, self.c, dict(self.features), self.group, self.weight)


def rule(rule_id: str, alpha: float, consequent: str = "LONG", T: str = "WEAK_UP", R: str = "NO_PRESSURE",
         V: str = "MID") -> FiredRule:
    return FiredRule(rule_id=rule_id, alpha=alpha, consequent=consequent, antecedent={"T": T, "R": R, "V": V})


@dataclass
class FakeEngine:
    u_raw: float = 0.0
    fired: tuple[FiredRule, ...] = ()
    name: str = "mamdani"
    seen: list[tuple[float, float, float]] = field(default_factory=list)

    def infer(self, T: float, R: float, V: float) -> FuzzyResult:
        self.seen.append((T, R, V))
        grid = np.linspace(-1.0, 1.0, 5)
        return FuzzyResult(
            u_raw=self.u_raw,
            inputs={"T": T, "R": R, "V": V},
            memberships={"T": {"STRONG_UP": 0.62, "WEAK_UP": 0.38}, "R": {"NO_PRESSURE": 1.0},
                         "V": {"MID": 0.9, "LO": np.float64(0.1)}},
            fired=self.fired,
            grid=grid,
            mu_agg=np.array([0.0, 0.1, 0.2, 0.62, 0.3]),
            engine=self.name,
            extras={"centroid": self.u_raw},
        )


def unanimous_long_detectors() -> list[FakeDetector]:
    return [
        FakeDetector("ema_slope", TREND, 1.0, 0.9),
        FakeDetector("donchian", TREND, 1.0, 0.5),
        FakeDetector("rsi_exhaustion", REV, 1.0, 0.7, weight=0.8),
        FakeDetector("vol_regime", CTX, 0.0, 1.0, features={"V": 0.51}),
    ]


def make_trace(fired: tuple[FiredRule, ...] = (), u_raw: float = 0.0, engine: str = "mamdani",
               detectors: list[FakeDetector] | None = None) -> DecisionTrace:
    core = DecisionCore(detectors or unanimous_long_detectors(), FakeEngine(u_raw, fired, engine))
    return core.decide(FakeWindow())


def assert_protocols() -> None:
    assert isinstance(FakeDetector("x", TREND, 0.0, 0.0), Detector)
    assert isinstance(FakeEngine(), InferenceEngine)


def test_fakes_implement_protocols() -> None:
    assert_protocols()


# --- H: консенсус ---------------------------------------------------------------------------------

def test_consensus_zero_when_all_confidences_zero() -> None:
    outputs = [
        out("ema_slope", 0.9, 0.0), out("donchian", -0.4, 0.0),
        out("rsi_exhaustion", 0.7, 0.0, REV, 0.8), out("bollinger_z", -1.0, 0.0, REV, 0.8),
        out("candle_geometry", 0.3, 0.0, REV, 0.6), vol(0.3),
    ]
    c = consensus(outputs)
    assert c.T == 0.0 and c.R == 0.0          # точний нуль, а не NaN від 0/0
    assert c.V == 0.3 and c.v_source == "vol_regime"
    assert c.trend is not None and c.trend.mass == 0.0
    assert c.reversion is not None and c.reversion.mass == 0.0
    # і узгодженість: немає свідчень ⇒ максимальне гасіння
    assert agreement(outputs).kappa == pytest.approx(0.35, abs=1e-12)


def test_consensus_matches_hand_computed_example() -> None:
    outputs = [
        out("ema_slope", 0.8, 0.5), out("donchian", 0.2, 1.0),
        out("bollinger_z", -0.6, 0.5, REV, 0.8), out("candle_geometry", 0.3, 1.0, REV, 0.6),
        vol(0.42),
    ]
    c = consensus(outputs)
    # T = (1·0.5·0.8 + 1·1·0.2) / (0.5 + 1) = 0.6 / 1.5
    assert pytest.approx(0.4, abs=1e-15) == c.T
    # R = (0.8·0.5·(−0.6) + 0.6·1·0.3) / (0.4 + 0.6) = (−0.24 + 0.18) / 1.0
    assert pytest.approx(-0.06, abs=1e-15) == c.R
    assert c.V == 0.42
    assert c.trend is not None and c.reversion is not None
    assert c.trend.names == ("ema_slope", "donchian")
    assert c.trend.omegas == (1.0, 1.0)
    assert c.trend.effective == pytest.approx((0.5, 1.0))
    assert c.reversion.effective == pytest.approx((0.4, 0.6))
    assert c.trend.shares == pytest.approx((1 / 3, 2 / 3))


def test_group_shares_reconstruct_group_value() -> None:
    outputs = [out("a", 0.7, 0.3, weight=2.0), out("b", -0.2, 0.9), out("c", 0.1, 0.4, weight=0.5)]
    g = consensus(outputs).trend
    assert g is not None
    rebuilt = sum(sh * s for sh, s in zip(g.shares, g.strengths, strict=True))
    assert rebuilt == pytest.approx(g.value, abs=1e-15)


def test_consensus_context_group_does_not_vote() -> None:
    base = [out("ema_slope", 0.5, 1.0), out("bollinger_z", -0.5, 1.0, REV)]
    loud_context = [*base, out("vol_regime", 0.0, 1.0, CTX, 5.0, {"V": 0.9})]
    assert consensus(base).T == consensus(loud_context).T == 0.5
    assert consensus(base).R == consensus(loud_context).R == -0.5


def test_consensus_v_falls_back_to_prior_without_context() -> None:
    c = consensus([out("ema_slope", 0.5, 1.0)])
    assert c.V == 0.5 and c.v_source == "default"
    warm = consensus([out("ema_slope", 0.5, 1.0), out("vol_regime", 0.0, 0.0, CTX, 1.0, {"V": math.nan})])
    assert warm.V == 0.5 and warm.v_source == "default"


@pytest.mark.parametrize("weight", [-1.0, math.inf, math.nan])
def test_detector_output_rejects_invalid_weight(weight: float) -> None:
    # з DEC-04 вагу валідує сам DetectorOutput (фундамент), ще до consensus/agreement
    with pytest.raises(ValueError):
        out("a", 0.5, 1.0, weight=weight)


@pytest.mark.parametrize("outputs", [
    [vol(0.2, "v1"), vol(0.3, "v2")],
    [vol(1.5)],
])
def test_consensus_rejects_invalid_inputs(outputs: list[DetectorOutput]) -> None:
    with pytest.raises(ValueError):
        consensus(outputs)


# --- H: узгодженість і κ --------------------------------------------------------------------------

def test_agreement_is_one_when_all_same_sign() -> None:
    long_all = [out("ema_slope", 1.0, 0.9), out("donchian", 1.0, 0.2, weight=0.5),
                out("rsi_exhaustion", 1.0, 0.7, REV, 0.8), out("bollinger_z", 1.0, 1.0, REV, 0.8),
                vol(0.5)]  # VolRegime (s = 0, c = 1) не голосує — інакше «розмив» би згоду через p₀
    a = agreement(long_all)
    assert (a.p_plus, a.p_minus, a.p_zero) == (1.0, 0.0, 0.0)
    assert a.H == 0.0 and a.A_g == 1.0 and a.kappa == 1.0

    short_all = [out("ema_slope", -1.0, 0.3), out("candle_geometry", -1.0, 0.6, REV, 0.6)]
    b = agreement(short_all)
    assert (b.p_plus, b.p_minus, b.p_zero) == (0.0, 1.0, 0.0)
    assert b.H == 0.0 and b.A_g == 1.0 and b.kappa == 1.0


def test_agreement_same_sign_partial_strength_is_bounded_below() -> None:
    # Уточнення до брифінгу (див. deviations.d/decision.md): однаковий знак при |s| < 1 не дає A_g = 1,
    # бо незадіяна маса йде в p₀. Точне значення для s ≡ 0.5: p = (½, 0, ½) ⇒ H = ln2/ln3.
    a = agreement([out("ema_slope", 0.5, 1.0), out("bollinger_z", 0.5, 0.4, REV, 0.8)])
    assert a.p_plus == pytest.approx(0.5) and a.p_minus == 0.0 and a.p_zero == pytest.approx(0.5)
    assert pytest.approx(math.log(2) / math.log(3), abs=1e-12) == a.H
    assert a.A_g == pytest.approx(1 - math.log(2) / math.log(3), abs=1e-12)


def test_agreement_matches_hand_computed_example() -> None:
    outputs = [
        out("ema_slope", 0.8, 0.5), out("donchian", 0.2, 1.0),
        out("bollinger_z", -0.6, 0.5, REV, 0.8), out("candle_geometry", 0.3, 1.0, REV, 0.6),
        vol(0.42),
    ]
    a = agreement(outputs)
    # Z = 0.5 + 1 + 0.4 + 0.6 = 2.5; p₊ = 0.78/2.5; p₋ = 0.24/2.5; p₀ = 1.48/2.5
    p = (0.312, 0.096, 0.592)
    assert (a.p_plus, a.p_minus, a.p_zero) == pytest.approx(p, abs=1e-15)
    assert a.mass == pytest.approx(2.5)
    h_ref = -sum(x * math.log(x) for x in p) / math.log(3)
    assert pytest.approx(h_ref, abs=1e-12) == a.H
    assert a.kappa == pytest.approx(0.35 + 0.65 * (1 - h_ref), abs=1e-12)


def test_kappa_reaches_kmin_on_uniform_split() -> None:
    # p = (⅓, ⅓, ⅓): три детектори з рівною масою ω·c — «за», «проти» і «утримався»
    outputs = [out("ema_slope", 1.0, 0.6), out("donchian", -1.0, 0.6),
               out("bollinger_z", 0.0, 0.75, REV, 0.8)]
    a = agreement(outputs, kappa_min=0.35, nu=1.0)
    assert (a.p_plus, a.p_minus, a.p_zero) == pytest.approx((1 / 3, 1 / 3, 1 / 3), abs=1e-15)
    assert pytest.approx(1.0, abs=1e-12) == a.H
    assert a.kappa == pytest.approx(0.35, abs=1e-6)
    # та сама межа через чисті функції
    h = normalized_entropy(1 / 3, 1 / 3, 1 / 3)
    assert kappa_from_agreement(1.0 - h, 0.35, 1.0) == pytest.approx(0.35, abs=1e-6)
    # і верхня межа: p = (1, 0, 0) ⇒ κ = 1
    assert kappa_from_agreement(1.0 - normalized_entropy(1.0, 0.0, 0.0), 0.35, 1.0) == 1.0
    # максимальне гасіння 1/0.35 ≈ 2.86× (брифінг §5.4)
    assert 1 / a.kappa == pytest.approx(2.857, abs=1e-3)


def test_entropy_handles_zero_probability() -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        for p in ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)):
            assert normalized_entropy(*p) == 0.0
        assert normalized_entropy(0.5, 0.5, 0.0) == pytest.approx(math.log(2) / math.log(3), abs=1e-15)
        assert normalized_entropy(0.0, 0.25, 0.75) == pytest.approx(
            -(0.25 * math.log(0.25) + 0.75 * math.log(0.75)) / math.log(3), abs=1e-15)
        # через повний шлях: p₋ = p₀ = 0 не дає ні NaN, ні винятку math.log(0)
        a = agreement([out("ema_slope", 1.0, 1.0)])
        assert a.H == 0.0 and not math.isnan(a.kappa)
        # Z = 0: немає свідчень ⇒ рівномірний апріорний розподіл, H = 1, κ = κ_min
        z = agreement([out("ema_slope", 1.0, 0.0), vol(0.3)])
        assert (z.p_plus, z.p_minus, z.p_zero) == pytest.approx((1 / 3, 1 / 3, 1 / 3), abs=1e-15)
        assert pytest.approx(1.0, abs=1e-12) == z.H
        assert z.kappa == pytest.approx(0.35, abs=1e-12)
        assert not z.has_evidence and z.mass == 0.0


def test_agreement_without_any_voting_detector_gives_kmin() -> None:
    a = agreement([vol(0.9)], kappa_min=0.2)
    assert a.kappa == pytest.approx(0.2, abs=1e-12)
    assert a.p_plus + a.p_minus + a.p_zero == pytest.approx(1.0, abs=1e-15)


def test_agreement_small_mass_regularization_is_continuous_at_eps() -> None:
    eps = 1e-9
    above = agreement([out("a", 1.0, eps * (1 + 1e-6))], eps=eps)
    below = agreement([out("a", 1.0, eps * (1 - 1e-6))], eps=eps)
    assert (above.p_plus, above.p_minus, above.p_zero) == (1.0, 0.0, 0.0)   # Z ≥ ε — формула брифінгу
    assert below.p_plus == pytest.approx(1 - 2e-6 / 3, abs=1e-12)           # Z < ε — доповнення ⅓(ε − Z)
    assert below.p_minus == pytest.approx(1e-6 / 3, abs=1e-12)
    assert abs(above.kappa - below.kappa) < 1e-4
    assert below.p_plus + below.p_minus + below.p_zero == pytest.approx(1.0, abs=1e-15)


def test_agreement_ignores_context_group() -> None:
    base = [out("ema_slope", 0.6, 1.0), out("bollinger_z", -0.2, 0.5, REV)]
    assert agreement(base) == agreement([*base, vol(0.7)])


@pytest.mark.parametrize(("kappa_min", "nu"), [(-0.1, 1.0), (1.1, 1.0), (0.35, 0.0), (0.35, -1.0),
                                                (0.35, math.inf), (math.nan, 1.0)])
def test_agreement_rejects_invalid_params(kappa_min: float, nu: float) -> None:
    with pytest.raises(ValueError):
        agreement([out("a", 0.5, 1.0)], kappa_min=kappa_min, nu=nu)


def test_nu_shapes_kappa_curve() -> None:
    # ν > 1 — гасіння сильніше при частковій згоді, ν < 1 — слабше; межі κ_min і 1 не змінюються
    assert kappa_from_agreement(0.5, 0.35, 2.0) == pytest.approx(0.35 + 0.65 * 0.25)
    assert kappa_from_agreement(0.5, 0.35, 0.5) == pytest.approx(0.35 + 0.65 * math.sqrt(0.5))
    for nu in (0.5, 1.0, 2.0):
        assert kappa_from_agreement(0.0, 0.35, nu) == 0.35
        assert kappa_from_agreement(1.0, 0.35, nu) == 1.0


# --- DecisionCore -------------------------------------------------------------------------------

def test_decision_core_calls_each_detector_exactly_once_with_window() -> None:
    dets = unanimous_long_detectors()
    window = FakeWindow()
    DecisionCore(dets, FakeEngine(0.5, (rule("R08", 0.4),))).decide(window)
    for d in dets:
        assert d.calls == [window]


def test_decision_core_feeds_consensus_into_engine() -> None:
    dets = [FakeDetector("ema_slope", TREND, 0.8, 0.5), FakeDetector("donchian", TREND, 0.2, 1.0),
            FakeDetector("bollinger_z", REV, -0.6, 0.5, weight=0.8),
            FakeDetector("candle_geometry", REV, 0.3, 1.0, weight=0.6),
            FakeDetector("vol_regime", CTX, 0.0, 1.0, features={"V": 0.42})]
    engine = FakeEngine(0.3, (rule("R08", 0.4),))
    trace = DecisionCore(dets, engine).decide(FakeWindow())
    assert len(engine.seen) == 1
    T, R, V = engine.seen[0]
    assert (T, R, V) == (trace.T, trace.R, trace.V)
    assert pytest.approx((0.4, -0.06, 0.42), abs=1e-15) == (T, R, V)


def test_decision_core_u_final_is_clipped_kappa_times_u_raw() -> None:
    # одностайний лонг: κ = 1 ⇒ u_final = u_raw
    t1 = make_trace((rule("R07", 0.62, "STRONG_LONG"),), u_raw=0.8)
    assert t1.kappa == 1.0 and t1.u_final == 0.8
    # рівномірний розкол: κ = 0.35 ⇒ u_final = 0.35·u_raw
    split = [FakeDetector("ema_slope", TREND, 1.0, 0.6), FakeDetector("donchian", TREND, -1.0, 0.6),
             FakeDetector("bollinger_z", REV, 0.0, 0.75, weight=0.8)]
    t2 = make_trace((rule("R08", 0.4),), u_raw=-0.8, detectors=split)
    assert t2.kappa == pytest.approx(0.35, abs=1e-12)
    assert t2.u_final == pytest.approx(-0.28, abs=1e-12)
    # рушій з порушеним контрактом (|u_raw| > 1) не проходить за межі [−1; 1]
    t3 = make_trace((rule("R07", 1.0, "STRONG_LONG"),), u_raw=1.5)
    assert t3.u_final == 1.0


def test_decision_core_rejects_non_finite_u_raw() -> None:
    with pytest.raises(ValueError, match="non-finite"):
        make_trace(u_raw=math.nan)


def test_decision_core_rejects_duplicate_detector_names_and_bad_params() -> None:
    with pytest.raises(ValueError, match="duplicate"):
        DecisionCore([FakeDetector("a", TREND, 0, 0), FakeDetector("a", REV, 0, 0)], FakeEngine())
    with pytest.raises(ValueError):
        DecisionCore([], FakeEngine(), kappa_min=1.5)
    with pytest.raises(ValueError):
        DecisionCore([], FakeEngine(), nu=0.0)


def test_decision_core_open_time_from_window_or_override() -> None:
    core = DecisionCore(unanimous_long_detectors(), FakeEngine())
    assert core.decide(FakeWindow(t_ns=123)).open_time_ns == 123
    assert core.decide(FakeWindow(t_ns=123), open_time_ns=456).open_time_ns == 456


def test_decision_core_is_deterministic() -> None:
    core = DecisionCore(unanimous_long_detectors(), FakeEngine(0.7, (rule("R07", 0.62, "STRONG_LONG"),)))
    a, b = core.decide(FakeWindow()), core.decide(FakeWindow())
    assert a.to_dict() == b.to_dict()


def test_decision_core_from_config_reads_agreement_section(config_dir: Path) -> None:
    cfg = load_yaml("detectors", config_dir)
    core = DecisionCore.from_config([], FakeEngine(), cfg)
    assert (core.kappa_min, core.nu) == (cfg["agreement"]["kappa_min"], cfg["agreement"]["nu"])
    assert agreement_params_from_config(None).kappa_min == 0.35
    with pytest.raises(ConfigValidationError):
        agreement_params_from_config({"agreement": {"kappa_min": 2.0}})
    with pytest.raises(ConfigValidationError):
        agreement_params_from_config({"agreement": {"kappa_min": 0.35, "typo": 1}})


# --- DecisionTrace -------------------------------------------------------------------------------

def test_decision_trace_contains_every_fired_rule() -> None:
    rules = [rule("R07", 0.62, "STRONG_LONG", "STRONG_UP"), rule("R08", 0.31), rule("R12", 0.05, "HOLD"),
             rule("R22", 0.31, "LONG"), rule("R03", 0.18, "SHORT"), rule("R41", 0.001, "HOLD")]
    shuffled = rules[:]
    random.Random(7).shuffle(shuffled)
    trace = make_trace(tuple(shuffled), u_raw=0.55)

    expected = sorted(rules, key=lambda r: (-r.alpha, r.rule_id))
    assert trace.fired_rules == tuple(expected)          # усі правила, α спадно, нічьї — за rule_id
    assert trace.top_rule == expected[0]
    d = trace.to_dict()
    assert [(r["rule_id"], r["alpha"], r["consequent"]) for r in d["fired_rules"]] == [
        (r.rule_id, r.alpha, r.consequent) for r in expected]
    assert all(r["antecedent"] == dict(e.antecedent) for r, e in zip(d["fired_rules"], expected, strict=True))
    alphas = [r["alpha"] for r in d["fired_rules"]]
    assert alphas == sorted(alphas, reverse=True)
    assert "R07" in narrate(trace)


def test_trace_carries_everything_explain_needs() -> None:
    trace = make_trace((rule("R07", 0.62, "STRONG_LONG", "STRONG_UP"),), u_raw=0.7)
    d = trace.to_dict()
    for key in ("open_time_ns", "engine", "inputs", "consensus", "detector_outputs", "memberships",
                "fired_rules", "u_raw", "kappa", "u_final", "agreement", "grid", "mu_agg", "sizing", "risk"):
        assert key in d
    names = {o["name"] for o in d["detector_outputs"]}
    assert names == {"ema_slope", "donchian", "rsi_exhaustion", "vol_regime"}
    assert d["detector_outputs"][0].keys() >= {"name", "group", "s", "c", "weight", "features"}
    assert d["detector_outputs"][0]["group"] == "trend"
    assert d["memberships"]["T"]["STRONG_UP"] == 0.62
    assert set(d["agreement"]) >= {"p_plus", "p_minus", "p_zero", "H", "A_g", "kappa"}
    assert d["grid"] == [-1.0, -0.5, 0.0, 0.5, 1.0] and len(d["mu_agg"]) == 5
    assert d["u_final"] == pytest.approx(d["kappa"] * d["u_raw"])
    assert d["engine"] == "mamdani"
    assert d["consensus"]["trend"]["members"][0].keys() >= {"name", "omega", "effective", "s", "share"}


def test_trace_to_dict_is_json_safe() -> None:
    dets = unanimous_long_detectors()
    dets[0].features = {"g": np.float64(0.25), "bad": math.nan}
    trace = make_trace((rule("R07", 0.62, "STRONG_LONG"),), u_raw=0.7, detectors=dets)
    trace = trace.with_sizing({
        "qty": Decimal("0.012"), "big": Decimal("1E+2"), "side": Side.LONG,
        "binding_constraint": "VOL_TARGET", "q_atr": np.float64(0.05), "reject_code": None,
    }).with_risk({"state": RiskState.WARNING, "verdict": {"kind": VerdictKind.SHRINK, "factor": 0.5},
                  "vetoes": ()})
    d = trace.to_dict()
    text = json.dumps(d, allow_nan=False, ensure_ascii=False)
    back = json.loads(text)
    assert back["sizing"]["qty"] == "0.012" and back["sizing"]["big"] == "100"   # без експоненти
    assert back["sizing"]["side"] == 1
    assert back["risk"]["state"] == "WARNING" and back["risk"]["verdict"]["kind"] == "SHRINK"
    assert back["detector_outputs"][0]["features"] == {"g": 0.25, "bad": None}
    assert isinstance(back["mu_agg"], list)


def test_trace_with_sizing_and_risk_return_copies() -> None:
    t = make_trace((rule("R07", 0.62, "STRONG_LONG"),), u_raw=0.7)
    t2 = t.with_sizing({"qty": Decimal("0.01")})
    t3 = t2.with_risk({"state": "NORMAL"})
    assert t.sizing is None and t.risk is None
    assert t2.sizing == {"qty": Decimal("0.01")} and t2.risk is None
    assert t3.sizing == t2.sizing and t3.risk == {"state": "NORMAL"}
    assert t3.u_final == t.u_final and t3.fuzzy is t.fuzzy


# --- narrative_uk --------------------------------------------------------------------------------

def test_narrate_reproduces_demo_sentence_exactly() -> None:
    r07 = rule("R07", 0.62, "STRONG_LONG", T="STRONG_UP", R="NO_PRESSURE", V="MID")
    assert rule_sentence(r07) == DEMO_SENTENCE
    text = narrate(make_trace((r07, rule("R08", 0.2)), u_raw=0.7))
    assert text.startswith(DEMO_SENTENCE)


def test_narrate_is_cyrillic_nonempty_with_top_rule_and_alpha() -> None:
    trace = make_trace((rule("R08", 0.31), rule("R13", 0.734, "STRONG_LONG", "STRONG_UP")), u_raw=0.6)
    text = narrate(trace)
    assert text.strip()
    assert re.search(r"[А-ЯҐЄІЇа-яґєії]", text)
    assert "R13" in text and "α = 0.73" in text
    assert text.index("R13") < text.index("R08")        # головне правило — найсильніше


def test_narrate_antecedent_order_and_any_term() -> None:
    r = FiredRule("R30", 0.5, "HOLD", {"V": "HI", "T": "any", "R": "SELL_PRESSURE"})
    assert rule_sentence(r) == (
        "Спрацювало правило R30 з α = 0.50: ЯКЩО тренд БУДЬ-ЯКА І реверсія ТИСК_ПРОДАВЦІВ "
        "І волатильність ВИСОКА ТО сигнал УТРИМАННЯ."
    )


@pytest.mark.parametrize(("term", "uk"), [("STRONG_DOWN", "СИЛЬНЕ_ПАДІННЯ"), ("WEAK_DOWN", "ПОМІРНЕ_ПАДІННЯ"),
                                          ("NEUTRAL", "БЕЗ_ТРЕНДУ"), ("WEAK_UP", "ПОМІРНЕ_ЗРОСТАННЯ")])
def test_narrate_trend_terms(term: str, uk: str) -> None:
    assert f"тренд {uk} " in rule_sentence(rule("R01", 0.3, T=term))


@pytest.mark.parametrize(("term", "uk"), [("STRONG_SHORT", "СИЛЬНИЙ_ШОРТ"), ("SHORT", "ШОРТ"),
                                          ("HOLD", "УТРИМАННЯ"), ("LONG", "ЛОНГ")])
def test_narrate_output_terms(term: str, uk: str) -> None:
    assert rule_sentence(rule("R01", 0.3, consequent=term)).endswith(f"ТО сигнал {uk}.")


def test_narrate_empty_activation_says_hold() -> None:
    text = narrate(make_trace((), u_raw=0.0))
    assert "Жодне правило не спрацювало" in text and "УТРИМАННЯ" in text


def test_narrate_rule_free_engine_is_not_called_empty_activation() -> None:
    text = narrate(make_trace((), u_raw=0.42, engine="linear"))
    assert "Жодне правило" not in text
    assert "«linear»" in text and "u_raw = 0.42" in text


def test_narrate_lists_up_to_three_other_rules() -> None:
    rules = tuple(rule(f"R{i:02d}", 0.9 - i * 0.1) for i in range(1, 7))   # 6 правил
    text = narrate(make_trace(rules, u_raw=0.5))
    assert "Також спрацювали правила R02 (α = 0.70, сигнал ЛОНГ), R03 (α = 0.60, сигнал ЛОНГ), R04" in text
    assert "R05" not in text and "R06" not in text
    assert "ще 2 правила з меншою активацією" in text
    single = narrate(make_trace((rule("R01", 0.5), rule("R02", 0.25, "HOLD")), u_raw=0.3))
    assert "Також спрацювало правило R02 (α = 0.25, сигнал УТРИМАННЯ)." in single


def test_narrate_agreement_and_output_numbers_two_decimals() -> None:
    split = [FakeDetector("ema_slope", TREND, 1.0, 0.6), FakeDetector("donchian", TREND, -1.0, 0.6),
             FakeDetector("bollinger_z", REV, 0.0, 0.75, weight=0.8),
             FakeDetector("vol_regime", CTX, 0.0, 1.0, features={"V": 0.5})]
    text = narrate(make_trace((rule("R08", 0.5),), u_raw=-0.8, detectors=split))
    assert "κ = 0.35" in text and "H = 1.00" in text
    assert "p₊ = 0.33, p₋ = 0.33, p₀ = 0.33" in text
    assert "u_raw = -0.80" in text and "u_final = -0.28" in text and "у бік шорту" in text
    assert "T = 0.00" in text and "-0.00" not in text
    assert not re.search(r"\d,\d", text)                # десятковий роздільник — лише крапка


def test_narrate_no_evidence_sentence() -> None:
    silent = [FakeDetector("ema_slope", TREND, 0.9, 0.0), FakeDetector("vol_regime", CTX, 0.0, 1.0)]
    text = narrate(make_trace((), u_raw=0.0, detectors=silent))
    assert "не дали свідчень" in text and "κ = κ_min = 0.35" in text
    assert "апріорне значення" in text                   # V без рангу — чесно позначено


@pytest.mark.parametrize(("code", "uk"), [("ATR_RISK", "ризик на ATR"),
                                          ("VOL_TARGET", "таргет волатильності"),
                                          ("LEVERAGE", "ліміт плеча")])
def test_narrate_sizing_binding_constraint(code: str, uk: str) -> None:
    t = make_trace((rule("R07", 0.62, "STRONG_LONG"),), u_raw=0.7).with_sizing({
        "binding_constraint": code, "qty": Decimal("0.012"), "q_atr": 0.05, "q_vt": 0.012345678,
        "q_lev": 1.5, "kappa_mode": 0.5,
    })
    text = narrate(t)
    assert f"обмежувальний чинник — {uk} ({code})" in text
    assert "кількість — 0.012" in text and "q_vt = 0.01234568" in text and "κ_mode = 0.50" in text


def test_narrate_sizing_reject_code() -> None:
    t = make_trace((rule("R07", 0.62, "STRONG_LONG"),), u_raw=0.7).with_sizing(
        {"binding_constraint": "ATR_RISK", "qty": Decimal("0"), "reject_code": RejectCode.BELOW_MIN_NOTIONAL})
    assert "ордер відхилено з кодом BELOW_MIN_NOTIONAL" in narrate(t)


def test_narrate_risk_state_and_vetoes() -> None:
    t = make_trace((rule("R07", 0.62, "STRONG_LONG"),), u_raw=0.7).with_risk({
        "state": RiskState.COOLDOWN, "kappa_mode": 0.25,
        "verdict": {"kind": "VETO", "factor": None},
        "vetoes": [{"rule": "MaxDailyLoss", "observed": -0.021, "limit": Decimal("-0.02")}, "StaleDataGuard"],
    })
    text = narrate(t)
    assert "стан ОХОЛОДЖЕННЯ (COOLDOWN)" in text and "κ_mode = 0.25" in text
    assert "вердикт — заборонено (VETO)" in text
    assert "MaxDailyLoss (спостережено -0.021, ліміт -0.02)" in text and "StaleDataGuard" in text
    ok = make_trace((rule("R07", 0.62, "STRONG_LONG"),), u_raw=0.7).with_risk(
        {"state": "NORMAL", "verdict": "SHRINK", "vetoes": []})
    assert "вето немає" in narrate(ok) and "стан НОРМА (NORMAL)" in narrate(ok)


def test_agreement_record_roundtrips_to_dict() -> None:
    a = agreement([out("ema_slope", 0.5, 1.0)])
    d = a.to_dict()
    assert isinstance(a, Agreement)
    assert d["kappa"] == a.kappa and d["has_evidence"] is True
    assert isinstance(Consensus(0.1, 0.2, 0.3).to_dict()["trend"], type(None))


# --- допоміжні гілки: валідація, серіалізація, форматування --------------------------------------

def test_eps_and_prior_validation() -> None:
    with pytest.raises(ValueError):
        consensus([out("a", 0.5, 1.0)], eps=0.0)
    with pytest.raises(ValueError):
        agreement([out("a", 0.5, 1.0)], eps=-1.0)
    with pytest.raises(ValueError):
        agreement([out("a", 0.5, 1.0, weight=-0.5)])
    with pytest.raises(ValueError):
        DecisionCore([], FakeEngine(), eps=0.0)
    with pytest.raises(ValueError):
        DecisionCore([], FakeEngine(), v_default=1.5)
    with pytest.raises(ConfigValidationError):
        agreement_params_from_config({"agreement": [0.35, 1.0]})
    engine = FakeEngine()
    core = DecisionCore(unanimous_long_detectors(), engine)
    assert core.engine is engine and len(core.detectors) == 4


@dataclass(frozen=True)
class _Verdict:
    kind: str
    factor: float | None


class _Model:
    """Качиний аналог pydantic-моделі: json_safe викликає model_dump(mode="python")."""

    def model_dump(self, mode: str = "python") -> dict[str, object]:
        return {"mode": mode, "x": Decimal("1.50")}


def test_json_safe_handles_dataclasses_models_and_exotic_values() -> None:
    got = json_safe({
        "dc": _Verdict("SHRINK", 0.5), "model": _Model(), "set": frozenset({1}), "frac": Fraction(1, 4),
        "inf": math.inf, "np_int": np.int64(3), "obj": RejectCode, 7: "int-key",
    })
    assert got["dc"] == {"kind": "SHRINK", "factor": 0.5}
    assert got["model"] == {"mode": "python", "x": "1.50"}
    assert got["set"] == [1] and got["frac"] == 0.25 and got["inf"] is None and got["np_int"] == 3
    assert isinstance(got["obj"], str) and got["7"] == "int-key"
    json.dumps(got, allow_nan=False)


def test_trace_projection_properties() -> None:
    t = make_trace((rule("R07", 0.62, "STRONG_LONG"),), u_raw=0.7)
    assert t.memberships is t.fuzzy.memberships
    assert (t.p_plus, t.p_minus, t.p_zero, t.H, t.A_g) == (
        t.agreement.p_plus, t.agreement.p_minus, t.agreement.p_zero, t.agreement.H, t.agreement.A_g)
    assert make_trace(()).top_rule is None
    assert t.consensus.trend is not None and t.consensus.trend.value == t.T


def test_narrate_fully_damped_when_kappa_min_zero() -> None:
    split = [FakeDetector("ema_slope", TREND, 1.0, 0.6), FakeDetector("donchian", TREND, -1.0, 0.6),
             FakeDetector("bollinger_z", REV, 0.0, 0.75, weight=0.8)]
    core = DecisionCore(split, FakeEngine(0.9, (rule("R07", 0.62, "STRONG_LONG"),)), kappa_min=0.0)
    t = core.decide(FakeWindow())
    assert t.kappa == pytest.approx(0.0, abs=1e-12)
    assert "сигнал практично погашено" in narrate(t)


def test_narrate_risk_shrink_with_object_verdict_and_quantities() -> None:
    t = make_trace((rule("R07", 0.62, "STRONG_LONG"),), u_raw=0.7).with_sizing(
        {"binding_constraint": RejectCode.ZERO_QTY, "qty": 3, "q_atr": np.float64(0.25), "q_vt": Side.LONG}
    ).with_risk({"verdict": _Verdict("SHRINK", 0.5), "approved_qty": Decimal("0.0060")})
    text = narrate(t)
    assert "(ZERO_QTY)" in text and "кількість — 3" in text and "q_atr = 0.25" in text and "q_vt = 1" in text
    assert "вердикт — зменшено (SHRINK) ×0.5" in text and "дозволена кількість — 0.0060" in text
    empty = make_trace((rule("R07", 0.62, "STRONG_LONG"),), u_raw=0.7).with_sizing({}).with_risk({})
    assert "Сайзер: дані розрахунку відсутні." in narrate(empty)
    assert "Ризик-контур: дані перевірки відсутні." in narrate(empty)


# --- регресії незалежного рев'ю -----------------------------------------------------------------

def test_consensus_context_warmup_with_zero_confidence_uses_default() -> None:
    # форма виходу справжнього VolRegime до прогріву: c = 0, features["V"] = 0.5 — заглушка, не вимір
    warm = out("vol_regime", 0.0, 0.0, CTX, 1.0, {"V": 0.5, "warm": 0.0})
    c = consensus([out("ema_slope", 0.5, 1.0), warm], v_default=0.3)
    assert c.V == 0.3 and c.v_source == "default"          # V узято з v_default, а не із заглушки
    ready = out("vol_regime", 0.0, 1.0, CTX, 1.0, {"V": 0.8, "warm": 1.0})
    c2 = consensus([out("ema_slope", 0.5, 1.0), ready], v_default=0.3)
    assert c2.V == 0.8 and c2.v_source == "vol_regime"
    # два джерела V — помилка конфігурації навіть тоді, коли перше ще не прогріте
    with pytest.raises(ValueError, match="ambiguous"):
        consensus([out("v1", 0.0, 0.0, CTX, 1.0, {"V": math.nan}), vol(0.4, "v2")])


def test_narrate_direction_follows_displayed_rounding() -> None:
    # чисельний шум центроїда справжнього рушія (1.26e−19 на порожньому ринку) — це «0.00», а не лонг
    hold = make_trace((rule("R23", 1.0, "HOLD", "NEUTRAL"),), u_raw=1.2639090925518138e-19)
    assert "u_final = 0.00 (нейтральний)" in narrate(hold)
    small = make_trace((rule("R24", 0.5),), u_raw=0.006)          # одностайний лонг: κ = 1
    assert "u_final = 0.01 (у бік лонгу)" in narrate(small)
    short = make_trace((rule("R22", 0.5, "SHORT"),), u_raw=-0.3)
    assert "у бік шорту" in narrate(short)


@dataclass
class _LinearLike:
    """Рушій без бази правил (як LinearVoteEngine): memberships = {}, fired = ()."""

    u_raw: float = 0.0
    name: str = "linear"

    def infer(self, T: float, R: float, V: float) -> FuzzyResult:
        return FuzzyResult(self.u_raw, {"T": T, "R": R, "V": V}, {}, (), engine=self.name)


def test_narrate_linear_engine_with_zero_output_is_not_empty_activation() -> None:
    t = DecisionCore(unanimous_long_detectors(), _LinearLike(0.0)).decide(FakeWindow())
    text = narrate(t)
    assert "Жодне правило" not in text
    assert "«linear» не використовує бази правил" in text and "u_final = 0.00 (нейтральний)" in text


@dataclass(frozen=True)
class _RuleVerdictLike:
    rule: str
    verdict: str
    observed: Decimal | None
    limit: Decimal | None


def test_narrate_vetoes_accept_rule_verdict_objects() -> None:
    t = make_trace((rule("R07", 0.62, "STRONG_LONG"),), u_raw=0.7).with_risk(
        {"vetoes": [_RuleVerdictLike("MaxDailyLoss", "VETO", Decimal("-0.021"), Decimal("-0.02"))]})
    assert "вето: MaxDailyLoss (спостережено -0.021, ліміт -0.02)" in narrate(t)


def test_decision_trace_equality_handles_numpy_arrays() -> None:
    core = DecisionCore(unanimous_long_detectors(), FakeEngine(0.7, (rule("R07", 0.62, "STRONG_LONG"),)))
    a, b = core.decide(FakeWindow()), core.decide(FakeWindow())
    assert a.fuzzy.grid is not None and a.fuzzy.grid is not b.fuzzy.grid
    assert a == b                                 # згенерований __eq__ падав би на numpy-масивах
    assert a != a.with_sizing({"qty": Decimal("0.01")})
    assert a != "not a trace"
    assert hash(a) == hash(b) and len({a, b}) == 1


def test_decision_core_end_to_end_with_real_detectors_and_mamdani(
    fixtures_dir: Path, config_dir: Path
) -> None:
    # справжні FeaturePipeline → BarWindow → 6 детекторів → MamdaniEngine на реальних хвилинних свічках;
    # імпорт локальний, щоб поломка чужого пакета валила лише цей тест, а не збір усього файлу
    from fuzzhelm.detectors.registry import build_detectors  # noqa: PLC0415
    from fuzzhelm.features.pipeline import FeatureParams, FeaturePipeline  # noqa: PLC0415
    from fuzzhelm.features.window import BarWindow  # noqa: PLC0415
    from fuzzhelm.fuzzy.mamdani import default_engine  # noqa: PLC0415
    from fuzzhelm.fuzzy.membership import load_membership  # noqa: PLC0415
    from fuzzhelm.fuzzy.rules import load_rulebase  # noqa: PLC0415

    cfg = load_yaml("detectors", config_dir)
    source = json.loads((fixtures_dir / "golden" / "source_klines_btcusdt_1m.json").read_text())
    klines = source["klines"][:700]                 # ≈520 барів прогріву VolRegime + виміряний режим
    membership = load_membership()
    rulebase = load_rulebase(None, membership)

    def run() -> list[DecisionTrace]:
        pipe, win = FeaturePipeline(FeatureParams.from_config(cfg)), BarWindow(64)
        core = DecisionCore.from_config(build_detectors(cfg), default_engine(), cfg)
        traces = []
        for k in klines:
            bar = Bar(t_ns=k[0] * 1_000_000, o=float(k[1]), h=float(k[2]), l=float(k[3]), c=float(k[4]),
                      v=float(k[5]), qv=float(k[7]), n=int(k[8]))
            win.append(bar, pipe.update(bar))
            traces.append(core.decide(win))
        return traces

    traces = run()
    warm_v = measured_v = 0
    for i, tr in enumerate(traces):
        assert tr.open_time_ns == klines[i][0] * 1_000_000
        assert tr.u_final == max(-1.0, min(1.0, tr.kappa * tr.u_raw))
        assert 0.35 - 1e-12 <= tr.kappa <= 1.0
        vol_out = next(o for o in tr.detector_outputs if o.group == CTX)
        assert (tr.consensus.v_source == "default") == (vol_out.c == 0.0)   # заглушка прогріву ≠ вимір
        warm_v += vol_out.c == 0.0
        measured_v += vol_out.c > 0.0
        # кожне правило з α_r = min(μ_T, μ_R, μ_V) > 0 (еталон — база правил і належності з трасування)
        mu = tr.memberships
        expected = {r.id: min(mu[v][r.antecedent[v]] for v in ("T", "R", "V")) for r in rulebase}
        assert {r.rule_id: r.alpha for r in tr.fired_rules} == pytest.approx(
            {rid: a for rid, a in expected.items() if a > 0.0}, abs=1e-12)
    assert warm_v > 0 and measured_v > 0                  # фікстура покриває обидва режими V
    last = traces[-1]
    json.dumps(last.to_dict(), allow_nan=False)
    top = last.top_rule
    assert top is not None and narrate(last).startswith(f"Спрацювало правило {top.rule_id} з α = ")
    assert traces == run()                                # детермінізм: той самий вхід → рівні трасування
