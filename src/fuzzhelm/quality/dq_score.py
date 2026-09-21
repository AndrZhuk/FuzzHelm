"""Скор якості даних Q ∈ [0, 1] за годину (брифінг §5.17).

Найменування: quality/dq_score.py
Призначення: чотири компоненти якості потоку і їх зважена сума (ваги — config/dq_weights.yaml);
накопичувач статистики вікна,
з якого конвеєр інжесту формує рядок таблиці dq_score.
Автор: Андрій Жук, 2026.

Формули (§5.17):
  completeness = N_obs / N_exp                    (частка очікуваних 1m-кошиків, що є)
  validity     = 1 − N_invalid / N_total          (частка подій без порушень інваріантів)
  timeliness   = exp(−lag_p95 / τ₀),  τ₀ = 1000 мс
  continuity   = 1 − gap_sec / 3600
  Q = w₁·completeness + w₂·validity + w₃·timeliness + w₄·continuity

Кожна компонента обрізається до [0, 1] (дублікати можуть дати N_obs > N_exp, прогалини довші за
годину — gap_sec > 3600). Ваги мусять бути невід'ємні з Σw = 1 — тоді Q — опукла комбінація чисел з
[0, 1] і сама лежить у [0, 1] (property-тест test_dq_score_in_unit_interval).

Угоди на виродках: N_exp = 0 → completeness = 1 (нічого не очікувалось); N_total = 0 → validity = 1
(немає свідчень невалідності; брак даних штрафує completeness); lag < 0 (зсув годинника) → 0;
NaN у лагу чи тривалості прогалин → ValueError (а не «ідеальна» компонента).
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

import numpy as np

from fuzzhelm.config import load_yaml

TAU0_MS: Final = 1000.0
# порядок компонент у вагах config/dq_weights.yaml
CRITERIA: Final = ("completeness", "validity", "timeliness", "continuity")
HOUR_S: Final = 3600.0
WEIGHT_SUM_TOL: Final = 1e-6


@dataclass(frozen=True, slots=True)
class DqInputs:
    expected_buckets: int            # N_exp
    observed_buckets: int            # N_obs
    total_count: int                 # N_total (усі перевірені події/свічки вікна)
    invalid_count: int = 0           # порушення інваріантів / нормалізації
    gap_seconds: float = 0.0
    lag_p95_ms: float = 0.0
    window_s: float = HOUR_S         # знаменник continuity (година за §5.17)


@dataclass(frozen=True, slots=True)
class DqScore:
    completeness: float
    validity: float
    timeliness: float
    continuity: float
    score: float
    weights: tuple[float, float, float, float]
    inputs: DqInputs

    def as_row(self) -> dict[str, float | int]:
        """Поля таблиці dq_score (без ключів instrument_id/hour_start)."""
        i = self.inputs
        return {
            "expected_buckets": i.expected_buckets, "observed_buckets": i.observed_buckets,
            "invalid_count": i.invalid_count,
            "gap_seconds": round(i.gap_seconds, 2), "lag_p95_ms": round(i.lag_p95_ms, 2),
            "completeness": round(self.completeness, 4), "validity": round(self.validity, 4),
            "timeliness": round(self.timeliness, 4), "continuity": round(self.continuity, 4),
            "score": round(self.score, 4),
        }


def _clip01(x: float) -> float:
    if math.isnan(x):
        raise ValueError("NaN in DQ component")
    return 0.0 if x < 0.0 else 1.0 if x > 1.0 else x


def completeness(observed: int, expected: int) -> float:
    return 1.0 if expected <= 0 else _clip01(observed / expected)


def validity(invalid: int, total: int) -> float:
    return 1.0 if total <= 0 else _clip01(1.0 - invalid / total)


def _not_nan(x: float, name: str) -> float:
    # max(0.0, nan) у Python дає 0.0 — без цієї перевірки NaN тихо ставав би «ідеальним» входом
    if math.isnan(x):
        raise ValueError(f"{name} is NaN")
    return x


def timeliness(lag_p95_ms: float, tau0_ms: float = TAU0_MS) -> float:
    if not tau0_ms > 0:
        raise ValueError("tau0_ms must be > 0")
    return _clip01(math.exp(-max(0.0, _not_nan(lag_p95_ms, "lag_p95_ms")) / tau0_ms))


def continuity(gap_seconds: float, window_s: float = HOUR_S) -> float:
    if not window_s > 0:
        raise ValueError("window_s must be > 0")
    return _clip01(1.0 - max(0.0, _not_nan(gap_seconds, "gap_seconds")) / window_s)


def check_weights(weights: Sequence[float]) -> tuple[float, float, float, float]:
    if len(weights) != 4:
        raise ValueError(f"expected 4 weights, got {len(weights)}")
    w = tuple(float(x) for x in weights)
    if any(not math.isfinite(x) or x < 0 for x in w):
        raise ValueError(f"weights must be finite and >= 0, got {w}")
    if abs(sum(w) - 1.0) > WEIGHT_SUM_TOL:
        raise ValueError(f"weights must sum to 1, got {sum(w)}")
    return w[0], w[1], w[2], w[3]


def dq_score(inputs: DqInputs, weights: Sequence[float], tau0_ms: float = TAU0_MS) -> DqScore:
    w = check_weights(weights)
    comp = (
        completeness(inputs.observed_buckets, inputs.expected_buckets),
        validity(inputs.invalid_count, inputs.total_count),
        timeliness(inputs.lag_p95_ms, tau0_ms),
        continuity(inputs.gap_seconds, inputs.window_s),
    )
    # опукла комбінація; clip — лише від похибки округлення Σw ≈ 1
    q = _clip01(math.fsum(wi * ci for wi, ci in zip(w, comp, strict=True)))
    return DqScore(comp[0], comp[1], comp[2], comp[3], q, w, inputs)


def load_dq_weights(config_dir: Path | None = None) -> tuple[float, float, float, float]:
    """Ваги з config/dq_weights.yaml (порядок: повнота, коректність, своєчасність, безперервність)."""
    w = [float(x) for x in load_yaml("dq_weights", config_dir)["weights"]]
    s = sum(w)
    return check_weights([x / s for x in w])   # 6 знаків у YAML → ренормування до Σ = 1


def load_tau0_ms(config_dir: Path | None = None) -> float:
    return float(load_yaml("dq_weights", config_dir).get("tau0_ms", TAU0_MS))


def p95(values: Iterable[float]) -> float:
    """95-й перцентиль (лінійна інтерполяція numpy); порожньо → 0."""
    arr = np.fromiter(values, dtype=np.float64)
    return float(np.percentile(arr, 95)) if arr.size else 0.0


def merged_length_ns(intervals: Iterable[tuple[int, int]]) -> int:
    """Довжина об'єднання півінтервалів [lo, hi) — перекриття прогалин не рахуються двічі."""
    total = 0
    cur_lo: int | None = None
    cur_hi = 0
    for lo, hi in sorted((a, b) for a, b in intervals if b > a):
        if cur_lo is None or lo > cur_hi:
            if cur_lo is not None:
                total += cur_hi - cur_lo
            cur_lo, cur_hi = lo, hi
        else:
            cur_hi = max(cur_hi, hi)
    if cur_lo is not None:
        total += cur_hi - cur_lo
    return total


