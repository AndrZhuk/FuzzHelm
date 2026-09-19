"""Історія ставок фінансування за вікном датасету → data/funding_<SYMBOL>.json.

Найменування: scripts/fetch_funding.py
Призначення: GET /fapi/v1/fundingRate (пагінація за часом), рядки біржі дослівно + BLAKE2b-дайджест;
    рушій бектесту хешує їх у dataset_hash (ingest.funding.funding_columns).
    Тонка обгортка над `fuzzhelm fetch-funding` (уся логіка — у src/fuzzhelm/cli.py, там її й тестує
    tests/unit/test_cli.py); аргументи передаються без змін, `--help` — повний перелік.
Автор: Андрій Жук, 2026.

Запуск:  uv run python scripts/fetch_funding.py [--symbols BTCUSDT,ETHUSDT] [--dry-run]
"""

from __future__ import annotations

import sys

from fuzzhelm.cli import main

if __name__ == "__main__":
    sys.exit(main(["fetch-funding", *sys.argv[1:]]))
