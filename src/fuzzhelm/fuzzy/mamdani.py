"""Рушій нечіткого виведення Мамдані (max-min, центроїд).

Найменування: fuzzy/mamdani.py
Призначення: реалізація InferenceEngine за брифінгом §5.6–5.7 без сторонніх fuzzy-бібліотек
    (механізм виведення і є темою роботи).
Автор: Андрій Жук, 2026.

Для правила r: «ЯКЩО T є A_r І R є B_r І V є C_r ТО U є D_r»:
    α_r      = w_r · min(μ_{A_r}(T), μ_{B_r}(R), μ_{C_r}(V))      терм "any" дає внесок 1.0
    μ'_r(u)  = min(α_r, μ_{D_r}(u))                               обрізання
    μ_agg(u) = max_r μ'_r(u)                                       s-норма max
    u_raw    = ∫u·μ_agg du / ∫μ_agg du                             центроїд (defuzz.py)

Оптимізація без зміни результату: max_r min(α_r, μ_{D_r}(u)) = max_k min(β_k, μ_k(u)), де
β_k = max{α_r : D_r = k} — тому агрегація робиться по 5 термах U, а не по 45 правилах, над
попередньо обчисленими на сітці масивами μ_k(u_j). Правила згруповані за термом T: правила з
μ_T = 0 мають α = 0 і не змінюють β, тож швидкий шлях `infer_u` їх пропускає.
"""

from __future__ import annotations

import math

import numpy as np
import numpy.typing as npt

from fuzzhelm.fuzzy.base import FiredRule, FuzzyResult
from fuzzhelm.fuzzy.defuzz import aggregate, centroid_weighted, make_grid, quadrature_weights
from fuzzhelm.fuzzy.membership import (
    ANY_TERM,
    DEFUZZ_SCHEMES,
    INPUT_VARIABLES,
    LinguisticVariable,
    MembershipConfig,
    MembershipFunction,
    load_membership,
)
from fuzzhelm.fuzzy.rules import RuleBase, load_rulebase, validate_rulebase

FloatArray = npt.NDArray[np.float64]
_BATCH_CHUNK = 2048                 # точок за раз у infer_batch: (5 × chunk × nodes) float64


def _check_finite(name: str, x: float) -> float:
    xf = float(x)
    if not math.isfinite(xf):
        raise ValueError(f"input {name}={x!r} is not finite")
    return xf


