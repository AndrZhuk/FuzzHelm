# Крива капіталу і режими ризику: db run 2c9344ad-726e-400a-80fc-cb8f98371099

Згенеровано `scripts/plot_equity.py`. Автор: Андрій Жук, 2026.

Команда: `uv run python scripts/plot_equity.py --db --symbol ETHUSDT --params @artifacts/exp_search/grid/grid_ETHUSDT_mamdani.json --persist --out docs/figures`

* git_sha: `b93380254e142a2766ecfb4ea875819ef6955567`
* seed: 20260918
* dataset_hash: `9a04aeae9ba15d8636a05342070d3617f4240aecb588fc89f23b970b6d91671f`
* config_hash `run`: `c1fd0694560996a42cf4e4b31a2227f884b96fdd06bee8ab9ee08807b3fbbf91`

| величина | значення |
|---|---|
| точок | 64 800 |
| капітал на початку, USDT | 10 000.0 |
| капітал наприкінці, USDT | 8 800.1 |
| дохідність | -0.12 |
| MaxDD | 0.1201 |
| змін стану | 5 |
| барів у NORMAL | 2193 |
| барів у WARNING | 8202 |
| барів у COOLDOWN | 16 112 |
| барів у HALTED | 38 293 |

Рисунок: `docs/figures/equity_run_2c9344ad.png`.
