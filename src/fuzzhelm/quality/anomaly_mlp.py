"""Нейромережевий детектор аномалій котирувань: MLP-автокодувальник N-3-N (робочий — 5-3-5).

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

Робочий контур (PLAT-05 → WIRE-01): навчена мережа зберігається JSON-артефактом
`data/anomaly_mlp_<SYMBOL>.json` (архітектура, mean/scale скейлера, coefs_/intercepts_, поріг q₉₉, вікно
навчання і його dataset_hash, seed, версії бібліотек) — НЕ pickle: артефакт читається без виконання коду,
diff-ується в git, а мережа відтворюється `FrozenAutoencoder` чистим numpy тими самими операціями, що й
`MLPRegressor.predict` (скейлер: x −= μ; x /= σ; шар: a @ W; a += b; tanh на місці), тож скори побітово
збігаються зі скорами моделі в пам'яті (тест test_wiring_anomaly). Робоча архітектура — 5-3-5 (п'ять ознак
§5.17): на справжньому IS-вікні вона краща за 8-3-8 у 6/6 seed (docs/figures/quality_mlp_rocauc.md).
"""

from __future__ import annotations

import hashlib
import logging
import math
import warnings
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from decimal import ROUND_HALF_EVEN, Decimal
from pathlib import Path
from typing import Any, Final, Literal, Protocol

import numpy as np
import orjson
from sklearn.exceptions import ConvergenceWarning
from sklearn.neural_network import MLPRegressor
from sklearn.preprocessing import StandardScaler

from fuzzhelm.config import ROOT
from fuzzhelm.features.convert import Bar
from fuzzhelm.features.indicators import SMA, PercentileRank, RollingWelford, WilderATR

log = logging.getLogger("fuzzhelm.quality.anomaly")

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

    @property
    def n_features(self) -> int:
        """Кількість ознак на вході (перші n з FEATURE_NAMES): 8 для 8-3-8, 5 для 5-3-5."""
        if self._scaler is None:
            raise RuntimeError("AnomalyAutoencoder is not fitted")
        return int(self._scaler.n_features_in_)

    def export_params(self) -> dict[str, Any]:
        """Усе, що потрібно для відтворення скорів без sklearn: скейлер, ваги шарів, активації, поріг."""
        if self._scaler is None or self._mlp is None:
            raise RuntimeError("AnomalyAutoencoder is not fitted")
        mlp = self._mlp
        return {
            "features": list(FEATURE_NAMES[: self.n_features]),
            "scaler": {"mean": [float(v) for v in self._scaler.mean_],
                       "scale": [float(v) for v in self._scaler.scale_]},
            "layers": [{"coef": np.asarray(w, dtype=np.float64).tolist(),
                        "intercept": np.asarray(b, dtype=np.float64).tolist()}
                       for w, b in zip(mlp.coefs_, mlp.intercepts_, strict=True)],
            "hidden_activation": str(mlp.activation),
            "out_activation": str(mlp.out_activation_),
            "score": "sum((z - zhat)^2) over standardized features",
            "threshold": float(self.threshold),
            "quantile": float(self.quantile),
        }

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


class AnomalyModel(Protocol):
    """Що потрібно скореру: поріг, кількість ознак на вході і скор рядків (навчена або відтворена мережа)."""

    @property
    def threshold(self) -> float: ...
    @property
    def n_features(self) -> int: ...
    def score(self, X: np.ndarray) -> np.ndarray: ...


