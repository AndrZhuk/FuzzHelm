"""Ядро рішень: детектори → консенсус → нечіткий вивід → κ → u_final.

Найменування: decision/core.py
Призначення: єдиний шлях обчислення наміру u_final ∈ [−1; 1] для live і бектесту (той самий
код — межа детермінізму): кожен детектор викликається рівно один раз на бар, результат —
повне трасування `DecisionTrace`.
Автор: Андрій Жук, 2026.

    (T, R, V) = consensus(outputs);  fuzzy = engine.infer(T, R, V);  κ = agreement(outputs).kappa
    u_final = clip(κ · u_raw, −1, 1)

М'яке пропонує, жорстке вирішує: DecisionCore видає лише намір, розмір і дозвіл визначають
sizing і risk.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

from fuzzhelm.core.errors import ConfigValidationError
from fuzzhelm.decision.aggregator import EPS, V_DEFAULT, consensus
from fuzzhelm.decision.agreement import KAPPA_MIN, NU, agreement, validate_params
from fuzzhelm.decision.trace import DecisionTrace
from fuzzhelm.detectors.base import Detector
from fuzzhelm.fuzzy.base import InferenceEngine

if TYPE_CHECKING:
    from fuzzhelm.features.window import BarWindow


class AgreementParams(BaseModel):
    """Секція `agreement` у config/detectors.yaml."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kappa_min: float = Field(default=KAPPA_MIN, ge=0.0, le=1.0)
    nu: float = Field(default=NU, gt=0.0)


def agreement_params_from_config(cfg: Mapping[str, Any] | None) -> AgreementParams:
    """Прочитати κ_min, ν з розібраного detectors.yaml (відсутня секція → типові значення брифінгу)."""
    section = {} if cfg is None else cfg.get("agreement", {})
    if not isinstance(section, Mapping):
        raise ConfigValidationError("agreement must be a mapping", path="agreement")
    try:
        return AgreementParams.model_validate(dict(section))
    except ValueError as e:
        raise ConfigValidationError(str(e), path="agreement") from e


def clip_unit(x: float) -> float:
    return -1.0 if x < -1.0 else (1.0 if x > 1.0 else x)


class DecisionCore:
    """Композиція детекторів і рушія виведення. Без стану між барами (чиста функція від вікна)."""

    __slots__ = ("_detectors", "_engine", "eps", "kappa_min", "nu", "v_default")

    def __init__(
        self,
        detectors: Sequence[Detector],
        engine: InferenceEngine,
        kappa_min: float = KAPPA_MIN,
        nu: float = NU,
        *,
        eps: float = EPS,
        v_default: float = V_DEFAULT,
    ) -> None:
        validate_params(kappa_min, nu)
        if not eps > 0.0:
            raise ValueError(f"eps={eps} must be > 0")
        if not 0.0 <= v_default <= 1.0:
            raise ValueError(f"v_default={v_default} must lie in [0, 1]")
        names = [d.name for d in detectors]
        if len(set(names)) != len(names):
            raise ValueError(f"duplicate detector names: {names}")
        self._detectors: tuple[Detector, ...] = tuple(detectors)
        self._engine = engine
        self.kappa_min = kappa_min
        self.nu = nu
        self.eps = eps
        self.v_default = v_default

    @classmethod
    def from_config(
        cls,
        detectors: Sequence[Detector],
        engine: InferenceEngine,
        cfg: Mapping[str, Any] | None,
    ) -> DecisionCore:
        """κ_min і ν з секції `agreement` розібраного config/detectors.yaml (`fuzzhelm.config.load_yaml`)."""
        p = agreement_params_from_config(cfg)
        return cls(detectors, engine, p.kappa_min, p.nu)

    @property
    def detectors(self) -> tuple[Detector, ...]:
        return self._detectors

    @property
    def engine(self) -> InferenceEngine:
        return self._engine

    def decide(self, window: BarWindow, open_time_ns: int | None = None) -> DecisionTrace:
        """Рішення на закритті поточного бару вікна.

        `open_time_ns` за замовчуванням — `window.bar(0).t_ns`.
        """
        outputs = tuple(d.compute(window) for d in self._detectors)
        cons = consensus(outputs, self.eps, self.v_default)
        fuzzy = self._engine.infer(cons.T, cons.R, cons.V)
        u_raw = fuzzy.u_raw
        if not math.isfinite(u_raw):
            # NaN пройшов би крізь clip і сайзер мовчки — зупиняємо на межі ядра
            raise ValueError(f"engine {self._engine.name!r} returned non-finite u_raw={u_raw}")
        agr = agreement(outputs, self.kappa_min, self.nu, self.eps)
        kappa = agr.kappa
        if open_time_ns is None:
            open_time_ns = window.bar(0).t_ns
        return DecisionTrace(
            open_time_ns=open_time_ns,
            detector_outputs=outputs,
            consensus=cons,
            fuzzy=fuzzy,
            agreement=agr,
            u_raw=u_raw,
            kappa=kappa,
            u_final=clip_unit(kappa * u_raw),
            engine=self._engine.name,
        )
