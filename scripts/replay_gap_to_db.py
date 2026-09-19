"""Реальні рядки ingest_gap: реплей gap.jsonl.gz із БД-стоками і справжнім REST-добором.

Найменування: scripts/replay_gap_to_db.py
Призначення: IngestPipeline над fixtures/ws/pathological/gap.jsonl.gz → ingest_gap (OPEN → FILLING → FILLED),
    candle, event_journal; добір — BinanceRestClient до fapi.binance.com;
    звіт docs/figures/backfill_gap_replay.md.
    Тонка обгортка над `fuzzhelm replay-gap` (уся логіка — у src/fuzzhelm/cli.py, там її й тестує
    tests/unit/test_cli.py); аргументи передаються без змін, `--help` — повний перелік.
Автор: Андрій Жук, 2026.

Запуск:  uv run python scripts/replay_gap_to_db.py [--dry-run]
"""

from __future__ import annotations

import sys

from fuzzhelm.cli import main

if __name__ == "__main__":
    sys.exit(main(["replay-gap", *sys.argv[1:]]))
