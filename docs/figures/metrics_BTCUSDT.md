# Бектест: BTCUSDT, 45 днів, профіль `backtest`

Згенеровано `scripts/run_backtest.py` з рядків PostgreSQL (таблиці `run`, `run_metric`,
`equity_point`, `risk_event`, `event_journal`). Стратегія може бути збитковою: тема роботи — метод
і перевірюваний стенд, а не прибутковість.

## Паспорт прогону

| поле | значення |
|---|---|
| run_id | `29aca2ed-c976-46df-8074-36de129303f4` |
| kind / status | backtest / **DONE** |
| вікно | 2026-08-04 00:00 — 2026-09-18 00:00 UTC (кінець виключно), 64800 барів 1m |
| config_hash | `a44f1cf37b4e876e74490cc6fe39aedacb5e1ca203083bbd947dc37807fc4516` |
| dataset_hash | `99382675fda6dcab9be0fa566ac6c602686bf19685aa9c5557f5b7cdf3b8c345` (= `data/dataset_window.json` + ряд фандингу) |
| git_sha | `a994bf342938443375148b9947c7ec1399730c05` |
| seed / engine | 20260918 / mamdani |
| journal_head_hash | `9d87581b10ae056e0f758d323d50b496249c4f897e17082753c5d35e586b6e64` |
| equity_hash | `3b5009cd6577676c0035d3eb6a0b06a2f0fdec2fe2784c13fd7d7fe93c629986` |

Перевірка з БД: `equity_hash`, перерахований з 64800 рядків `equity_point`, **збігається** з паспортом; хеш-ланцюг `event_journal` (5959 записів) цілий, голова = паспорт.

## Метрики (вікно оцінки — з бару першого можливого рішення)

| метрика | значення |
|---|---|
| Загальна дохідність (`total_return`) | -0.120036 |
| CAGR (`cagr`) | -0.648535 |
| Волатильність (річна) (`ann_vol`) | 0.0226646 |
| Шарп (річний) (`sharpe`) | -46.1241 |
| Сортіно (`sortino`) | -48.8548 |
| Макс. просадка (`max_drawdown`) | 0.120036 |
| Калмар (`calmar`) | -5.40282 |
| Ulcer index (`ulcer_index`) | 0.113615 |
| Profit factor (`profit_factor`) | 0.0100863 |
| Очікування угоди, USDT (`expectancy`) | -3.9616 |
| Частка прибуткових (`win_rate`) | 0.0264026 |
| Сер. виграш, USDT (`avg_win`) | 1.52883 |
| Сер. програш, USDT (`avg_loss`) | 4.11049 |
| Угод (`n_trades`) | 303 |
| Оборот (капіталів/рік) (`turnover`) | 2 906 |
| Частка часу в ринку (`exposure`) | 0.014904 |
| Tail ratio (`tail_ratio`) | 0 |

Інші величини `run_metric`: `git_dirty` = 0, `halted` = 1, `n_fills` = 586, `traded_notional` = 3 158 679.

## Що записано

* `decision`: 497 (профіль `backtest`: повне трасування лише рішень із заявками);
* `sim_order`: 809, кожен з `decision_id` (NOT NULL + FK); `position`: 303 (SIGNAL: 214, STOP: 80, TP: 9);
* `risk_event`: 4076 (з них VETO: 82); `equity_point`: 64800.

## Шлях автомата ризику

| час (UTC) | перехід | подія | просадка | денний PnL |
|---|---|---|---|---|
| 2026-08-04 10:45 | NORMAL → WARNING | WARN_BREACH | 0.0076 | -0.0076 |
| 2026-08-04 11:00 | WARNING → NORMAL | RECOVERY | 0.0076 | -0.0076 |
| 2026-08-04 13:20 | NORMAL → WARNING | WARN_BREACH | 0.0076 | -0.0076 |
| 2026-08-04 14:54 | WARNING → NORMAL | RECOVERY | 0.0076 | -0.0076 |
| 2026-08-04 15:08 | NORMAL → WARNING | WARN_BREACH | 0.0076 | -0.0076 |
| 2026-08-04 15:23 | WARNING → NORMAL | RECOVERY | 0.0076 | -0.0076 |
| 2026-08-04 15:33 | NORMAL → WARNING | WARN_BREACH | 0.0076 | -0.0076 |
| 2026-08-04 15:58 | WARNING → NORMAL | RECOVERY | 0.0076 | -0.0076 |
| 2026-08-04 15:59 | NORMAL → WARNING | WARN_BREACH | 0.0076 | -0.0076 |
| 2026-08-04 16:14 | WARNING → NORMAL | RECOVERY | 0.0076 | -0.0076 |
| 2026-08-04 18:15 | NORMAL → WARNING | WARN_BREACH | 0.0091 | -0.0091 |
| 2026-08-04 18:30 | WARNING → NORMAL | RECOVERY | 0.0091 | -0.0091 |
| 2026-08-04 22:53 | NORMAL → COOLDOWN | COOL_BREACH | 0.0203 | -0.0203 |
| 2026-08-05 00:00 | COOLDOWN → WARNING | RECOVERY | 0.0206 | 0.0000 |
| 2026-08-05 00:15 | WARNING → NORMAL | RECOVERY | 0.0206 | 0.0000 |
| 2026-08-05 02:18 | NORMAL → WARNING | WARN_BREACH | 0.0218 | -0.0013 |
| 2026-08-05 02:33 | WARNING → NORMAL | RECOVERY | 0.0218 | -0.0013 |
| 2026-08-05 03:18 | NORMAL → WARNING | WARN_BREACH | 0.0234 | -0.0029 |
| 2026-08-05 03:34 | WARNING → NORMAL | RECOVERY | 0.0234 | -0.0029 |
| 2026-08-05 11:54 | NORMAL → COOLDOWN | COOL_BREACH | 0.0403 | -0.0201 |
| 2026-08-06 00:00 | COOLDOWN → WARNING | RECOVERY | 0.0406 | 0.0000 |
| 2026-08-06 20:50 | WARNING → COOLDOWN | COOL_BREACH | 0.0599 | -0.0201 |
| 2026-08-12 03:26 | COOLDOWN → HALTED | HALT_BREACH | 0.1200 | -0.0021 |

Барів у кожному режимі: COOLDOWN: 8389, HALTED: 53074, NORMAL: 1862, WARNING: 1475.

## Час прогону (заміряно під час запису; паралельно працювали інші процеси)

* engine_s: 4.50 с
* load_db_s: 0.38 с
* persist_s: 1.46 с

Рисунок: `docs/figures/equity_BTCUSDT.png`.
