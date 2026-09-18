"""G. Нечітке ядро: МФ, база правил, Мамдані, дефазифікація, лінійна базова лінія.

Найменування: tests/unit/test_fuzzy.py
Призначення: 15 із 17 тестів групи G брифінгу (§10) + додаткові перевірки контрактів модуля.
    Тести монотонності (#16, #17) — у tests/property/test_fuzzy_property.py.
Автор: Андрій Жук, 2026.
"""

from __future__ import annotations

import copy
import math
import pickle
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import yaml

from fuzzhelm.core.errors import ConfigValidationError
from fuzzhelm.fuzzy.base import FuzzyResult, InferenceEngine
from fuzzhelm.fuzzy.defuzz import (
    aggregate,
    centroid,
    convergence_study,
    exact_centroid,
    make_grid,
    nodes_for_delta,
    quadrature_weights,
)
from fuzzhelm.fuzzy.linear import LinearVoteEngine
from fuzzhelm.fuzzy.mamdani import MamdaniEngine, default_engine
from fuzzhelm.fuzzy.membership import (
    LinguisticVariable,
    MembershipConfig,
    Triangular,
    gauss,
    load_membership,
    membership_from_dict,
    trap,
    tri,
)
from fuzzhelm.fuzzy.rules import Rule, RuleBase, format_rule_table_md, load_rulebase, rule_table
from fuzzhelm.fuzzy.surface import control_surface, monotonicity_scan

ROOT = Path(__file__).resolve().parents[2]
RULES_YAML = ROOT / "config" / "rules_mamdani.yaml"
MEMBERSHIP_YAML = ROOT / "config" / "membership.yaml"

T_IDX = {"STRONG_DOWN": -2, "WEAK_DOWN": -1, "NEUTRAL": 0, "WEAK_UP": 1, "STRONG_UP": 2}
R_IDX = {"SELL_PRESSURE": -1, "NO_PRESSURE": 0, "BUY_PRESSURE": 1}
U_IDX = {"STRONG_SHORT": -2, "SHORT": -1, "HOLD": 0, "LONG": 1, "STRONG_LONG": 2}


@pytest.fixture(scope="module")
def membership() -> MembershipConfig:
    return load_membership(MEMBERSHIP_YAML)


@pytest.fixture(scope="module")
def rulebase(membership: MembershipConfig) -> RuleBase:
    return load_rulebase(RULES_YAML, membership)


@pytest.fixture(scope="module")
def engine(membership: MembershipConfig, rulebase: RuleBase) -> MamdaniEngine:
    return MamdaniEngine(membership, rulebase)


