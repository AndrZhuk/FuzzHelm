"""Нейромережевий детектор аномалій котирувань: MLP-автокодувальник 8-3-8.

Найменування: quality/anomaly_mlp.py
Призначення: бар, який мережа, навчена лише на нормальних барах, не вміє відтворити через вузьке
горло з 3 нейронів, — аномалія; такі бари додаються до N_invalid скору якості Q (§5.17).
Автор: Андрій Жук, 2026.

Вектор ознак бару t (усі безрозмірні, рахуються інкрементально O(1) з OHLCV; нормувальні статистики
беруться за ПОПЕРЕДНІМИ барами, щоб аномалія не «розмивала» власний масштаб):
  1  dlogp     = ln(c_t / c_{t−1})                              ┐
  2  log_rg_atr= ln(Rg_t / ATR14_{t−1}),  Rg = h − l             │ п'ять ознак брифінгу §5.17
  3  log_v_vbar= ln(v_t / v̄60_{t−1})                            │ x = (Δlog p, log(Rg/ATR),
  4  bw_pct    = перцентильний ранг ширини Боллінджера 2σ20/μ20  │      log(v/v̄), bw_pct, |z|)
               серед останніх 200 барів                          │
  5  abs_z     = |c_t − μ20_{t−1}| / σ20_{t−1}                   ┘
  6  log_n_nbar= ln((n_t + 1) / (n̄60_{t−1} + 1))  — кількість угод відносно середньої
  7  body      = (c_t − o_t) / Rg_t ∈ [−1, 1]      — геометрія тіла свічки
  8  open_gap  = (o_t − c_{t−1}) / ATR14_{t−1}      — розрив між закриттям і наступним відкриттям
Ознаки 6–8 розширюють п'ятірку брифінгу до архітектури 8-3-8, названої в §3 (див.
docs/deviations.d/ingest_ws.md): кожна несе незалежний від 1–5 сигнал (активність, форма, стик барів).

Модель: StandardScaler → MLPRegressor(hidden_layer_sizes=(3,), activation="tanh", max_iter=500,
random_state=seed), ціль = вхід. Скор бару — ‖x − x̂‖² у стандартизованому просторі;
аномалія при скорі > q₉₉ скорів навчальної вибірки.
"""

from __future__ import annotations

import math
import warnings
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Final, Literal

import numpy as np
from sklearn.exceptions import ConvergenceWarning
from sklearn.neural_network import MLPRegressor
from sklearn.preprocessing import StandardScaler

from fuzzhelm.features.convert import Bar
from fuzzhelm.features.indicators import SMA, PercentileRank, RollingWelford, WilderATR

FEATURE_NAMES: Final = ("dlogp", "log_rg_atr", "log_v_vbar", "bw_pct", "abs_z", "log_n_nbar", "body",
                        "open_gap")
N_FEATURES: Final = len(FEATURE_NAMES)
REL_EPS: Final = 1e-9          # «нуль» відносно ціни: розмах/σ, менші за 1e−9·c, — шум округлення


@dataclass(frozen=True, slots=True)
class ExtractorParams:
    n_atr: int = 14
    n_bb: int = 20
    bw_rank_window: int = 200
    volume_window: int = 60


