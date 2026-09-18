"""G16–G17. Властивості нечіткого ядра: монотонність за T, контрприклади, симетрія, межі.

Найменування: tests/property/test_fuzzy_property.py
Призначення: property-тести (hypothesis) і задокументовані контрприклади до питання 2 захисту
    («чи несуперечлива база правил»), брифінг §3, §10 група G.
Автор: Андрій Жук, 2026.

РЕЗУЛЬТАТ (див. docs/deviations.d/fuzzy.md, D-FZ-1): твердження брифінгу «при w ≡ 1 і розбитті
Руспіні u(T₂) ≥ u(T₁) − 1e−9 для T₂ > T₁» для Мамдані max-min + центроїд НЕ виконується — hypothesis
знаходить порушення за кілька десятків прикладів. Причина — провал висоти спільного наслідку на
перетині сусідніх термів T (max(λ, 1−λ) ≥ 0.5 замість 1) плюс зсув центроїда обрізаного
несиметричного плеча і «витік» правил сусідніх V-термів через гаусіани. Тому:
  * test_u_nondecreasing_in_trend_input перевіряє те, що ДІЙСНО виконується: «грубу» монотонність —
    T₂ ≥ T₁ + 1.0 ⇒ u(T₂) ≥ u(T₁) (інфімум запасу ≈ 0.00723 при T₁ = 0, T₂ = 1, R ≈ 0.00707, V = 1 —
    диференціальна еволюція, 12 сідів + Нелдер–Мід; попередня оцінка 0.0081 була завищеною);
  * test_u_coarse_monotonicity_margin_is_small_but_positive фіксує цю найгіршу точку;
  * test_reversal_comes_from_repeated_consequents_along_t показує механізм на одновимірній таблиці;
  * test_u_local_monotonicity_fails_with_unit_weights_counterexample фіксує контрприклади при w ≡ 1;
  * test_u_local_reversal_is_bounded — амплітуда локального реверсу ≤ 0.16 (sup ≈ 0.15902);
  * test_weighted_rules_break_monotonicity_counterexample — при w = 0.8 ламається навіть «груба».

МФ зафіксовані в цьому файлі (значення брифінгу §5.5 + тимчасовий V-блок з config/membership.yaml
станом на хвилю 1): числові межі стосуються саме цієї конфігурації; після калібрування V
перерахувати їх скриптом scripts/plot_control_surface.py (друкує звіт monotonicity_scan).
"""

from __future__ import annotations

import copy
import math
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import yaml
from hypothesis import example, given, settings
from hypothesis import strategies as st

from fuzzhelm.core.errors import ConfigValidationError
from fuzzhelm.fuzzy.mamdani import MamdaniEngine
from fuzzhelm.fuzzy.membership import ANY_TERM, MembershipConfig, gauss, membership_from_dict, trap, tri
from fuzzhelm.fuzzy.rules import Rule, RuleBase, load_rulebase

pytestmark = pytest.mark.property

ROOT = Path(__file__).resolve().parents[2]
RULES_YAML = ROOT / "config" / "rules_mamdani.yaml"