def _rules_yaml() -> dict[str, Any]:
    data = yaml.safe_load(RULES_YAML.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def _sample_inputs(n: int, seed: int) -> list[tuple[float, float, float]]:
    rng = np.random.default_rng(seed)
    pts = rng.uniform((-1.0, -1.0, 0.0), (1.0, 1.0, 1.0), size=(n, 3))
    return [(float(t), float(r), float(v)) for t, r, v in pts]


# ================================================================== G1–G5: функції належності


def test_tri_mf_peak_equals_one() -> None:
    for a, b, c in [(-0.70, -0.35, 0.0), (-0.35, 0.0, 0.35), (0.0, 0.40, 0.80), (-0.3, 0.1, 0.9)]:
        mf = tri(a, b, c)
        assert mf(b) == 1.0
        assert mf(np.array([b]))[0] == 1.0
        assert mf(a) == 0.0 and mf(c) == 0.0
        xs = np.linspace(a - 0.5, c + 0.5, 4001)
        mu = mf(xs)
        assert mu.max() <= 1.0 and mu.min() >= 0.0
        # пік єдиний: поза вершиною μ < 1
        assert np.all(mu[np.abs(xs - b) > 1e-9] < 1.0)


def test_trap_plateau_is_flat() -> None:
    for a, b, c, d in [(-1.0, -1.0, -0.70, -0.35), (0.35, 0.70, 1.0, 1.0), (-0.9, -0.5, 0.2, 0.6)]:
        mf = trap(a, b, c, d)
        plateau = np.linspace(b, c, 1001)
        assert np.all(mf(plateau) == 1.0)                       # рівно 1, не «майже»
        assert all(mf(float(x)) == 1.0 for x in plateau[::50])
        if b > a:
            assert mf(a) == 0.0 and 0.0 < mf((a + b) / 2) < 1.0
        if d > c:
            assert mf(d) == 0.0 and 0.0 < mf((c + d) / 2) < 1.0
    # вертикальне ребро a == b: μ(a) = 1 (плече), лівіше — 0
    shoulder = trap(-1.0, -1.0, -0.70, -0.35)
    assert shoulder(-1.0) == 1.0 and shoulder(-1.0000001) == 0.0


def test_gauss_mf_symmetry() -> None:
    for m, s in [(0.17, 0.17), (0.51, 0.17), (0.88, 0.185), (0.0, 0.3)]:
        mf = gauss(m, s)
        assert mf(m) == 1.0
        deltas = np.linspace(0.0, 1.0, 501)
        left, right = mf(m - deltas), mf(m + deltas)
        assert np.max(np.abs(left - right)) <= 1e-15
        assert math.isclose(mf(m + s), math.exp(-0.5), rel_tol=1e-14)
        assert np.all(np.diff(right) <= 0.0)                    # спадає від центру


def test_mf_partition_of_unity_ruspini(membership: MembershipConfig) -> None:
    # Умова Руспіні перевіряється для кусково-лінійних розбиттів T і R. V — гаусіани (брифінг §5.5):
    # точно Σμ=1 вони не дають за побудовою; див. test_v_gaussians_are_not_a_ruspini_partition і
    # docs/deviations.d/fuzzy.md.
    for name in ("T", "R"):
        var = membership[name]
        x = np.linspace(var.range[0], var.range[1], 2001)
        total = var.evaluate(x).sum(axis=0)
        assert np.max(np.abs(total - 1.0)) <= 1e-12, name
        # скалярний шлях теж
        assert all(abs(sum(var.fuzzify(float(xi)).values()) - 1.0) <= 1e-12 for xi in x[::20])


def test_mf_coverage_no_dead_zones(membership: MembershipConfig) -> None:
    # max_i μ_i(x) ≥ 0.5 для всіх вхідних змінних. На перетині двох сусідніх термів розбиття Руспіні
    # обидва дорівнюють рівно 0.5 в точній арифметиці, тож допуск 1e−12 — лише на округлення.
    for name in ("T", "R", "V"):
        var = membership[name]
        x = np.linspace(var.range[0], var.range[1], 2001)
        cover = var.evaluate(x).max(axis=0)
        assert cover.min() >= 0.5 - 1e-12, (name, float(cover.min()), float(x[np.argmin(cover)]))


def test_v_gaussians_are_not_a_ruspini_partition(membership: MembershipConfig) -> None:
    """Документує розходження D-FZ-2: Σμ_V ≠ 1 (гаусіани), тому Руспіні для V не перевіряється."""
    var = membership.V
    total = var.evaluate(np.linspace(0.0, 1.0, 2001)).sum(axis=0)
    assert np.max(np.abs(total - 1.0)) > 1e-3


def test_scalar_and_vector_mf_paths_agree(membership: MembershipConfig) -> None:
    for var in membership.variables.values():
        x = np.linspace(var.range[0] - 0.1, var.range[1] + 0.1, 777)
        for mf in var.terms.values():
            vec = mf(x)
            sca = np.array([mf(float(xi)) for xi in x])
            if mf.kind == "gauss":
                assert np.max(np.abs(vec - sca)) <= 1e-15
            else:
                assert np.array_equal(vec, sca)                  # tri/trap побітово


def test_fuzzify_clips_to_range_and_rejects_nan(membership: MembershipConfig) -> None:
    assert membership.T.fuzzify(-1.5) == membership.T.fuzzify(-1.0)
    assert membership.V.fuzzify(1.7) == membership.V.fuzzify(1.0)
    for bad in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValueError):
            membership.T.fuzzify(bad)


def test_membership_loader_keeps_provisional_metadata(membership: MembershipConfig) -> None:
    raw = yaml.safe_load(MEMBERSHIP_YAML.read_text(encoding="utf-8"))
    for name, var in membership.variables.items():
        for key in ("source", "source_run_id", "provisional", "silhouette"):
            assert var.meta.get(key) == raw["variables"][name].get(key), (name, key)
    data = copy.deepcopy(raw)
    data["variables"]["V"].update(provisional=True, source="kmeans", calibrated_k=3)
    cfg = membership_from_dict(data)
    assert cfg.V.meta["provisional"] is True and cfg.V.meta["calibrated_k"] == 3
    # to_dict → from_dict — та сама конфігурація
    again = membership_from_dict(cfg.to_dict())
    for name, var in cfg.variables.items():
        assert again[name].terms == var.terms and again[name].range == var.range
        assert again[name].meta == var.meta


