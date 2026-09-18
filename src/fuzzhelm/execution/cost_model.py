"""Модель витрат виконання: спред, квадратно-кореневий імпакт, комісії, фандинг, seeded-шум.

Найменування: execution/cost_model.py
Призначення: ціна виконання ринкової/стоп-заявки та грошові витрати угоди (брифінг §5.14).
Автор: Андрій Жук, 2026.

    P_fill  = P_ref + side·( δ_spread/2 + k_s·σ_t·P_ref·√(Q/V_bar) ) + P_ref·noise_bps·10⁻⁴·z,
              z ~ N(0,1) з numpy.random.Generator(PCG64(seed))
    fee     = N·τ,   τ_taker = 0.0004, τ_maker = 0.0002,   N = Q·P_fill
    funding = q·P_mark·f_rate  о 00/08/16 UTC  (q зі знаком: лонг платить додатний фандинг)

Три рівні деградації (експеримент «наскільки наївна модель завищує результат»):
    zero        — P_fill = P_ref, без комісій і фандингу (ідеальне виконання);
    sqrt_impact — спред + імпакт + шум, без комісій і фандингу;
    full        — sqrt_impact + комісії + фандинг (робочий режим, config/cost_model.yaml).

Шум (noise_bps) — навмисне і ЄДИНЕ джерело залежності кривої капіталу від seed: без нього
негативний контроль test_different_seed_changes_equity був би порожнім (deviations.d/execution.md).
Генератор — власний екземпляр PCG64, ніколи не глобальний numpy/random.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal
from enum import StrEnum
from typing import Any

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, field_validator

from fuzzhelm.config import load_yaml
from fuzzhelm.core.clock import NS_PER_DAY
from fuzzhelm.core.enums import Liquidity, Side
from fuzzhelm.core.money import D0, D1, quantize_price, quantize_step
from fuzzhelm.sizing.convert import float_to_decimal_exact

NS_PER_HOUR = 3_600_000_000_000
BPS = Decimal("0.0001")
SLIPPAGE_BPS_QUANTUM = Decimal("0.0001")   # NUMERIC(12,4) у sim_order.slippage_bps


class CostMode(StrEnum):
    ZERO = "zero"
    SQRT_IMPACT = "sqrt_impact"
    FULL = "full"


class CostModelConfig(BaseModel):
    """Схема config/cost_model.yaml. Числа YAML (float) pydantic перетворює на Decimal через str."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    mode: CostMode = CostMode.FULL
    k_s: Decimal = Field(default=Decimal("0.5"), ge=0)
    taker_fee: Decimal = Field(default=Decimal("0.0004"), ge=0)
    maker_fee: Decimal = Field(default=Decimal("0.0002"), ge=0)
    default_spread_ticks: int = Field(default=1, ge=0)
    funding_hours_utc: tuple[int, ...] = (0, 8, 16)
    noise_bps: Decimal = Field(default=Decimal(0), ge=0)

    @field_validator("funding_hours_utc")
    @classmethod
    def _hours_valid(cls, v: tuple[int, ...]) -> tuple[int, ...]:
        if not v or any(h < 0 or h > 23 for h in v) or len(set(v)) != len(v):
            raise ValueError("funding_hours_utc must be distinct hours in [0, 23]")
        return tuple(sorted(v))


@dataclass(frozen=True, slots=True)
class FillQuote:
    """Результат ціноутворення однієї заявки (усе в Decimal)."""

    price: Decimal          # квантована до tick (HALF_EVEN), > 0
    raw_price: Decimal      # до квантування
    half_spread: Decimal    # δ_spread/2 (у ціні)
    impact: Decimal         # k_s·σ·P_ref·√(Q/V_bar)
    noise: Decimal          # P_ref·noise_bps·10⁻⁴·z (зі знаком, не залежить від side)
    slippage_bps: Decimal   # side·(price − P_ref)/P_ref·10⁴, квантовано до 0.0001


@dataclass(frozen=True, slots=True)
class FundingCharge:
    """Нарахування фандингу на відкриту позицію в момент ts_ns (00/08/16 UTC)."""

    instrument: str
    ts_ns: int
    position_qty: Decimal   # зі знаком (лонг > 0)
    mark_price: Decimal
    rate: Decimal
    amount: Decimal         # сплачено рахунком: > 0 — витрата, < 0 — дохід


