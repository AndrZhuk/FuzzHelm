# Ціна відсутності гістерезису (виміряно) проти твердження §5.9

Згенеровано `scripts/export_report_tables.py`. Автор: Андрій Жук, 2026.

### BTCUSDT, контур ризику: default

джерело `docs/report_tables/raw/hysteresis_BTCUSDT/hysteresis_cost.json`; git_sha `b93380254e142a27…`; git_dirty `False`; dataset_hash `99382675fda6dcab…`; symbol `BTCUSDT`; команда `uv run python scripts/hysteresis_cost.py --db --symbol BTCUSDT --workers 8 --params @artifacts/exp_search/grid/grid_BTCUSDT_mamdani.json --out docs/report_tables/raw/hysteresis_BTCUSDT`

| область | комісії %E₀/добу з петлею | без петлі | різниця | виконань з | виконань без | виконань/бар без | номінал/E₀ | твердження §5.9, %/добу |
|---|---|---|---|---|---|---|---|---|
| oos | 0.9056 | 0.964 | 0.05842 | 1307 | 1478 | 0.03422 | 0.4891 | 1.15 |
| full | 0.2878 | 0.2713 | -0.01643 | 616 | 618 | 0.009615 | 0.4899 | 1.15 |

### BTCUSDT, контур ризику: relaxed

джерело `docs/report_tables/raw/hysteresis_BTCUSDT_relaxed/hysteresis_cost.json`; git_sha `b93380254e142a27…`; git_dirty `False`; dataset_hash `99382675fda6dcab…`; symbol `BTCUSDT`; команда `uv run python scripts/hysteresis_cost.py --db --symbol BTCUSDT --workers 8 --params @artifacts/exp_search/grid/grid_BTCUSDT_mamdani.json --risk-loop relaxed --out docs/report_tables/raw/hysteresis_BTCUSDT_relaxed`

| область | комісії %E₀/добу з петлею | без петлі | різниця | виконань з | виконань без | виконань/бар без | номінал/E₀ | твердження §5.9, %/добу |
|---|---|---|---|---|---|---|---|---|
| oos | 1.24 | 1.37 | 0.1292 | 1314 | 1494 | 0.03459 | 0.6874 | 1.15 |
| full | 1.025 | 1.083 | 0.05814 | 1919 | 2154 | 0.03351 | 0.5609 | 1.15 |

### ETHUSDT, контур ризику: default

джерело `docs/report_tables/raw/hysteresis_ETHUSDT/hysteresis_cost.json`; git_sha `b93380254e142a27…`; git_dirty `False`; dataset_hash `9a04aeae9ba15d86…`; symbol `ETHUSDT`; команда `uv run python scripts/hysteresis_cost.py --db --symbol ETHUSDT --workers 8 --params @artifacts/exp_search/grid/grid_ETHUSDT_mamdani.json --out docs/report_tables/raw/hysteresis_ETHUSDT`

| область | комісії %E₀/добу з петлею | без петлі | різниця | виконань з | виконань без | виконань/бар без | номінал/E₀ | твердження §5.9, %/добу |
|---|---|---|---|---|---|---|---|---|
| oos | 0.8719 | 0.891 | 0.01902 | 1420 | 1560 | 0.03612 | 0.4283 | 1.15 |
| full | 0.2614 | 0.2585 | -0.002904 | 847 | 750 | 0.01167 | 0.3847 | 1.15 |

### ETHUSDT, контур ризику: relaxed

джерело `docs/report_tables/raw/hysteresis_ETHUSDT_relaxed/hysteresis_cost.json`; git_sha `b93380254e142a27…`; git_dirty `False`; dataset_hash `9a04aeae9ba15d86…`; symbol `ETHUSDT`; команда `uv run python scripts/hysteresis_cost.py --db --symbol ETHUSDT --workers 8 --params @artifacts/exp_search/grid/grid_ETHUSDT_mamdani.json --risk-loop relaxed --out docs/report_tables/raw/hysteresis_ETHUSDT_relaxed`

| область | комісії %E₀/добу з петлею | без петлі | різниця | виконань з | виконань без | виконань/бар без | номінал/E₀ | твердження §5.9, %/добу |
|---|---|---|---|---|---|---|---|---|
| oos | 1.159 | 1.258 | 0.09828 | 1420 | 1576 | 0.03649 | 0.5984 | 1.15 |
| full | 0.9953 | 1.059 | 0.0642 | 2099 | 2344 | 0.03647 | 0.5044 | 1.15 |