@pytest.mark.parametrize(("mutate", "path"), [
    (lambda d: d["variables"]["T"]["terms"]["WEAK_UP"].update(points=[0.0, 0.7, 0.35]),
     "variables.T.terms.WEAK_UP.points"),
    (lambda d: d["variables"]["V"]["terms"]["LO"].update(sigma=0.0), "variables.V.terms.LO.sigma"),
    (lambda d: d["variables"]["R"]["terms"]["NO_PRESSURE"].update(type="bell"),
     "variables.R.terms.NO_PRESSURE.type"),
    (lambda d: d["variables"]["U"].update(range=[1.0, -1.0]), "variables.U.range"),
    (lambda d: d["variables"].pop("V"), "variables"),
    (lambda d: d["variables"]["V"].update(provisional="yes"), "variables.V.provisional"),
    (lambda d: d["defuzz"].update(scheme="simpson", grid_nodes=200), "defuzz.grid_nodes"),
    (lambda d: d["variables"]["T"]["terms"]["NEUTRAL"].update(points=[-0.35, "0", 0.35]),
     "variables.T.terms.NEUTRAL.points[1]"),
])
def test_membership_validation_error_paths(mutate: Any, path: str) -> None:
    data = yaml.safe_load(MEMBERSHIP_YAML.read_text(encoding="utf-8"))
    mutate(data)
    with pytest.raises(ConfigValidationError) as exc:
        membership_from_dict(data)
    assert exc.value.path == path


# ================================================================== G6–G8: база правил


def test_rulebase_yaml_has_exactly_45_rules(membership: MembershipConfig, rulebase: RuleBase) -> None:
    assert len(rulebase) == 45 == 5 * 3 * 3
    assert [r.id for r in rulebase] == [f"R{i:02d}" for i in range(1, 46)]
    assert all(r.w == 1.0 for r in rulebase)
    data = _rules_yaml()
    data["rules"] = data["rules"][:44]
    with pytest.raises(ConfigValidationError) as exc:
        load_rulebase(data, membership)
    assert exc.value.path == "rules" and "exactly 45" in exc.value.detail
    assert "STRONG_UP/BUY_PRESSURE/HI" in exc.value.detail      # підказка: яку комбінацію пропущено


def test_rulebase_no_duplicate_antecedents(membership: MembershipConfig, rulebase: RuleBase) -> None:
    keys = [(r.antecedent["T"], r.antecedent["R"], r.antecedent["V"]) for r in rulebase]
    assert len(set(keys)) == 45
    full = {(t, r, v) for t in membership.T.term_names for r in membership.R.term_names
            for v in membership.V.term_names}
    assert set(keys) == full                                    # кожна комбінація рівно раз
    data = _rules_yaml()
    data["rules"][7]["if"] = dict(data["rules"][2]["if"])       # R08 ← антецедент R03
    with pytest.raises(ConfigValidationError) as exc:
        load_rulebase(data, membership)
    assert exc.value.path == "rules[7].if" and "R03" in exc.value.detail


def test_rulebase_all_terms_exist_in_membership_config(membership: MembershipConfig,
                                                       rulebase: RuleBase) -> None:
    for r in rulebase:
        for var, term in r.antecedent.items():
            assert term in membership[var].term_names
        assert r.consequent in membership.U.term_names
    for field_path, mutate in [
        ("rules[3].if.T", lambda rule: rule["if"].update(T="SIDEWAYS")),
        ("rules[3].if.V", lambda rule: rule["if"].update(V="EXTREME")),
        ("rules[3].then", lambda rule: rule.update(then="ALL_IN")),
        ("rules[3].if.Q", lambda rule: rule["if"].update(Q="LO")),
        ("rules[3].if.R", lambda rule: rule["if"].pop("R")),
    ]:
        data = _rules_yaml()
        mutate(data["rules"][3])
        with pytest.raises(ConfigValidationError) as exc:
            load_rulebase(data, membership)
        assert exc.value.path == field_path


@pytest.mark.parametrize(("mutate", "path"), [
    (lambda d: d["rules"][5].update(w="1.0"), "rules[5].w"),
    (lambda d: d["rules"][5].update(w=1.5), "rules[5].w"),
    (lambda d: d["rules"][5].update(extra=1), "rules[5].extra"),
    (lambda d: d["rules"][5].pop("then"), "rules[5].then"),
    (lambda d: d["rules"][5].update(id="R01"), "rules[5].id"),
    (lambda d: d.update(rules={"R01": 1}), "rules"),
])
def test_rulebase_validation_error_paths(membership: MembershipConfig, mutate: Any, path: str) -> None:
    data = _rules_yaml()
    mutate(data)
    with pytest.raises(ConfigValidationError) as exc:
        load_rulebase(data, membership)
    assert exc.value.path == path