class CostModel:
    """Детермінована (за seed) модель витрат. Один екземпляр — один потік нормальних величин."""

    def __init__(  # noqa: PLR0917 — позиційний порядок зафіксовано в contracts.md §12
        self,
        mode: CostMode | str = CostMode.FULL,
        k_s: Decimal = Decimal("0.5"),
        taker: Decimal = Decimal("0.0004"),
        maker: Decimal = Decimal("0.0002"),
        noise_bps: Decimal = D0,
        seed: int = 0,
        *,
        spread_ticks: int = 1,
        funding_hours_utc: Sequence[int] = (0, 8, 16),
    ) -> None:
        cfg = CostModelConfig(
            mode=CostMode(mode), k_s=k_s, taker_fee=taker, maker_fee=maker,
            default_spread_ticks=spread_ticks, funding_hours_utc=tuple(funding_hours_utc),
            noise_bps=noise_bps,
        )
        self.config = cfg
        self.mode = cfg.mode
        self.k_s = cfg.k_s
        self.taker = cfg.taker_fee
        self.maker = cfg.maker_fee
        self.noise_bps = cfg.noise_bps
        self.spread_ticks = cfg.default_spread_ticks
        self.funding_hours_utc = cfg.funding_hours_utc
        self.seed = seed
        self._funding_offsets_ns = tuple(h * NS_PER_HOUR for h in cfg.funding_hours_utc)
        self._noise_scale = cfg.noise_bps * BPS
        self._rng = np.random.Generator(np.random.PCG64(seed))

    # ------------------------------------------------------------ побудова

    @classmethod
    def from_config(
        cls,
        cfg: Mapping[str, Any] | None = None,
        *,
        seed: int,
        mode: CostMode | str | None = None,
    ) -> CostModel:
        """З config/cost_model.yaml (або переданого dict); `mode` перекриває режим з конфігурації."""
        parsed = CostModelConfig.model_validate(cfg if cfg is not None else load_yaml("cost_model"))
        return cls(
            mode if mode is not None else parsed.mode,
            k_s=parsed.k_s, taker=parsed.taker_fee, maker=parsed.maker_fee,
            noise_bps=parsed.noise_bps, seed=seed, spread_ticks=parsed.default_spread_ticks,
            funding_hours_utc=parsed.funding_hours_utc,
        )

    # ------------------------------------------------------------ властивості режиму

    @property
    def charges_fees(self) -> bool:
        return self.mode == CostMode.FULL

    @property
    def charges_funding(self) -> bool:
        return self.mode == CostMode.FULL

    @property
    def has_price_impact(self) -> bool:
        return self.mode != CostMode.ZERO

    # ------------------------------------------------------------ ціна виконання

    def impact(self, p_ref: Decimal, qty: Decimal, sigma: Decimal, v_bar: Decimal) -> Decimal:
        """Квадратно-кореневий імпакт k_s·σ_t·P_ref·√(Q/V_bar) (без знака, у ціні).

        V_bar ≤ 0 (порожня історія обсягів) → участь Q/V_bar вважається рівною 1: консервативна
        заміна нескінченності, а не нуль (нуль занижував би витрати саме там, де ліквідності немає).
        """
        if not self.has_price_impact or qty <= 0 or sigma <= 0:
            return D0
        participation = qty / v_bar if v_bar > 0 else D1
        return self.k_s * sigma * p_ref * participation.sqrt()

    def quote(
        self,
        side: Side,
        p_ref: Decimal,
        qty: Decimal,
        *,
        sigma: Decimal,
        v_bar: Decimal,
        tick: Decimal,
    ) -> FillQuote:
        """Ціна виконання для заявки `side` обсягом `qty` від опорної ціни `p_ref`.

        У режимах sqrt_impact/full кожен виклик споживає рівно одне число z з PCG64(seed) —
        порядок викликів (порядок виконань) визначає послідовність, тож прогін відтворюваний.
        """
        if side == Side.FLAT:
            raise ValueError("fill side must be LONG or SHORT")
        if p_ref <= 0:
            raise ValueError(f"reference price must be > 0, got {p_ref}")
        if not self.has_price_impact:
            price = quantize_price(p_ref, tick)
            return FillQuote(price, p_ref, D0, D0, D0, self._slippage_bps(side, price, p_ref))
        half_spread = tick * self.spread_ticks / 2
        imp = self.impact(p_ref, qty, sigma, v_bar)
        noise = D0
        if self._noise_scale > 0:
            z = float_to_decimal_exact(self._rng.standard_normal())
            noise = p_ref * self._noise_scale * z
        sgn = int(side)
        raw = p_ref + sgn * (half_spread + imp) + noise
        # екстремальний від'ємний шум/імпакт на шорті не може дати нульову чи від'ємну ціну
        price = max(quantize_price(raw, tick), tick)
        return FillQuote(price, raw, half_spread, imp, noise, self._slippage_bps(side, price, p_ref))

    @staticmethod
    def _slippage_bps(side: Side, price: Decimal, p_ref: Decimal) -> Decimal:
        bps = int(side) * (price - p_ref) / p_ref * 10_000
        return quantize_step(bps, SLIPPAGE_BPS_QUANTUM, ROUND_HALF_EVEN)

    # ------------------------------------------------------------ гроші

    def fee_rate(self, liquidity: Liquidity) -> Decimal:
        return self.taker if liquidity == Liquidity.TAKER else self.maker

    def fee(self, notional: Decimal, liquidity: Liquidity) -> Decimal:
        """fee = |N|·τ (у режимах zero/sqrt_impact — 0)."""
        if not self.charges_fees:
            return D0
        return abs(notional) * self.fee_rate(liquidity)

    def funding_payment(self, position_qty: Decimal, mark_price: Decimal, rate: Decimal) -> Decimal:
        """Сума, яку СПЛАЧУЄ рахунок: q·P_mark·rate. Лонг (q>0) при rate>0 платить, шорт отримує."""
        if not self.charges_funding:
            return D0
        return position_qty * mark_price * rate

    # ------------------------------------------------------------ розклад фандингу

    def is_funding_time(self, t_ns: int) -> bool:
        return (t_ns % NS_PER_DAY) in self._funding_offsets_ns

    def next_funding_time(self, t_ns: int) -> int:
        """Найближчий момент фандингу, строго пізніший за t_ns."""
        day = t_ns - (t_ns % NS_PER_DAY)
        for off in self._funding_offsets_ns:
            if day + off > t_ns:
                return day + off
        return day + NS_PER_DAY + self._funding_offsets_ns[0]

    def funding_times(self, t_from_ns: int, t_to_ns: int) -> list[int]:
        """Моменти фандингу в напіввідкритому інтервалі (t_from_ns, t_to_ns], у зростаючому порядку."""
        if t_to_ns <= t_from_ns:
            return []
        out: list[int] = []
        day = t_from_ns - (t_from_ns % NS_PER_DAY)
        while day <= t_to_ns:
            for off in self._funding_offsets_ns:
                t = day + off
                if t_from_ns < t <= t_to_ns:
                    out.append(t)
            day += NS_PER_DAY
        return out
