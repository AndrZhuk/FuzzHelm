# Крива капіталу і режими ризику: db run b0638bf7-56a2-4ad7-8d62-11bad97cb2d6

Згенеровано `scripts/plot_equity.py`. Автор: Андрій Жук, 2026.

Команда: `uv run python scripts/plot_equity.py --db --symbol BTCUSDT --params @artifacts/exp_search/grid/grid_BTCUSDT_mamdani.json --persist --out docs/figures`

* git_sha: `b93380254e142a2766ecfb4ea875819ef6955567`
* seed: 20260918
* dataset_hash: `99382675fda6dcab9be0fa566ac6c602686bf19685aa9c5557f5b7cdf3b8c345`
* config_hash `run`: `bdfa3fef3871f8bbd188a2ee0326489e224a49b5e4e06ca096633f92842938ee`

| величина | значення |
|---|---|
| точок | 64 800 |
| капітал на початку, USDT | 10 000.0 |
| капітал наприкінці, USDT | 8 798.7 |
| дохідність | -0.1201 |
| MaxDD | 0.1201 |
| змін стану | 15 |
| барів у NORMAL | 3432 |
| барів у WARNING | 7002 |
| барів у COOLDOWN | 11 713 |
| барів у HALTED | 42 653 |

Рисунок: `docs/figures/equity_run_b0638bf7.png`.
