"""Межові контракти ключових пакетів (core, fuzzy, decision, sizing, risk): помилкові входи і крайні випадки.

Найменування: tests/unit/test_contract_edges.py
Призначення: ті гілки публічних функцій, які основні тести груп A–N не зачіпають, але від яких залежить
    коректність решти: відмова на некоректному вході (замість тихого неправильного числа), вироджені
    випадки (нульова площа агрегату, E_open ≤ 0, порожня область прийняття Купця) і сталість
    допоміжних властивостей (ядро/носій МФ, κ-режим політики охолодження).
Автор: Андрій Жук, 2026.
"""

from __future__ import annotations

import copy
import dataclasses
import math
import uuid
from decimal import Decimal
from enum import Enum
from fractions import Fraction
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import yaml

from fuzzhelm.core.clock import ManualClock
from fuzzhelm.core.digest import assert_no_float, canonical_json, to_canonical
from fuzzhelm.core.dto import Candle, OrderRequest
from fuzzhelm.core.enums import OrderType, RiskState, Side, Src, Venue
from fuzzhelm.core.errors import ConfigValidationError, OrderRejectedError
from fuzzhelm.core.money import dec, quantize_step
from fuzzhelm.decision.aggregator import V_SOURCE_DEFAULT, consensus
from fuzzhelm.decision.core import DecisionCore
from fuzzhelm.decision.narrative_uk import fnum
from fuzzhelm.detectors.base import DetectorGroup, DetectorOutput
from fuzzhelm.features.window import BarWindow
from fuzzhelm.fuzzy.defuzz import centroid, convergence_study, exact_centroid, make_grid
from fuzzhelm.fuzzy.mamdani import default_engine
from fuzzhelm.fuzzy.membership import (
    ANY_TERM,
    LinguisticVariable,
    MembershipConfig,
    gauss,
    load_membership,
    membership_from_dict,
    read_yaml_source,
    trap,
    tri,
)
from fuzzhelm.fuzzy.rules import load_rulebase
from fuzzhelm.risk.config import load_risk_config
from fuzzhelm.risk.context import EquitySnapshot, EquityTracker
from fuzzhelm.risk.kupiec import acceptance_region, kupiec_pof
from fuzzhelm.risk.margin import side_liq_price
from fuzzhelm.risk.state import RiskEvent, RiskStateMachine
from fuzzhelm.risk.var import RollingVarCvar
from fuzzhelm.sizing.convert import to_decimal

ROOT = Path(__file__).resolve().parents[2]
MEMBERSHIP_YAML = ROOT / "config" / "membership.yaml"
RULES_YAML = ROOT / "config" / "rules_mamdani.yaml"


# ================================================================== core


def test_manual_clock_refuses_to_go_backwards() -> None:
    clk = ManualClock(100)
    clk.set(100)                                  # той самий момент — дозволено (неспадний час)
    clk.set(250)
    with pytest.raises(ValueError, match="backwards"):
        clk.set(249)
    assert clk.now_ns() == 250                    # відмова не зсунула годинник


class _Color(Enum):
    RED = "red"


@dataclasses.dataclass(frozen=True)
class _Row:
    qty: Decimal
    tag: str


def test_canonical_form_of_every_supported_type_and_refusals() -> None:
    uid = uuid.UUID(int=7)
    doc = {"e": _Color.RED, "u": uid, "b": b"\x00\xff", "row": _Row(Decimal("1.50"), "x"),
           "s": {3, 1, 2}, "t": (1, "a")}
    assert to_canonical(doc) == {"e": "red", "u": str(uid), "b": "00ff", "row": {"qty": "1.50", "tag": "x"},
                                 "s": [1, 2, 3], "t": [1, "a"]}
    assert canonical_json({"s": frozenset({"b", "a"})}) == b'{"s":["a","b"]}'   # множина — відсортована
    with pytest.raises(TypeError, match="not canonical-serializable"):
        to_canonical(object())
    # предвалідатор: нерядковий ключ і numpy-скаляр відкидаються з точним шляхом
    with pytest.raises(TypeError, match=r"non-str key 1 at \$\.a"):
        assert_no_float({"a": {1: "x"}})
    with pytest.raises(TypeError, match=r"numpy value .* at \$\.q\[0\]"):
        assert_no_float({"q": [np.int64(3)]})
    with pytest.raises(TypeError, match="float"):
        assert_no_float(_Row(Decimal(1), "x").__class__(qty=0.5, tag="x"))      # type: ignore[arg-type]