class AnomalyScorer:
    """Потоковий скоринг закритих барів навченою моделлю (для конвеєра інжесту).

    Екстрактор рахує всі 8 ознак; на вхід моделі йдуть перші `model.n_features` (5 для 5-3-5).
    `warm_up(bars)` проганяє історію, що передує потоку, лише через екстрактор (без скорів і лічильників),
    щоб перша ж жива свічка мала вектор ознак (інакше перші warmup − 1 барів лишаються без скору).
    Екстрактор — рекурсивний стан (ATR Уайлдера, σ20, ранг, середні), тож бари подаються строго за
    зростанням open_time: бар, не новіший за вже поданий (свічка потоку, що перекривається з прогрівом, —
    торговий воркер такі пропускає; повтор), не подається і не скориться (`stale_skipped`), інакше один бар
    врахувався б двічі.
    """

    def __init__(self, model: AnomalyModel | AnomalyAutoencoder, params: ExtractorParams | None = None, *,
                 label: str | None = None) -> None:
        if isinstance(model, AnomalyAutoencoder) and not model.fitted:
            raise ValueError("model must be fitted")
        n = int(model.n_features)
        if not 1 <= n <= N_FEATURES:
            raise ValueError(f"model expects {n} features, extractor provides {N_FEATURES}")
        self.model = model
        self.n_features = n
        self.label = label
        self._ex = AnomalyFeatureExtractor(params)
        self._last_t: int | None = None          # open_time останнього поданого бару (прогрів або потік)
        self.scored = 0
        self.flagged = 0
        self.warmup_bars_fed = 0
        self.stale_skipped = 0

    @property
    def threshold(self) -> float:
        return float(self.model.threshold)

    @property
    def warmup(self) -> int:
        """Номер бару (з 1), на якому з'являється перший скор (219 за типових параметрів)."""
        return self._ex.warmup

    @property
    def bars_seen(self) -> int:
        return self._ex.n_seen

    def _fresh(self, bar: Bar) -> bool:
        """Бар новіший за всі подані → запам'ятати його час; інакше — пропуск (stale_skipped)."""
        if self._last_t is not None and bar.t_ns <= self._last_t:
            self.stale_skipped += 1
            return False
        self._last_t = bar.t_ns
        return True

    def warm_up(self, bars: Iterable[Bar]) -> int:
        """Історія до потоку → стан екстрактора; скорів не видає. Повертає кількість поданих барів."""
        n = 0
        for b in bars:
            if self._fresh(b):
                self._ex.update(b)
                n += 1
        self.warmup_bars_fed += n
        return n

    def update(self, bar: Bar) -> AnomalyVerdict | None:
        if not self._fresh(bar):
            return None
        x = self._ex.update(bar)
        if x is None:
            return None
        xs = x[: self.n_features]
        s = float(self.model.score(xs)[0])
        thr = self.threshold
        flagged = s > thr
        self.scored += 1
        self.flagged += int(flagged)
        return AnomalyVerdict(bar.t_ns, s, thr, flagged, tuple(float(v) for v in xs))

    def describe(self) -> dict[str, Any]:
        """Короткий опис для health / журналу сесії."""
        return {"model": self.label, "n_features": self.n_features, "threshold": self.threshold,
                "scored": self.scored, "flagged": self.flagged, "warmup_bars_fed": self.warmup_bars_fed,
                "stale_skipped": self.stale_skipped}


def anomaly_health(scorer: AnomalyScorer | None, flagged: int) -> dict[str, Any]:
    """Поля health-знімка воркера про QualityGate: модель, поріг, скільки закритих свічок оцінено і скільки
    позначено аномаліями (`flagged` — PipelineHealth.anomalies). Видно в GET /market/health → pipelines."""
    if scorer is None:
        return {"anomaly_model": None, "anomaly_scored": 0, "anomalies": flagged}
    return {"anomaly_model": scorer.label, "anomaly_threshold": scorer.threshold,
            "anomaly_scored": scorer.scored, "anomalies": flagged}


# ---------------------------------------------------------------- JSON-артефакт моделі (без pickle)

ARTIFACT_KIND: Final = "fuzzhelm.anomaly_mlp"
ARTIFACT_VERSION: Final = 1
DEFAULT_MODEL_DIR: Final = ROOT / "data"
# candle.anomaly_score — NUMERIC(10,6): більші скори (обвал ціни на десятки σ) обрізаються до максимуму
# колонки, інакше INSERT упав би з numeric field overflow і забрав би з собою запис свічки
ANOMALY_SCORE_DB_MAX: Final = Decimal("9999.999999")
_SCORE_STEP: Final = Decimal("0.000001")
_HIDDEN_ACTIVATIONS: Final = frozenset({"identity", "tanh", "relu", "logistic"})


class AnomalyModelError(ValueError):
    """Артефакт моделі пошкоджений або несумісний (не плутати з відсутнім файлом — тоді скорер вимкнено)."""


def _apply_activation(name: str, a: np.ndarray) -> None:
    """Активація на місці — ті самі функції, що sklearn.neural_network._base.ACTIVATIONS."""
    if name == "tanh":
        np.tanh(a, out=a)
    elif name == "relu":
        np.maximum(a, 0, out=a)
    elif name == "logistic":
        from scipy.special import expit  # noqa: PLC0415 — sklearn: logistic_sigmoid = expit

        expit(a, out=a)
    elif name != "identity":
        raise AnomalyModelError(f"unsupported activation {name!r}")


