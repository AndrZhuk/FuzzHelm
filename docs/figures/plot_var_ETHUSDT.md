# Гістограми доходностей з VaR₉₅/CVaR₉₅

Згенеровано `scripts/plot_var.py`. Автор: Андрій Жук, 2026. Оцінки на всій вибірці (опис розподілу; бектест прогнозу і тест Купця — у виводі `scripts/var_backtest.py`).

Команда: `uv run python scripts/plot_var.py --results docs/report_tables/raw/var_ETHUSDT --out docs/figures`

* git_sha: `b93380254e142a2766ecfb4ea875819ef6955567`
* seed: 20260918
* dataset_hash: `9a04aeae9ba15d8636a05342070d3617f4240aecb588fc89f23b970b6d91671f` (db:ETHUSDT, 64800 барів)
* config_hash `chosen`: `c1fd0694560996a42cf4e4b31a2227f884b96fdd06bee8ab9ee08807b3fbbf91`

| область | підмножина | n | частка r = 0 | VaR₉₅ іст. | CVaR₉₅ іст. | VaR₉₅ парам. |
|---|---|---|---|---|---|---|
| oos | усі бари | 43 194 | 0.927 | 0 | 0.0001867 | 0.0001043 |
| oos | бари в позиції | 3178 | 0.007867 | 0.0004512 | 0.0006261 | 0.0003617 |
| full | усі бари | 64 277 | 0.9732 | 0 | 5.157e-05 | 4.795e-05 |
| full | бари в позиції | 1768 | 0.02432 | 0.0003648 | 0.0005351 | 0.0002644 |

Рисунок: `docs/figures/var_hist_ETHUSDT_oos.png`.
Рисунок: `docs/figures/var_hist_ETHUSDT_full.png`.