class AnomalyFeatureExtractor:
    """Потоковий обчислювач 8 ознак; `update(bar)` → вектор або None під час прогріву."""

    def __init__(self, params: ExtractorParams | None = None) -> None:
        p = params or ExtractorParams()
        self.params = p
        self._atr = WilderATR(p.n_atr)
        self._bb = RollingWelford(p.n_bb)
        self._rank = PercentileRank(p.bw_rank_window)
        self._vsma = SMA(p.volume_window)
        self._nsma = SMA(p.volume_window)
        self._prev_c: float | None = None
        self.n_seen = 0

    @property
    def warmup(self) -> int:
        """Номер бару (з 1), на якому вперше з'являється вектор ознак."""
        p = self.params
        # ранг ширини смуги — після bw_rank_window значень σ20; ATR_{t−1} — з (n_atr+2)-го бару
        # (перше ATR — на (n_atr+1)-му); v̄_{t−1}, n̄_{t−1} — з (volume_window+1)-го; μ20_{t−1} — з 21-го
        return max(p.n_bb + p.bw_rank_window - 1, p.n_atr + 2, p.volume_window + 1, p.n_bb + 1)

    def update(self, bar: Bar) -> np.ndarray | None:
        self.n_seen += 1
        prev_c = self._prev_c
        atr_prev = self._atr.value
        v_prev = self._vsma.value
        n_prev = self._nsma.value
        mu_prev = self._bb.mean if self._bb.ready else None
        sd_prev = self._bb.std if self._bb.ready else None
        # стан індикаторів — з поточним баром (для наступного кроку)
        self._atr.update(bar.h, bar.l, bar.c)
        self._vsma.update(bar.v)
        self._nsma.update(float(bar.n))
        self._bb.update(bar.c)
        bw_pct: float | None = None
        if self._bb.ready and self._bb.mean and self._bb.std is not None:
            bw_pct = self._rank.update(2.0 * self._bb.std / abs(self._bb.mean))
        self._prev_c = bar.c
        if (prev_c is None or atr_prev is None or v_prev is None or n_prev is None or mu_prev is None
                or sd_prev is None or bw_pct is None):
            return None
        floor = REL_EPS * abs(bar.c)
        rg = bar.h - bar.l
        rg_safe = max(rg, floor)
        atr_safe = max(atr_prev, floor)
        return np.array([
            math.log(bar.c / prev_c),
            math.log(rg_safe / atr_safe),
            math.log((bar.v + REL_EPS) / (v_prev + REL_EPS)),
            bw_pct,
            abs(bar.c - mu_prev) / max(sd_prev, floor),
            math.log((bar.n + 1.0) / (n_prev + 1.0)),
            (bar.c - bar.o) / rg if rg > floor else 0.0,
            (bar.o - prev_c) / atr_safe,
        ], dtype=np.float64)


def feature_matrix(bars: Sequence[Bar], params: ExtractorParams | None = None
                   ) -> tuple[np.ndarray, np.ndarray]:
    """(X, idx): рядок X[k] — ознаки бару bars[idx[k]]; бари прогріву пропускаються."""
    ex = AnomalyFeatureExtractor(params)
    rows: list[np.ndarray] = []
    idx: list[int] = []
    for i, b in enumerate(bars):
        x = ex.update(b)
        if x is not None:
            rows.append(x)
            idx.append(i)
    if not rows:
        return np.empty((0, N_FEATURES)), np.empty(0, dtype=np.int64)
    return np.vstack(rows), np.asarray(idx, dtype=np.int64)


