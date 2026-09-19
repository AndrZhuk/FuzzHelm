# Гістограми доходностей з VaR₉₅/CVaR₉₅

Згенеровано `scripts/plot_var.py`. Автор: Андрій Жук, 2026. Оцінки на всій вибірці (опис розподілу; бектест прогнозу і тест Купця — у виводі `scripts/var_backtest.py`).

Команда: `uv run python scripts/plot_var.py --results docs/report_tables/raw/var_BTCUSDT --out docs/figures`

* git_sha: `b93380254e142a2766ecfb4ea875819ef6955567`
* seed: 20260918
* dataset_hash: `99382675fda6dcab9be0fa566ac6c602686bf19685aa9c5557f5b7cdf3b8c345` (db:BTCUSDT, 64800 барів)
* config_hash `chosen`: `bdfa3fef3871f8bbd188a2ee0326489e224a49b5e4e06ca096633f92842938ee`

| область | підмножина | n | частка r = 0 | VaR₉₅ іст. | CVaR₉₅ іст. | VaR₉₅ парам. |
|---|---|---|---|---|---|---|
| oos | усі бари | 43 194 | 0.9321 | 0 | 0.0001789 | 9.826e-05 |
| oos | бари в позиції | 2987 | 0.01808 | 0.0004463 | 0.0005979 | 0.0003445 |
| full | усі бари | 64 277 | 0.9811 | 0 | 4.965e-05 | 5.117e-05 |
| full | бари в позиції | 1294 | 0.06337 | 0.0004327 | 0.000614 | 0.0003229 |

Рисунок: `docs/figures/var_hist_BTCUSDT_oos.png`.
Рисунок: `docs/figures/var_hist_BTCUSDT_full.png`.
