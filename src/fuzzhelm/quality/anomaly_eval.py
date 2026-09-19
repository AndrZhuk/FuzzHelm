"""Оцінювання MLP-автокодувальника аномалій на розмічених ін'єкціях: ROC-AUC, recall і FPR при порозі q₉₉.

Найменування: quality/anomaly_eval.py
Призначення: брифінг §5.17 — «ROC-AUC на розмічених ін'єкованих аномаліях — таблиця у звіті». Модель
навчається на нормальних барах навчального відрізка (IS), а на відкладеному відрізку (held-out) кожен
чистий бар — негативний приклад, і на випадкових позиціях по одній вносяться аномалії чотирьох типів
(позитивні приклади). Та сама процедура для двох архітектур: 8-3-8 (усі 8 ознак) і 5-3-5 (п'ять ознак
брифінгу). Скрипт `scripts/train_anomaly_mlp.py` лише завантажує бари (з БД або фікстури) і пише звіт.
Автор: Андрій Жук, 2026.

Оцінювання «по одній»: стан екстрактора ознак до бару t береться з чистого ряду (знімок), змінюється лише
бар t, тож аномалії не впливають одна на одну і на сусідні бари — чиста задача розпізнавання бару.
Позиції, амплітуди й ініціалізація мережі залежать лише від seed (numpy Generator), тож прогін відтворний.
Нічого не підбирається на held-out: архітектура 8-3-8 задана наперед (§3), 5-3-5 — лише для порівняння.
"""

from __future__ import annotations

import copy
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final

import numpy as np
from sklearn.metrics import roc_auc_score

from fuzzhelm.features.convert import Bar
from fuzzhelm.quality.anomaly_mlp import (
    ANOMALY_KINDS,
    FEATURE_NAMES,
    N_FEATURES,
    AnomalyAutoencoder,
    AnomalyFeatureExtractor,
    AnomalyKind,
    inject_anomaly,
)

# діапазони амплітуд: price_spike/wick — відносний зсув ціни, volume_burst — множник обсягу й угод
DEFAULT_MAGNITUDES: Final[Mapping[AnomalyKind, tuple[float, float]]] = {
    "price_spike": (0.003, 0.02),     # |Δ| 0.3–2 % за хвилину, знак випадковий
    "wick": (0.003, 0.02),            # «товстий палець»: тінь 0.3–2 % над тілом
    "volume_burst": (5.0, 30.0),      # обсяг і кількість угод ×5…×30
    "frozen": (0.0, 0.0),             # застиглий бар: o=h=l=c=c_{t−1}, v=0, n=0
}
# архітектура → кількість перших ознак FEATURE_NAMES на вході (прихований шар — 3 нейрони в обох)
ARCHITECTURES: Final[Mapping[str, int]] = {"8-3-8": N_FEATURES, "5-3-5": 5}


def structurally_valid(b: Bar) -> bool:
    """Структурні інваріанти бару у float: ціни > 0, h ≥ max(o,c), l ≤ min(o,c), v ≥ 0, n ≥ 0."""
    if not all(math.isfinite(x) for x in (b.o, b.h, b.l, b.c, b.v)):
        return False
    prices_ok = min(b.o, b.h, b.l, b.c) > 0 and b.h >= max(b.o, b.c) and b.l <= min(b.o, b.c)
    return prices_ok and b.v >= 0 and b.n >= 0


@dataclass(frozen=True, slots=True)
class InjectionPlan:
    """Позиції (індекси барів) і амплітуди ін'єкцій кожного типу."""

    positions: Mapping[AnomalyKind, np.ndarray]
    magnitudes: Mapping[AnomalyKind, np.ndarray]

    def n_positive(self) -> int:
        return int(sum(len(p) for p in self.positions.values()))


