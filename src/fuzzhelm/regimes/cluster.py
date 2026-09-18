"""Кластеризація режимів ринку: KMeans для k ∈ k_range і вибір k за силуетним коефіцієнтом.

Найменування: regimes/cluster.py
Призначення: обчислювально-інтелектуальна частина калібрування МФ (брифінг §3 питання 1, §5.5, ПР12):
             центроїди KMeans(k=3) стають центрами гаусових термів змінної V.
Автор: Андрій Жук, 2026.

Ознаки мають різні масштаби (перцентиль ∈ [0,1], z-score обсягу ~ одиниці), тому за замовчуванням
кластеризуємо стандартизовані ознаки; центроїди повертаються в ОРИГІНАЛЬНИХ одиницях (стандартизація
афінна, тож центроїд = середнє точок кластера в будь-якій із двох систем координат).
Силует — O(n²) за пам'яттю/часом, тому для великих вибірок рахується на детермінованій підвибірці
(`silhouette_sample`, random_state=seed).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import numpy.typing as npt
from sklearn.cluster import KMeans
from sklearn.metrics import silhouette_score

FloatArray = npt.NDArray[np.float64]
IntArray = npt.NDArray[np.int_]


@dataclass(frozen=True)
class ClusterResult:
    k: int                                          # k з найбільшим силуетом
    centroids: FloatArray                           # (k, d) — для обраного k, оригінальні одиниці
    silhouette_by_k: dict[int, float]
    labels: IntArray                                # мітки для обраного k
    centroids_by_k: dict[int, FloatArray] = field(default_factory=dict)
    labels_by_k: dict[int, IntArray] = field(default_factory=dict)
    inertia_by_k: dict[int, float] = field(default_factory=dict)
    n_samples: int = 0
    seed: int = 0


def fit_regimes(X: npt.ArrayLike, k_range: range | tuple[int, ...] = range(2, 7), seed: int = 0, *,
                n_init: int = 10, standardize: bool = True,
                silhouette_sample: int | None = 10_000) -> ClusterResult:
    """KMeans(n_clusters=k, n_init=n_init, random_state=seed) для кожного k; вибір k = argmax силуету."""
    x = np.asarray(X, dtype=np.float64)
    if x.ndim == 1:
        x = x[:, None]
    if x.ndim != 2 or x.shape[0] == 0:
        raise ValueError("X must be a non-empty (n, d) array")
    if not np.all(np.isfinite(x)):
        raise ValueError("X contains non-finite values; drop warm-up rows first")
    ks = sorted(set(int(k) for k in k_range))
    if not ks or ks[0] < 2 or ks[-1] >= x.shape[0]:
        raise ValueError(f"k_range {ks} must satisfy 2 <= k < n_samples={x.shape[0]}")

    if standardize:
        mu = x.mean(axis=0)
        sd = x.std(axis=0)
        sd = np.where(sd > 0.0, sd, 1.0)
        xs = (x - mu) / sd
    else:
        mu = np.zeros(x.shape[1])
        sd = np.ones(x.shape[1])
        xs = x

    sample = silhouette_sample if silhouette_sample is not None and x.shape[0] > silhouette_sample else None
    sil: dict[int, float] = {}
    cents: dict[int, FloatArray] = {}
    labs: dict[int, IntArray] = {}
    inert: dict[int, float] = {}
    for k in ks:
        km = KMeans(n_clusters=k, n_init=n_init, random_state=seed).fit(xs)
        lab = km.labels_.astype(np.int_)
        n_distinct = len(np.unique(lab))
        if n_distinct < 2:
            sil[k] = float("nan")
        else:
            sil[k] = float(silhouette_score(xs, lab, sample_size=sample, random_state=seed))
        cents[k] = km.cluster_centers_ * sd + mu
        labs[k] = lab
        inert[k] = float(km.inertia_)
    valid = {k: s for k, s in sil.items() if not math.isnan(s)}
    if not valid:
        raise ValueError("silhouette undefined for every k (degenerate data)")
    # найбільший силует; при рівності — менше k (простіша модель)
    k_best = max(valid, key=lambda k: (valid[k], -k))
    return ClusterResult(
        k=k_best, centroids=cents[k_best], silhouette_by_k=sil, labels=labs[k_best],
        centroids_by_k=cents, labels_by_k=labs, inertia_by_k=inert, n_samples=int(x.shape[0]), seed=seed,
    )
