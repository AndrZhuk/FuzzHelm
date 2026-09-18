"""H. Агрегація і κ — property-based тести (hypothesis).

Найменування: tests/property/test_decision_property.py
Автор: Андрій Жук, 2026.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import pytest
from hypothesis import given
from hypothesis import strategies as st

from fuzzhelm.decision.aggregator import EPS, consensus
from fuzzhelm.decision.agreement import agreement, kappa_from_agreement
from fuzzhelm.decision.core import DecisionCore
from fuzzhelm.decision.narrative_uk import narrate
from fuzzhelm.detectors.base import DetectorGroup, DetectorOutput
from fuzzhelm.features.convert import Bar
from fuzzhelm.fuzzy.base import FiredRule, FuzzyResult

pytestmark = pytest.mark.property

TOL = 1e-12
LOG3_2 = math.log(2.0) / math.log(3.0)

strength = st.floats(-1.0, 1.0, allow_nan=False)
confidence = st.floats(0.0, 1.0, allow_nan=False)
omega = st.floats(0.0, 3.0, allow_nan=False)
voting_group = st.sampled_from([DetectorGroup.TREND, DetectorGroup.REVERSION])


@st.composite
def outputs_st(
    draw: st.DrawFn, s: st.SearchStrategy[float] = strength, min_size: int = 0
) -> list[DetectorOutput]:
    n = draw(st.integers(min_size, 8))
    res = [
        DetectorOutput(f"d{i}", draw(s), draw(confidence), {}, draw(voting_group), draw(omega))
        for i in range(n)
    ]
    if draw(st.booleans()):
        v = draw(confidence)
        res.append(DetectorOutput("vol_regime", 0.0, 1.0, {"V": v}, DetectorGroup.CONTEXT, 1.0))
    return res


def _hull_check(value: float, members: list[DetectorOutput], mass: float) -> None:
    contributing = [o.s for o in members if o.weight > 0.0 and o.c > 0.0]
    if not contributing:
        assert value == 0.0
        return
    lo, hi = min(contributing), max(contributing)
    # завжди: опукла комбінація, стиснута до нуля множником mass/max(ε, mass) ≤ 1
    assert min(lo, 0.0) - TOL <= value <= max(hi, 0.0) + TOL
    if mass >= EPS:
        # маса свідчень достатня — T/R лежить між мінімальним і максимальним внеском
        assert lo - TOL <= value <= hi + TOL


@given(outputs_st())
def test_consensus_between_min_and_max_contribution(outputs: list[DetectorOutput]) -> None:
    c = consensus(outputs)
    assert c.trend is not None and c.reversion is not None
    # лінива погрупова діагностика відтворює рівно ті самі T і R (та сама формула, той самий порядок)
    assert c.trend.value == c.T and c.reversion.value == c.R
    trend = [o for o in outputs if o.group == DetectorGroup.TREND]
    rev = [o for o in outputs if o.group == DetectorGroup.REVERSION]
    _hull_check(c.T, trend, c.trend.mass)
    _hull_check(c.R, rev, c.reversion.mass)
    assert -1.0 <= c.T <= 1.0 and -1.0 <= c.R <= 1.0
    assert 0.0 <= c.V <= 1.0


@given(outputs_st(), st.floats(0.0, 1.0), st.floats(0.05, 5.0))
def test_membership_probabilities_sum_to_one(
    outputs: list[DetectorOutput], kappa_min: float, nu: float
) -> None:
    a = agreement(outputs, kappa_min=kappa_min, nu=nu)
    assert min(a.p_plus, a.p_minus, a.p_zero) >= 0.0
    assert abs(a.p_plus + a.p_minus + a.p_zero - 1.0) <= TOL
    assert 0.0 <= a.H <= 1.0 and 0.0 <= a.A_g <= 1.0
    assert kappa_min - TOL <= a.kappa <= 1.0 + TOL
    assert not any(math.isnan(x) for x in (a.p_plus, a.p_minus, a.p_zero, a.H, a.kappa))


@given(outputs_st(), outputs_st(), st.floats(0.0, 1.0), st.floats(0.05, 5.0),
       st.floats(0.0, 1.0), st.floats(0.0, 1.0))
def test_kappa_monotone_in_agreement(o1: list[DetectorOutput], o2: list[DetectorOutput], kappa_min: float,
                                     nu: float, g1: float, g2: float) -> None:
    # через повний шлях agreement(): більша узгодженість ніколи не дає меншого κ
    a1 = agreement(o1, kappa_min=kappa_min, nu=nu)
    a2 = agreement(o2, kappa_min=kappa_min, nu=nu)
    lo, hi = sorted((a1, a2), key=lambda a: a.A_g)
    assert lo.kappa <= hi.kappa
    # і безпосередньо для довільної пари A_g
    x, y = sorted((g1, g2))
    assert kappa_from_agreement(x, kappa_min, nu) <= kappa_from_agreement(y, kappa_min, nu)


@given(outputs_st(s=st.floats(0.0, 1.0), min_size=1), st.booleans())
def test_same_sign_agreement_is_bounded_by_log3_2(outputs: list[DetectorOutput], flip: bool) -> None:
    # однаковий знак ⇒ p₋ = 0 (або p₊ = 0) ⇒ H ≤ log₃2 ⇒ A_g ≥ 1 − log₃2 ≈ 0.369 (уточнення до брифінгу)
    if flip:
        outputs = [DetectorOutput(o.name, -o.s, o.c, o.features, o.group, o.weight) for o in outputs]
    a = agreement(outputs)
    if not a.has_evidence:
        return
    assert (a.p_plus if flip else a.p_minus) == 0.0
    assert a.H <= LOG3_2 + TOL
    assert a.A_g >= 1.0 - LOG3_2 - TOL


@dataclass
class _Engine:
    u_raw: float
    fired: tuple[FiredRule, ...]
    name: str = "mamdani"

    def infer(self, T: float, R: float, V: float) -> FuzzyResult:
        return FuzzyResult(self.u_raw, {"T": T, "R": R, "V": V}, {}, self.fired)


@dataclass
class _Det:
    output: DetectorOutput
    warmup: int = 0

    @property
    def name(self) -> str:
        return self.output.name

    @property
    def group(self) -> DetectorGroup:
        return self.output.group

    @property
    def weight(self) -> float:
        return self.output.weight

    def compute(self, window: object) -> DetectorOutput:
        return self.output


class _Window:
    def bar(self, lag: int = 0) -> Bar:
        return Bar(0, 1.0, 1.0, 1.0, 1.0, 0.0)


fired_rule = st.builds(
    FiredRule,
    rule_id=st.from_regex(r"R[0-9]{2}", fullmatch=True),
    alpha=st.floats(0.0, 1.0),
    consequent=st.sampled_from(["STRONG_SHORT", "SHORT", "HOLD", "LONG", "STRONG_LONG"]),
    antecedent=st.fixed_dictionaries({
        "T": st.sampled_from(["STRONG_DOWN", "WEAK_DOWN", "NEUTRAL", "WEAK_UP", "STRONG_UP", "any"]),
        "R": st.sampled_from(["SELL_PRESSURE", "NO_PRESSURE", "BUY_PRESSURE", "any"]),
        "V": st.sampled_from(["LO", "MID", "HI", "any"]),
    }),
)


@given(outputs_st(), strength, st.lists(fired_rule, max_size=8))
def test_u_final_damps_without_flipping_sign(outputs: list[DetectorOutput], u_raw: float,
                                             fired: list[FiredRule]) -> None:
    trace = DecisionCore([_Det(o) for o in outputs], _Engine(u_raw, tuple(fired))).decide(_Window())
    assert trace.u_final == pytest.approx(trace.kappa * u_raw, abs=1e-15)
    assert abs(trace.u_final) <= abs(u_raw)            # κ ≤ 1: ядро довіри лише гасить
    assert trace.u_final * u_raw >= 0.0                 # і ніколи не перевертає знак
    alphas = [r.alpha for r in trace.fired_rules]
    assert alphas == sorted(alphas, reverse=True) and len(alphas) == len(fired)
    text = narrate(trace)
    assert text.strip()
    if fired:
        assert trace.fired_rules[0].rule_id in text