class MamdaniEngine:
    """Мамдані max-min з центроїдом на сітці u_j = lo + jΔ (за замовчуванням 201 вузол, трапеції)."""

    name = "mamdani"

    def __init__(self, membership: MembershipConfig, rulebase: RuleBase,
                 scheme: str | None = None, nodes: int | None = None) -> None:
        # структурна перевірка (терми існують, без дублікатів); повноту/ваги перевіряє load_rulebase
        validate_rulebase(rulebase, membership, production=False, require_complete=False)
        self._membership = membership
        self._rulebase = rulebase
        self.scheme = scheme if scheme is not None else membership.defuzz.scheme
        self.nodes = int(nodes if nodes is not None else membership.defuzz.grid_nodes)
        if self.scheme not in DEFUZZ_SCHEMES:
            raise ValueError(f"unknown scheme {self.scheme!r}; expected one of {DEFUZZ_SCHEMES}")
        U = membership.U
        self._u_lo, self._u_hi = U.range
        self.grid: FloatArray = make_grid(self.nodes, self._u_lo, self._u_hi)
        self._w = quadrature_weights(self.nodes, self.scheme, self._u_lo, self._u_hi)
        self._wu = self._w * self.grid
        self._mu_terms = U.evaluate(self.grid)             # (n_U, nodes), попередньо обчислено
        self._mu_terms.setflags(write=False)
        self.grid.setflags(write=False)

        self._vars: tuple[LinguisticVariable, ...] = tuple(membership[v] for v in INPUT_VARIABLES)
        self._mfs: tuple[tuple[MembershipFunction, ...], ...] = tuple(
            tuple(var.terms.values()) for var in self._vars)
        self._ranges = tuple(var.range for var in self._vars)
        u_index = {name: k for k, name in enumerate(U.term_names)}
        self._u_names = U.term_names
        # правило → (i_T, i_R, i_V, k_U, w); "any" → індекс останнього елемента [..., 1.0]
        compiled: list[tuple[int, int, int, int, float]] = []
        for rule in rulebase.rules:
            idx: list[int] = []
            for var in self._vars:
                term = rule.antecedent[var.name]
                idx.append(len(var.terms) if term == ANY_TERM else var.term_names.index(term))
            compiled.append((idx[0], idx[1], idx[2], u_index[rule.consequent], float(rule.w)))
        self._compiled = tuple(compiled)
        n_t = len(self._mfs[0])
        groups: list[list[tuple[int, int, int, float]]] = [[] for _ in range(n_t + 1)]
        for ti, ri, vi, ci, w in compiled:
            groups[ti].append((ri, vi, ci, w))
        self._by_t = tuple(tuple(g) for g in groups)
        # масиви для векторизованого infer_batch
        self._rule_idx = np.array([c[:4] for c in compiled], dtype=np.intp).reshape(-1, 4)
        self._rule_w = np.array([c[4] for c in compiled], dtype=np.float64)

    # ------------------------------------------------------------------ властивості

    @property
    def membership(self) -> MembershipConfig:
        return self._membership

    @property
    def rulebase(self) -> RuleBase:
        return self._rulebase

    @property
    def consequent_mu(self) -> FloatArray:
        """μ_k(u_j) термів U на сітці рушія, форма (n_U, nodes); лише для читання."""
        return self._mu_terms

    def with_grid(self, nodes: int | None = None, scheme: str | None = None) -> MamdaniEngine:
        """Той самий рушій з іншою сіткою/схемою (для дослідження збіжності)."""
        return MamdaniEngine(self._membership, self._rulebase,
                             scheme=scheme or self.scheme, nodes=nodes or self.nodes)

    # ------------------------------------------------------------------ ядро

    def _fuzzify_all(self, T: float, R: float, V: float) -> tuple[list[float], list[float], list[float]]:
        out: list[list[float]] = []
        for x, name, mfs, (lo, hi) in zip((T, R, V), INPUT_VARIABLES, self._mfs, self._ranges, strict=True):
            xf = _check_finite(name, x)
            xc = lo if xf < lo else hi if xf > hi else xf
            mu = [mf.scalar(xc) for mf in mfs]
            mu.append(1.0)                                  # слот для терму "any"
            out.append(mu)
        return out[0], out[1], out[2]

    def _strengths(self, mt: list[float], mr: list[float], mv: list[float]) -> list[float]:
        beta = [0.0] * len(self._u_names)
        last = len(mt) - 1
        for ti, mu_t in enumerate(mt):
            if mu_t <= 0.0 and ti != last:
                continue                                    # α = 0 для всієї групи: β не змінюється
            for ri, vi, ci, w in self._by_t[ti]:
                beta[ci] = max(beta[ci], min(mu_t, mr[ri], mv[vi]) * w)
        return beta

    def consequent_strengths(self, T: float, R: float, V: float) -> FloatArray:
        """β_k = max{α_r : D_r = k} для кожного терму U (у порядку термів U)."""
        mt, mr, mv = self._fuzzify_all(T, R, V)
        return np.array(self._strengths(mt, mr, mv), dtype=np.float64)

    def infer_u(self, T: float, R: float, V: float) -> float:
        """Швидкий шлях: лише u_raw, без трасування. Побітово дорівнює infer(T,R,V).u_raw."""
        mt, mr, mv = self._fuzzify_all(T, R, V)
        beta = self._strengths(mt, mr, mv)
        if max(beta) <= 0.0:
            return 0.0
        mu = aggregate(beta, self._mu_terms)
        return centroid_weighted(self._w, self._wu, mu, self._u_lo, self._u_hi)

    def infer(self, T: float, R: float, V: float) -> FuzzyResult:
        """Повне виведення з трасуванням (МФ, спрацьовані правила, μ_agg на сітці)."""
        mt, mr, mv = self._fuzzify_all(T, R, V)
        beta = [0.0] * len(self._u_names)
        fired: list[FiredRule] = []
        for rule, (ti, ri, vi, ci, w) in zip(self._rulebase.rules, self._compiled, strict=True):
            a = min(mt[ti], mr[ri], mv[vi]) * w
            beta[ci] = max(beta[ci], a)
            if a > 0.0:
                fired.append(FiredRule(rule_id=rule.id, alpha=a, consequent=rule.consequent,
                                       antecedent=dict(rule.antecedent)))
        fired.sort(key=lambda f: (-f.alpha, f.rule_id))
        memberships = {
            var.name: dict(zip(var.term_names, mu[:-1], strict=True))
            for var, mu in zip(self._vars, (mt, mr, mv), strict=True)
        }
        mu_agg = aggregate(beta, self._mu_terms)
        if max(beta) <= 0.0:
            u_raw = 0.0
        else:
            u_raw = centroid_weighted(self._w, self._wu, mu_agg, self._u_lo, self._u_hi)
        area = float(self._w @ mu_agg)
        return FuzzyResult(
            u_raw=u_raw,
            inputs={"T": float(T), "R": float(R), "V": float(V)},
            memberships=memberships,
            fired=tuple(fired),
            grid=self.grid,
            mu_agg=mu_agg,
            engine=self.name,
            extras={"area": area, "height": float(mu_agg.max())},
        )

    def infer_batch(self, T: npt.ArrayLike, R: npt.ArrayLike, V: npt.ArrayLike) -> FloatArray:
        """Векторизоване u_raw для масивів входів (broadcast); збігається з infer_u до ~1e−15."""
        t, r, v = np.broadcast_arrays(np.asarray(T, dtype=np.float64), np.asarray(R, dtype=np.float64),
                                      np.asarray(V, dtype=np.float64))
        shape = t.shape
        flat = [a.reshape(-1) for a in (t, r, v)]
        for name, a in zip(INPUT_VARIABLES, flat, strict=True):
            if not np.all(np.isfinite(a)):
                raise ValueError(f"input {name} contains non-finite values")
        n = flat[0].size
        out = np.empty(n, dtype=np.float64)
        n_u = len(self._u_names)
        for s in range(0, n, _BATCH_CHUNK):
            e = min(n, s + _BATCH_CHUNK)
            mus = []
            for var, a in zip(self._vars, flat, strict=True):
                lo, hi = var.range
                m = var.evaluate(np.clip(a[s:e], lo, hi))
                mus.append(np.vstack([m, np.ones((1, e - s))]))
            idx = self._rule_idx
            alpha = np.minimum(np.minimum(mus[0][idx[:, 0]], mus[1][idx[:, 1]]), mus[2][idx[:, 2]])
            alpha *= self._rule_w[:, None]
            beta = np.zeros((n_u, e - s), dtype=np.float64)
            for k in range(n_u):
                sel = idx[:, 3] == k
                if np.any(sel):
                    beta[k] = alpha[sel].max(axis=0)
            mu = np.minimum(beta.T[:, :, None], self._mu_terms[None, :, :]).max(axis=1)
            den = mu @ self._w
            num = mu @ self._wu
            safe = den > 0.0
            u = np.zeros(e - s, dtype=np.float64)
            u[safe] = num[safe] / den[safe]
            out[s:e] = np.clip(u, self._u_lo, self._u_hi)
        return out.reshape(shape)


def default_engine(scheme: str | None = None, nodes: int | None = None) -> MamdaniEngine:
    """Рушій з робочою конфігурацією `config/membership.yaml` + `config/rules_mamdani.yaml`."""
    membership = load_membership()
    return MamdaniEngine(membership, load_rulebase(None, membership, production=True), scheme, nodes)
