# Таблиці звіту: зміст і походження

Згенеровано `scripts/export_report_tables.py`. Автор: Андрій Жук, 2026.

Команда: `uv run python scripts/export_report_tables.py --db --results-dir docs/report_tables/raw --run-id 4e5be0de-a4b7-4ef1-b84e-4a563b248be3 --run-id b0638bf7-56a2-4ad7-8d62-11bad97cb2d6 --run-id 2c9344ad-726e-400a-80fc-cb8f98371099 --cov-file docs/report_tables/raw/coverage_combined.txt --out docs/report_tables`

* git_sha: `b0c0396ad615cbce7cc52a30026199f8a9e30cfa` — **код мав незакомічені зміни** ( M Makefile;  M data/anomaly_mlp_ETHUSDT.json;  M scripts/export_report_tables.py)
* seed: 20260918

Таблиць: 15; маркерів TBD разом: **0** (кожен — відсутній вхід, а не підставлене число).

| файл | таблиця | TBD |
|---|---|---|
| `metrics.md` | 17 метрик бектесту + PSR/DSR | 0 |
| `wf_folds.md` | Walk-forward: IS/OOS по фолдах | 0 |
| `pareto.md` | Парето-фронт (SR_OOS ↑, MaxDD_OOS ↓, Turnover ↓) | 0 |
| `sensitivity.md` | Аналіз чутливості до 8 параметрів | 0 |
| `amdahl.md` | Прискорення S(p) проти межі Амдала | 0 |
| `cost_models.md` | Три моделі витрат виконання (§5.14) | 0 |
| `ablation.md` | Ablation: детектори, Мамдані/лінійне, κ, гістерезис | 0 |
| `var_kupiec.md` | VaR₉₅/CVaR₉₅: історичний проти параметричного, тест Купця | 0 |
| `hysteresis.md` | Ціна відсутності гістерезису (виміряно) проти твердження §5.9 | 0 |
| `pathological.md` | Патологічні WS-сесії: втрачено / дублів / час відновлення | 0 |
| `mlp_rocauc.md` | MLP-автокодувальник: ROC-AUC на ін'єкціях | 0 |
| `calibration.md` | Калібрування функцій належності (KMeans, перцентилі) | 0 |
| `test_groups_summary.md` | Тести за групами A–N брифінгу (§10), зведення | 0 |
| `traceability.md` | Таблиця трасування індивідуального завдання (брифінг §2) | 0 |
| `deviations.md` | Перелік розходжень «спека ↔ реальність» | 0 |

Знайдені файли результатів:

* ablation: `docs/report_tables/raw/ablation_BTCUSDT/ablation.json`, `docs/report_tables/raw/ablation_BTCUSDT_isgrid/ablation.json`, `docs/report_tables/raw/ablation_ETHUSDT/ablation.json`, `docs/report_tables/raw/ablation_ETHUSDT_isgrid/ablation.json`
* amdahl: `docs/report_tables/raw/exp_search/amdahl/amdahl_BTCUSDT.json`
* cost_models: `docs/report_tables/raw/cost_models_BTCUSDT/cost_models.json`, `docs/report_tables/raw/cost_models_BTCUSDT_isgrid/cost_models.json`, `docs/report_tables/raw/cost_models_ETHUSDT/cost_models.json`, `docs/report_tables/raw/cost_models_ETHUSDT_isgrid/cost_models.json`
* grid: `docs/report_tables/raw/exp_search/grid/grid_BTCUSDT_mamdani.json`, `docs/report_tables/raw/exp_search/grid/grid_ETHUSDT_mamdani.json`
* hysteresis_cost: `docs/report_tables/raw/hysteresis_BTCUSDT/hysteresis_cost.json`, `docs/report_tables/raw/hysteresis_BTCUSDT_relaxed/hysteresis_cost.json`, `docs/report_tables/raw/hysteresis_ETHUSDT/hysteresis_cost.json`, `docs/report_tables/raw/hysteresis_ETHUSDT_relaxed/hysteresis_cost.json`
* sensitivity: `docs/report_tables/raw/exp_search/sensitivity/sensitivity_BTCUSDT_mamdani_full.json`, `docs/report_tables/raw/exp_search/sensitivity/sensitivity_BTCUSDT_mamdani_oos.json`
* var_backtest: `docs/report_tables/raw/var_BTCUSDT/var_backtest.json`, `docs/report_tables/raw/var_ETHUSDT/var_backtest.json`
* wf_folds: `docs/report_tables/raw/exp_search/walkforward/walkforward_BTCUSDT_engines.json`, `docs/report_tables/raw/exp_search/walkforward/walkforward_BTCUSDT_linear.json`, `docs/report_tables/raw/exp_search/walkforward/walkforward_BTCUSDT_mamdani.json`, `docs/report_tables/raw/exp_search/walkforward/walkforward_ETHUSDT_engines.json`, `docs/report_tables/raw/exp_search/walkforward/walkforward_ETHUSDT_linear.json`, `docs/report_tables/raw/exp_search/walkforward/walkforward_ETHUSDT_mamdani.json`