def _candle(**kw: Any) -> Candle:
    base: dict[str, Any] = dict(instrument="BTC-USDT-PERP", venue=Venue.BINANCE_USDM, tf="1m",
                                open_time_ns=60_000_000_000, close_time_ns=119_999_000_000,
                                o=Decimal(100), h=Decimal(110), l=Decimal(90), c=Decimal(105),
                                volume=Decimal(1), is_closed=True, src=Src.REST,
                                ts_event_ns=119_999_000_000, ts_ingest_ns=120_000_000_000, event_uid="0" * 32)
    base.update(kw)
    return Candle(**base)


def test_candle_rejects_vwap_outside_range_and_reversed_time() -> None:
    assert _candle(vwap=Decimal(90)).vwap == Decimal(90)            # межа [l, h] включна
    with pytest.raises(ValueError, match="vwap outside"):
        _candle(vwap=Decimal("110.01"))
    with pytest.raises(ValueError, match="close_time < open_time"):
        _candle(close_time_ns=59_000_000_000)


def test_order_request_requires_direction_and_stop_price() -> None:
    kw: dict[str, Any] = dict(client_order_id=uuid.UUID(int=1), instrument="BTC-USDT-PERP",
                              qty=Decimal("0.001"), ts_created_ns=1)
    assert OrderRequest(side=Side.LONG, otype=OrderType.MARKET, **kw).stop_price is None
    with pytest.raises(ValueError, match="LONG or SHORT"):
        OrderRequest(side=Side.FLAT, otype=OrderType.MARKET, **kw)
    with pytest.raises(ValueError, match="requires stop_price"):
        OrderRequest(side=Side.SHORT, otype=OrderType.STOP_MARKET, **kw)


def test_order_rejected_error_keeps_code_and_defaults_message_to_it() -> None:
    e = OrderRejectedError("MIN_NOTIONAL")
    assert e.code == "MIN_NOTIONAL" and str(e) == "MIN_NOTIONAL"
    assert str(OrderRejectedError("X", "detail")) == "detail"


def test_money_constructors_refuse_non_positive_step_and_foreign_types() -> None:
    with pytest.raises(ValueError, match="step must be > 0"):
        quantize_step(Decimal("1.5"), Decimal(0))
    d = Decimal("1.10")
    assert dec(d) is d and dec(3) == Decimal(3) and dec("0.1") == Decimal("0.1")
    for bad in (True, 0.1):
        with pytest.raises(TypeError, match="refuses"):
            dec(bad)                                                       # type: ignore[arg-type]
    with pytest.raises(TypeError, match="cannot convert list"):
        dec([1])                                                           # type: ignore[arg-type]


@pytest.mark.parametrize("x", [math.nan, math.inf, -math.inf])
def test_float_to_decimal_refuses_non_finite(x: float) -> None:
    with pytest.raises(ValueError, match="non-finite"):
        to_decimal(x, Decimal("0.001"))


# ================================================================== fuzzy


def test_membership_core_support_and_knots_of_every_shape() -> None:
    t, z, g = tri(-1.0, 0.0, 1.0), trap(-1.0, -0.5, 0.5, 1.0), gauss(0.2, 0.1)
    assert (t.core, t.support, t.knots) == ((0.0, 0.0), (-1.0, 1.0), (-1.0, 0.0, 0.0, 1.0))
    assert (z.core, z.support) == ((-0.5, 0.5), (-1.0, 1.0))
    assert g.core == (0.2, 0.2) and g.support == (-math.inf, math.inf) and g.knots is None
    # ядро — там, де μ = 1; поза носієм tri/trap μ = 0
    for mf in (t, z, g):
        assert mf(mf.core[0]) == 1.0 and mf(mf.core[1]) == 1.0
    assert t(1.0001) == 0.0 and z(-1.0001) == 0.0


