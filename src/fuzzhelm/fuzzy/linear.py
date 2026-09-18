"""Базова лінія для ablation: лінійне голосування T і R.

Найменування: fuzzy/linear.py
Призначення: найпростіший інтерпретований рушій з тим самим контрактом InferenceEngine, щоб
    порівняння «Мамдані vs лінійне» показувало внесок саме режимно-залежних політик (брифінг §11).
Автор: Андрій Жук, 2026.

u_raw = clip((w_T·T + w_R·R) / (|w_T| + |w_R|), −1, 1).
V свідомо не входить: лінійне голосування «сліпе» до режиму волатильності за побудовою — саме
це й відрізняє його від бази правил з трьома політиками. Функція монотонна за T при w_T ≥ 0.
"""

from __future__ import annotations

import math
from collections.abc import Mapping

import numpy as np
import numpy.typing as npt

from fuzzhelm.fuzzy.base import FuzzyResult

FloatArray = npt.NDArray[np.float64]
DEFAULT_WEIGHTS: Mapping[str, float] = {"T": 0.5, "R": 0.5}


class LinearVoteEngine:
    """Лінійне голосування: `name = "linear"`, `fired = ()`, внески голосів — у `extras`."""

    name = "linear"

    def __init__(self, weights: Mapping[str, float] | None = None) -> None:
        w = dict(DEFAULT_WEIGHTS if weights is None else weights)
        unknown = set(w) - {"T", "R"}
        if unknown:
            raise ValueError(f"unknown vote inputs {sorted(unknown)}; expected T and/or R")
        self.w_T = float(w.get("T", 0.0))
        self.w_R = float(w.get("R", 0.0))
        if not (math.isfinite(self.w_T) and math.isfinite(self.w_R)):
            raise ValueError("weights must be finite")
        self._norm = abs(self.w_T) + abs(self.w_R)
        if self._norm == 0.0:
            raise ValueError("at least one weight must be non-zero")

    @property
    def weights(self) -> dict[str, float]:
        return {"T": self.w_T, "R": self.w_R}

    def infer_u(self, T: float, R: float, V: float) -> float:
        for name, x in (("T", T), ("R", R), ("V", V)):
            if not math.isfinite(x):
                raise ValueError(f"input {name}={x!r} is not finite")
        u = (self.w_T * T + self.w_R * R) / self._norm
        return -1.0 if u < -1.0 else 1.0 if u > 1.0 else u

    def infer(self, T: float, R: float, V: float) -> FuzzyResult:
        u = self.infer_u(T, R, V)
        return FuzzyResult(
            u_raw=u,
            inputs={"T": float(T), "R": float(R), "V": float(V)},
            memberships={},
            fired=(),
            engine=self.name,
            extras={"vote_T": self.w_T * T / self._norm, "vote_R": self.w_R * R / self._norm},
        )

    def infer_batch(self, T: npt.ArrayLike, R: npt.ArrayLike, V: npt.ArrayLike) -> FloatArray:
        t, r, v = np.broadcast_arrays(np.asarray(T, dtype=np.float64), np.asarray(R, dtype=np.float64),
                                      np.asarray(V, dtype=np.float64))
        if not (np.all(np.isfinite(t)) and np.all(np.isfinite(r)) and np.all(np.isfinite(v))):
            raise ValueError("inputs contain non-finite values")
        return np.clip((self.w_T * t + self.w_R * r) / self._norm, -1.0, 1.0)
