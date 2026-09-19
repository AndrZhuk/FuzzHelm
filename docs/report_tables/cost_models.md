# Три моделі витрат виконання (§5.14)

Згенеровано `scripts/export_report_tables.py`. Автор: Андрій Жук, 2026.

### BTCUSDT: вибір fixed, контур ризику default

джерело `docs/report_tables/raw/cost_models_BTCUSDT/cost_models.json`; git_sha `b93380254e142a27…`; git_dirty `False`; dataset_hash `99382675fda6dcab…`; symbol `BTCUSDT`; команда `uv run python scripts/cost_models.py --db --symbol BTCUSDT --workers 8 --params @artifacts/exp_search/grid/grid_BTCUSDT_mamdani.json --out docs/report_tables/raw/cost_models_BTCUSDT`

**Увага: параметри `--params` обрано за OOS-метриками тих самих фолдів** (`artifacts/exp_search/grid/grid_BTCUSDT_mamdani.json`, front_space = oos, правило fallback_min_dd). OOS-зведення цієї конфігурації — не чиста поза-вибіркова оцінка (вибіркове зміщення, яке DSR враховує через N прогонів сітки); у порівняннях варіантів зміщення на користь базової конфігурації. Чиста OOS-оцінка — `--selection is_grid` (вибір на IS кожного фолду, для кожного варіанта окремо).

**OOS-фолди walk-forward (зчеплено)**

| варіант | Шарп | дохідність | MaxDD | оборот | комісії, USDT | фандинг, USDT | угод | PSR |
|---|---|---|---|---|---|---|---|---|
| zero (без витрат) | 5.179 | 0.0144 | 0.005358 | 10 396.0 | 0 | 0 | 673 | 0.9317 |
| sqrt_impact (спред + √-імпакт) | 2.756 | 0.007721 | 0.005969 | 10 395.1 | 0 | 0 | 673 | 0.7855 |
| full (√-імпакт + комісії + фандинг) | -76.617 | -0.2387 | 0.2387 | 8 453.6 | 2 716.4 | -0.1681 | 669 | 0 |

**Повне вікно (для BTCUSDT містить дні 1–15 калібрування МФ — не поза-вибіркове)**

| варіант | Шарп | дохідність | MaxDD | оборот | комісії, USDT | фандинг, USDT | угод | PSR |
|---|---|---|---|---|---|---|---|---|
| zero (без витрат) | 7.347 | 0.03188 | 0.007065 | 11 983.8 | 0 | 0 | 981 | 0.9952 |
| sqrt_impact (спред + √-імпакт) | 4.605 | 0.02003 | 0.009489 | 11 987.0 | 0 | 0 | 981 | 0.9471 |
| full (√-імпакт + комісії + фандинг) | -46.385 | -0.1201 | 0.1201 | 2 932.6 | 1 284.5 | -0.5394 | 314 | 0 |

* OOS-фолди walk-forward (зчеплено): Шарп zero − full = 81.795, відношення —, зміна знака: так.
* Повне вікно (для BTCUSDT містить дні 1–15 калібрування МФ — не поза-вибіркове): Шарп zero − full = 53.732, відношення —, зміна знака: так.

### BTCUSDT: вибір is_grid (front), контур ризику default

джерело `docs/report_tables/raw/cost_models_BTCUSDT_isgrid/cost_models.json`; git_sha `b93380254e142a27…`; git_dirty `False`; dataset_hash `99382675fda6dcab…`; symbol `BTCUSDT`; команда `uv run python scripts/cost_models.py --db --symbol BTCUSDT --workers 8 --selection is_grid --scope oos --out docs/report_tables/raw/cost_models_BTCUSDT_isgrid`

**OOS-фолди walk-forward (зчеплено)**

| варіант | Шарп | дохідність | MaxDD | оборот | комісії, USDT | фандинг, USDT | угод | PSR |
|---|---|---|---|---|---|---|---|---|
| zero (без витрат) | 6.66 | 0.02486 | 0.006464 | 21 077.3 | 0 | 0 | 1376 | 0.9727 |
| sqrt_impact (спред + √-імпакт) | 1.379 | 0.005148 | 0.01061 | 20 398.9 | 0 | 0 | 1351 | 0.6539 |
| full (√-імпакт + комісії + фандинг) | -76.642 | -0.239 | 0.239 | 8 478.4 | 2 723.6 | -0.1603 | 672 | 0 |