def _membership_raw() -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load(MEMBERSHIP_YAML.read_text(encoding="utf-8"))
    return data


@pytest.mark.parametrize(("mutate", "path"), [
    # гаусіана задається m і σ, а не точками; tri/trap — навпаки
    (lambda d: d["variables"]["V"]["terms"]["LO"].update(points=[0.0, 0.1, 0.2]),
     "variables.V.terms.LO.points"),
    (lambda d: d["variables"]["T"]["terms"]["NEUTRAL"].update(sigma=0.1), "variables.T.terms.NEUTRAL.sigma"),
    (lambda d: d["variables"]["V"]["terms"]["LO"].update(m=math.nan), "variables.V.terms.LO.m"),
    (lambda d: d["variables"]["T"]["terms"]["WEAK_UP"].update(points=[0.0, 0.35]),
     "variables.T.terms.WEAK_UP.points"),
    (lambda d: d["variables"]["T"]["terms"]["WEAK_UP"].update(points=[0.0, math.inf, 1.0]),
     "variables.T.terms.WEAK_UP.points"),
    # «any» зарезервовано для «байдуже» в правилах — терм з такою назвою зробив би правило неоднозначним
    (lambda d: d["variables"]["R"]["terms"].update({ANY_TERM: {"type": "tri", "points": [-1, 0, 1]}}),
     "variables.R.terms"),
])
def test_membership_term_spec_rejects_mixed_or_invalid_parameters(mutate: Any, path: str) -> None:
    data = _membership_raw()
    mutate(data)
    with pytest.raises(ConfigValidationError) as exc:
        membership_from_dict(data)
    assert exc.value.path == path


def test_membership_config_requires_all_four_variables_even_without_yaml() -> None:
    full = load_membership(MEMBERSHIP_YAML)
    variables = dict(full.variables)
    variables.pop("U")
    with pytest.raises(ConfigValidationError) as exc:
        MembershipConfig(variables=variables)
    assert exc.value.path == "variables" and "U" in exc.value.detail


def test_config_source_of_unsupported_type_is_a_type_error() -> None:
    assert read_yaml_source({"a": 1}, default="membership") == {"a": 1}
    with pytest.raises(TypeError, match="unsupported config source list"):
        read_yaml_source([("a", 1)], default="membership")                 # type: ignore[arg-type]


def test_rule_weight_outside_unit_interval_is_rejected_with_path() -> None:
    m = load_membership(MEMBERSHIP_YAML)
    for bad in (0.0, 1.5):
        data = yaml.safe_load(RULES_YAML.read_text(encoding="utf-8"))
        data["rules"][3]["w"] = bad
        with pytest.raises(ConfigValidationError) as exc:
            load_rulebase(copy.deepcopy(data), m, production=False)
        assert exc.value.path == "rules[3].w"


def test_defuzz_degenerate_inputs() -> None:
    with pytest.raises(ValueError, match="at least 2"):
        make_grid(1)
    assert make_grid(2).tolist() == [-1.0, 1.0]
    # активні терми цілком поза [lo; hi]: площа агрегату 0 → центроїд рівно 0 (як і для порожньої активації)
    assert exact_centroid([trap(2.0, 3.0, 4.0, 5.0)], [1.0], -1.0, 1.0) == 0.0
    assert exact_centroid([tri(-1.0, 0.0, 1.0)], [0.0]) == 0.0
    with pytest.raises(ValueError, match="non-empty"):
        convergence_study(default_engine(), [])


class _FixedStrengths:
    """StrengthEngine для convergence_study: задана МФ U і сили наслідків β на кожен вхід (T — індекс)."""

    def __init__(self, U: LinguisticVariable, betas: list[list[float]]) -> None:
        variables = dict(load_membership(MEMBERSHIP_YAML).variables)
        variables["U"] = U
        self._membership = MembershipConfig(variables=variables)
        self._betas = betas

    @property
    def membership(self) -> MembershipConfig:
        return self._membership

    def consequent_strengths(self, T: float, R: float, V: float) -> np.ndarray:
        return np.asarray(self._betas[int(T)], dtype=np.float64)


