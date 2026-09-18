"""Повне трасування одного рішення — джерело для /explain, таблиці `decision` і україномовного тексту.

Найменування: decision/trace.py
Призначення: незмінний запис «звідки взявся u_final»: виходи детекторів, консенсус T/R/V,
ступені належності, спрацьовані правила з α, μ_agg, узгодженість і κ; поля `sizing`/`risk`
дописує рушій пізніше через `with_sizing` / `with_risk` (запис frozen — повертається копія).
Автор: Андрій Жук, 2026.

`to_dict()` дає JSON-сумісну структуру (numpy → списки/скаляри Python, Enum → значення,
Decimal → рядок без експоненти, NaN/inf → None). float тут допустимі: це дані для API і БД,
а не канонічні дані для хешу стану.
"""

from __future__ import annotations

import dataclasses
import math
import numbers
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any

import numpy as np

from fuzzhelm.core.money import dec_str
from fuzzhelm.decision.aggregator import Consensus
from fuzzhelm.decision.agreement import Agreement
from fuzzhelm.detectors.base import DetectorOutput
from fuzzhelm.fuzzy.base import FiredRule, FuzzyResult


def rule_order(rule: FiredRule) -> tuple[float, str]:
    """Ключ сортування спрацьованих правил: α за спаданням, далі rule_id (детермінований порядок)."""
    return (-rule.alpha, rule.rule_id)


def json_safe(obj: Any) -> Any:
    """Рекурсивно звести об'єкт до типів JSON (dict/list/str/int/float/bool/None)."""
    if obj is None or isinstance(obj, bool):
        return obj
    if isinstance(obj, Enum):
        return json_safe(obj.value)
    if isinstance(obj, int):
        return int(obj)
    if isinstance(obj, float):
        return float(obj) if math.isfinite(obj) else None
    if isinstance(obj, str):
        return str(obj)
    if isinstance(obj, np.ndarray):
        return json_safe(obj.tolist())
    if isinstance(obj, np.generic):
        return json_safe(obj.item())
    if isinstance(obj, Mapping):
        return {str(k): json_safe(v) for k, v in obj.items()}
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: json_safe(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
    model_dump = getattr(obj, "model_dump", None)
    if callable(model_dump):
        return json_safe(model_dump(mode="python"))
    if isinstance(obj, list | tuple | set | frozenset):
        return [json_safe(v) for v in obj]
    if isinstance(obj, numbers.Number) and not isinstance(obj, numbers.Complex):
        # Decimal зареєстровано лише як numbers.Number; float-домен не імпортує decimal,
        # тому розпізнаємо його структурно і серіалізуємо канонічним dec_str (без експоненти)
        return dec_str(obj)  # type: ignore[arg-type]
    if isinstance(obj, numbers.Real):
        return json_safe(float(obj))
    return str(obj)


@dataclass(frozen=True, slots=True)
class DecisionTrace:
    open_time_ns: int
    detector_outputs: tuple[DetectorOutput, ...]
    consensus: Consensus
    fuzzy: FuzzyResult
    agreement: Agreement
    u_raw: float
    kappa: float
    u_final: float                           # clip(κ·u_raw, −1, 1)
    engine: str
    sizing: Mapping[str, Any] | None = None  # заповнює рушій після PositionSizer
    risk: Mapping[str, Any] | None = None    # заповнює рушій після RiskGuard/RiskStateMachine

    # --- зручні проєкції для /explain і narrative_uk -------------------------------------------

    @property
    def T(self) -> float:
        return self.consensus.T

    @property
    def R(self) -> float:
        return self.consensus.R

    @property
    def V(self) -> float:
        return self.consensus.V

    @property
    def memberships(self) -> Mapping[str, Mapping[str, float]]:
        return self.fuzzy.memberships

    @property
    def fired_rules(self) -> tuple[FiredRule, ...]:
        """Усі спрацьовані правила рушія, відсортовані за α спадно (незалежно від порядку в FuzzyResult)."""
        return tuple(sorted(self.fuzzy.fired, key=rule_order))

    @property
    def top_rule(self) -> FiredRule | None:
        fired = self.fuzzy.fired
        return min(fired, key=rule_order) if fired else None

    @property
    def p_plus(self) -> float:
        return self.agreement.p_plus

    @property
    def p_minus(self) -> float:
        return self.agreement.p_minus

    @property
    def p_zero(self) -> float:
        return self.agreement.p_zero

    @property
    def H(self) -> float:
        return self.agreement.H

    @property
    def A_g(self) -> float:
        return self.agreement.A_g

    # --- рівність ---------------------------------------------------------------------------

    def __eq__(self, other: object) -> bool:
        """Структурна рівність через `to_dict()`.

        Згенерований dataclass-ом `__eq__` порівнював би `FuzzyResult.grid/mu_agg` (numpy) через `==`
        і падав би з «truth value of an array is ambiguous» — саме на тестах детермінізму
        (live ≡ бектест). NaN/inf у to_dict() стають None, тому порівнюються як рівні.
        """
        if not isinstance(other, DecisionTrace):
            return NotImplemented
        return self.to_dict() == other.to_dict()

    def __hash__(self) -> int:
        # узгоджено з __eq__: рівні to_dict() ⇒ рівні ці скінченні скаляри (u_final завжди скінченне)
        return hash((self.open_time_ns, self.engine, self.u_final))

    # --- доповнення рушієм -------------------------------------------------------------------

    def with_sizing(self, sizing: Mapping[str, Any] | None) -> DecisionTrace:
        return dataclasses.replace(self, sizing=None if sizing is None else dict(sizing))

    def with_risk(self, risk: Mapping[str, Any] | None) -> DecisionTrace:
        return dataclasses.replace(self, risk=None if risk is None else dict(risk))

    # --- серіалізація ------------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        fz = self.fuzzy
        raw: dict[str, Any] = {
            "open_time_ns": self.open_time_ns,
            "engine": self.engine,
            "inputs": {"T": self.consensus.T, "R": self.consensus.R, "V": self.consensus.V},
            "consensus": self.consensus.to_dict(),
            "detector_outputs": [
                {
                    "name": o.name, "group": o.group, "s": o.s, "c": o.c,
                    "weight": o.weight, "features": o.features,
                }
                for o in self.detector_outputs
            ],
            "memberships": fz.memberships,
            "fired_rules": [
                {"rule_id": r.rule_id, "alpha": r.alpha, "consequent": r.consequent,
                 "antecedent": r.antecedent}
                for r in self.fired_rules
            ],
            "u_raw": self.u_raw,
            "kappa": self.kappa,
            "u_final": self.u_final,
            "agreement": self.agreement.to_dict(),
            "grid": fz.grid,
            "mu_agg": fz.mu_agg,
            "fuzzy_extras": fz.extras,
            "sizing": self.sizing,
            "risk": self.risk,
        }
        out: dict[str, Any] = json_safe(raw)
        return out
