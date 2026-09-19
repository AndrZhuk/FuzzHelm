# 17 метрик бектесту + PSR/DSR

Згенеровано `scripts/export_report_tables.py`. Автор: Андрій Жук, 2026.

Стовпці:
* прогін `4e5be0de…` (БД, backtest): run 4e5be0de-a4b7-4ef1-b84e-4a563b248be3, kind backtest, git_sha `415acbd2d913…`, dataset_hash `99382675fda6dcab…`
* прогін `b0638bf7…` (БД, backtest): run b0638bf7-56a2-4ad7-8d62-11bad97cb2d6, kind backtest, git_sha `b93380254e14…`, dataset_hash `99382675fda6dcab…`
* прогін `2c9344ad…` (БД, backtest): run 2c9344ad-726e-400a-80fc-cb8f98371099, kind backtest, git_sha `b93380254e14…`, dataset_hash `9a04aeae9ba15d86…`
* обрана конфігурація, повне вікно (BTCUSDT, db:BTCUSDT, 64800 барів): джерело `docs/report_tables/raw/var_BTCUSDT/var_backtest.json`; git_sha `b93380254e142a27…`; git_dirty `False`; dataset_hash `99382675fda6dcab…`; symbol `BTCUSDT`; команда `uv run python scripts/var_backtest.py --db --symbol BTCUSDT --workers 8 --params @artifacts/exp_search/grid/grid_BTCUSDT_mamdani.json --out docs/report_tables/raw/var_BTCUSDT`
* обрана конфігурація, повне вікно (ETHUSDT, db:ETHUSDT, 64800 барів): джерело `docs/report_tables/raw/var_ETHUSDT/var_backtest.json`; git_sha `b93380254e142a27…`; git_dirty `False`; dataset_hash `9a04aeae9ba15d86…`; symbol `ETHUSDT`; команда `uv run python scripts/var_backtest.py --db --symbol ETHUSDT --workers 8 --params @artifacts/exp_search/grid/grid_ETHUSDT_mamdani.json --out docs/report_tables/raw/var_ETHUSDT`

| метрика | прогін `4e5be0de…` (БД, backtest) | прогін `b0638bf7…` (БД, backtest) | прогін `2c9344ad…` (БД, backtest) | обрана конфігурація, повне вікно (BTCUSDT, db:BTCUSDT, 64800 барів) | обрана конфігурація, повне вікно (ETHUSDT, db:ETHUSDT, 64800 барів) |
|---|---|---|---|---|---|
| Загальна дохідність (`total_return`) | -0.12 | -0.1201 | -0.12 | -0.1201 | -0.12 |
| CAGR (`cagr`) | -0.6485 | -0.6488 | -0.6484 | -0.6488 | -0.6484 |
| Волатильність (річна) (`ann_vol`) | 0.02266 | 0.02256 | 0.02113 | 0.02256 | 0.02113 |
| Шарп (річний) (`sharpe`) | -46.124 | -46.385 | -49.445 | -46.385 | -49.445 |
| Сортіно (`sortino`) | -48.855 | -49.427 | -52.22 | -49.427 | -52.22 |
| Макс. просадка (`max_drawdown`) | 0.12 | 0.1201 | 0.1201 | 0.1201 | 0.1201 |
| Калмар (`calmar`) | -5.403 | -5.401 | -5.399 | -5.401 | -5.399 |
| Ulcer index (`ulcer_index`) | 0.1136 | 0.1085 | 0.1078 | 0.1085 | 0.1078 |
| Profit factor (`profit_factor`) | 0.01009 | 0.01521 | 0.01632 | 0.01521 | 0.01632 |
| Очікування угоди, USDT (`expectancy`) | -3.962 | -3.826 | -2.746 | -3.826 | -2.746 |
| Частка прибуткових (`win_rate`) | 0.0264 | 0.02229 | 0.09153 | 0.02229 | 0.09153 |
| Сер. виграш, USDT (`avg_win`) | 1.529 | 2.651 | 0.4975 | 2.651 | 0.4975 |
| Сер. програш, USDT (`avg_loss`) | 4.11 | 3.973 | 3.073 | 3.973 | 3.073 |
| Угод (`n_trades`) | 303 | 314 | 437 | 314 | 437 |
| Оборот (капіталів/рік) (`turnover`) | 2 906.3 | 2 932.6 | 2 662.0 | 2 932.6 | 2 662.0 |
| Частка часу в ринку (`exposure`) | 0.0149 | 0.01543 | 0.02113 | 0.01543 | 0.02113 |
| Tail ratio (`tail_ratio`) | 0 | 0 | 0 | 0 | 0 |
| PSR (SR* = 0) (`psr`) | 0 | 0 | 0 | 0 | 0 |
| DSR (SR* = SR₀ за N прогонами сітки) (`dsr`) | 0 | 0 | 0 | 0 | 0 |