class FrozenAutoencoder:
    """Мережа з артефакту: прямий прохід numpy, що повторює StandardScaler.transform + MLPRegressor.predict
    операція в операцію (тому скори побітово ті самі, що в навченої моделі)."""

    def __init__(self, params: Mapping[str, Any]) -> None:
        try:
            features = [str(f) for f in params["features"]]
            mean = np.asarray(params["scaler"]["mean"], dtype=np.float64)
            scale = np.asarray(params["scaler"]["scale"], dtype=np.float64)
            coefs = [np.asarray(layer["coef"], dtype=np.float64) for layer in params["layers"]]
            intercepts = [np.asarray(layer["intercept"], dtype=np.float64) for layer in params["layers"]]
            hidden = str(params["hidden_activation"])
            out = str(params["out_activation"])
            threshold = float(params["threshold"])
        except (KeyError, TypeError, ValueError) as e:
            raise AnomalyModelError(f"malformed model params: {e!r}") from e
        n = len(features)
        if not 1 <= n <= N_FEATURES or tuple(features) != FEATURE_NAMES[:n]:
            raise AnomalyModelError(f"features must be a prefix of {FEATURE_NAMES}, got {features}")
        if mean.shape != (n,) or scale.shape != (n,) or not np.all(np.isfinite(mean)) \
                or not np.all(scale > 0):
            raise AnomalyModelError("scaler mean/scale must be finite feature-length vectors, scale > 0")
        if len(coefs) < 1:
            raise AnomalyModelError("no layers")
        width = n
        for i, (w, b) in enumerate(zip(coefs, intercepts, strict=True)):
            if w.ndim != 2 or w.shape[0] != width or b.shape != (w.shape[1],) \
                    or not (np.all(np.isfinite(w)) and np.all(np.isfinite(b))):
                raise AnomalyModelError(f"layer {i}: bad shape {w.shape}/{b.shape} or non-finite weights")
            width = w.shape[1]
        if width != n:
            raise AnomalyModelError(f"autoencoder output width {width} != input width {n}")
        if hidden not in _HIDDEN_ACTIVATIONS or out != "identity":
            raise AnomalyModelError(
                f"activations {hidden!r}/{out!r}: need one of {sorted(_HIDDEN_ACTIVATIONS)}/identity")
        if not (math.isfinite(threshold) and threshold > 0):
            raise AnomalyModelError(f"threshold must be finite and > 0, got {threshold}")
        self.features = tuple(features)
        self.mean = mean
        self.scale = scale
        self.coefs = coefs
        self.intercepts = intercepts
        self.hidden_activation = hidden
        self._threshold = threshold

    @property
    def threshold(self) -> float:
        return self._threshold

    @property
    def n_features(self) -> int:
        return len(self.features)

    def reconstruct(self, Z: np.ndarray) -> np.ndarray:
        a = Z
        last = len(self.coefs) - 1
        for i, (w, b) in enumerate(zip(self.coefs, self.intercepts, strict=True)):
            a = a @ w                           # sklearn.utils.extmath.safe_sparse_dot: щільні 2-D → a @ b
            a += b
            if i != last:
                _apply_activation(self.hidden_activation, a)
        return a                                # вихідна активація регресора — identity

    def score(self, X: np.ndarray) -> np.ndarray:
        Z = np.array(np.atleast_2d(np.asarray(X, dtype=np.float64)), dtype=np.float64, copy=True)
        if Z.shape[1] != self.n_features:
            raise ValueError(f"expected {self.n_features} features, got {Z.shape[1]}")
        Z -= self.mean                          # StandardScaler.transform: X −= mean_; X /= scale_
        Z /= self.scale
        R = self.reconstruct(Z).reshape(Z.shape)
        return np.asarray(np.sum((Z - R) ** 2, axis=1), dtype=np.float64)


def params_digest(params: Mapping[str, Any]) -> str:
    """SHA-256 канонічного JSON параметрів мережі (orjson, OPT_SORT_KEYS; float — найкоротший repr)."""
    return hashlib.sha256(orjson.dumps(params, option=orjson.OPT_SORT_KEYS)).hexdigest()


def model_artifact(model: AnomalyAutoencoder, *, symbol: str, extractor: ExtractorParams | None = None,
                   **sections: Any) -> dict[str, Any]:
    """Документ артефакту: модель + її дайджест; `sections` (training, evaluation, provenance, …) — як є."""
    params = model.export_params()
    ex = extractor or ExtractorParams()
    params["extractor"] = asdict(ex)
    params["warmup_bars"] = AnomalyFeatureExtractor(ex).warmup
    n = model.n_features
    return {"kind": ARTIFACT_KIND, "v": ARTIFACT_VERSION, "symbol": symbol,
            "architecture": f"{n}-{model.hidden}-{n}", "model": params,
            "params_sha256": params_digest(params), **sections}