def test_production_rejects_non_unit_weight_but_nonproduction_allows(membership: MembershipConfig) -> None:
    data = _rules_yaml()
    data["rules"][14]["w"] = 0.8
    with pytest.raises(ConfigValidationError) as exc:
        load_rulebase(data, membership, production=True)
    assert exc.value.path == "rules[14].w"
    rb = load_rulebase(copy.deepcopy(data), membership, production=False)
    assert rb.by_id("R15").w == 0.8


def test_rulebase_loads_from_yaml_text(membership: MembershipConfig, rulebase: RuleBase) -> None:
    text = RULES_YAML.read_text(encoding="utf-8")
    assert load_rulebase(text, membership).rules == rulebase.rules
    assert load_rulebase(str(RULES_YAML), membership).rules == rulebase.rules


def test_rule_table_satisfies_declared_constraints(membership: MembershipConfig, rulebase: RuleBase) -> None:
    """Вичерпна перевірка таблиці 45 правил: монотонність за індексами T і R та непарна симетрія."""
    c = {(T_IDX[r.antecedent["T"]], R_IDX[r.antecedent["R"]], r.antecedent["V"]): U_IDX[r.consequent]
         for r in rulebase}
    for v in ("LO", "MID", "HI"):
        for r in (-1, 0, 1):
            row = [c[(t, r, v)] for t in range(-2, 3)]
            assert row == sorted(row), (v, r, row)                          # не спадає за T
        for t in range(-2, 3):
            col = [c[(t, r, v)] for r in (-1, 0, 1)]
            assert col == sorted(col), (v, t, col)                          # не спадає за R
            for r in (-1, 0, 1):
                assert c[(-t, -r, v)] == -c[(t, r, v)]                      # непарна симетрія
    assert rulebase.by_id("R01").antecedent == {"T": "STRONG_DOWN", "R": "SELL_PRESSURE", "V": "LO"}
    assert rulebase.by_id("R01").consequent == "STRONG_SHORT"


def test_rule_table_implements_three_declared_policies(rulebase: RuleBase) -> None:
    def sign(x: int) -> int:
        return (x > 0) - (x < 0)

    def expected(v: str, t: int, r: int) -> int:
        if v == "LO":                                    # домінує реверсія
            return max(-2, min(2, 2 * r + int(t / 2)))
        if v == "MID":                                   # тренд перемагає у конфлікті
            if t != 0 and sign(r) == sign(t) and abs(t) == 1:
                return t + r
            return t if t != 0 else r
        return sign(t) if sign(t) == sign(r) != 0 else 0  # HI: усе до HOLD, крім згоди T і R

    for rule in rulebase:
        t, r, v = T_IDX[rule.antecedent["T"]], R_IDX[rule.antecedent["R"]], rule.antecedent["V"]
        assert U_IDX[rule.consequent] == expected(v, t, r), rule.id
    # політика HI: ненульовий вихід лише при узгоджених T і R
    for rule in rulebase:
        if rule.antecedent["V"] == "HI" and rule.consequent != "HOLD":
            t, r = T_IDX[rule.antecedent["T"]], R_IDX[rule.antecedent["R"]]
            assert sign(t) == sign(r) != 0


def test_rule_table_helper_matches_rulebase(membership: MembershipConfig, rulebase: RuleBase) -> None:
    table = rule_table(rulebase, membership)
    assert table["LO"][0][0] == "STRONG_SHORT"                 # R01
    assert table["MID"][1][4] == "STRONG_LONG"                 # (STRONG_UP, NO_PRESSURE, MID)
    assert sum(len(row) for rows in table.values() for row in rows) == 45


# ================================================================== G9–G14: виведення Мамдані


def test_rule_firing_is_min_of_memberships(engine: MamdaniEngine, rulebase: RuleBase) -> None:
    for T, R, V in [*_sample_inputs(40, seed=11), (0.0, 0.0, 0.5), (-1.0, -1.0, 0.0), (0.525, 0.2, 0.9)]:
        res = engine.infer(T, R, V)
        mu = res.memberships
        fired = {f.rule_id: f.alpha for f in res.fired}
        for rule in rulebase:
            a = rule.antecedent
            expected = min(mu["T"][a["T"]], mu["R"][a["R"]], mu["V"][a["V"]])
            if expected > 0.0:
                assert fired[rule.id] == expected                # рівно min, без допуску
            else:
                assert rule.id not in fired
        # ступені належності в трасі — ті самі, що дає фазифікація кожної змінної окремо
        for name, x in (("T", T), ("R", R), ("V", V)):
            assert mu[name] == engine.membership[name].fuzzify(x), name
        alphas = [f.alpha for f in res.fired]
        assert alphas == sorted(alphas, reverse=True)            # за спаданням α