def test_convergence_study_of_gaussian_consequents_uses_an_accurate_fine_trapezoid_reference() -> None:
    """U з гаусіанами не кусково-лінійне → точного еталона exact_centroid немає; еталон — трапеції з Δ = 1e−5.
    Для однієї незрізаної гаусіани центроїд на [lo; hi] відомий у замкненій формі (через erf), тож похибка
    кожного рядка дослідження мусить дорівнювати |u_Δ − u_точне|: еталон збігається з точним до 1e−10."""
    m, s, lo, hi = 0.3, 0.25, -1.0, 1.0
    U = LinguisticVariable("U", (lo, hi), {"G": gauss(m, s)})
    study = convergence_study(_FixedStrengths(U, [[1.0]]), [(0.0, 0.0, 0.0)])
    assert study["reference"] == "trapezoid_delta_1e-5"
    a, b = (lo - m) / (s * math.sqrt(2.0)), (hi - m) / (s * math.sqrt(2.0))
    area = s * math.sqrt(math.pi / 2.0) * (math.erf(b) - math.erf(a))
    tail = math.exp(-((lo - m) ** 2) / (2 * s * s)) - math.exp(-((hi - m) ** 2) / (2 * s * s))
    moment = m * area + s * s * tail
    exact = moment / area
    trap_s = study["schemes"]["trapezoid"]
    for row in trap_s["rows"]:
        g = make_grid(row["nodes"], lo, hi)
        u = centroid(g, U.evaluate(g)[0], "trapezoid")               # β = 1: агрегат = μ_G
        assert abs(row["max_abs_err"] - abs(u - exact)) < 1e-10, row
    assert 1.95 <= trap_s["fitted_order"] <= 2.05                    # гладкий інтегранд: трапеції — порядок 2
    # три зрізані гаусіани (злами min(β, μ) у невузлових точках): порядок трапецій лишається ≈ 2
    U3 = LinguisticVariable("U", (lo, hi), {"N": gauss(-0.6, 0.3), "Z": gauss(0.0, 0.3),
                                            "P": gauss(0.6, 0.3)})
    betas = np.random.default_rng(20260918).uniform(0.0, 1.0, (12, 3)).tolist()
    s3 = convergence_study(_FixedStrengths(U3, betas), [(float(i), 0.0, 0.0) for i in range(12)])
    t3 = s3["schemes"]["trapezoid"]
    assert s3["reference"] == "trapezoid_delta_1e-5" and 1.85 <= t3["fitted_order"] <= 2.15
    errs = [row["rms_err"] for row in t3["rows"]]
    assert errs == sorted(errs, reverse=True) and t3["criterion_max_diff"] < 1e-3


def test_batch_inference_refuses_non_finite_inputs() -> None:
    eng = default_engine()
    np.testing.assert_allclose(eng.infer_batch([0.3], [0.0], [0.5]), [eng.infer(0.3, 0.0, 0.5).u_raw],
                               rtol=0, atol=1e-15)
    for bad in ((math.nan, 0.0, 0.5), (0.0, math.inf, 0.5), (0.0, 0.0, -math.inf)):
        with pytest.raises(ValueError, match="non-finite"):
            eng.infer_batch(*bad)


# ================================================================== decision


def _out(name: str, s: float, c: float, group: DetectorGroup, **features: float) -> DetectorOutput:
    return DetectorOutput(name=name, s=s, c=c, group=group, features=features)


def test_non_finite_v_falls_back_to_default_and_out_of_range_v_is_an_error() -> None:
    trend = _out("ema", 0.5, 1.0, DetectorGroup.TREND)
    cons = consensus([trend, _out("vol", 0.0, 1.0, DetectorGroup.CONTEXT, V=math.nan)], v_default=0.4)
    assert (cons.V, cons.v_source) == (0.4, V_SOURCE_DEFAULT)
    cons = consensus([trend, _out("vol", 0.0, 1.0, DetectorGroup.CONTEXT, V=0.8)])
    assert (cons.V, cons.v_source) == (0.8, "vol")
    with pytest.raises(ValueError, match="outside"):
        consensus([trend, _out("vol", 0.0, 1.0, DetectorGroup.CONTEXT, V=1.2)])