SPEC_MEMBERSHIP: dict[str, Any] = {
    "version": 3,
    "variables": {
        "T": {"range": [-1.0, 1.0], "terms": {
            "STRONG_DOWN": {"type": "trap", "points": [-1.0, -1.0, -0.70, -0.35]},
            "WEAK_DOWN": {"type": "tri", "points": [-0.70, -0.35, 0.0]},
            "NEUTRAL": {"type": "tri", "points": [-0.35, 0.0, 0.35]},
            "WEAK_UP": {"type": "tri", "points": [0.0, 0.35, 0.70]},
            "STRONG_UP": {"type": "trap", "points": [0.35, 0.70, 1.0, 1.0]}}},
        "R": {"range": [-1.0, 1.0], "terms": {
            "SELL_PRESSURE": {"type": "trap", "points": [-1.0, -1.0, -0.45, 0.0]},
            "NO_PRESSURE": {"type": "tri", "points": [-0.45, 0.0, 0.45]},
            "BUY_PRESSURE": {"type": "trap", "points": [0.0, 0.45, 1.0, 1.0]}}},
        "V": {"range": [0.0, 1.0], "terms": {
            "LO": {"type": "gauss", "m": 0.17, "sigma": 0.17},
            "MID": {"type": "gauss", "m": 0.51, "sigma": 0.17},
            "HI": {"type": "gauss", "m": 0.88, "sigma": 0.185}}},
        "U": {"range": [-1.0, 1.0], "terms": {
            "STRONG_SHORT": {"type": "trap", "points": [-1.0, -1.0, -0.80, -0.45]},
            "SHORT": {"type": "tri", "points": [-0.80, -0.40, 0.0]},
            "HOLD": {"type": "tri", "points": [-0.40, 0.0, 0.40]},
            "LONG": {"type": "tri", "points": [0.0, 0.40, 0.80]},
            "STRONG_LONG": {"type": "trap", "points": [0.45, 0.80, 1.0, 1.0]}}},
    },
    "defuzz": {"scheme": "trapezoid", "grid_nodes": 201},
}
MEMBERSHIP: MembershipConfig = membership_from_dict(SPEC_MEMBERSHIP)
RULEBASE: RuleBase = load_rulebase(RULES_YAML, MEMBERSHIP, production=True)
ENGINE = MamdaniEngine(MEMBERSHIP, RULEBASE)

COARSE_GAP = 1.0          # «груба» монотонність: крок T щонайменше на половину діапазону
REVERSAL_BOUND = 0.16     # sup локального реверсу ≈ 0.15902 (сітка + Нелдер–Мід; DE з 12 сідів — 0.159017)

unit = st.floats(-1.0, 1.0, allow_nan=False, allow_infinity=False)
unit01 = st.floats(0.0, 1.0, allow_nan=False, allow_infinity=False)


def test_all_weights_are_one_in_pinned_rulebase() -> None:
    assert len(RULEBASE) == 45 and all(r.w == 1.0 for r in RULEBASE)


# ================================================================== G16 (рескоупнутий)


@settings(max_examples=500, deadline=None)
@given(t1=st.floats(-1.0, 0.0, allow_nan=False), s=unit01, r=unit, v=unit01)
@example(t1=0.0, s=1.0, r=0.007066, v=1.0)         # найгірша точка: запас ≈ 0.00723 (див. нижче)
@example(t1=-0.35, s=0.0, r=0.4, v=0.0)           # вхід контрприкладу з w = 0.8: при w ≡ 1 — монотонно
@example(t1=-1.0, s=1.0, r=-1.0, v=0.0)
def test_u_nondecreasing_in_trend_input(t1: float, s: float, r: float, v: float) -> None:
    """w_r ≡ 1: T₂ ≥ T₁ + 1.0 ⇒ u(T₂) ≥ u(T₁) − 1e−9 (500 прикладів).

    Рескоуп дослівного твердження брифінгу (T₂ > T₁ ⇒ u(T₂) ≥ u(T₁) − 1e−9), яке для Мамдані
    max-min + центроїд хибне навіть при w ≡ 1 — див. наступний тест і deviations D-FZ-1.
    """
    t2 = min(1.0, t1 + COARSE_GAP + s * (-t1))    # T₂ ∈ [T₁ + 1.0; 1]
    assert t2 - t1 >= COARSE_GAP - 1e-12
    u1, u2 = ENGINE.infer_u(t1, r, v), ENGINE.infer_u(t2, r, v)
    assert u2 >= u1 - 1e-9, (t1, t2, r, v, u1, u2)