def plan_injections(lo: int, hi: int, *, per_kind: int, rng: np.random.Generator,
                    magnitudes: Mapping[AnomalyKind, tuple[float, float]] = DEFAULT_MAGNITUDES
                    ) -> InjectionPlan:
    """По `per_kind` різних позицій з [lo, hi) на тип (типи незалежні: кожна ін'єкція оцінюється окремо)."""
    if lo < 1 or hi <= lo:
        raise ValueError(f"bad injection range [{lo}, {hi})")
    if per_kind > hi - lo:
        raise ValueError(f"per_kind={per_kind} exceeds the {hi - lo} available positions")
    positions: dict[AnomalyKind, np.ndarray] = {}
    mags: dict[AnomalyKind, np.ndarray] = {}
    for kind in ANOMALY_KINDS:
        positions[kind] = np.sort(rng.choice(np.arange(lo, hi), size=per_kind, replace=False))
    for kind in ANOMALY_KINDS:
        lo_m, hi_m = magnitudes[kind]
        m = rng.uniform(lo_m, hi_m, size=per_kind) if hi_m > 0 else np.zeros(per_kind)
        if kind == "price_spike":
            m = np.where(rng.random(per_kind) < 0.5, -m, m)
        mags[kind] = m
    return InjectionPlan(positions, mags)


def clean_pass(bars: Sequence[Bar], snap_at: set[int]
               ) -> tuple[np.ndarray, np.ndarray, dict[int, AnomalyFeatureExtractor]]:
    """Один прохід чистим рядом: ознаки кожного бару + знімки стану екстрактора ДО барів snap_at."""
    ex = AnomalyFeatureExtractor()
    rows: list[np.ndarray] = []
    idx: list[int] = []
    snaps: dict[int, AnomalyFeatureExtractor] = {}
    for i, b in enumerate(bars):
        if i in snap_at:
            snaps[i] = copy.deepcopy(ex)
        x = ex.update(b)
        if x is not None:
            rows.append(x)
            idx.append(i)
    if not rows:
        return np.empty((0, N_FEATURES)), np.empty(0, dtype=np.int64), snaps
    return np.vstack(rows), np.asarray(idx, dtype=np.int64), snaps


def positive_vectors(bars: Sequence[Bar], plan: InjectionPlan,
                     snaps: Mapping[int, AnomalyFeatureExtractor]) -> dict[AnomalyKind, np.ndarray]:
    """Вектори ознак спотворених барів: стан до бару t — зі знімка чистого ряду, змінено лише бар t."""
    out: dict[AnomalyKind, np.ndarray] = {}
    for kind in ANOMALY_KINDS:
        vecs = []
        for pos, mag in zip(plan.positions[kind], plan.magnitudes[kind], strict=True):
            ex = copy.deepcopy(snaps[int(pos)])
            x = ex.update(inject_anomaly(bars, int(pos), kind, float(mag)))
            if x is None:
                raise ValueError(f"position {int(pos)} is inside the extractor warm-up")
            vecs.append(x)
        out[kind] = np.vstack(vecs) if vecs else np.empty((0, N_FEATURES))
    return out


@dataclass(frozen=True, slots=True)
class ArchResult:
    label: str
    n_features: int
    auc_all: float
    per_kind: Mapping[str, Mapping[str, float]]   # kind → {"auc", "recall_at_q99"}
    fpr_at_q99: float
    threshold: float
    n_iter: int
    converged: bool


@dataclass(frozen=True, slots=True)
class EvalReport:
    seed: int
    per_kind: int
    n_train_bars: int
    n_holdout_bars: int
    train_vectors: int
    excluded_train_vectors: int
    neg_vectors: int
    sigma_dlogp_train: float
    archs: Mapping[str, ArchResult] = field(default_factory=dict)
    sigma_dlogp_holdout: float = math.nan
    # дрейф held-out відносно навчання по кожній ознаці: {"sd_ratio": σ_held-out/σ_train,
    #  "tail_share": частка held-out поза [q0.5 %, q99.5 %] навчання (номінал 0.01)}
    feature_drift: Mapping[str, Mapping[str, float]] = field(default_factory=dict)


