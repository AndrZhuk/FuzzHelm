"""Добір 45 днів 1m-свічок Binance USDⓈ-M у PostgreSQL (фаза 1, gate ≥ 60 000 рядків).

Найменування: scripts/backfill.py
Призначення: REST-добір з перекриттям 1 бар → CandleRepo (COPY-upsert), прогалини → ingest_gap → добір;
    звіт docs/figures/backfill_report.md і зафіксоване вікно data/dataset_window.json.
    Тонка обгортка над `fuzzhelm backfill` (уся логіка — у src/fuzzhelm/cli.py, там її й тестує
    tests/unit/test_cli.py); аргументи передаються без змін, `--help` — повний перелік.
Автор: Андрій Жук, 2026.

Запуск:  uv run python scripts/backfill.py --days 45 --symbols BTCUSDT,ETHUSDT [--dry-run]
"""

from __future__ import annotations

import sys

from fuzzhelm.cli import main

if __name__ == "__main__":
    sys.exit(main(["backfill", *sys.argv[1:]]))
