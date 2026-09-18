"""Схема конфігурації ризик-контуру і сайзера (config/risk_limits.yaml).

Найменування: risk/config.py
Призначення: валідований (pydantic, frozen, extra=forbid) образ risk_limits.yaml — шість лімітів,
пороги автомата станів, параметри сайзера і тригера Шмітта. Невалідний файл → ConfigValidationError
з точним шляхом до поля (напр. `state_machine.warn_exit`).
Автор: Андрій Жук, 2026.

Типи: пороги ризик-контуру (частки капіталу, просадки, плече) — Decimal (Decimal-домен `risk`);
параметри сайзера — float (математика сайзера живе у float-домені, Decimal лише на виході qty).
YAML-числа pydantic перетворює на Decimal через str(float), тож 0.30 → Decimal('0.3') точно.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from fuzzhelm.config import load_yaml, parse_yaml_text
from fuzzhelm.core.enums import RiskState
from fuzzhelm.core.errors import ConfigValidationError


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


# ---------------------------------------------------------------- шість лімітів (§5.11, §7)


class MaxPositionNotionalCfg(_Frozen):
    value: Decimal = Field(gt=0, le=1)
    unit: Literal["equity_fraction"] = "equity_fraction"
    action: Literal["shrink"] = "shrink"


class MaxGrossLeverageCfg(_Frozen):
    value: Decimal = Field(gt=0)
    action: Literal["shrink"] = "shrink"


class MaxDailyLossCfg(_Frozen):
    value: Decimal = Field(gt=0, lt=1)
    unit: Literal["equity_fraction"] = "equity_fraction"
    action: Literal["veto"] = "veto"
    reset: Literal["utc_midnight"] = "utc_midnight"


class MaxDrawdownHaltCfg(_Frozen):
    value: Decimal = Field(gt=0, lt=1)
    action: Literal["halt"] = "halt"


class LiquidationBufferCfg(_Frozen):
    min_dtl_atr: Decimal = Field(gt=0)
    action: Literal["reduce_leverage"] = "reduce_leverage"
    b: Decimal = Field(ge=0, lt=1)


class StaleDataCfg(_Frozen):
    max_lag_s: Decimal = Field(gt=0)
    min_dq: Decimal = Field(ge=0, le=1)
    action: Literal["veto"] = "veto"


class LimitsCfg(_Frozen):
    max_position_notional: MaxPositionNotionalCfg
    max_gross_leverage: MaxGrossLeverageCfg
    max_daily_loss: MaxDailyLossCfg
    max_drawdown_halt: MaxDrawdownHaltCfg
    liquidation_buffer: LiquidationBufferCfg
    stale_data: StaleDataCfg


# ---------------------------------------------------------------- автомат станів (§5.12)


class StateMachineCfg(_Frozen):
    warn_enter: Decimal = Field(default=Decimal("0.04"), gt=0, lt=1)
    warn_exit: Decimal = Field(default=Decimal("0.025"), ge=0, lt=1)
    warn_dwell: int = Field(default=15, ge=0)
    warn_vol_ratio: float = Field(default=1.6, gt=0)
    cool_enter: Decimal = Field(default=Decimal("0.08"), gt=0, lt=1)
    cool_exit: Decimal = Field(default=Decimal("0.05"), ge=0, lt=1)
    cool_dwell: int = Field(default=30, ge=0)
    cool_daily_loss: Decimal = Field(default=Decimal("0.02"), gt=0, lt=1)
    halt_enter: Decimal = Field(default=Decimal("0.12"), gt=0, lt=1)
    halt_daily_loss: Decimal = Field(default=Decimal("0.03"), gt=0, lt=1)
    kappa_mode: dict[RiskState, Decimal] = Field(default_factory=lambda: {
        RiskState.NORMAL: Decimal("1.0"), RiskState.WARNING: Decimal("0.5"),
        RiskState.COOLDOWN: Decimal("0.25"), RiskState.HALTED: Decimal("0.0"),
    })

    @model_validator(mode="after")
    def _ordered(self) -> StateMachineCfg:
        # гістерезис має сенс лише тоді, коли поріг виходу строго нижчий за поріг входу,
        # а смуги станів вкладені: warn_exit < warn_enter ≤ cool_exit < cool_enter < halt_enter
        if not self.warn_exit < self.warn_enter:
            raise ValueError("warn_exit must be < warn_enter (hysteresis band)")
        if not self.cool_exit < self.cool_enter:
            raise ValueError("cool_exit must be < cool_enter (hysteresis band)")
        if not self.warn_enter <= self.cool_exit:
            raise ValueError("warn_enter must be <= cool_exit (nested bands)")
        if not self.cool_enter < self.halt_enter:
            raise ValueError("cool_enter must be < halt_enter")
        if not self.cool_daily_loss < self.halt_daily_loss:
            raise ValueError("cool_daily_loss must be < halt_daily_loss")
        missing = set(RiskState) - set(self.kappa_mode)
        if missing:
            raise ValueError(f"kappa_mode misses states {sorted(missing)}")
        k = self.kappa_mode
        for s, v in k.items():
            if not Decimal(0) <= v <= Decimal(1):
                raise ValueError(f"kappa_mode[{s}] must be in [0, 1]")
        if not (k[RiskState.NORMAL] >= k[RiskState.WARNING] >= k[RiskState.COOLDOWN]
                >= k[RiskState.HALTED]):
            raise ValueError("kappa_mode must be non-increasing NORMAL ≥ WARNING ≥ COOLDOWN ≥ HALTED")
        if k[RiskState.HALTED] != 0:
            raise ValueError("kappa_mode[HALTED] must be 0 (flatten-all)")
        return self


# ---------------------------------------------------------------- сайзер і гістерезис (§5.8, §5.9)


class SizingCfg(_Frozen):
    ewma_lambda: float = Field(default=0.94, gt=0, lt=1)
    annualization_bars: int = Field(default=525_600, gt=0)
    sigma_target: float = Field(default=0.20, gt=0)
    scale_min: float = Field(default=0.25, gt=0)
    scale_max: float = Field(default=3.0, gt=0)
    filter_gamma: float = Field(default=0.2, gt=0, le=1)
    rho_base: float = Field(default=0.005, gt=0, lt=1)
    chi_atr: float = Field(default=2.0, gt=0)
    max_leverage: float = Field(default=3.0, gt=0)

    @model_validator(mode="after")
    def _bounds(self) -> SizingCfg:
        if not self.scale_min <= self.scale_max:
            raise ValueError("scale_min must be <= scale_max")
        return self


class HysteresisCfg(_Frozen):
    enter: float = Field(default=0.25, gt=0, le=1)
    exit: float = Field(default=0.12, ge=0, le=1)

    @model_validator(mode="after")
    def _band(self) -> HysteresisCfg:
        if not self.exit <= self.enter:
            raise ValueError("exit must be <= enter (Schmitt trigger band)")
        return self


class RiskConfig(_Frozen):
    limits: LimitsCfg
    state_machine: StateMachineCfg
    sizing: SizingCfg = Field(default_factory=SizingCfg)
    hysteresis: HysteresisCfg = Field(default_factory=HysteresisCfg)


def _error_path(err: ValidationError) -> tuple[str, str]:
    first = err.errors()[0]
    parts = [str(p) for p in first.get("loc", ())]
    return (".".join(parts) or "$"), str(first.get("msg", "invalid"))


def load_risk_config(source: str | Path | dict[str, Any] | None = None) -> RiskConfig:
    """Прочитати і провалідувати risk_limits.yaml.

    source: None → `config/risk_limits.yaml`; Path → YAML-файл; str → YAML-текст; dict → вже розібране дерево.
    Помилка схеми → ConfigValidationError(path="limits.max_daily_loss.value").
    """
    if source is None:
        data: dict[str, Any] = load_yaml("risk_limits")
    elif isinstance(source, Path):
        data = load_yaml(source.name, source.parent)
    elif isinstance(source, str):
        data = parse_yaml_text(source, "risk_limits.yaml")
    else:
        data = source
    try:
        return RiskConfig.model_validate(data)
    except ValidationError as e:
        path, msg = _error_path(e)
        raise ConfigValidationError(msg, path=path) from e