def test_u_coarse_monotonicity_margin_is_small_but_positive() -> None:
    """Найгірша точка «грубої» монотонності (w ≡ 1, крок T рівно 1.0) — запас додатний, але малий.

    Як знайдено: мінімізація u(T₂) − u(T₁) за (T₁, T₂ ≥ T₁ + 1, R, V) диференціальною еволюцією
    (scipy, 12 сідів) з доуточненням Нелдером–Мідом: T₁ = 0, T₂ = 1, R ≈ 0.007066, V = 1; дзеркальна
    точка (непарна симетрія) — T₁ = −1, T₂ = 0, R ≈ −0.007066. Поріг кроку ≈ 0.99: при кроці 0.975
    запас уже ≈ −1.3e−4 (та сама процедура), тож 1.0 — майже впритул.
    """
    r = 0.007066
    slack = ENGINE.infer_u(1.0, r, 1.0) - ENGINE.infer_u(0.0, r, 1.0)
    assert slack == pytest.approx(0.0072329, abs=1e-6)
    assert 0.0 < slack < 0.01
    mirror = ENGINE.infer_u(0.0, -r, 1.0) - ENGINE.infer_u(-1.0, -r, 1.0)
    assert mirror == pytest.approx(slack, abs=1e-12)


def test_reversal_comes_from_repeated_consequents_along_t() -> None:
    """Механізм D-FZ-1 на одновимірній таблиці T → U (R, V — "any"), ті самі МФ і той самий рушій.

    * строго зростаюча таблиця (SD→SS, WD→S, NEU→H, WU→L, SU→SL) дає монотонне u(T) — немонотонність
      не є властивістю самого рушія на будь-якій таблиці;
    * рядок (SELL_PRESSURE, LO) робочої бази — SS, SS, SS, SS, S — немонотонний навіть без R і V:
      між ядрами WEAK_DOWN (−0.35) і NEUTRAL (0) обидва терми ведуть у STRONG_SHORT, висота обрізання
      провалюється до 0.5 при T = −0.175, а центроїд обрізаного несиметричного плеча trap(−1,−1,−0.8,−0.45)
      з меншою висотою зсувається праворуч: u = −0.79893 → −0.76605 → −0.79893.
    Декларовані політики (§5.6) неминуче дають повтори наслідків уздовж T (строго зростаючий рядок
    мусив би бути SS, S, H, L, SL для всіх (R, V), тобто без залежності від R і V), а повтор несиметричного
    наслідку чи повтор поруч з іншими активними правилами і дає реверс.
    """
    t_names, u_names = MEMBERSHIP.T.term_names, MEMBERSHIP.U.term_names

    def one_d(row: tuple[str, ...]) -> MamdaniEngine:
        rules = tuple(Rule(f"X{i}", {"T": t, "R": ANY_TERM, "V": ANY_TERM}, c)
                      for i, (t, c) in enumerate(zip(t_names, row, strict=True)))
        return MamdaniEngine(MEMBERSHIP, RuleBase(rules))

    ts = np.linspace(-1.0, 1.0, 20001)
    strict = one_d(tuple(u_names)).infer_batch(ts, 0.0, 0.5)
    assert float(np.max(np.maximum.accumulate(strict) - strict)) <= 1e-12

    flat = one_d(("STRONG_SHORT",) * 4 + ("SHORT",))
    u = flat.infer_batch(ts, 0.0, 0.5)
    assert float(np.max(np.maximum.accumulate(u) - u)) == pytest.approx(0.032885, abs=1e-6)
    u_core, u_cross, u_next = (flat.infer_u(t, 0.0, 0.5) for t in (-0.35, -0.175, 0.0))
    assert u_core == pytest.approx(-0.798933, abs=1e-6) == u_next
    assert u_cross == pytest.approx(-0.766049, abs=1e-6)
    assert u_cross > u_core and u_next < u_cross                    # T зростає: u вгору, потім униз