class _NanEngine:
    name = "nan"

    def infer(self, T: float, R: float, V: float) -> Any:
        raise AssertionError("intent() must use infer_u when the engine provides it")

    def infer_u(self, T: float, R: float, V: float) -> float:
        return math.nan


class _ConstDetector:
    name, group, weight, warmup = "const", DetectorGroup.TREND, 1.0, 1

    def compute(self, window: BarWindow) -> DetectorOutput:
        return DetectorOutput(name=self.name, s=0.5, c=1.0)


def test_decision_core_refuses_non_finite_engine_output() -> None:
    core = DecisionCore([_ConstDetector()], _NanEngine())       # структурно: Detector, InferenceEngine
    with pytest.raises(ValueError, match="non-finite u_raw"):
        core.intent(BarWindow(8))


def test_narrative_numbers_are_exact_and_exponent_free() -> None:
    assert fnum(Decimal("1E+2")) == "100" and fnum(Decimal("0.000000010")) == "0.000000010"
    assert fnum(Fraction(1, 4)) == "0.25"                                   # numbers.Real → через float
    assert fnum(-0.0) == "0" and fnum(RiskState.HALTED) == "HALTED" and fnum(None) == "None"


# ================================================================== sizing / risk


def test_day_return_is_zero_when_day_open_equity_is_not_positive() -> None:
    snap = EquitySnapshot(ts_ns=0, equity=Decimal(-5), peak=Decimal(10), drawdown=Decimal("1.5"),
                          day_start_ns=0, equity_day_open=Decimal(0), pnl_day=Decimal(-5))
    assert snap.day_return == Decimal(0)                   # спрацьовує просадка, а не ділення на нуль
    assert EquityTracker().rebase() is None                # перебазувати нічого, поки немає спостережень


def test_kupiec_acceptance_region_collapses_to_expected_count_at_zero_critical() -> None:
    # LR = 0 рівно тоді, коли x = W·p; з критичним значенням 0 приймається лише ця кількість пробоїв
    assert kupiec_pof(1, 20, 0.05).lr == 0.0
    assert acceptance_region(20, 0.05, critical=0.0) == (1, 1)
    with pytest.raises(ValueError, match="empty acceptance region"):
        acceptance_region(10, 0.05, critical=0.0)          # W·p = 0.5 — жодна ціла кількість не має LR = 0


def test_flat_position_has_no_liquidation_price() -> None:
    with pytest.raises(ValueError, match="FLAT"):
        side_liq_price(Side.FLAT, Decimal(1), Decimal(100), Decimal(10), Decimal("0.005"))


def test_halted_never_recovers_by_classification_alone() -> None:
    sm = RiskStateMachine()
    calm = EquitySnapshot(ts_ns=0, equity=Decimal(100), peak=Decimal(100), drawdown=Decimal(0),
                          day_start_ns=0, equity_day_open=Decimal(100), pnl_day=Decimal(0))
    assert sm.classify(calm, None, state=RiskState.HALTED, dwell=10**6) is RiskEvent.STEADY
    assert sm.classify(calm, None, state=RiskState.COOLDOWN, dwell=10**6) is RiskEvent.RECOVERY
    assert sm.cooldown_policy is sm.cfg.cooldown_policy


def test_rolling_var_refuses_non_positive_equity() -> None:
    rv = RollingVarCvar(window=3)
    rv.update(Decimal(0))                                  # перше спостереження — лише запам'ятовується
    with pytest.raises(ValueError, match="non-positive equity"):
        rv.update(Decimal(1))


def test_risk_config_loads_from_a_path(tmp_path: Path) -> None:
    p = tmp_path / "limits.yaml"
    p.write_text((ROOT / "config" / "risk_limits.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    assert load_risk_config(p) == load_risk_config()