def test_dont_care_term_contributes_one(membership: MembershipConfig) -> None:
    any_rb = RuleBase((Rule("A1", {"T": "WEAK_UP", "R": "any", "V": "any"}, "LONG"),))
    explicit_rb = RuleBase((Rule("E1", {"T": "WEAK_UP", "R": "NO_PRESSURE", "V": "MID"}, "LONG"),))
    e_any = MamdaniEngine(membership, any_rb)
    e_exp = MamdaniEngine(membership, explicit_rb)
    for T, R, V in [(0.2, -0.9, 0.05), (0.35, 0.7, 0.99), (0.5, 0.0, 0.51), (0.1, 0.3, 0.3)]:
        mu_t = membership.T.fuzzify(T)["WEAK_UP"]
        (f_any,) = e_any.infer(T, R, V).fired
        assert f_any.alpha == mu_t                               # "any" дає рівно 1.0 у min
        exp_fired = e_exp.infer(T, R, V).fired
        mu_r = membership.R.fuzzify(R)["NO_PRESSURE"]
        mu_v = membership.V.fuzzify(V)["MID"]
        assert (exp_fired[0].alpha if exp_fired else 0.0) == min(mu_t, mu_r, mu_v)
    # "any" у повній базі неможливий: розгортається в 15 комбінацій, які вже покриті
    with pytest.raises(ConfigValidationError):
        load_rulebase({"rules": [r.to_dict() for r in any_rb.rules]}, membership)


def test_clipped_consequent_never_exceeds_alpha(membership: MembershipConfig) -> None:
    grid = make_grid(201)
    for cons in membership.U.term_names:
        rb = RuleBase((Rule("S1", {"T": "WEAK_UP", "R": "any", "V": "any"}, cons),))
        eng = MamdaniEngine(membership, rb)
        for T in (0.05, 0.1, 0.2, 0.3, 0.35):
            res = eng.infer(T, 0.0, 0.5)
            alpha = res.fired[0].alpha
            assert res.mu_agg is not None
            assert res.mu_agg.max() <= alpha                     # обрізання: ніде не вище α
            assert res.mu_agg.max() == alpha                     # і досягає α на ядрі наслідку
            mu_d = membership.U.terms[cons](grid)
            assert np.array_equal(res.mu_agg, np.minimum(alpha, mu_d))


def test_aggregation_is_pointwise_max(engine: MamdaniEngine, membership: MembershipConfig) -> None:
    grid = engine.grid
    u_terms = membership.U.terms
    for T, R, V in _sample_inputs(30, seed=7):
        res = engine.infer(T, R, V)
        # еталон: по правилах (не по термах), max_r min(α_r, μ_{D_r}(u)) — незалежно від оптимізації β_k
        ref = np.zeros_like(grid)
        for f in res.fired:
            ref = np.maximum(ref, np.minimum(f.alpha, u_terms[f.consequent](grid)))
        assert res.mu_agg is not None
        assert np.array_equal(res.mu_agg, ref)
        # max-агрегація ≥ кожного обрізаного наслідку і не є сумою
        for f in res.fired:
            assert np.all(res.mu_agg >= np.minimum(f.alpha, u_terms[f.consequent](grid)))


def test_centroid_of_symmetric_aggregate_is_zero(engine: MamdaniEngine) -> None:
    rng = np.random.default_rng(3)
    for scheme in ("rect", "trapezoid", "simpson"):
        grid = make_grid(201)
        for _ in range(20):
            half = rng.uniform(0.0, 1.0, 101)
            mu = np.concatenate([half, half[-2::-1]])            # μ(u) = μ(−u)
            assert abs(centroid(grid, mu, scheme)) <= 1e-12
    # непарна симетрія бази правил ⇒ при T = R = 0 агрегована фігура симетрична ⇒ u = 0
    for V in (0.0, 0.2, 0.5, 0.88, 1.0):
        res = engine.infer(0.0, 0.0, V)
        assert res.mu_agg is not None
        assert np.array_equal(res.mu_agg, res.mu_agg[::-1])
        assert abs(res.u_raw) <= 1e-12


