"""Калібрування МФ з даних: T — перцентилі, V — KMeans(k=3) на першому IS-вікні.

Найменування: scripts/calibrate_mf.py
Призначення: БД (дні 1–15 вікна датасету) → FeaturePipeline → детектори → consensus → regimes.calibrate →
    config/membership.yaml, data/calibration_manifest.json, docs/figures/calibration_report.md,
    docs/figures/regimes_clusters.png.
    Тонка обгортка над `fuzzhelm calibrate` (уся логіка — у src/fuzzhelm/cli.py, там її й тестує
    tests/unit/test_cli.py); аргументи передаються без змін, `--help` — повний перелік.
Автор: Андрій Жук, 2026.

Запуск:  uv run python scripts/calibrate_mf.py [--symbol BTCUSDT] [--seed 20260918]
             [--from-fixture PATH] [--no-write]
"""

from __future__ import annotations

import sys

from fuzzhelm.cli import main

if __name__ == "__main__":
    sys.exit(main(["calibrate", *sys.argv[1:]]))
