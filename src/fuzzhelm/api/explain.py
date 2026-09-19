"""Формальне виведення рішення для GET /decisions/{id}/explain — ядро демо (брифінг §8.1, §8.2).

Найменування: api/explain.py
Призначення: із збереженого рядка `decision` і конфігурації стратегії прогону відтворити все, що
показує ExplainView: значення T/R/V і ступені належності до кожного терму (з точками кривих МФ),
спрацьовані правила за спаданням α з україномовним текстом, агреговану фігуру μ_agg(u) і центроїд,
узгодженість і κ, розкладку сайзера з binding_constraint, стоп/тейк/ліквідацію та україномовне трасування.
Автор: Андрій Жук, 2026.

Детермінізм: μ_agg не зберігається (201 вузол × кожен бар), а перераховується тим самим MamdaniEngine з
конфігурації стратегії прогону. Входи T/R/V беруться з точних float-виходів детекторів (JSONB) через
decision.aggregator.consensus — колонки t_in/r_in/v_in округлені до NUMERIC(8,5). Блок `consistency`
чесно показує, чи перерахунок збігся зі збереженим (u_raw у межах округлення колонки, α кожного правила
≤ 1e−9); розбіжність означає, що конфігурацію стратегії змінили після прогону, — її не приховуємо.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from functools import lru_cache
from typing import Any

import numpy as np

from fuzzhelm.api.validation import validate_strategy_texts
from fuzzhelm.config import load_yaml
from fuzzhelm.core.money import dec_str
from fuzzhelm.decision.aggregator import Consensus, consensus
from fuzzhelm.decision.agreement import Agreement, agreement
from fuzzhelm.decision.core import agreement_params_from_config, clip_unit
from fuzzhelm.decision.narrative_uk import TERMS_UK, VAR_NAMES, f2, narrate, rule_sentence, term_uk
from fuzzhelm.decision.trace import DecisionTrace, json_safe, rule_order
from fuzzhelm.detectors.base import DetectorGroup, DetectorOutput
from fuzzhelm.features.convert import to_float
from fuzzhelm.fuzzy.base import FiredRule, FuzzyResult, InferenceEngine
from fuzzhelm.fuzzy.linear import LinearVoteEngine
from fuzzhelm.fuzzy.mamdani import MamdaniEngine, default_engine
from fuzzhelm.fuzzy.membership import LinguisticVariable, MembershipConfig

# половина одиниці останнього розряду NUMERIC(8,5) + запас на float
COLUMN_TOL = 5e-6 + 1e-9
ALPHA_TOL = 1e-9
ANTECEDENT_ORDER = ("T", "R", "V")


class ExplainError(Exception):
    """Рішення неможливо пояснити (напр. збережена стратегія не проходить валідацію)."""


@dataclass(frozen=True, slots=True)
class StrategyRef:
    id: int | None
    name: str | None
    version: int | None
    source: str  # "strategy" | "default_config"


@lru_cache(maxsize=16)
def _engine_from_texts(rules_yaml: str, membership_yaml: str) -> MamdaniEngine:
    v = validate_strategy_texts(rules_yaml, membership_yaml)
    return MamdaniEngine(v.membership, v.rulebase)


@lru_cache(maxsize=1)
def _default_mamdani() -> MamdaniEngine:
    return default_engine()


def mamdani_for(strategy: Any | None) -> tuple[MamdaniEngine, StrategyRef]:
    """Рушій із текстів версії стратегії прогону або з робочих конфігів (якщо прогін без стратегії)."""
    if strategy is None:
        return _default_mamdani(), StrategyRef(None, None, None, "default_config")
    try:
        engine = _engine_from_texts(strategy.rules_yaml, strategy.membership_yaml)
    except Exception as e:  # збережена версія мала пройти валідацію під час POST/PUT
        raise ExplainError(f"stored strategy {strategy.id} is not loadable: {e}") from e
    return engine, StrategyRef(strategy.id, strategy.name, strategy.version, "strategy")


def _f(x: Decimal | float | int | None) -> float | None:
    if x is None:
        return None
    if isinstance(x, Decimal):
        return to_float(x)
    return float(x)


def parse_detector_outputs(raw: Sequence[Mapping[str, Any]] | None) -> tuple[DetectorOutput, ...] | None:
    """JSONB detector_outputs → DetectorOutput; будь-яка неповнота → None (тоді входи — з колонок)."""
    if not raw:
        return None
    out: list[DetectorOutput] = []
    try:
        for o in raw:
            feats = {
                str(k): v
                for k, v in (o.get("features") or {}).items()
                if v is None or (isinstance(v, int | float) and not isinstance(v, bool))
            }
            out.append(
                DetectorOutput(
                    name=str(o["name"]),
                    s=float(o["s"]),
                    c=float(o["c"]),
                    features=feats,
                    group=DetectorGroup(str(o.get("group") or "trend").lower()),
                    weight=float(o.get("weight", 1.0)),
                )
            )
    except (KeyError, TypeError, ValueError):
        return None
    return tuple(out)


def _close(a: float | None, b: float | None, tol: float) -> bool:
    return a is None or b is None or abs(a - b) <= tol


def _curve_x(var: LinguisticVariable, n: int) -> np.ndarray:
    lo, hi = var.range
    return np.linspace(lo, hi, n)


def variable_block(
    var: LinguisticVariable, value: float | None, mus: Mapping[str, float] | None, n_points: int
) -> dict[str, Any]:
    """Опис лінгвістичної змінної для MembershipPlot: терми з параметрами, μ у точці входу, точки кривих."""
    xs = _curve_x(var, n_points)
    ys = var.evaluate(xs)
    terms = []
    for i, name in enumerate(var.term_names):
        mf = var.terms[name]
        mu = None if mus is None else mus.get(name)
        terms.append(
            {
                "name": name,
                "name_uk": term_uk(var.name, name),
                "kind": mf.kind,
                "params": mf.to_dict(),
                "mu": mu,
                "active": bool(mu is not None and mu > 0.0),
                "curve": ys[i].tolist(),
            }
        )
    return {
        "name": var.name,
        "name_uk": VAR_NAMES.get(var.name, var.name),
        "range": list(var.range),
        "value": value,
        "x": xs.tolist(),
        "terms": terms,
    }


def rule_text_uk(rule_id: str, antecedent: Mapping[str, str], consequent: str) -> str:
    """«ЯКЩО тренд … І реверсія … І волатильність … ТО сигнал …» (як у narrative_uk, без α)."""
    keys = [k for k in ANTECEDENT_ORDER if k in antecedent] + sorted(
        k for k in antecedent if k not in ANTECEDENT_ORDER
    )
    clauses = " І ".join(f"{VAR_NAMES.get(k, k)} {term_uk(k, antecedent[k])}" for k in keys)
    return f"{rule_id}: ЯКЩО {clauses} ТО {VAR_NAMES['U']} {term_uk('U', consequent)}"


def fired_block(stored: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    rules = []
    for r in stored:
        ante = {str(k): str(v) for k, v in (r.get("antecedent") or {}).items()}
        cons = str(r.get("consequent"))
        rules.append(
            FiredRule(
                rule_id=str(r.get("rule_id")),
                alpha=float(r.get("alpha") or 0.0),
                consequent=cons,
                antecedent=ante,
            )
        )
    rules.sort(key=rule_order)
    return [
        {
            "rule_id": fr.rule_id,
            "alpha": fr.alpha,
            "consequent": fr.consequent,
            "consequent_uk": term_uk("U", fr.consequent),
            "antecedent": dict(fr.antecedent),
            "antecedent_uk": {k: term_uk(k, v) for k, v in fr.antecedent.items()},
            "text_uk": rule_text_uk(fr.rule_id, fr.antecedent, fr.consequent),
        }
        for fr in rules
    ]


def _alpha_diff(stored: Sequence[Mapping[str, Any]], fz: FuzzyResult) -> tuple[bool, float]:
    a = {str(r.get("rule_id")): float(r.get("alpha") or 0.0) for r in stored}
    b = {fr.rule_id: fr.alpha for fr in fz.fired}
    diff = max((abs(a.get(k, 0.0) - b.get(k, 0.0)) for k in set(a) | set(b)), default=0.0)
    return set(a) == set(b) and diff <= ALPHA_TOL, diff


def _fallback_narrative(
    fired: Sequence[FiredRule], u_raw: float | None, kappa: float | None, u_final: float | None
) -> str:
    """Скорочений текст, коли виходів детекторів немає (агрегатор/κ неможливо відтворити)."""
    parts: list[str] = []
    if fired:
        parts.append(rule_sentence(fired[0]))
    else:
        parts.append(f"Жодне правило не спрацювало, рішення — {term_uk('U', 'HOLD')}.")
    if u_raw is not None and kappa is not None and u_final is not None:
        parts.append(f"Вихід нечіткого ядра u_raw = {f2(u_raw)}; κ = {f2(kappa)}; u_final = {f2(u_final)}.")
    return " ".join(parts)


def _dec(x: Decimal | None) -> str | None:
    return None if x is None else dec_str(x)


def explain_decision(
    row: Any,
    *,
    run: Any | None,
    strategy: Any | None,
    detectors_cfg: Mapping[str, Any] | None = None,
    mf_points: int = 101,
) -> dict[str, Any]:
    """Повне пояснення рішення (dict, JSON-сумісний). `row` — storage DecisionRow."""
    engine_kind = (getattr(run, "engine", None) or "mamdani") if run is not None else "mamdani"
    stored = {
        "T": _f(row.t_in),
        "R": _f(row.r_in),
        "V": _f(row.v_in),
        "agreement": _f(row.agreement),
        "kappa": _f(row.kappa),
        "u_raw": _f(row.u_raw),
        "u_final": _f(row.u_final),
    }
    outputs = parse_detector_outputs(row.detector_outputs)
    cfg = detectors_cfg if detectors_cfg is not None else load_yaml("detectors")
    params = agreement_params_from_config(cfg)

    cons: Consensus | None = consensus(outputs) if outputs else None
    inputs_source = "detector_outputs"
    if cons is not None and all(_close(getattr(cons, k), stored[k], COLUMN_TOL) for k in ANTECEDENT_ORDER):
        T, R, V = cons.T, cons.R, cons.V
    else:
        if any(stored[k] is None for k in ANTECEDENT_ORDER):
            raise ExplainError("decision has neither usable detector outputs nor stored T/R/V")
        T, R, V = float(stored["T"]), float(stored["R"]), float(stored["V"])  # type: ignore[arg-type]
        inputs_source = "stored_columns" if cons is None else "stored_columns(detector_outputs_mismatch)"
        cons = Consensus(T=T, R=R, V=V)

    engine: InferenceEngine
    membership: MembershipConfig | None
    if engine_kind == "linear":
        engine, strategy_ref, membership = LinearVoteEngine(), StrategyRef(None, None, None, "linear"), None
    else:
        mengine, strategy_ref = mamdani_for(strategy)
        engine, membership = mengine, mengine.membership
    fz = engine.infer(T, R, V)

    agr: Agreement | None = agreement(outputs, params.kappa_min, params.nu) if outputs else None
    kappa = agr.kappa if agr is not None else stored["kappa"]
    u_final = clip_unit(kappa * fz.u_raw) if kappa is not None else stored["u_final"]

    rules_match, max_alpha_diff = _alpha_diff(row.fired_rules or [], fz)
    u_raw_diff = None if stored["u_raw"] is None else abs(stored["u_raw"] - fz.u_raw)
    kappa_diff = None if stored["kappa"] is None or kappa is None else abs(stored["kappa"] - kappa)
    consistency = {
        "u_raw_stored": stored["u_raw"],
        "u_raw_recomputed": fz.u_raw,
        "u_raw_abs_diff": u_raw_diff,
        "kappa_abs_diff": kappa_diff,
        "fired_rules_match": rules_match,
        "max_alpha_abs_diff": max_alpha_diff,
        "column_tolerance": COLUMN_TOL,
    }
    consistency["ok"] = bool(
        rules_match
        and (u_raw_diff is None or u_raw_diff <= COLUMN_TOL)
        and (kappa_diff is None or kappa_diff <= 5e-5 + 1e-9)
    )

    variables: dict[str, Any] = {}
    stored_mus: Mapping[str, Mapping[str, float]] = row.memberships or {}
    if membership is not None:
        for name, value in (("T", T), ("R", R), ("V", V)):
            mus = stored_mus.get(name) or fz.memberships.get(name)
            variables[name] = variable_block(membership[name], value, mus, mf_points)
        u_mus = (
            {
                t: float(b)
                for t, b in zip(
                    membership.U.term_names, engine.consequent_strengths(T, R, V).tolist(), strict=True
                )
            }
            if isinstance(engine, MamdaniEngine)
            else None
        )
        variables["U"] = variable_block(membership.U, fz.u_raw, u_mus, mf_points)

    aggregate = None
    if fz.grid is not None and fz.mu_agg is not None and isinstance(engine, MamdaniEngine):
        strengths = engine.consequent_strengths(T, R, V).tolist()
        aggregate = {
            "grid": np.asarray(fz.grid).tolist(),
            "mu": np.asarray(fz.mu_agg).tolist(),
            "centroid": fz.u_raw,
            "scheme": engine.scheme,
            "nodes": engine.nodes,
            "area": fz.extras.get("area"),
            "height": fz.extras.get("height"),
            "strengths": [
                {"term": t, "term_uk": term_uk("U", t), "beta": float(b)}
                for t, b in zip(engine.membership.U.term_names, strengths, strict=True)
            ],
        }

    sizing = dict(row.sizing) if row.sizing else None
    risk = dict(row.risk) if row.risk else None
    fired_rules = fired_block(row.fired_rules or [])
    if row.narrative:
        narrative, narrative_source = row.narrative, "stored"
    elif agr is not None and outputs is not None:
        trace = DecisionTrace(
            open_time_ns=row.open_time_ns or 0,
            detector_outputs=outputs,
            consensus=cons,
            fuzzy=fz,
            agreement=agr,
            u_raw=fz.u_raw,
            kappa=agr.kappa,
            u_final=u_final,
            engine=fz.engine,
            sizing=sizing,
            risk=risk,
        )
        narrative, narrative_source = narrate(trace), "recomputed"
    else:
        fr = [FiredRule(r["rule_id"], r["alpha"], r["consequent"], r["antecedent"]) for r in fired_rules]
        narrative = _fallback_narrative(fr, fz.u_raw, kappa, u_final)
        narrative_source = "recomputed(partial)"

    target = {
        "side": row.target_side,
        "qty": _dec(row.target_qty),
        "binding_constraint": row.binding_constraint,
    }
    payload = {
        "decision_id": row.id,
        "run_id": str(row.run_id),
        "instrument_id": row.instrument_id,
        "open_time_ns": row.open_time_ns,
        "engine": fz.engine,
        "strategy": {
            "id": strategy_ref.id,
            "name": strategy_ref.name,
            "version": strategy_ref.version,
            "source": strategy_ref.source,
        },
        "inputs": {"T": T, "R": R, "V": V},
        "inputs_source": inputs_source,
        "v_source": getattr(cons, "v_source", None),
        "stored": stored,
        "variables": variables,
        "memberships": stored_mus or fz.memberships,
        "fired_rules": fired_rules,
        "n_fired": len(fired_rules),
        "aggregate": aggregate,
        "u_raw": fz.u_raw,
        "kappa": kappa,
        "u_final": u_final,
        "agreement": agr.to_dict()
        if agr is not None
        else {"A_g": stored["agreement"], "kappa": stored["kappa"]},
        "detector_outputs": list(row.detector_outputs or []),
        "sizing": sizing,
        "risk": risk,
        "target": target,
        "prices": {"stop": _dec(row.stop_price), "tp": _dec(row.tp_price), "liq": _dec(row.liq_price)},
        "narrative_uk": narrative,
        "narrative_source": narrative_source,
        "consistency": consistency,
        "terms_uk": TERMS_UK,
    }
    out: dict[str, Any] = json_safe(payload)
    return out


def is_finite_json(obj: Any) -> bool:
    """Перевірка для тестів: у відповіді немає NaN/inf (JSON їх не має)."""
    if isinstance(obj, float):
        return math.isfinite(obj)
    if isinstance(obj, Mapping):
        return all(is_finite_json(v) for v in obj.values())
    if isinstance(obj, list | tuple):
        return all(is_finite_json(v) for v in obj)
    return True
