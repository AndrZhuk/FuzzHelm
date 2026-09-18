"""Реєстр детекторів: config/detectors.yaml → шість екземплярів з вагами ω_k.

Найменування: detectors/registry.py
Призначення: валідація конфігурації (pydantic-схема модуля) і побудова ансамблю в канонічному
             порядку DETECTOR_NAMES; структурні параметри (довжини вікон) ідуть у FeatureParams,
             щоб прогрів детекторів і конвеєра ознак брався з одного джерела.
Автор: Андрій Жук, 2026.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from fuzzhelm.core.errors import ConfigValidationError
from fuzzhelm.detectors.base import Detector
from fuzzhelm.detectors.bollinger_z import BollingerZ
from fuzzhelm.detectors.candle_geometry import CandleGeometry
from fuzzhelm.detectors.donchian import Donchian
from fuzzhelm.detectors.ema_slope import EmaSlope
from fuzzhelm.detectors.rsi_exhaustion import RsiExhaustion
from fuzzhelm.detectors.vol_regime import VolRegime
from fuzzhelm.features.pipeline import FeatureParams

DETECTOR_NAMES: tuple[str, ...] = (
    "ema_slope", "donchian", "rsi_exhaustion", "bollinger_z", "candle_geometry", "vol_regime",
)
# Найглибший лаг, який читають детектори: CandleGeometry — bar(1) (поглинання); решта — лише lag 0,
# бо вся історія (лаги EMA/RSI, вік пробою, ранги) уже згорнута в Features конвеєром.
MIN_WINDOW_CAPACITY = 2


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    weight: float = Field(ge=0.0)


class EmaSlopeCfg(_Section):
    weight: float = Field(1.0, ge=0.0)
    n_ema: int = Field(21, ge=1)
    horizon: int = Field(5, ge=1)
    scale: float = Field(0.15, gt=0.0)
    r2_points: int = Field(5, ge=2)


class DonchianCfg(_Section):
    weight: float = Field(1.0, ge=0.0)
    n: int = Field(20, ge=1)
    decay: float = Field(0.3, ge=0.0)


class RsiExhaustionCfg(_Section):
    weight: float = Field(0.8, ge=0.0)
    n: int = Field(14, ge=1)
    power: float = Field(1.6, gt=0.0)
    div_lookback: int = Field(14, ge=1)


class BollingerZCfg(_Section):
    weight: float = Field(0.8, ge=0.0)
    n: int = Field(20, ge=2)
    k: float = Field(2.0, gt=0.0)
    z_scale: float = Field(2.0, gt=0.0)
    bw_rank_window: int = Field(200, ge=1)


class CandleGeometryCfg(_Section):
    weight: float = Field(0.6, ge=0.0)
    w_pin: float = Field(1.0, ge=0.0)
    w_engulf: float = Field(1.0, ge=0.0)


class VolRegimeCfg(_Section):
    weight: float = Field(1.0, ge=0.0)
    window: int = Field(24, ge=1)
    rank_window: int = Field(500, ge=1)


class DetectorsSection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    ema_slope: EmaSlopeCfg
    donchian: DonchianCfg
    rsi_exhaustion: RsiExhaustionCfg
    bollinger_z: BollingerZCfg
    candle_geometry: CandleGeometryCfg
    vol_regime: VolRegimeCfg


class FeaturesSection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    n_atr: int = Field(14, ge=1)
    volume_window: int = Field(60, ge=2)


class DetectorsConfig(BaseModel):
    # інші секції файлу (напр. agreement) належать модулю decision — тут їх пропускаємо
    model_config = ConfigDict(extra="allow", frozen=True)
    detectors: DetectorsSection
    features: FeaturesSection = FeaturesSection()


def load_detectors_config(cfg: Mapping[str, Any] | None = None) -> DetectorsConfig:
    if cfg is None:
        from fuzzhelm.config import load_yaml  # noqa: PLC0415

        cfg = load_yaml("detectors")
    try:
        return DetectorsConfig.model_validate(dict(cfg))
    except ValidationError as e:
        err = e.errors()[0]
        path = ".".join(str(p) for p in err["loc"])
        raise ConfigValidationError(err["msg"], path=path) from e


def build_detectors(cfg: Mapping[str, Any] | None = None) -> list[Detector]:
    """Шість детекторів у порядку DETECTOR_NAMES; ваги й параметри — з detectors.yaml."""
    if cfg is None:
        from fuzzhelm.config import load_yaml  # noqa: PLC0415

        cfg = load_yaml("detectors")
    dc = load_detectors_config(cfg)
    fp = FeatureParams.from_config(cfg)
    d = dc.detectors
    out: list[Detector] = [
        EmaSlope(d.ema_slope.weight, horizon=d.ema_slope.horizon, scale=d.ema_slope.scale, params=fp),
        Donchian(d.donchian.weight, decay=d.donchian.decay, params=fp),
        RsiExhaustion(d.rsi_exhaustion.weight, power=d.rsi_exhaustion.power, params=fp),
        BollingerZ(d.bollinger_z.weight, z_scale=d.bollinger_z.z_scale, params=fp),
        CandleGeometry(d.candle_geometry.weight, w_pin=d.candle_geometry.w_pin,
                       w_engulf=d.candle_geometry.w_engulf, params=fp),
        VolRegime(d.vol_regime.weight, params=fp),
    ]
    assert tuple(x.name for x in out) == DETECTOR_NAMES
    return out


def max_warmup(detectors: list[Detector]) -> int:
    return max(d.warmup for d in detectors)
