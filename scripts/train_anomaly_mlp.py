"""Навчання MLP-автокодувальника аномалій котирувань і замір ROC-AUC на розмічених ін'єкціях.

Найменування: train_anomaly_mlp.py
Призначення: брифінг §5.17 / фаза 7 — «ROC-AUC на розмічених ін'єкованих аномаліях — таблиця у звіті».
Дані — справжні 3000 закритих 1m-барів BTCUSDT (fixtures/rest/binance_klines.json.gz). IS — перші 60 %
барів (навчання лише на нормальних барах), OOS — решта 40 %: кожен OOS-бар — негативний приклад, а на
випадкових OOS-позиціях по одній вноситься аномалія заданого типу (позитивний приклад).
Автор: Андрій Жук, 2026.

Оцінювання «по одній»: стан екстрактора ознак до бару t береться з чистого ряду, змінюється лише бар t —
так аномалії не впливають одна на одну і на сусідні бари (чиста постановка задачі розпізнавання бару).
Випадковість (позиції, амплітуди, ініціалізація мережі) — лише з фіксованого seed.

Запуск:  uv run python scripts/train_anomaly_mlp.py [--seed 20260918] [--per-kind 150]
Результат: docs/figures/quality_mlp_rocauc.md (усі числа в ньому — з цього прогону).
"""

from __future__ import annotations

import argparse
import copy
import gzip
import time
from pathlib import Path
from typing import Any

import numpy as np
import orjson
from sklearn.metrics import roc_auc_score

from fuzzhelm.features.convert import Bar, bar_from_candle
from fuzzhelm.ingest.normalize import normalize_rest_klines
from fuzzhelm.ingest.symbols import BTC_USDT_PERP
from fuzzhelm.quality.anomaly_mlp import (
    ANOMALY_KINDS,
    FEATURE_NAMES,
    AnomalyAutoencoder,
    AnomalyFeatureExtractor,
    inject_anomaly,
)

ROOT = Path(__file__).resolve().parents[1]
KLINES = ROOT / "fixtures" / "rest" / "binance_klines.json.gz"
OUT = ROOT / "docs" / "figures" / "quality_mlp_rocauc.md"
IS_FRACTION = 0.6
# діапазони амплітуд (σ 1m-дохідності BTC на цих даних — див. рядок «σ dlogp» у звіті)
MAGNITUDE: dict[str, tuple[float, float]] = {
    "price_spike": (0.003, 0.02),     # |Δ| 0.3–2 % за хвилину, знак випадковий
    "wick": (0.003, 0.02),            # «товстий палець»: тінь 0.3–2 % над тілом
    "volume_burst": (5.0, 30.0),      # обсяг і кількість угод ×5…×30
    "frozen": (0.0, 0.0),             # застиглий бар: o=h=l=c=c_{t−1}, v=0
}


def load_bars() -> list[Bar]:
    rows: list[list[Any]] = orjson.loads(gzip.decompress(KLINES.read_bytes()))
    now_ms = rows[-1][6] + 1_000
    return [bar_from_candle(c) for c in normalize_rest_klines(rows, BTC_USDT_PERP, now_ms * 1_000_000,
                                                              server_time_ms=now_ms)]


