"""Спільна навчена модель автокодувальника аномалій для тестів (мемоізація чистої функції).

Найменування: tests/helpers/quality_models.py
Призначення: кілька тестів (tests/unit/test_quality.py, tests/unit/test_ws_ingest.py) навчали ту саму модель
    8-3-8 на тих самих 1500 реальних барах з тим самим seed — ~0.17 с на кожне навчання. Навчання
    детерміноване за seed (це окремо перевіряє test_mlp_autoencoder_flags_injected_spike_and_not_normal_bar:
    свіже навчання порівнюється саме з цією кешованою моделлю), тож повторне використання результату нічого
    не послаблює. Модель лише читається (score / is_anomaly / AnomalyScorer) — жоден тест її не змінює.
Автор: Андрій Жук, 2026.
"""

from __future__ import annotations

import gzip
from functools import cache
from pathlib import Path

import orjson

from fuzzhelm.features.convert import Bar, bar_from_candle
from fuzzhelm.ingest.normalize import normalize_exchange_info, normalize_rest_klines
from fuzzhelm.quality.anomaly_mlp import AnomalyAutoencoder, feature_matrix

REST = Path(__file__).resolve().parents[2] / "fixtures" / "rest"
SEED = 20260918


@cache
def fixture_bars() -> tuple[Bar, ...]:
    """3000 реальних 1m-барів BTCUSDT (fixtures/rest/binance_klines.json.gz), нормалізація — як у тестах."""
    rows = orjson.loads(gzip.decompress((REST / "binance_klines.json.gz").read_bytes()))
    now_ms = rows[-1][6] + 1_000
    instr = normalize_exchange_info(orjson.loads((REST / "exchange_info.json").read_bytes()),
                                    ["BTCUSDT"])["BTCUSDT"]
    candles = normalize_rest_klines(rows, instr, now_ms * 1_000_000, server_time_ms=now_ms)
    return tuple(bar_from_candle(c) for c in candles)


@cache
def trained_autoencoder(n_train: int = 1500, seed: int = SEED) -> AnomalyAutoencoder:
    """AnomalyAutoencoder(seed).fit(ознаки перших n_train барів) — навчається один раз на процес."""
    X, _ = feature_matrix(list(fixture_bars()[:n_train]))
    return AnomalyAutoencoder(seed=seed).fit(X)