class AnomalyAutoencoder:
    """Автокодувальник N-3-N поверх стандартизованих ознак; поріг — q₉₉ навчальних скорів."""

    def __init__(self, seed: int = 0, *, hidden: int = 3, max_iter: int = 500, quantile: float = 0.99,
                 activation: Literal["identity", "logistic", "tanh", "relu"] = "tanh") -> None:
        if not 0.0 < quantile < 1.0:
            raise ValueError("quantile must be in (0, 1)")
        self.seed = seed
        self.hidden = hidden
        self.max_iter = max_iter
        self.quantile = quantile
        self.activation = activation
        self._scaler: StandardScaler | None = None
        self._mlp: MLPRegressor | None = None
        self.threshold: float = math.inf
        self.train_scores: np.ndarray = np.empty(0)
        self.n_iter: int = 0
        self.converged: bool = False

    @property
    def fitted(self) -> bool:
        return self._mlp is not None

    def fit(self, X: np.ndarray) -> AnomalyAutoencoder:
        X = np.asarray(X, dtype=np.float64)
        if X.ndim != 2 or X.shape[0] < 2:
            raise ValueError(f"need a 2-D training matrix with >= 2 rows, got {X.shape}")
        if not np.all(np.isfinite(X)):
            raise ValueError("training matrix contains NaN/inf")
        self._scaler = StandardScaler().fit(X)
        Z = self._scaler.transform(X)
        mlp = MLPRegressor(hidden_layer_sizes=(self.hidden,), activation=self.activation,
                           max_iter=self.max_iter, random_state=self.seed)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", ConvergenceWarning)
            mlp.fit(Z, Z)
        self.converged = not any(issubclass(w.category, ConvergenceWarning) for w in caught)
        self.n_iter = int(mlp.n_iter_)
        self._mlp = mlp
        self.train_scores = self._errors(Z)
        self.threshold = float(np.quantile(self.train_scores, self.quantile))
        return self

    def _errors(self, Z: np.ndarray) -> np.ndarray:
        assert self._mlp is not None
        R = self._mlp.predict(Z)
        R = R.reshape(Z.shape)
        return np.asarray(np.sum((Z - R) ** 2, axis=1), dtype=np.float64)

    def score(self, X: np.ndarray) -> np.ndarray:
        """‖x − x̂‖² у стандартизованому просторі для кожного рядка X."""
        if self._scaler is None or self._mlp is None:
            raise RuntimeError("AnomalyAutoencoder is not fitted")
        X2 = np.atleast_2d(np.asarray(X, dtype=np.float64))
        return self._errors(self._scaler.transform(X2))

    def is_anomaly(self, X: np.ndarray) -> np.ndarray:
        return self.score(X) > self.threshold


@dataclass(frozen=True, slots=True)
class AnomalyVerdict:
    t_ns: int
    score: float
    threshold: float
    anomaly: bool
    features: tuple[float, ...]


class AnomalyScorer:
    """Потоковий скоринг закритих барів навченою моделлю (для конвеєра інжесту)."""

    def __init__(self, model: AnomalyAutoencoder, params: ExtractorParams | None = None) -> None:
        if not model.fitted:
            raise ValueError("model must be fitted")
        self.model = model
        self._ex = AnomalyFeatureExtractor(params)

    def update(self, bar: Bar) -> AnomalyVerdict | None:
        x = self._ex.update(bar)
        if x is None:
            return None
        s = float(self.model.score(x)[0])
        return AnomalyVerdict(bar.t_ns, s, self.model.threshold, s > self.model.threshold,
                              tuple(float(v) for v in x))


# ---------------------------------------------------------------- ін'єкція розмічених аномалій

AnomalyKind = Literal["price_spike", "wick", "volume_burst", "frozen"]
ANOMALY_KINDS: Final[tuple[AnomalyKind, ...]] = ("price_spike", "wick", "volume_burst", "frozen")


def inject_anomaly(bars: Sequence[Bar], i: int, kind: AnomalyKind, magnitude: float) -> Bar:
    """Спотворена копія bars[i] (OHLC лишається узгодженим: h ≥ max(o,c), l ≤ min(o,c)).

    magnitude: для price_spike/wick — відносний зсув ціни (0.01 = 1 %); для volume_burst — множник
    обсягу й кількості угод; для frozen — ігнорується (бар «застиг»: o=h=l=c=c_{t−1}, v=0, n=0).
    """
    if i < 1:
        raise ValueError("need a previous bar")
    b = bars[i]
    prev_c = bars[i - 1].c
    if kind == "price_spike":
        c = prev_c * (1.0 + magnitude)
        return replace(b, c=c, h=max(b.h, b.o, c), l=min(b.l, b.o, c))
    if kind == "wick":
        h = max(b.o, b.c) * (1.0 + abs(magnitude))
        return replace(b, h=max(b.h, h))
    if kind == "volume_burst":
        return replace(b, v=b.v * magnitude, qv=b.qv * magnitude, n=round(b.n * magnitude))
    if kind == "frozen":
        return replace(b, o=prev_c, h=prev_c, l=prev_c, c=prev_c, v=0.0, qv=0.0, n=0)
    raise ValueError(f"unknown anomaly kind {kind!r}")
