# Таблиці звіту: зміст і походження

Згенеровано `scripts/export_report_tables.py`. Автор: Андрій Жук, 2026.

Команда: `uv run python scripts/export_report_tables.py --db --results-dir docs/report_tables/raw --run-id 4e5be0de-a4b7-4ef1-b84e-4a563b248be3 --run-id b0638bf7-56a2-4ad7-8d62-11bad97cb2d6 --run-id 2c9344ad-726e-400a-80fc-cb8f98371099 --cov-file docs/report_tables/raw/coverage_combined.txt --out docs/report_tables`

* git_sha: `415acbd2d9137655d90a48476170ac1d0dab21e8`
* seed: 20260918

Таблиць експорту: 15 (+ поіменний `test_groups.md` окремим генератором, тож у переліку нижче 16 файлів); маркерів TBD після
генерації: 3 (усі — у `traceability.md`, жорстко закодовані в скрипті). Після ручної правки — **0** (див. «Ручні правки після генерації» нижче).

Прогони в стовпцях `metrics.md`: `4e5be0de…` — чистий прогін фази 6 (BTCUSDT, `415acbd`, `git_dirty = 0`); `b0638bf7…` і `2c9344ad…` —
робочі точки сітки BTCUSDT і ETHUSDT, записані `scripts/plot_equity.py --persist` на `b933802` (`docs/figures/plot_equity_run_*.md`).

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
| `test_groups.md` | Тести за групами A–N брифінгу (§10): **поіменний перелік** (генератор `tests/helpers/brief_test_groups.py`) | 0 |
| `test_groups_summary.md` | Тести за групами A–N: короткий підсумок цього експорту (перейменовано, див. нижче) | 0 |
| `traceability.md` | Таблиця трасування індивідуального завдання (брифінг §2) | 0 (3 до ручної правки) |
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

## Ручні правки після генерації (2026-09-19, `docs/deviations.d/final_docs.md`)

1. **Один каталог результатів (FIN-02).** Команду з двома каталогами (`--results-dir docs/report_tables/raw --results-dir
   docs/report_tables/raw/exp_search`) замінено командою вище з одним каталогом. `discover()` обходить каталоги рекурсивно й не
   прибирає повторів, тож вкладений каталог дублював секції exp_search. Той самий дефект має `make report`, бо
   `artifacts/exp_search` ідентичний `docs/report_tables/raw/exp_search`.
2. **`test_groups.md` (FIN-03).** Експорт перезаписує поіменний перелік тестів своєю короткою версією. Її перейменовано на
   `test_groups_summary.md` (і `.csv`), а `test_groups.md` згенеровано заново: `uv run python -m tests.helpers.brief_test_groups` на `415acbd`.
3. **`traceability.md` / `.csv` (FIN-04).** Рядки 2, 9, 10 мали жорстко закодовані маркери. Їх переписано вручну: statechart —
   `docs/diagrams/risk_fsm.puml`, testnet-ордер і CRUD через UI — «не виконано: причина; що зробити і ким».

4. **`deviations.md` / `.csv`.** Перегенеровано тією самою командою після появи `docs/deviations.d/final_docs.md`: 227 ідентифікаторів
   (218 + FIN-01…FIN-09). Повторний експорт у тимчасовий каталог дав побайтово ті самі `metrics`, `wf_folds`, `pareto`, `sensitivity`,
   `amdahl`, `cost_models`, `ablation`, `var_kupiec`, `hysteresis`, `pathological`, `mlp_rocauc`, `calibration` (фінальний аудит, 2026-09-19).

Повторний `make report` відтворить стан до правок 1–3. Сирі виводи тестів, покриття, lint, pip-audit, журналу і RAM — у `raw/`:
`test_runs.txt`, `coverage_default.txt`, `coverage_combined.txt`, `coverage_packages.md`, `lint.txt`, `pip_audit.txt`,
`verify_journal.txt`, `ram_profile.txt`. Зведення результатів з поясненнями — [`docs/results.md`](../results.md).
