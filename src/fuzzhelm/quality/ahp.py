"""Метод аналізу ієрархій (AHP, Сааті): ваги компонент скору якості Q з матриці парних порівнянь.

Найменування: quality/ahp.py
Призначення: ваги w₁..w₄ (completeness, validity, timeliness, continuity) не «на око», а головний
власний вектор матриці парних порівнянь із перевіркою узгодженості (брифінг §5.17).
Автор: Андрій Жук, 2026.

Формули:
  A·w = λ_max·w,  Σw = 1  (за теоремою Перрона — Фробеніуса для додатної матриці власний вектор
                           при λ_max можна взяти додатним і він єдиний з точністю до множника)
  CI = (λ_max − n)/(n − 1),   CR = CI / RI(n),   узгоджено при CR < 0.1
  RI — випадковий індекс Сааті (середній CI випадкових обернено-симетричних матриць).

Запуск `python -m fuzzhelm.quality.ahp` перераховує ваги з config/dq_weights.yaml і вписує фактичні
`weights` і `consistency_ratio` у той самий файл (решта файлу, включно з коментарями, не змінюється).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

import numpy as np

from fuzzhelm.config import CONFIG_DIR, load_yaml

# Випадкові індекси Сааті (Saaty, 1980), n = 1..10
SAATY_RI: Final[dict[int, float]] = {1: 0.0, 2: 0.0, 3: 0.58, 4: 0.90, 5: 1.12, 6: 1.24, 7: 1.32,
                                     8: 1.41, 9: 1.45, 10: 1.49}
CR_THRESHOLD: Final = 0.1
CRITERIA: Final = ("completeness", "validity", "timeliness", "continuity")
# 0.333 у конфігурації замість 1/3: обернена симетрія перевіряється з цим допуском
RECIPROCAL_RTOL: Final = 5e-3


@dataclass(frozen=True, slots=True)
class AhpResult:
    weights: tuple[float, ...]      # Σ = 1, усі > 0
    lambda_max: float
    ci: float
    cr: float
    n: int
    ri: float
    method: str

    @property
    def consistent(self) -> bool:
        return self.cr < CR_THRESHOLD


def _validate(matrix: Sequence[Sequence[float]] | np.ndarray) -> np.ndarray:
    a = np.asarray(matrix, dtype=np.float64)
    if a.ndim != 2 or a.shape[0] != a.shape[1] or a.shape[0] < 1:
        raise ValueError(f"AHP matrix must be square, got shape {a.shape}")
    if not np.all(np.isfinite(a)) or np.any(a <= 0):
        raise ValueError("AHP matrix entries must be finite and > 0")
    if not np.allclose(np.diag(a), 1.0):
        raise ValueError("AHP matrix diagonal must be 1")
    if not np.allclose(a * a.T, 1.0, rtol=RECIPROCAL_RTOL, atol=0.0):
        raise ValueError(f"AHP matrix must be reciprocal (a_ij·a_ji = 1 within {RECIPROCAL_RTOL})")
    if a.shape[0] not in SAATY_RI:
        raise ValueError(f"no Saaty random index for n={a.shape[0]}")
    return a


def _eig(a: np.ndarray) -> tuple[np.ndarray, float]:
    vals, vecs = np.linalg.eig(a)
    i = int(np.argmax(vals.real))
    v = np.abs(vecs[:, i].real)          # знак власного вектора довільний; компоненти одного знака
    return v / v.sum(), float(vals[i].real)


def _power(a: np.ndarray, tol: float = 1e-15, max_iter: int = 10_000) -> tuple[np.ndarray, float]:
    n = a.shape[0]
    w = np.full(n, 1.0 / n)
    for _ in range(max_iter):
        nxt = a @ w
        nxt /= nxt.sum()
        if np.max(np.abs(nxt - w)) < tol:
            w = nxt
            break
        w = nxt
    # λ_max = середнє (A·w)_i / w_i — оцінка Сааті, точна для власного вектора
    lam = float(np.mean((a @ w) / w))
    return w, lam


def ahp_weights(matrix: Sequence[Sequence[float]] | np.ndarray,
                method: Literal["eig", "power"] = "eig") -> AhpResult:
    """Ваги AHP (головний власний вектор, Σ=1), λ_max, CI, CR."""
    a = _validate(matrix)
    n = a.shape[0]
    w, lam = _eig(a) if method == "eig" else _power(a)
    ri = SAATY_RI[n]
    ci = (lam - n) / (n - 1) if n > 1 else 0.0
    cr = ci / ri if ri > 0 else 0.0
    return AhpResult(tuple(float(x) for x in w), lam, ci, cr, n, ri, method)


def load_matrix(config_dir: Path | None = None) -> list[list[float]]:
    cfg = load_yaml("dq_weights", config_dir)
    m = cfg.get("ahp_matrix")
    if not isinstance(m, list):
        raise ValueError("dq_weights.yaml: ahp_matrix must be a list of rows")
    return [[float(x) for x in row] for row in m]


def write_weights_yaml(result: AhpResult, path: Path | None = None, *, decimals: int = 6) -> str:
    """Вписати `weights` і `consistency_ratio` у YAML, зберігши решту тексту (коментарі, порядок)."""
    p = path or (CONFIG_DIR / "dq_weights.yaml")
    text = p.read_text(encoding="utf-8")
    w = ", ".join(f"{x:.{decimals}f}" for x in result.weights)
    new_w = (f"weights: [{w}]   # головний власний вектор (quality/ahp.py, {result.method}); "
             f"λ_max = {result.lambda_max:.6f}")
    new_cr = f"consistency_ratio: {result.cr:.6f}   # CR = CI/RI(n={result.n}) = " \
             f"{result.ci:.6f}/{result.ri:.2f} < 0.1"
    text, k1 = re.subn(r"(?m)^weights:.*$", new_w, text)
    text, k2 = re.subn(r"(?m)^consistency_ratio:.*$", new_cr, text)
    if k1 != 1 or k2 != 1:
        raise ValueError(f"{p}: expected exactly one 'weights:' and one 'consistency_ratio:' line")
    p.write_text(text, encoding="utf-8")
    return text


def main() -> None:  # pragma: no cover - CLI-обгортка
    res = ahp_weights(load_matrix())
    alt = ahp_weights(load_matrix(), method="power")
    write_weights_yaml(res)
    print(f"weights={res.weights} lambda_max={res.lambda_max:.6f} CI={res.ci:.6f} CR={res.cr:.6f}")
    print(f"power-iteration cross-check: max |Δw| = "
          f"{max(abs(a - b) for a, b in zip(res.weights, alt.weights, strict=True)):.2e}")


if __name__ == "__main__":  # pragma: no cover
    main()