def test_u_local_monotonicity_fails_with_unit_weights_counterexample() -> None:
    """Суворий тест-документ: дослівна властивість брифінгу порушується при w ≡ 1.

    (а) мінімальний контрприклад, знайдений і стиснутий hypothesis для дослівної властивості
        (500 прикладів, T₁ < T₂ довільні): T₁ = −0.1875, T₂ = 0.0, R = −1.0, V = 0.0.
        Механізм: обидва терми WEAK_DOWN/NEUTRAL при V = LO, R = SELL дають STRONG_SHORT; при
        T₁ висота β_SS = max(0.464, 0.536) = 0.536 < 0.6065 = β_SS(T₂), а центроїд обрізаного
        плеча trap(−1,−1,−0.8,−0.45) зсувається ліворуч зі зростанням висоти обрізання.
    (б) найбільший реверс (сітка 2001×201×101 + Нелдер–Мід): T₁ = −0.35 (ядро WEAK_DOWN),
        T₂ = −0.175 (точка перетину WEAK_DOWN/NEUTRAL, λ = 0.5), R ≈ 0.4788, V ≈ 0.1827:
        u падає ≈ на 0.159 — спільний наслідок STRONG_LONG просідає до висоти 0.5, і вагу
        отримують правила MID (WEAK_DOWN, BUY, MID → SHORT).
    """
    u1, u2 = ENGINE.infer_u(-0.1875, -1.0, 0.0), ENGINE.infer_u(0.0, -1.0, 0.0)
    assert u1 == pytest.approx(-0.757898291420177, abs=1e-9)
    assert u2 == pytest.approx(-0.7639553919693896, abs=1e-9)
    assert u2 < u1 - 5e-3                                          # T зросло, u спало

    t1, t2, r, v = -0.35, -0.175, 0.47882051, 0.18266415
    w1, w2 = ENGINE.infer_u(t1, r, v), ENGINE.infer_u(t2, r, v)
    assert w1 - w2 == pytest.approx(0.15902, abs=1e-4)
    # механізм: на перетині T-термів висота STRONG_LONG = 0.5, у ядрі WEAK_DOWN — min(μ_BUY, μ_LO)
    k_sl = MEMBERSHIP.U.term_names.index("STRONG_LONG")
    mu_t2 = MEMBERSHIP.T.fuzzify(t2)
    assert mu_t2["WEAK_DOWN"] == pytest.approx(0.5, abs=1e-12) == pytest.approx(mu_t2["NEUTRAL"], abs=1e-12)
    assert ENGINE.consequent_strengths(t2, r, v)[k_sl] == pytest.approx(0.5, abs=1e-12)
    expected_core = min(MEMBERSHIP.R.fuzzify(r)["BUY_PRESSURE"], MEMBERSHIP.V.fuzzify(v)["LO"])
    assert ENGINE.consequent_strengths(t1, r, v)[k_sl] == pytest.approx(expected_core, abs=1e-12)


@settings(max_examples=500, deadline=None)
@given(a=unit, b=unit, r=unit, v=unit01)
@example(a=-0.35, b=-0.175, r=0.47882051, v=0.18266415)
def test_u_local_reversal_is_bounded(a: float, b: float, r: float, v: float) -> None:
    """Локальна немонотонність обмежена: T₁ ≤ T₂ ⇒ u(T₂) ≥ u(T₁) − 0.16 (w ≡ 1)."""
    t1, t2 = min(a, b), max(a, b)
    assert ENGINE.infer_u(t2, r, v) >= ENGINE.infer_u(t1, r, v) - REVERSAL_BOUND


# ================================================================== G17