* OOS-фолди walk-forward (зчеплено): Шарп zero − full = 83.302, відношення —, зміна знака: так.

### ETHUSDT: вибір fixed, контур ризику default

джерело `docs/report_tables/raw/cost_models_ETHUSDT/cost_models.json`; git_sha `b93380254e142a27…`; git_dirty `False`; dataset_hash `9a04aeae9ba15d86…`; symbol `ETHUSDT`; команда `uv run python scripts/cost_models.py --db --symbol ETHUSDT --workers 8 --params @artifacts/exp_search/grid/grid_ETHUSDT_mamdani.json --out docs/report_tables/raw/cost_models_ETHUSDT`

**Увага: параметри `--params` обрано за OOS-метриками тих самих фолдів** (`artifacts/exp_search/grid/grid_ETHUSDT_mamdani.json`, front_space = oos, правило fallback_min_dd). OOS-зведення цієї конфігурації — не чиста поза-вибіркова оцінка (вибіркове зміщення, яке DSR враховує через N прогонів сітки); у порівняннях варіантів зміщення на користь базової конфігурації. Чиста OOS-оцінка — `--selection is_grid` (вибір на IS кожного фолду, для кожного варіанта окремо).

**OOS-фолди walk-forward (зчеплено)**

| варіант | Шарп | дохідність | MaxDD | оборот | комісії, USDT | фандинг, USDT | угод | PSR |
|---|---|---|---|---|---|---|---|---|
| zero (без витрат) | 3.376 | 0.01099 | 0.007896 | 10 106.4 | 0 | 0 | 733 | 0.834 |
| sqrt_impact (спред + √-імпакт) | 0.9162 | 0.002957 | 0.008926 | 10 103.9 | 0 | 0 | 733 | 0.6036 |
| full (√-імпакт + комісії + фандинг) | -69.61 | -0.2314 | 0.2315 | 8 110.5 | 2 615.5 | 0.168 | 733 | 0 |

**Повне вікно (для BTCUSDT містить дні 1–15 калібрування МФ — не поза-вибіркове)**

| варіант | Шарп | дохідність | MaxDD | оборот | комісії, USDT | фандинг, USDT | угод | PSR |
|---|---|---|---|---|---|---|---|---|
| zero (без витрат) | 2.877 | 0.01407 | 0.008084 | 11 822.9 | 0 | 0 | 1084 | 0.8434 |
| sqrt_impact (спред + √-імпакт) | -0.7037 | -0.003564 | 0.009718 | 11 828.4 | 0 | 0 | 1084 | 0.4029 |
| full (√-імпакт + комісії + фандинг) | -49.445 | -0.12 | 0.1201 | 2 662.0 | 1 167.0 | 0.2656 | 437 | 0 |

* OOS-фолди walk-forward (зчеплено): Шарп zero − full = 72.986, відношення —, зміна знака: так.
* Повне вікно (для BTCUSDT містить дні 1–15 калібрування МФ — не поза-вибіркове): Шарп zero − full = 52.322, відношення —, зміна знака: так.

### ETHUSDT: вибір is_grid (front), контур ризику default

джерело `docs/report_tables/raw/cost_models_ETHUSDT_isgrid/cost_models.json`; git_sha `b93380254e142a27…`; git_dirty `False`; dataset_hash `9a04aeae9ba15d86…`; symbol `ETHUSDT`; команда `uv run python scripts/cost_models.py --db --symbol ETHUSDT --workers 8 --selection is_grid --scope oos --out docs/report_tables/raw/cost_models_ETHUSDT_isgrid`

**OOS-фолди walk-forward (зчеплено)**

| варіант | Шарп | дохідність | MaxDD | оборот | комісії, USDT | фандинг, USDT | угод | PSR |
|---|---|---|---|---|---|---|---|---|
| zero (без витрат) | 0.5368 | 0.002237 | 0.01217 | 19 796.4 | 0 | 0 | 1562 | 0.5612 |
| sqrt_impact (спред + √-імпакт) | -0.7114 | -0.002868 | 0.01343 | 15 928.9 | 0 | 0 | 1245 | 0.4192 |
| full (√-імпакт + комісії + фандинг) | -69.621 | -0.2306 | 0.2307 | 7 977.2 | 2 572.9 | 0.168 | 729 | 0 |

* OOS-фолди walk-forward (зчеплено): Шарп zero − full = 70.158, відношення —, зміна знака: так.

