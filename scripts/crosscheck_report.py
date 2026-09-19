"""Крос-звірка Binance BTC-USDT-PERP ↔ Kraken BTC-USD-SPOT (останні 720 хв Kraken).

Найменування: scripts/crosscheck_report.py
Призначення: сирі відповіді → data/crosscheck_input.json.gz; таблиця і статистика розбіжностей, розклад на
    премію перпетуала / дисконт USDT / залишок → docs/figures/crosscheck_report.md і
    docs/figures/crosscheck_divergence.png.
    Тонка обгортка над `fuzzhelm crosscheck` (уся логіка — у src/fuzzhelm/cli.py, там її й тестує
    tests/unit/test_cli.py); аргументи передаються без змін, `--help` — повний перелік.
Автор: Андрій Жук, 2026.

Запуск:  uv run python scripts/crosscheck_report.py [--from-input data/crosscheck_input.json.gz] [--dry-run]
"""

from __future__ import annotations

import sys

from fuzzhelm.cli import main

if __name__ == "__main__":
    sys.exit(main(["crosscheck", *sys.argv[1:]]))
