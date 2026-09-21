# Бектест: ETHUSDT, 45 днів, профіль `backtest`

Згенеровано `scripts/run_backtest.py` з рядків PostgreSQL (таблиці `run`, `run_metric`,
`equity_point`, `risk_event`, `event_journal`). Стратегія може бути збитковою: тема роботи — метод
і перевірюваний стенд, а не прибутковість.

## Паспорт прогону

| поле | значення |
|---|---|
| run_id | `7c401f64-9164-459b-a516-ab052843486b` |
| kind / status | backtest / **DONE** |
| вікно | 2026-08-04 00:00 — 2026-09-18 00:00 UTC (кінець виключно), 64800 барів 1m |
| config_hash | `a44f1cf37b4e876e74490cc6fe39aedacb5e1ca203083bbd947dc37807fc4516` |
| dataset_hash | `9a04aeae9ba15d8636a05342070d3617f4240aecb588fc89f23b970b6d91671f` (= `data/dataset_window.json` + ряд фандингу) |
| git_sha | `a994bf342938443375148b9947c7ec1399730c05` |
| seed / engine | 20260918 / mamdani |
| journal_head_hash | `4fec1ef4b7865aeed7492b21582ee59978bde8a2143529ec5b7a2d1ae55ea51f` |
| equity_hash | `f8fc549ab8ff24fb3f12f229249f79acbbe65ddc16f1f1bbedc6bb54b2622274` |

Перевірка з БД: `equity_hash`, перерахований з 64800 рядків `equity_point`, **збігається** з паспортом; хеш-ланцюг `event_journal` (6713 записів) цілий, голова = паспорт.

## Метрики (вікно оцінки — з бару першого можливого рішення)

| метрика | значення |
|---|---|
| Загальна дохідність (`total_return`) | -0.119516 |
| CAGR (`cagr`) | -0.646832 |
| Волатильність (річна) (`ann_vol`) | 0.0222779 |
| Шарп (річний) (`sharpe`) | -46.7082 |
| Сортіно (`sortino`) | -49.7543 |
| Макс. просадка (`max_drawdown`) | 0.120002 |
| Калмар (`calmar`) | -5.39019 |
| Ulcer index (`ulcer_index`) | 0.114045 |
| Profit factor (`profit_factor`) | 0.0117939 |
| Очікування угоди, USDT (`expectancy`) | -3.33844 |
| Частка прибуткових (`win_rate`) | 0.0335196 |
| Сер. виграш, USDT (`avg_win`) | 1.18865 |
| Сер. програш, USDT (`avg_loss`) | 3.49545 |
| Угод (`n_trades`) | 358 |
| Оборот (капіталів/рік) (`turnover`) | 2 690 |
| Частка часу в ринку (`exposure`) | 0.0174088 |
| Tail ratio (`tail_ratio`) | 0 |

Інші величини `run_metric`: `git_dirty` = 0, `halted` = 1, `n_fills` = 691, `traded_notional` = 2 923 698.

## Що записано

* `decision`: 589 (профіль `backtest`: повне трасування лише рішень із заявками);
* `sim_order`: 950, кожен з `decision_id` (NOT NULL + FK); `position`: 358 (SIGNAL: 256, STOP: 99, TP: 3);
* `risk_event`: 4486 (з них VETO: 49); `equity_point`: 64800.

## Шлях автомата ризику

| час (UTC) | перехід | подія | просадка | денний PnL |
|---|---|---|---|---|
| 2026-08-04 10:44 | NORMAL → WARNING | WARN_BREACH | 0.0065 | -0.0059 |
| 2026-08-04 11:01 | WARNING → NORMAL | RECOVERY | 0.0065 | -0.0059 |
| 2026-08-04 13:20 | NORMAL → WARNING | WARN_BREACH | 0.0065 | -0.0059 |
| 2026-08-04 13:35 | WARNING → NORMAL | RECOVERY | 0.0065 | -0.0059 |
| 2026-08-04 13:46 | NORMAL → WARNING | WARN_BREACH | 0.0065 | -0.0059 |
| 2026-08-04 14:39 | WARNING → NORMAL | RECOVERY | 0.0065 | -0.0059 |
| 2026-08-04 14:40 | NORMAL → WARNING | WARN_BREACH | 0.0065 | -0.0059 |
| 2026-08-04 14:55 | WARNING → NORMAL | RECOVERY | 0.0065 | -0.0059 |
| 2026-08-04 15:44 | NORMAL → WARNING | WARN_BREACH | 0.0074 | -0.0068 |
| 2026-08-04 15:59 | WARNING → NORMAL | RECOVERY | 0.0074 | -0.0068 |
| 2026-08-04 23:08 | NORMAL → COOLDOWN | COOL_BREACH | 0.0210 | -0.0205 |
| 2026-08-05 00:00 | COOLDOWN → WARNING | RECOVERY | 0.0210 | 0.0000 |
| 2026-08-05 00:15 | WARNING → NORMAL | RECOVERY | 0.0210 | 0.0000 |
| 2026-08-05 03:20 | NORMAL → WARNING | WARN_BREACH | 0.0219 | -0.0009 |
| 2026-08-05 03:35 | WARNING → NORMAL | RECOVERY | 0.0219 | -0.0009 |
| 2026-08-05 13:11 | NORMAL → WARNING | WARN_BREACH | 0.0387 | -0.0180 |
| 2026-08-05 20:08 | WARNING → COOLDOWN | COOL_BREACH | 0.0407 | -0.0201 |
| 2026-08-06 00:00 | COOLDOWN → WARNING | RECOVERY | 0.0406 | 0.0000 |
| 2026-08-06 22:50 | WARNING → COOLDOWN | COOL_BREACH | 0.0600 | -0.0202 |
| 2026-08-11 20:45 | COOLDOWN → HALTED | HALT_BREACH | 0.1200 | -0.0096 |

Барів у кожному режимі: COOLDOWN: 7359, HALTED: 53475, NORMAL: 2034, WARNING: 1932.

## Час прогону (заміряно під час запису; паралельно працювали інші процеси)

* engine_s: 4.78 с
* load_db_s: 0.44 с
* persist_s: 1.89 с

Рисунок: `docs/figures/equity_ETHUSDT.png`.