@dataclass
class DqAccumulator:
    """Статистика одного вікна (зазвичай години) для DqInputs."""

    window_start_ns: int
    window_ns: int = int(HOUR_S) * 1_000_000_000
    buckets: set[int] = field(default_factory=set)          # open_time закритих свічок
    total: int = 0
    invalid: int = 0
    lags_ms: list[float] = field(default_factory=list)
    gaps: list[tuple[int, int]] = field(default_factory=list)

    def add_bucket(self, open_time_ns: int) -> None:
        self.buckets.add(open_time_ns)

    def add_checked(self, *, invalid: bool = False) -> None:
        self.total += 1
        self.invalid += int(invalid)

    def add_lag_ms(self, lag_ms: float) -> None:
        self.lags_ms.append(lag_ms)

    def add_gap(self, lo_ns: int, hi_ns: int) -> None:
        # лише частина прогалини, що лежить у вікні
        lo = max(lo_ns, self.window_start_ns)
        hi = min(hi_ns, self.window_start_ns + self.window_ns)
        if hi > lo:
            self.gaps.append((lo, hi))

    def inputs(self, expected_buckets: int) -> DqInputs:
        return DqInputs(
            expected_buckets=expected_buckets, observed_buckets=len(self.buckets),
            total_count=self.total, invalid_count=self.invalid,
            gap_seconds=merged_length_ns(self.gaps) / 1e9, lag_p95_ms=p95(self.lags_ms),
            window_s=self.window_ns / 1e9,
        )
