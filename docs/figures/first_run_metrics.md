# Перший повний прогін: BTCUSDT, 45 днів, профіль `backtest`

Згенеровано `scripts/run_backtest.py` з рядків PostgreSQL (таблиці `run`, `run_metric`,
`equity_point`, `risk_event`, `event_journal`). Стратегія може бути збитковою: тема роботи — метод
і перевірюваний стенд, а не прибутковість (брифінг §0.2).

## Паспорт прогону

| поле | значення |
|---|---|
| run_id | `89619416-720b-4880-b4e4-26d2c74bca68` |
| kind / status | backtest / **DONE** |
| вікно | 2026-08-04 00:00 — 2026-09-18 00:00 UTC (кінець виключно), 64800 барів 1m |
| config_hash | `7f91fc3408ff0766c232eb339f6ea935a1a83294ce4f72128dbe2214fc56575e` |
| dataset_hash | `3e58da26163555bc6cfb2127d83c9fa8e811c23ab9ee40731f025ad4259908b0` (= `data/dataset_window.json` + ряд фандингу) |
| git_sha | `5fed0fdb996b25794b2941fb9a71e31b7dd66d4e` — **дерево мало незакомічені зміни** (`run_metric.git_dirty = 1`) |
| seed / engine | 20260918 / mamdani |
| journal_head_hash | `a0c35dc58cb9f62e40b2b91eaa264a1451b68ab420e4be44dceaf97af385c333` |
| equity_hash | `8a82f814e93855ce434ab22aeb02c34fb64c211b2664c716603b2e6301c0ad7d` |

Перевірка з БД: `equity_hash`, перерахований з 64800 рядків `equity_point`, **збігається** з паспортом; хеш-ланцюг `event_journal` (27663 записів) цілий, голова = паспорт.

Відтворення поточним кодом (HEAD `5fed0fd` + незакомічені зміни; той самий run_id, seed і дані): `equity_hash` **відтворюється**; голова журналу **НЕ відтворюється** (зараз `8cc9dd23f983a32f…`).
Крива капіталу та сама, а записи журналу інші: код змінився після запису прогону (журнал містить `client_order_id`; див. `docs/deviations.d/workers.md` W-10).

## Метрики (вікно оцінки — з бару першого можливого рішення)

| метрика | значення |
|---|---|
| Загальна дохідність (`total_return`) | -0.0599259 |
| CAGR (`cagr`) | -0.396686 |
| Волатильність (річна) (`ann_vol`) | 0.0190193 |
| Шарп (річний) (`sharpe`) | -26.5591 |
| Сортіно (`sortino`) | -28.6948 |
| Макс. просадка (`max_drawdown`) | 0.0599259 |
| Калмар (`calmar`) | -6.61961 |
| Ulcer index (`ulcer_index`) | 0.0589084 |
| Profit factor (`profit_factor`) | 0.0200013 |
| Очікування угоди, USDT (`expectancy`) | -5.30318 |
| Частка прибуткових (`win_rate`) | 0.0707965 |
| Сер. виграш, USDT (`avg_win`) | 1.52883 |
| Сер. програш, USDT (`avg_loss`) | 5.82371 |
| Угод (`n_trades`) | 113 |
| Оборот (капіталів/рік) (`turnover`) | 1 438 |
| Частка часу в ринку (`exposure`) | 0.00546066 |
| Tail ratio (`tail_ratio`) | 0 |
| PSR (SR* = 0) (`psr`) | 0 |

Інші величини `run_metric`: `git_dirty` = 1, `halted` = 0, `kurt` = 833.69, `n_fills` = 211, `n_obs` = 64 277, `skew` = -19.4015, `sr_period` = -0.0366341, `traded_notional` = 1 655 484.

## Що записано

* `decision`: 180 (профіль `backtest`: повне трасування лише рішень із заявками, ENG-10);
* `sim_order`: 295, кожен з `decision_id` (NOT NULL + FK); `position`: 113 (SIGNAL: 82, STOP: 29, TP: 2);
* `risk_event`: 26979 (з них VETO: 3753); `equity_point`: 64800 (з VaR₉₅/CVaR₉₅ у USDT на вікні 500 бар-дохідностей).

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

Барів у кожному режимі: COOLDOWN: 61463, NORMAL: 1862, WARNING: 1475.

## Час прогону (заміряно під час запису; паралельно працювали інші процеси)

* engine_s: 4.77 с
* load_db_s: 0.31 с
* persist_s: 1.83 с

Рисунок: `docs/figures/first_run_equity.png`.