def evaluate_injections(bars: Sequence[Bar], *, train: tuple[int, int], holdout: tuple[int, int], seed: int,
                        per_kind: int, normal: Sequence[bool] | np.ndarray | None = None,
                        magnitudes: Mapping[AnomalyKind, tuple[float, float]] = DEFAULT_MAGNITUDES,
                        architectures: Mapping[str, int] = ARCHITECTURES) -> EvalReport:
    """Навчити кожну архітектуру на нормальних барах `bars[train]` і оцінити на ін'єкціях у `bars[holdout]`.

    `train`, `holdout` — напіввідкриті діапазони індексів, holdout строго після train (причинність: ознаки
    бару рахуються лише з попередніх барів, тож held-out бачить кінець train як історію, але не навпаки).
    `normal[i] = False` вилучає вектор бару i з навчання (невалідний / синтетичний бар); негативи held-out —
    усі чисті бари відрізка.
    """
    t_lo, t_hi = train
    h_lo, h_hi = holdout
    if not (0 <= t_lo < t_hi <= h_lo < h_hi <= len(bars)):
        raise ValueError(f"need 0 <= train < holdout <= len(bars), got train={train}, holdout={holdout}")
    rng = np.random.default_rng(seed)
    warm = AnomalyFeatureExtractor().warmup            # номер (з 1) першого бару з вектором
    plan = plan_injections(max(h_lo, warm), h_hi, per_kind=per_kind, rng=rng, magnitudes=magnitudes)
    snap_at = {int(p) for ps in plan.positions.values() for p in ps}
    X, idx, snaps = clean_pass(bars[:h_hi], snap_at)
    in_train = (idx >= t_lo) & (idx < t_hi)
    keep = in_train.copy()
    if normal is not None:
        mask = np.asarray(normal, dtype=bool)
        if mask.shape != (len(bars),):
            raise ValueError("normal mask must have one flag per bar")
        keep &= mask[idx]
    train_X = X[keep]
    neg = X[(idx >= h_lo) & (idx < h_hi)]
    pos = positive_vectors(bars, plan, snaps)
    archs: dict[str, ArchResult] = {}
    for label, n_feat in architectures.items():
        model = AnomalyAutoencoder(seed=seed).fit(train_X[:, :n_feat])
        s_neg = model.score(neg[:, :n_feat])
        per: dict[str, dict[str, float]] = {}
        all_pos: list[np.ndarray] = []
        for kind in ANOMALY_KINDS:
            s_pos = model.score(pos[kind][:, :n_feat])
            all_pos.append(s_pos)
            y = np.r_[np.zeros(len(s_neg)), np.ones(len(s_pos))]
            per[kind] = {"auc": float(roc_auc_score(y, np.r_[s_neg, s_pos])),
                         "recall_at_q99": float(np.mean(s_pos > model.threshold))}
        s_all = np.concatenate(all_pos)
        y_all = np.r_[np.zeros(len(s_neg)), np.ones(len(s_all))]
        archs[label] = ArchResult(
            label=label, n_features=n_feat, auc_all=float(roc_auc_score(y_all, np.r_[s_neg, s_all])),
            per_kind=per, fpr_at_q99=float(np.mean(s_neg > model.threshold)), threshold=model.threshold,
            n_iter=model.n_iter, converged=model.converged)
    return EvalReport(seed=seed, per_kind=per_kind, n_train_bars=t_hi - t_lo, n_holdout_bars=h_hi - h_lo,
                      train_vectors=int(keep.sum()), excluded_train_vectors=int(in_train.sum() - keep.sum()),
                      neg_vectors=len(neg), sigma_dlogp_train=float(np.std(train_X[:, 0])), archs=archs,
                      sigma_dlogp_holdout=float(np.std(neg[:, 0])) if len(neg) else math.nan,
                      feature_drift=feature_drift(train_X, neg))


def feature_drift(train_X: np.ndarray, test_X: np.ndarray) -> dict[str, dict[str, float]]:
    """Дрейф розподілу ознак: σ_test/σ_train і частка test поза центральними 99 % навчання (номінал 0.01)."""
    out: dict[str, dict[str, float]] = {}
    for j, name in enumerate(FEATURE_NAMES[: train_X.shape[1]]):
        tr, te = train_X[:, j], test_X[:, j]
        lo, hi = np.quantile(tr, [0.005, 0.995])
        sd = float(np.std(tr))
        out[name] = {"sd_ratio": float(np.std(te)) / sd if sd > 0 else math.nan,
                     "tail_share": float(np.mean((te < lo) | (te > hi))) if len(te) else math.nan}
    return out