* DSR «прогін `4e5be0de…` (БД, backtest)»: N = 108 (скінченних Шарпів 108; БД: 108 прогонів kind = grid_cell на тому самому dataset_hash, сітка git_sha `b93380254e14…`, seed 20260919, рушій mamdani), SR₀ = 0.007551 (Шарпи за період).
* DSR «прогін `b0638bf7…` (БД, backtest)»: N = 108 (скінченних Шарпів 108; БД: 108 прогонів kind = grid_cell на тому самому dataset_hash, сітка git_sha `b93380254e14…`, seed 20260919, рушій mamdani), SR₀ = 0.007551 (Шарпи за період).
* DSR «прогін `2c9344ad…` (БД, backtest)»: N = 108 (скінченних Шарпів 108; БД: 108 прогонів kind = grid_cell на тому самому dataset_hash, сітка git_sha `b93380254e14…`, seed 20260919, рушій mamdani), SR₀ = 0.005197 (Шарпи за період).
* DSR «обрана конфігурація, повне вікно (BTCUSDT, db:BTCUSDT, 64800 барів)»: N = 108 (скінченних Шарпів 108; БД: 108 прогонів kind = grid_cell на тому самому dataset_hash, сітка git_sha `b93380254e14…`, seed 20260919, рушій mamdani), SR₀ = 0.007551 (Шарпи за період).
* DSR «обрана конфігурація, повне вікно (ETHUSDT, db:ETHUSDT, 64800 барів)»: N = 108 (скінченних Шарпів 108; БД: 108 прогонів kind = grid_cell на тому самому dataset_hash, сітка git_sha `b93380254e14…`, seed 20260919, рушій mamdani), SR₀ = 0.005197 (Шарпи за період).

Якщо PSR < 0.95 — перевага статистично не встановлена (§5.15). DSR стовпця — проти Шарпів прогонів сітки на ТОМУ САМОМУ наборі (dataset_hash), однієї сітки (git_sha, seed, рушій).

DSR обраної точки сітки (вивід exp_search):

джерело `docs/report_tables/raw/exp_search/grid/grid_BTCUSDT_mamdani.json`; git_sha `b93380254e142a27…`; git_dirty `False`; dataset_hash `99382675fda6dcab…`; symbol `BTCUSDT`; команда `uv run python scripts/run_grid.py --db --symbol BTCUSDT --engine mamdani --workers 8`

| величина | простір вибору (oos) | усе вікно (in-sample, довідково) |
|---|---|---|
| N (фактично оцінених клітинок) | 108 | 108 |
| скінченних SR | 108 | 108 |
| Var(SR_i), вибіркова | 0.0008 | 8.717e-06 |
| SR₀ = E[max SR] | 0.0717 | 0.0076 |
| ŜR обраної (за період) | -0.1059 | -0.0638 |
| n доходностей | 43 194 | 64 277 |
| γ₃ / γ₄ | -5.1813 / 69.1209 | -12.4645 / 353.7025 |
| PSR | 0 | 0 |
| DSR | 0 | 0 |

DSR обраної точки сітки (вивід exp_search):

джерело `docs/report_tables/raw/exp_search/grid/grid_ETHUSDT_mamdani.json`; git_sha `b93380254e142a27…`; git_dirty `False`; dataset_hash `9a04aeae9ba15d86…`; symbol `ETHUSDT`; команда `uv run python scripts/run_grid.py --db --symbol ETHUSDT --engine mamdani --workers 8`

| величина | простір вибору (oos) | усе вікно (in-sample, довідково) |
|---|---|---|
| N (фактично оцінених клітинок) | 108 | 108 |
| скінченних SR | 108 | 108 |
| Var(SR_i), вибіркова | 0.0006 | 4.129e-06 |
| SR₀ = E[max SR] | 0.0649 | 0.0052 |
| ŜR обраної (за період) | -0.0959 | -0.0681 |
| n доходностей | 43 194 | 64 277 |
| γ₃ / γ₄ | -4.8845 / 78.7145 | -13.2388 / 319.1184 |
| PSR | 0 | 0 |
| DSR | 0 | 0 |