def test_empty_activation_returns_exactly_zero(membership: MembershipConfig) -> None:
    rb = RuleBase((Rule("X1", {"T": "STRONG_UP", "R": "BUY_PRESSURE", "V": "any"}, "STRONG_LONG"),))
    for scheme in ("rect", "trapezoid", "simpson"):
        eng = MamdaniEngine(membership, rb, scheme=scheme)
        res = eng.infer(-0.8, -0.9, 0.5)                         # правило не спрацьовує
        assert res.fired == ()
        assert res.u_raw == 0.0 and not math.isnan(res.u_raw)
        assert eng.infer_u(-0.8, -0.9, 0.5) == 0.0
        assert res.mu_agg is not None and not res.mu_agg.any()
        grid = make_grid(201)
        assert centroid(grid, np.zeros(201), scheme) == 0.0


# ================================================================== G15: збіжність дефазифікації


def test_defuzz_grid_convergence_order_is_two_for_trapezoid(engine: MamdaniEngine) -> None:
    # Злами μ_agg у невузлових точках роблять оцінку порядку з одного відношення шумною (див. p_brief_q1/q3),
    # тому порядок оцінюється МНК-нахилом log RMS(похибки) від log Δ за 8 половинними кроками
    # Δ = 0.04…0.0003125 відносно ТОЧНОГО центроїда (кусково-лінійне інтегрування) на 60 входах.
    inputs = _sample_inputs(60, seed=20260918)
    study = convergence_study(engine, inputs)
    assert study["reference"] == "exact_piecewise_linear"
    trap_s = study["schemes"]["trapezoid"]
    assert 1.85 <= trap_s["fitted_order"] <= 2.15, trap_s["fitted_order"]
    # похибка спадає з кроком (перевірка напряму, а не лише нахилу)
    errs = [row["rms_err"] for row in trap_s["rows"]]
    assert errs == sorted(errs, reverse=True)
    # робочий критерій брифінгу: |u(0.01) − u(0.001)| < 1e−3 (для всіх 60 входів)
    assert trap_s["criterion_max_diff"] < 1e-3
    # для порівняння: прямокутники без крайових поправок — перший порядок (тому й не робоча схема)
    assert 0.85 <= study["schemes"]["rect"]["fitted_order"] <= 1.15


def test_exact_reference_matches_very_fine_trapezoid(engine: MamdaniEngine) -> None:
    U = engine.membership.U
    mfs = list(U.terms.values())
    n = nodes_for_delta(1e-5)
    grid = make_grid(n)
    mu_terms = U.evaluate(grid)
    for T, R, V in _sample_inputs(4, seed=5):
        beta = engine.consequent_strengths(T, R, V)
        fine = centroid(grid, aggregate(beta, mu_terms), "trapezoid")
        assert abs(exact_centroid(mfs, beta.tolist()) - fine) < 1e-9


def test_quadrature_weights_and_grid() -> None:
    g = make_grid(201)
    assert g[0] == -1.0 and g[-1] == 1.0 and np.array_equal(g, -g[::-1])
    assert np.allclose(np.diff(g), 0.01, atol=1e-15)
    for scheme in ("rect", "trapezoid", "simpson"):
        w = quadrature_weights(201, scheme)
        assert math.isclose(float(w @ np.ones(201)), 2.0 + (0.01 if scheme == "rect" else 0.0), rel_tol=1e-12)
    with pytest.raises(ValueError):
        quadrature_weights(200, "simpson")                       # непарна кількість інтервалів
    with pytest.raises(ValueError):
        nodes_for_delta(0.03)


# ================================================================== рушій: контракти і швидкий шлях


def test_mamdani_engine_satisfies_protocol(engine: MamdaniEngine) -> None:
    assert isinstance(engine, InferenceEngine)
    assert engine.name == "mamdani" and engine.nodes == 201 and engine.scheme == "trapezoid"
    res = engine.infer(0.4, -0.3, 0.3)
    assert isinstance(res, FuzzyResult) and res.engine == "mamdani"
    assert set(res.inputs) == {"T", "R", "V"} and set(res.memberships) == {"T", "R", "V"}
    assert res.grid is not None and res.grid.shape == (201,)
    assert -1.0 <= res.u_raw <= 1.0
    assert res.extras["height"] == max(f.alpha for f in res.fired)


def test_infer_u_fast_path_equals_infer_and_batch(engine: MamdaniEngine) -> None:
    inputs = _sample_inputs(200, seed=1)
    fast = [engine.infer_u(*x) for x in inputs]
    full = [engine.infer(*x).u_raw for x in inputs]
    assert fast == full                                          # побітово
    t, r, v = (np.array(c) for c in zip(*inputs, strict=True))
    assert np.max(np.abs(engine.infer_batch(t, r, v) - np.array(fast))) <= 1e-12