def write_model_artifact(path: Path, doc: Mapping[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".part")
    tmp.write_bytes(orjson.dumps(doc, option=orjson.OPT_INDENT_2 | orjson.OPT_SORT_KEYS) + b"\n")
    tmp.replace(path)
    return path


@dataclass(frozen=True, slots=True)
class LoadedModel:
    model: FrozenAutoencoder
    extractor: ExtractorParams
    doc: Mapping[str, Any]
    path: Path

    @property
    def label(self) -> str:
        return f"{self.doc.get('symbol')} {self.doc.get('architecture')} sha256:{self.digest[:12]}"

    @property
    def digest(self) -> str:
        return str(self.doc["params_sha256"])

    def scorer(self) -> AnomalyScorer:
        return AnomalyScorer(self.model, self.extractor, label=self.label)


def load_model_artifact(path: Path) -> LoadedModel:
    """Прочитати й перевірити артефакт (тип, версія, дайджест параметрів, форми шарів) → LoadedModel."""
    try:
        doc = orjson.loads(Path(path).read_bytes())
    except orjson.JSONDecodeError as e:
        raise AnomalyModelError(f"{path}: not a JSON document ({e})") from e
    if not isinstance(doc, dict) or doc.get("kind") != ARTIFACT_KIND or doc.get("v") != ARTIFACT_VERSION:
        raise AnomalyModelError(f"{path}: not a {ARTIFACT_KIND} v{ARTIFACT_VERSION} artifact")
    params = doc.get("model")
    if not isinstance(params, dict):
        raise AnomalyModelError(f"{path}: no model section")
    if params_digest(params) != doc.get("params_sha256"):
        raise AnomalyModelError(f"{path}: params_sha256 mismatch (artifact edited or corrupted)")
    model = FrozenAutoencoder(params)
    try:
        extractor = ExtractorParams(**params.get("extractor", {}))
    except TypeError as e:
        raise AnomalyModelError(f"{path}: bad extractor params: {e}") from e
    return LoadedModel(model, extractor, doc, Path(path))


def default_model_path(symbol_venue: str, model_dir: Path | None = None) -> Path:
    return (model_dir or DEFAULT_MODEL_DIR) / f"anomaly_mlp_{symbol_venue.upper()}.json"


def load_anomaly_scorer(symbol_venue: str, *, model_dir: Path | None = None,
                        path: Path | None = None) -> AnomalyScorer | None:
    """Скорер інструмента з `data/anomaly_mlp_<SYMBOL>.json`. Файлу немає → попередження в журналі і None
    (контур працює без MLP: N_invalid — лише порушення інваріантів). Пошкоджений файл → AnomalyModelError:
    мовчки вимкнути перевірку якості через зіпсований артефакт гірше, ніж не стартувати."""
    p = path or default_model_path(symbol_venue, model_dir)
    if not p.is_file():
        log.warning("anomaly model %s not found: MLP anomaly scoring for %s is disabled "
                    "(train it with scripts/train_anomaly_mlp.py)", p, symbol_venue)
        return None
    loaded = load_model_artifact(p)
    sym = str(loaded.doc.get("symbol", "")).upper()
    if sym and sym != symbol_venue.upper():
        raise AnomalyModelError(f"{p}: artifact is for {sym}, not {symbol_venue}")
    sc = loaded.scorer()
    log.info("anomaly model %s loaded for %s: threshold %.6g, first score after %d bars", loaded.label,
             symbol_venue, sc.threshold, sc.warmup - 1)
    return sc


def db_anomaly_score(score: float) -> Decimal | None:
    """Скор → значення колонки candle.anomaly_score NUMERIC(10,6): округлення до 1e−6 (HALF_EVEN), обрізка
    зверху до максимуму колонки; неcкінченний скор не пишеться (None). Прапорець аномалії живий конвеєр
    ставить за НЕокругленим скором; збережене значення — для звітів і нічного scheduler.hourly_dq."""
    s = float(score)
    if not math.isfinite(s):
        return None
    if s >= float(ANOMALY_SCORE_DB_MAX):
        return ANOMALY_SCORE_DB_MAX
    return Decimal(repr(max(0.0, s))).quantize(_SCORE_STEP, rounding=ROUND_HALF_EVEN)


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