def clean_pass(bars: list[Bar], snap_at: set[int]
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
    return np.vstack(rows), np.asarray(idx), snaps


def run(seed: int, per_kind: int) -> dict[str, Any]:
    t0 = time.perf_counter()
    bars = load_bars()
    n_is = int(len(bars) * IS_FRACTION)
    rng = np.random.default_rng(seed)
    warm = AnomalyFeatureExtractor().warmup
    lo = max(n_is, warm)
    positions = {k: np.sort(rng.choice(np.arange(lo, len(bars)), size=per_kind, replace=False))
                 for k in ANOMALY_KINDS}
    snap_at = {int(p) for ps in positions.values() for p in ps}
    X, idx, snaps = clean_pass(bars, snap_at)
    train = X[idx < n_is]
    neg = X[idx >= n_is]

    results: dict[str, Any] = {"seed": seed, "bars": len(bars), "is_bars": n_is, "oos_bars": len(bars) - n_is,
                               "train_vectors": len(train), "neg_vectors": len(neg),
                               "sigma_dlogp": float(np.std(train[:, 0]))}
    # позитивні вектори будуються ОДИН раз (ті самі позиції й амплітуди для обох архітектур)
    pos_vecs: dict[str, np.ndarray] = {}
    for kind in ANOMALY_KINDS:
        lo_m, hi_m = MAGNITUDE[kind]
        vecs = []
        for pos in positions[kind]:
            p = int(pos)
            mag = float(rng.uniform(lo_m, hi_m)) if hi_m > 0 else 0.0
            if kind == "price_spike" and rng.random() < 0.5:
                mag = -mag
            ex = copy.deepcopy(snaps[p])
            x = ex.update(inject_anomaly(bars, p, kind, mag))    # type: ignore[arg-type]
            assert x is not None
            vecs.append(x)
        pos_vecs[kind] = np.vstack(vecs)
    for label, cols in (("8-3-8", slice(None)), ("5-3-5", slice(0, 5))):
        model = AnomalyAutoencoder(seed=seed).fit(train[:, cols])
        s_neg = model.score(neg[:, cols])
        per: dict[str, dict[str, float]] = {}
        all_pos: list[np.ndarray] = []
        for kind in ANOMALY_KINDS:
            s_pos = model.score(pos_vecs[kind][:, cols])
            all_pos.append(s_pos)
            y = np.r_[np.zeros(len(s_neg)), np.ones(len(s_pos))]
            per[kind] = {"auc": float(roc_auc_score(y, np.r_[s_neg, s_pos])),
                         "recall_at_q99": float(np.mean(s_pos > model.threshold))}
        pos = np.concatenate(all_pos)
        y = np.r_[np.zeros(len(s_neg)), np.ones(len(pos))]
        results[label] = {
            "per_kind": per, "auc_all": float(roc_auc_score(y, np.r_[s_neg, pos])),
            "fpr_at_q99": float(np.mean(s_neg > model.threshold)), "threshold": model.threshold,
            "n_iter": model.n_iter, "converged": model.converged,
        }
    results["seconds"] = time.perf_counter() - t0
    return results


def write_md(r: dict[str, Any], per_kind: int) -> str:
    lines = [
        "# MLP-автокодувальник аномалій: ROC-AUC на розмічених ін'єкціях",
        "",
        f"Згенеровано `scripts/train_anomaly_mlp.py --seed {r['seed']} --per-kind {per_kind}`. "
        "Автор: Андрій Жук, 2026.",
        "",
        f"* Дані: {r['bars']} справжніх закритих 1m-барів BTCUSDT (`fixtures/rest/binance_klines.json.gz`).",
        f"* IS (навчання, лише нормальні бари): перші {r['is_bars']} барів → {r['train_vectors']} "
        "векторів ознак (після прогріву екстрактора).",
        f"* OOS: {r['oos_bars']} барів → {r['neg_vectors']} негативних прикладів; по {per_kind} "
        "позитивних на кожен тип аномалії (вносяться по одній у чистий ряд).",
        "* Модель: StandardScaler → MLPRegressor(hidden_layer_sizes=(3,), activation=tanh, max_iter=500, "
        f"random_state={r['seed']}); скор — ‖x − x̂‖², поріг — q₉₉ навчальних скорів.",
        f"* σ(Δlog p) на IS = {r['sigma_dlogp']:.6f} (для масштабу амплітуд).",
        f"* Ознаки 8-3-8: {', '.join(FEATURE_NAMES)}; 5-3-5 — перші п'ять (вектор брифінгу §5.17).",
        "",
        "| тип аномалії | амплітуда | ROC-AUC 8-3-8 | recall@q99 8-3-8 | ROC-AUC 5-3-5 | recall@q99 5-3-5 |",
        "|---|---|---|---|---|---|",
    ]
    for kind in ANOMALY_KINDS:
        a, b = r["8-3-8"]["per_kind"][kind], r["5-3-5"]["per_kind"][kind]
        lo_m, hi_m = MAGNITUDE[kind]
        amp = "—" if hi_m == 0 else (f"×{lo_m:g}…×{hi_m:g}" if kind == "volume_burst"
                                     else f"{lo_m * 100:g}…{hi_m * 100:g} %")
        lines.append(f"| {kind} | {amp} | {a['auc']:.4f} | {a['recall_at_q99']:.3f} | {b['auc']:.4f} | "
                     f"{b['recall_at_q99']:.3f} |")
    e, f = r["8-3-8"], r["5-3-5"]
    lines += [
        f"| **усі типи** | | **{e['auc_all']:.4f}** | | **{f['auc_all']:.4f}** | |",
        "",
        f"Частка хибних тривог на нормальних OOS-барах при порозі q₉₉: 8-3-8 — {e['fpr_at_q99']:.4f}, "
        f"5-3-5 — {f['fpr_at_q99']:.4f} (номінал навчальної вибірки — 0.01).",
        f"Навчання: 8-3-8 — {e['n_iter']} ітерацій (збіжність: {e['converged']}), 5-3-5 — {f['n_iter']} "
        f"({f['converged']}). Час прогону скрипта: {r['seconds']:.1f} с.",
        "",
    ]
    text = "\n".join(lines)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(text, encoding="utf-8")
    return text


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--seed", type=int, default=20260918)
    ap.add_argument("--per-kind", type=int, default=150)
    a = ap.parse_args()
    r = run(a.seed, a.per_kind)
    print(write_md(r, a.per_kind))


if __name__ == "__main__":
    main()