def test_weighted_rules_break_monotonicity_counterexample() -> None:
    """При w ≠ const ламається навіть «груба» монотонність, яка при w ≡ 1 виконується.

    Як знайдено: перебір усіх 45 правил з w = 0.8 для одного правила (решта 1.0) на сітці
    T (крок 0.01) × R (41) × V (21) з пошуком min_{T₂ ≥ T₁+1.0} u(T₂) − u(T₁); найбільше порушення
    дали кутові правила R15 (STRONG_UP, BUY, LO → STRONG_LONG) і дзеркальне R01 (−0.022), далі —
    R07/R09 (−0.017). Беремо симетричну пару R01, R15 з w = 0.8 (зберігає непарну симетрію) і
    найгірший вхід з сітки: T₁ = −0.35, T₂ = 0.65, R = 0.4, V = 0.0.
    Механізм: у точці T₂ домінує R15 зі зниженою вагою — β_STRONG_LONG = 0.8·0.6065 = 0.485 замість
    0.6065, а при T₁ той самий наслідок дає R12 з w = 1; частка HOLD/LONG росте, центроїд падає.
    """
    data = yaml.safe_load(RULES_YAML.read_text(encoding="utf-8"))
    ids = [r["id"] for r in data["rules"]]
    for rid in ("R01", "R15"):
        data["rules"][ids.index(rid)]["w"] = 0.8
    with pytest.raises(ConfigValidationError) as exc:                  # у робочу конфігурацію — ні
        load_rulebase(copy.deepcopy(data), MEMBERSHIP, production=True)
    assert exc.value.path == "rules[0].w"
    weighted = MamdaniEngine(MEMBERSHIP, load_rulebase(data, MEMBERSHIP, production=False))

    t1, t2, r, v = -0.35, 0.65, 0.4, 0.0
    assert t2 - t1 >= COARSE_GAP - 1e-12
    u1_w1, u2_w1 = ENGINE.infer_u(t1, r, v), ENGINE.infer_u(t2, r, v)
    u1_w, u2_w = weighted.infer_u(t1, r, v), weighted.infer_u(t2, r, v)
    assert u2_w1 >= u1_w1                                              # w ≡ 1: монотонно
    assert u1_w == u1_w1                                               # R01/R15 при T₁ не спрацьовують
    assert u2_w < u1_w - 0.02                                          # w = 0.8: T зросло на 1.0, u спало
    assert u1_w == pytest.approx(0.57528896593477, abs=1e-9)
    assert u2_w == pytest.approx(0.5533251606684843, abs=1e-9)
    k_sl = MEMBERSHIP.U.term_names.index("STRONG_LONG")
    assert weighted.consequent_strengths(t2, r, v)[k_sl] == pytest.approx(0.8 * math.exp(-0.5), abs=1e-12)


# ================================================================== інші властивості


@settings(max_examples=300, deadline=None)
@given(t=unit, r=unit, v=unit01)
def test_engine_output_is_odd_symmetric(t: float, r: float, v: float) -> None:
    """Непарна симетрія таблиці + симетричні терми U ⇒ u(−T, −R, V) = −u(T, R, V)."""
    assert abs(ENGINE.infer_u(-t, -r, v) + ENGINE.infer_u(t, r, v)) <= 1e-12


@settings(max_examples=300, deadline=None)
@given(t=st.floats(-3.0, 3.0), r=st.floats(-3.0, 3.0), v=st.floats(-1.0, 2.0))
def test_u_is_finite_and_bounded(t: float, r: float, v: float) -> None:
    res = ENGINE.infer(t, r, v)
    assert math.isfinite(res.u_raw) and -1.0 <= res.u_raw <= 1.0
    assert res.u_raw == ENGINE.infer_u(t, r, v)
    assert all(0.0 < f.alpha <= 1.0 for f in res.fired)


@settings(max_examples=300, deadline=None)
@given(pts=st.lists(st.floats(-2.0, 2.0), min_size=4, max_size=4).map(sorted),
       x=st.floats(-3.0, 3.0), m=unit, s=st.floats(0.01, 1.0))
def test_membership_values_in_unit_interval(pts: list[float], x: float, m: float, s: float) -> None:
    a, b, c, d = pts
    mfs = [gauss(m, s)]
    if a < d:
        mfs.append(trap(a, b, c, d))
    if a < c:
        mfs.append(tri(a, b, c))
    for mf in mfs:
        y = mf(x)
        assert 0.0 <= y <= 1.0
        assert mf(np.array([x]))[0] == pytest.approx(y, abs=1e-15)