def test_engine_rejects_non_finite_inputs(engine: MamdaniEngine) -> None:
    for bad in [(float("nan"), 0.0, 0.5), (0.0, float("inf"), 0.5), (0.0, 0.0, float("nan"))]:
        with pytest.raises(ValueError):
            engine.infer(*bad)
        with pytest.raises(ValueError):
            engine.infer_u(*bad)


def test_engine_is_picklable_for_worker_processes(engine: MamdaniEngine) -> None:
    clone = pickle.loads(pickle.dumps(engine))
    assert clone.infer_u(0.3, -0.2, 0.7) == engine.infer_u(0.3, -0.2, 0.7)


def test_default_engine_uses_config_files() -> None:
    eng = default_engine()
    assert len(eng.rulebase) == 45 and eng.nodes == 201


def test_rule_ids_in_trace_are_sorted_ties_by_id(engine: MamdaniEngine) -> None:
    res = engine.infer(0.0, 0.0, 0.51)                           # T, R у ядрах: багато однакових α
    keys = [(-f.alpha, f.rule_id) for f in res.fired]
    assert keys == sorted(keys)


# ================================================================== лінійна базова лінія і поверхня


def test_linear_vote_engine_contract_and_formula() -> None:
    eng = LinearVoteEngine({"T": 0.6, "R": 0.4})
    assert isinstance(eng, InferenceEngine) and eng.name == "linear"
    res = eng.infer(0.5, -0.25, 0.9)
    assert res.fired == () and res.engine == "linear"
    assert math.isclose(res.u_raw, 0.6 * 0.5 - 0.4 * 0.25, rel_tol=1e-15)
    assert eng.infer_u(0.5, -0.25, 0.1) == res.u_raw             # V не впливає за побудовою
    assert LinearVoteEngine().infer_u(1.0, 1.0, 0.5) == 1.0
    assert LinearVoteEngine({"T": 1.0}).infer_u(0.3, -1.0, 0.5) == 0.3
    with pytest.raises(ValueError):
        LinearVoteEngine({"T": 0.0, "R": 0.0})
    with pytest.raises(ValueError):
        LinearVoteEngine({"V": 1.0})


def test_control_surface_shape_orientation_and_symmetry(engine: MamdaniEngine) -> None:
    T, R, U = control_surface(engine, V=0.5, n=21)
    assert T.shape == R.shape == U.shape == (21, 21)
    assert np.all(np.diff(T[0]) > 0) and np.all(np.diff(R[:, 0]) > 0)
    assert U[5, 7] == pytest.approx(engine.infer_u(T[5, 7], R[5, 7], 0.5), abs=1e-12)
    assert np.max(np.abs(U + U[::-1, ::-1])) <= 1e-12             # u(−T,−R) = −u(T,R)
    _, _, UL = control_surface(LinearVoteEngine(), V=0.5, n=5)
    assert UL[0, 0] == -1.0 and UL[-1, -1] == 1.0


def test_monotonicity_scan_detects_local_reversals(engine: MamdaniEngine) -> None:
    # числові межі для зафіксованих МФ — у tests/property/test_fuzzy_property.py; тут — лише те, що
    # не залежить від калібрування V: локальні реверси є (провал висоти на перетині термів T)
    rep = monotonicity_scan(engine, n_T=201, n_R=21, n_V=6)
    assert rep.decreasing_steps > 0 and rep.max_reversal > 0.0
    t1, t2, r, v, u1, u2 = rep.reversal_at
    assert t1 < t2 and u1 - u2 == pytest.approx(rep.max_reversal, abs=1e-15)
    assert engine.infer_u(t2, r, v) < engine.infer_u(t1, r, v)
    # точка найменшого запасу «грубої» монотонності: T₂ — фактичний argmin на [T₁ + gap; 1]
    g1, g2, gr, gv = rep.gap_slack_at
    assert g2 - g1 >= rep.gap - 1e-12
    slack = engine.infer_u(g2, gr, gv) - engine.infer_u(g1, gr, gv)
    assert slack == pytest.approx(rep.min_gap_slack, abs=1e-11)
    lin = monotonicity_scan(LinearVoteEngine(), n_T=101, n_R=11, n_V=3)
    assert lin.decreasing_steps == 0 and lin.max_reversal == 0.0


# ================================================================== допоміжні API і межові випадки


