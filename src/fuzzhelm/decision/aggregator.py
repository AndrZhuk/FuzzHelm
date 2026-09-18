"""Рівень 1 модуля рішень: довірчо-зважений консенсус детекторів.

Найменування: decision/aggregator.py
Призначення: звести 6 виходів детекторів (s_k, c_k, ω_k) у три входи нечіткого ядра T, R, V
(брифінг §5.3). Групи та ваги беруться з самих `DetectorOutput.group/.weight`, тому агрегатор
не знає імен детекторів і не залежить від реєстру.
Автор: Андрій Жук, 2026.

    T = Σ_{k∈A} ω_k c_k s_k / max(ε, Σ_{k∈A} ω_k c_k)      A — група TREND
    R = Σ_{k∈B} ω_k c_k s_k / max(ε, Σ_{k∈B} ω_k c_k)      B — група REVERSION
    V = features["V"] єдиного CONTEXT-виходу (VolRegime),   ε = 1e−9

Гарячий шлях бектесту (~7 млн кроків): один прохід чистим Python без numpy (для 6 елементів
накладні витрати numpy більші за саму арифметику); погрупова діагностика для /explain
обчислюється ліниво з тих самих виходів, лише коли її запитують.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

from fuzzhelm.detectors.base import DetectorGroup, DetectorOutput

EPS = 1e-9
V_DEFAULT = 0.5
V_SOURCE_DEFAULT = "default"

_TREND = DetectorGroup.TREND
_REVERSION = DetectorGroup.REVERSION
_CONTEXT = DetectorGroup.CONTEXT


@dataclass(frozen=True, slots=True)
class GroupConsensus:
    """Діагностика однієї групи для /explain: хто і з якою ефективною вагою сформував T або R."""

    group: DetectorGroup
    value: float                     # T або R
    mass: float                      # Σ ω_k c_k
    names: tuple[str, ...]
    omegas: tuple[float, ...]        # ω_k (конфігураційні ваги)
    effective: tuple[float, ...]     # ω_k · c_k (вага з урахуванням довіри)
    strengths: tuple[float, ...]     # s_k
    eps: float = EPS

    @property
    def shares(self) -> tuple[float, ...]:
        """Частка кожного детектора у значенні групи: value = Σ share_k · s_k."""
        den = max(self.eps, self.mass)
        return tuple(e / den for e in self.effective)

    def to_dict(self) -> dict[str, object]:
        return {
            "group": self.group.value if isinstance(self.group, DetectorGroup) else str(self.group),
            "value": self.value,
            "mass": self.mass,
            "members": [
                {"name": n, "omega": w, "effective": e, "s": s, "share": sh}
                for n, w, e, s, sh in zip(
                    self.names, self.omegas, self.effective, self.strengths, self.shares, strict=True
                )
            ],
        }


def _check_weight(o: DetectorOutput) -> float:
    w = o.weight
    if not 0.0 <= w < math.inf:
        # від'ємна вага зламала б опуклість комбінації (T вийшов би за межі [min s, max s])
        raise ValueError(f"{o.name}: weight={w} must be finite and >= 0")
    return w


def _clip_unit(x: float) -> float:
    # захист від накопичення похибки округлення за межі [−1; 1]
    return -1.0 if x < -1.0 else (1.0 if x > 1.0 else x)


def group_consensus(
    outputs: Sequence[DetectorOutput], group: DetectorGroup, eps: float = EPS
) -> GroupConsensus:
    """Значення і діагностика однієї групи (та сама формула, що й у `consensus`)."""
    members = [o for o in outputs if o.group == group]
    omegas = tuple(_check_weight(o) for o in members)
    effective = tuple(w * o.c for w, o in zip(omegas, members, strict=True))
    strengths = tuple(o.s for o in members)
    num = 0.0
    mass = 0.0
    for e, s in zip(effective, strengths, strict=True):
        num += e * s
        mass += e
    return GroupConsensus(
        group=group, value=_clip_unit(num / max(eps, mass)), mass=mass,
        names=tuple(o.name for o in members), omegas=omegas, effective=effective,
        strengths=strengths, eps=eps,
    )


@dataclass(frozen=True, slots=True)
class Consensus:
    """Входи нечіткого ядра T, R, V.

    `Consensus(T, R, V)` валідний і без діагностики; `consensus()` додатково зберігає виходи
    детекторів, з яких `trend` / `reversion` відтворюють погрупову розкладку на вимогу.
    """

    T: float
    R: float
    V: float
    v_source: str = V_SOURCE_DEFAULT            # ім'я CONTEXT-детектора, звідки взято V, або "default"
    outputs: tuple[DetectorOutput, ...] | None = None  # вхід агрегатора (для лінивої діагностики)
    eps: float = EPS

    @property
    def trend(self) -> GroupConsensus | None:
        return None if self.outputs is None else group_consensus(self.outputs, _TREND, self.eps)

    @property
    def reversion(self) -> GroupConsensus | None:
        return None if self.outputs is None else group_consensus(self.outputs, _REVERSION, self.eps)

    def to_dict(self) -> dict[str, object]:
        trend, reversion = self.trend, self.reversion
        return {
            "T": self.T,
            "R": self.R,
            "V": self.V,
            "v_source": self.v_source,
            "trend": None if trend is None else trend.to_dict(),
            "reversion": None if reversion is None else reversion.to_dict(),
        }


def _volatility(outputs: Sequence[DetectorOutput], v_default: float) -> tuple[float, str]:
    provider: str | None = None
    found: float | None = None
    for o in outputs:
        if o.group != _CONTEXT or "V" not in o.features:
            continue
        # два джерела V — помилка конфігурації незалежно від того, чи прогрілися вони
        if provider is not None:
            raise ValueError(f"ambiguous V: both {provider!r} and {o.name!r} provide features['V']")
        provider = o.name
        raw = o.features["V"]
        # контракт detectors/base.py: до прогріву c = 0 — features["V"] тоді лише заглушка
        # (VolRegime кладе туди 0.5), а не вимір; так само None / NaN
        if not o.c > 0.0 or raw is None:
            continue
        v = float(raw)
        if not math.isfinite(v):
            continue
        if not 0.0 <= v <= 1.0:
            raise ValueError(f"{o.name}: V={v} outside [0,1] (percentile rank expected)")
        found = v
    if found is None or provider is None:
        return v_default, V_SOURCE_DEFAULT
    return found, provider


def consensus(
    outputs: Sequence[DetectorOutput],
    eps: float = EPS,
    v_default: float = V_DEFAULT,
) -> Consensus:
    """Довірчо-зважений консенсус (брифінг §5.3).

    * T, R ∈ [−1; 1]; при нульовій сумарній довірі групи значення дорівнює 0 (а не NaN).
    * V береться з `features["V"]` єдиного CONTEXT-виходу. Якщо його немає, V не скінченне або
      вихід має `c = 0` (прогрів за контрактом detectors/base.py) — `v_default = 0.5`: математичне
      сподівання перцентильного рангу за відсутності інформації; `v_source = "default"` фіксує це
      у трасуванні.
    * Від'ємна / нескінченна вага або два джерела V → `ValueError`.
    """
    if not eps > 0.0:
        raise ValueError(f"eps={eps} must be > 0")
    num_t = mass_t = num_r = mass_r = 0.0
    has_context = False
    for o in outputs:
        g = o.group
        if g == _CONTEXT:
            has_context = True
            continue
        w = o.weight
        if not 0.0 <= w < math.inf:
            raise ValueError(f"{o.name}: weight={w} must be finite and >= 0")
        e = w * o.c
        if g == _TREND:
            num_t += e * o.s
            mass_t += e
        elif g == _REVERSION:
            num_r += e * o.s
            mass_r += e
    v, v_source = _volatility(outputs, v_default) if has_context else (v_default, V_SOURCE_DEFAULT)
    return Consensus(
        T=_clip_unit(num_t / max(eps, mass_t)),
        R=_clip_unit(num_r / max(eps, mass_r)),
        V=v,
        v_source=v_source,
        outputs=tuple(outputs),
        eps=eps,
    )
