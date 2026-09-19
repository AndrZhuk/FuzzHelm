# Парето-фронт (SR_OOS ↑, MaxDD_OOS ↓, Turnover ↓)

Згенеровано `scripts/export_report_tables.py`. Автор: Андрій Жук, 2026.

джерело `docs/report_tables/raw/exp_search/grid/grid_BTCUSDT_mamdani.json`; git_sha `b93380254e142a27…`; git_dirty `False`; dataset_hash `99382675fda6dcab…`; symbol `BTCUSDT`; команда `uv run python scripts/run_grid.py --db --symbol BTCUSDT --engine mamdani --workers 8`

| клітинка | n_ATR | χ | u_enter | ρ_base | λ | SR oos | MaxDD oos | оборот oos | дох. oos | SR усе вікно | MaxDD усе вікно | обрана |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 32 | 7 | 2.5 | 0.3 | 0.0025 | 0.94 | -75.8893 | 0.2451 | 8 768.6 | -0.2451 | -46.5099 | 0.1202 |  |
| 33 | 7 | 2.5 | 0.3 | 0.0025 | 0.97 | -76.769 | 0.238 | 8 458.0 | -0.238 | -46.2647 | 0.1202 | так |


Робоча точка (`choice.params`): `{"n_atr": 7, "chi": 2.5, "u_enter": 0.3, "rho_base": 0.0025, "lam": 0.97}`, правило `fallback_min_dd`, dd_cap 0.08.

джерело `docs/report_tables/raw/exp_search/grid/grid_ETHUSDT_mamdani.json`; git_sha `b93380254e142a27…`; git_dirty `False`; dataset_hash `9a04aeae9ba15d86…`; symbol `ETHUSDT`; команда `uv run python scripts/run_grid.py --db --symbol ETHUSDT --engine mamdani --workers 8`

| клітинка | n_ATR | χ | u_enter | ρ_base | λ | SR oos | MaxDD oos | оборот oos | дох. oos | SR усе вікно | MaxDD усе вікно | обрана |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 21 | 7 | 2 | 0.3 | 0.0025 | 0.97 | -69.5175 | 0.2301 | 8 039.3 | -0.23 | -49.3647 | 0.12 | так |
| 33 | 7 | 2.5 | 0.3 | 0.0025 | 0.97 | -69.8395 | 0.2305 | 7 837.0 | -0.2304 | -47.3433 | 0.12 |  |
| 35 | 7 | 2.5 | 0.3 | 0.005 | 0.97 | -69.5301 | 0.2305 | 7 864.0 | -0.2304 | -46.5759 | 0.1201 |  |
| 68 | 14 | 2.5 | 0.3 | 0.0025 | 0.94 | -68.7497 | 0.2396 | 8 311.0 | -0.2395 | -46.6526 | 0.1203 |  |
| 69 | 14 | 2.5 | 0.3 | 0.0025 | 0.97 | -69.3552 | 0.2302 | 7 921.2 | -0.2301 | -47.3253 | 0.1201 |  |
| 70 | 14 | 2.5 | 0.3 | 0.005 | 0.94 | -68.6215 | 0.2478 | 8 656.2 | -0.2477 | -45.9744 | 0.1202 |  |
| 71 | 14 | 2.5 | 0.3 | 0.005 | 0.97 | -69.3293 | 0.2336 | 8 067.4 | -0.2335 | -46.8397 | 0.1201 |  |


Робоча точка (`choice.params`): `{"n_atr": 7, "chi": 2.0, "u_enter": 0.3, "rho_base": 0.0025, "lam": 0.97}`, правило `fallback_min_dd`, dd_cap 0.08.