def test_rulebase_helpers_roundtrip_and_errors(membership: MembershipConfig, rulebase: RuleBase) -> None:
    again = load_rulebase(rulebase.to_dict(), membership)
    assert again.rules == rulebase.rules and again.version == rulebase.version
    assert rulebase.lookup("STRONG_UP", "NO_PRESSURE", "MID").id == "R25"
    with pytest.raises(KeyError):
        rulebase.by_id("R99")
    with pytest.raises(KeyError):
        rulebase.lookup("UP", "NO_PRESSURE", "MID")
    with pytest.raises(KeyError):
        rulebase.with_weights({"R99": 0.5})
    with pytest.raises(ValueError):
        rulebase.with_weights({"R01": 0.0})
    weighted = rulebase.with_weights({"R01": 0.8})
    assert weighted.by_id("R01").w == 0.8 and rulebase.by_id("R01").w == 1.0   # копія, не мутація
    md = format_rule_table_md(rulebase, membership)
    assert md.count("**V = ") == 3 and "STRONG_SHORT (R01)" in md


def test_engine_constructor_and_regrid(membership: MembershipConfig, rulebase: RuleBase) -> None:
    with pytest.raises(ValueError):
        MamdaniEngine(membership, rulebase, scheme="midpoint")
    with pytest.raises(ValueError):
        MamdaniEngine(membership, rulebase, scheme="simpson", nodes=200)
    bad = RuleBase((Rule("B1", {"T": "UP", "R": "any", "V": "any"}, "LONG"),))
    with pytest.raises(ConfigValidationError):
        MamdaniEngine(membership, bad)
    eng = MamdaniEngine(membership, rulebase).with_grid(nodes=2001, scheme="simpson")
    assert eng.nodes == 2001 and eng.scheme == "simpson" and eng.consequent_mu.shape == (5, 2001)
    assert not eng.consequent_mu.flags.writeable


def test_defuzz_argument_validation(membership: MembershipConfig) -> None:
    with pytest.raises(ValueError):
        centroid(make_grid(11), np.ones(10))
    with pytest.raises(ValueError):
        quadrature_weights(201, "midpoint")
    with pytest.raises(ValueError):
        quadrature_weights(2, "rect")
    with pytest.raises(ValueError):
        exact_centroid(list(membership.V.terms.values()), [1.0, 0.5, 0.2], 0.0, 1.0)  # гаусіани
    assert exact_centroid(list(membership.U.terms.values()), [0.0] * 5) == 0.0
    # точний центроїд одного необрізаного симетричного терму = його вершина
    assert exact_centroid([tri(0.0, 0.4, 0.8)], [1.0]) == pytest.approx(0.4, abs=1e-15)


def test_mf_constructors_reject_invalid_parameters(membership: MembershipConfig) -> None:
    for bad in [lambda: trap(0.0, 0.5, 0.4, 1.0), lambda: trap(0.5, 0.5, 0.5, 0.5),
                lambda: tri(0.0, float("nan"), 1.0), lambda: gauss(0.5, 0.0),
                lambda: Triangular(0.0, 0.3, 0.4, 1.0),
                lambda: LinguisticVariable("X", (1.0, 0.0), membership.T.terms),
                lambda: LinguisticVariable("X", (0.0, 1.0), {})]:
        with pytest.raises(ValueError):
            bad()
    assert tri(-0.35, 0.0, 0.35).to_dict() == {"type": "tri", "points": [-0.35, 0.0, 0.35]}
    assert load_membership(MEMBERSHIP_YAML.read_text(encoding="utf-8")).T.terms == membership.T.terms


def test_linear_engine_batch_and_non_finite() -> None:
    eng = LinearVoteEngine()
    out = eng.infer_batch(np.array([1.0, -0.2]), np.array([1.0, 0.6]), 0.5)
    assert np.allclose(out, [1.0, 0.2]) and eng.weights == {"T": 0.5, "R": 0.5}
    with pytest.raises(ValueError):
        eng.infer_u(float("nan"), 0.0, 0.5)
    with pytest.raises(ValueError):
        eng.infer_batch(np.array([np.inf]), 0.0, 0.5)
    with pytest.raises(ValueError):
        LinearVoteEngine({"T": float("inf")})


def test_surface_falls_back_to_infer_for_engines_without_batch(engine: MamdaniEngine) -> None:
    class InferOnly:
        name = "mamdani-infer-only"

        def infer(self, T: float, R: float, V: float) -> FuzzyResult:
            return engine.infer(T, R, V)

    _, _, U = control_surface(InferOnly(), V=0.3, n=7)
    _, _, U_batch = control_surface(engine, V=0.3, n=7)
    assert np.max(np.abs(U - U_batch)) <= 1e-12
    with pytest.raises(ValueError):
        control_surface(engine, V=0.3, n=1)
