# FuzzHelm

Сервіс агрегації біржових даних та автоматизації маржинальних торговельних операцій з нечітко-логічним ядром прийняття
рішень та ієрархічною підсистемою автоматичного контролю ризиків і лімітів. English version — [`README.en.md`](README.en.md).

> **М'яке пропонує, жорстке вирішує.** Нечітке ядро видає лише намір `u ∈ [−1; 1]`. Детермінований ризик-контур не має
> технічного способу підвищити експозицію: це властивість алгебри вердиктів, перевірена property-тестом
> (`test_risk_chain_never_increases_exposure`).

Проєктно-технологічна практика, кафедра АСУ ІКНІ НУ «Львівська політехніка», 2026. Автор: Андрій Жук. Версія пакета 0.1.0
(`pyproject.toml`), зміни — [`CHANGELOG.md`](CHANGELOG.md).

## Безпека і відмова від відповідальності

- **Лише публічні read-only дані і paper/testnet-виконання.** Реальних коштів і mainnet немає за побудовою. `fuzzhelm.config`
  має жорсткий allowlist хостів: ордери йдуть лише на `testnet.binancefuture.com` / `demo-fapi.binance.com`, дані беруться лише з
  публічних хостів Binance і Kraken. Порушення зупиняє старт (`MainnetHostRejected`, тест `test_mainnet_host_is_rejected_by_config`).
  Типовий виконавець торгового циклу — симулятор `PaperBroker`.
- **Система не є інвестиційною рекомендацією.** Тема роботи — метод і перевірюваний стенд, а не прибутковість. Стратегія на
  45-денному вікні **збиткова після витрат**, і це чесно зафіксовано: PSR = DSR = 0 (див. «Результати» нижче).
- Секрети — лише в `.env` (у `.gitignore`). Шаблон `.env.example` не містить справжніх секретів: ключі testnet і токен Telegram
  порожні, `FUZZHELM_JWT_SECRET=change-me` — заглушка, решта — типові несекретні значення (адреса локальної БД, testnet-хост, seed). Облікові записи, ключі testnet,
  токен Telegram створює людина: агент креденшлів не вводить.

## Результати коротко

Повний підрозділ з джерелом кожного числа — [`docs/results.md`](docs/results.md). Дані: 45 днів, 64 800 1m-барів BTCUSDT і
ETHUSDT (Binance USDⓈ-M).

| що | результат |
|---|---|
| прогін фази 6 (BTCUSDT, чистий код `415acbd`) | run `4e5be0de-a4b7-4ef1-b84e-4a563b248be3`: −12.00 %, Шарп −46.12, 303 угоди, HALTED через 8.14 доби; PSR 0, DSR 0 (N = 108); `git_dirty = 0`, `equity_hash 3b5009cd…` |
| сітка 108 клітинок × 2 символи | усі 216 клітинок збиткові (SR повного вікна від −54.11 до −44.58 на BTCUSDT); кожна зупинена засувкою HALTED при MaxDD 0.1200–0.1204 |
| walk-forward, 6 фолдів (конкатенований OOS) | Мамдані: Шарп −76.64 (BTCUSDT) / −69.62 (ETHUSDT); лінійне голосування: −34.53 / −34.80 (торгує в 4.8–5.9 раза рідше); PSR 0 |
| три моделі витрат (чиста OOS-оцінка) | без витрат BTCUSDT: Шарп 6.66, PSR 0.9727 (без поправки на множинний вибір); спред + імпакт: 1.38; повні витрати: −76.64 |
| ablation, чутливість | результат визначає частота торгівлі: u_enter і κ_min дають найбільший розмах Шарпа на OOS |
| ціна відсутності гістерезису | виміряно 0.019–0.129 % капіталу на добу (брифінг: «1.15 %», арифметика брифінгу — 115.2 %) |
| VaR₉₅ / тест Купця (бари в позиції, OOS) | історичний не відкинуто (LR 2.25 / 3.34), параметричний відкинуто (LR 164 / 80): нормальна модель занижує ризик |
| Амдал, 8 процесів на Apple M3 (4P + 4E) | S(8) = 3.855; НК f = 0.1412, структурна f = 0.0036; розрив пояснює здорожчання обчислень × 1.96, а не послідовна частка |
| MLP-автокодувальник аномалій | ROC-AUC 5-3-5 0.9729 / 0.9703 проти 8-3-8 0.9531 / 0.9567 → у контурі 5-3-5 |
| монотонність Мамдані при w ≡ 1 | твердження брифінгу хибне: реверс до 0.1435 (FZ-01) |

Висновок: сигнал має слабку позитивну валову перевагу, яку на 1-хвилинних барах знищують витрати виконання. Ризик-контур
стримує збиток на рівні засувки 12 %, як і спроєктовано.

## Архітектура коротко

```
REST (Binance klines/exchangeInfo/fundingRate, Kraken OHLC) ─┐
WS Binance: /market (kline, aggTrade, markPrice) + /public (depth20) ─┴→ IngestPipeline: нормалізація (Decimal),
   інваріанти, дедуплікація, прогалини → REST-добір, скор якості Q (AHP), MLP-аномалії 5-3-5 → PostgreSQL + журнал з ланцюгом хешів
 → FeaturePipeline (інкрементально) → 6 детекторів (s, c) → консенсус T, R, V → Мамдані (45 правил як дані) → κ → u_final
 → тригер Шмітта → сайзер (vol-target, ATR-ризик, плече) → RiskGuard (6 лімітів + режим) → автомат NORMAL/WARNING/COOLDOWN/HALTED
 → OrderRouter → PaperBroker | BinanceTestnetVenue → Portfolio → паспорт прогону, /explain, SSE
```

- **Межа детермінізму:** пакети `core, features, detectors, fuzzy, decision, risk, sizing` не читають настінного годинника й не
  мають випадковості (AST-тест). Тому один і той самий `TradingLoop.step()` працює в бектесті, live/replay і в клітинках сітки.
- **Межа типів:** гроші, ціни й обсяги — лише `Decimal`/`NUMERIC(38,18)`. Конвертація у float і назад — лише у двох точках
  (`features/convert.py`, `sizing/convert.py`).
- **Відтворюваність:** кожен прогін має паспорт (`config_hash`, `dataset_hash`, `git_sha`, `seed`, `engine`, `journal_head_hash`,
  `equity_hash`) і прапорець `run_metric.git_dirty` (незакомічені зміни коду поза `docs/` і `artifacts/`).
- Діаграми: [`docs/diagrams/`](docs/diagrams/) (компоненти, розгортання, statechart ризику, послідовності, ER, класи, діяльність).

Стек: Python 3.12 (`uv`), FastAPI, Pydantic 2, SQLAlchemy 2 async + asyncpg, Alembic, PostgreSQL 16 (без розширень), numpy,
scikit-learn, websockets, httpx, PyJWT, APScheduler, Docker Compose. Точні версії — `uv.lock`.

## Швидкий старт

Репозиторій має лежати **не** в iCloud (наприклад `~/dev/fuzzhelm`), бо синхронізація псує volume PostgreSQL.

```bash
cp .env.example .env                      # заповнити самостійно; для локального стенда достатньо значень за замовчуванням
docker compose up -d db                   # PostgreSQL 16 на порту хоста 5442
uv sync --frozen                          # залежності точно з uv.lock
uv run alembic upgrade head               # ревізії 0001…0004, роль fuzzhelm_app, append-only журнал і аудит
uv run fuzzhelm backfill --days 45        # 45 повних UTC-днів 1m-свічок BTCUSDT, ETHUSDT (+ data/dataset_window.json)
uv run fuzzhelm fetch-funding             # історія ставок фандингу за вікном
uv run fuzzhelm db-stats                  # стан БД
uv run uvicorn fuzzhelm.api.main:app --port 8000     # API: http://127.0.0.1:8000/docs, перевірка — /healthz
uv run python -m fuzzhelm.workers.trading_worker --profile replay --speed inf --no-db   # офлайн-реплей записаної сесії
```

Без `.env` типова адреса БД — `postgresql+asyncpg://fuzzhelm:fuzzhelm@localhost:5442/fuzzhelm` (`fuzzhelm.config`). Офлайн-реплей
не потребує ні БД, ні мережі: 45 свічок, `equity_hash d0f45aa3…`.

У контейнерах: `docker compose up -d --build db api worker` (api — `127.0.0.1:8000`, worker — реплей профілю `replay`).
Сервіс `ui` винесено в профіль `ui`, веб-панель ще не реалізовано. Докладно — [`docs/manuals/deployment.md`](docs/manuals/deployment.md).

Перший адміністратор API створюється вручну. Пароль вводиться двічі без відлуння і ніколи не передається в argv:

```bash
uv run fuzzhelm user add --login <логін> --role admin
```

## Команди CLI (`uv run fuzzhelm …`)

| Команда | Що робить |
|---|---|
| `backfill [--days 45] [--symbols BTCUSDT,ETHUSDT] [--end-date YYYY-MM-DD] [--dry-run]` | REST-добір 1m-свічок із перекриттям в один бар, фіксоване вікно в `data/dataset_window.json` |
| `fetch-funding [--dry-run]` | історія ставок фандингу → `data/funding_<SYMBOL>.json` |
| `crosscheck [--from-input data/crosscheck_input.json.gz]` | крос-звірка Binance BTC-USDT-PERP ↔ Kraken BTC-USD-SPOT (поріг 50 б.п.) |
| `calibrate [--symbol BTCUSDT] [--is-days 15] [--no-write]` | калібрування функцій належності T (перцентилі) і V (KMeans) на першому IS-вікні |
| `replay-gap` | реплей патологічної сесії `gap` у БД зі справжнім REST-добором (рядки `ingest_gap` FILLED) |
| `verify-journal [--run-id UUID]` | перерахунок ланцюга хешів журналу подій і звірка з паспортом прогону |
| `db-stats [--json]` | кількість рядків 15 таблиць, покриття свічок, статуси прогалин |
| `user add \| list \| set-role` | користувачі API (ролі operator, analyst, auditor, admin), кожна дія — в `audit_log` |

Інші точки входу: `python -m fuzzhelm.workers.trading_worker --profile replay|paper`,
`python -m fuzzhelm.workers.ingest_worker [--symbols …] [--minutes N]`, `python -m fuzzhelm.scheduler.jobs`,
`python -m fuzzhelm.backtest.runner [--db SYMBOL] [--report]`, `scripts/run_backtest.py`, `scripts/demo_flash_crash.py`,
`scripts/run_all_experiments.sh` (фаза 7). Живий testnet-ордер `scripts/testnet_one_order.py --confirm` запускає лише людина з
ключами testnet у `.env`. Повний опис — [`docs/api/cli.md`](docs/api/cli.md), [`docs/manuals/user_guide.md`](docs/manuals/user_guide.md).

## Цілі Makefile

Розгортання будь-якої цілі без виконання — `make -n <ціль>`. Параметри: `make grid SYMBOL=ETHUSDT WORKERS=4`.

| Ціль | Команда |
|---|---|
| `make up` / `make down` | `docker compose up -d --build` / `docker compose down` |
| `make migrate` | `uv run alembic upgrade head` |
| `make ingest` | `uv run python -m fuzzhelm.cli backfill --days 45` |
| `make record` | `scripts/record_ws_session.py --minutes 45` (запис живої WS-сесії) |
| `make replay` | `python -m fuzzhelm.workers.trading_worker --profile replay` |
| `make backtest` | `scripts/run_backtest.py --symbol $(SYMBOL)` (прогін фази 6 з записом у БД) |
| `make grid` / `make walkforward` | сітка 108 клітинок / walk-forward (обидва ядра), `--db --workers $(WORKERS)` |
| `make experiments` | `scripts/run_all_experiments.sh` — повний прогін фази 7 (≈ години) |
| `make verify` | `python -m fuzzhelm.cli verify-journal` |
| `make test` | `uv run pytest -q -n 6` (pytest-xdist; unit, property, arch, e2e — офлайн) |
| `make test-int` | тестова БД `docker-compose.test.yml` (порт 5443) + `pytest -m integration`, потім `down` |
| `make cov` | офлайн-покриття + таблиця й пороги пакетів (`tests.helpers.cov_packages`) |
| `make lint` | `ruff check src tests scripts` + `mypy` (`core`, `fuzzy`, `risk`) |
| `make audit` | `pip-audit --skip-editable` |
| `make report` | `scripts/export_report_tables.py --db …` → `docs/report_tables/` (застереження нижче) |
| `make anomaly` | навчання MLP-автокодувальника → `data/anomaly_mlp_$(SYMBOL).json` |
| `make users` | підказка, як створити користувача API (пароль вводить людина) |
| `make backup` / `make restore FILE=…` | `pg_dump -Fc` / `pg_restore --clean --if-exists` ([runbook](docs/manuals/backup_runbook.md)); перед першою копією — `mkdir -p backups` |

Застереження до `make report`: ціль передає два каталоги результатів з однаковим вмістом, і таблиці exp_search дублюються.
Команда, якою зібрано поточні `docs/report_tables/`, записана в [`docs/report_tables/index.md`](docs/report_tables/index.md) (FIN-02).

## Дані

- Вікно: `[2026-08-04T00:00Z, 2026-09-18T00:00Z)` — 45 повних UTC-днів, 64 800 закритих 1m-барів на символ (BTCUSDT, ETHUSDT),
  Binance USDⓈ-M. Машиночитна копія з хешами — `data/dataset_window.json`.
- Хеш свічок (BLAKE2b-256): BTCUSDT `41bc9f0b20a8d4d2…`, ETHUSDT `d9f11c7e9f1e718f…` (`docs/figures/backfill_report.md`).
- Перше IS-вікно (дні 1–15) — єдине, на якому калібруються функції належності (`cal-727167b30ad655a5`, `config/membership.yaml`;
  k = 3 за силуетом 0.3354).
- Ставки фандингу: `data/funding_BTCUSDT.json`, `data/funding_ETHUSDT.json` (по 135 записів). Моделі MLP:
  `data/anomaly_mlp_{BTCUSDT,ETHUSDT}.json`. Записані WS-сесії: `fixtures/ws/btcusdt_2026-09-18.jsonl.gz` (45 хв, 92 101 кадр),
  шість патологічних сценаріїв — у `fixtures/ws/pathological/`.

## Як відтворити прогін за `run_id`

1. Прочитати паспорт (приклад — чистий прогін фази 6):
   ```bash
   docker exec fuzzhelm-db-1 psql -U fuzzhelm -d fuzzhelm -c \
     "select id, kind, config_hash, dataset_hash, git_sha, seed, engine, equity_hash from run where id = '4e5be0de-a4b7-4ef1-b84e-4a563b248be3'"
   ```
   Прапорець незакомічених змін — `select value from run_metric where run_id = '<run_id>' and name = 'git_dirty'` (тут 0).
2. Перейти на той самий код: `git checkout <git_sha>`.
3. Перевірити дані: хеш свічок вікна має збігтися з `data/dataset_window.json` (скрипти звіряють його самі).
4. Повторити прогін тим самим профілем і seed: для профілю `backtest` — `uv run python scripts/run_backtest.py`. Якщо ідентичний
   прогін уже збережено, скрипт перебудовує звіт з нього і (типово, якщо не задано `--no-verify`) проганяє рушій поточним кодом,
   записуючи у звіт, чи відтворюються `equity_hash` і голова журналу. Для довільного символу — `uv run python -m fuzzhelm.backtest.runner --db <SYMBOL> --report`.
5. Перевірити журнал і якір: `uv run fuzzhelm verify-journal --run-id <run_id>` (очікувано «chain OK, anchor OK»).

Приклад відтворюваності: прогони `a848fa56…` (`b933802`) і `4e5be0de…` (`415acbd`) мають той самий `equity_hash 3b5009cd…`.
Будь-яке рішення прогону пояснює `GET /decisions/{id}/explain`: рушій перераховується з `run.config`, а блок `consistency`
показує збіг зі збереженим.

## Тести

```bash
make test                                          # uv run pytest -q -n 6: без integration/live/slow
docker compose -f docker-compose.test.yml up -d --wait
FUZZHELM_TEST_DATABASE_URL=postgresql+asyncpg://fuzzhelm:fuzzhelm@localhost:5443/fuzzhelm_test \
  uv run pytest -q -m 'integration or not integration' --cov=fuzzhelm --cov-report=term   # офіційне покриття
uv run python -m tests.helpers.cov_packages        # таблиця й пороги пакетів
docker compose -f docker-compose.test.yml down
```

Заміри на HEAD `415acbd`, Apple M3 ([`docs/report_tables/raw/test_runs.txt`](docs/report_tables/raw/test_runs.txt)):

| прогін | результат |
|---|---|
| `make test` (6 воркерів) | 979 passed за 12.83 с |
| послідовно `uv run pytest -q` | 979 passed, 55 deselected за 29.90 с |
| unit + property послідовно | 807 passed за 23.37 с |
| разом з інтеграційними (PostgreSQL 16) | 1034 passed за 108.17 с (979 + 54 integration + 1 slow) |
| покриття, об'єднаний прогін | **89.38 %**; fuzzy 99.4 %, risk 99.9 %, decision 98.7 %, sizing 100.0 % |
| покриття, офлайн (`make cov`) | 83.72 % |
| `make lint` | ruff — «All checks passed!», mypy — «Success: no issues found in 35 source files» |
| `uv run pip-audit` | «No known vulnerabilities found» |

У §10 брифінгу названо 133 тест-функції, у `tests/` є всі 133, від них 198 вузлів ([`docs/report_tables/test_groups.md`](docs/report_tables/test_groups.md)).

## Стан робіт

**Зроблено:** Python-бекенд (фази 0–8 брифінгу), обчислювальний експеримент фази 7 з результатами, добивання фази 10 у частині
коду (покриття, mypy, CI, MLP у контурі) і документації (ТЗ, реєстр ризиків, розходження, журнал, таблиці звіту).

**Не виконано** (причина; що потрібно і ким):

- веб-панель Vue (3 екрани, i18n) — порядок робіт D-06, спершу бекенд; автор реалізує окремим етапом, першою — `ExplainView`;
- живий testnet-ордер і скріншот — немає testnet-ключів; студент реєструється на Binance Futures Testnet, вписує ключі в `.env` і
  запускає `scripts/testnet_one_order.py --confirm`;
- деплой Fly.io — потрібна реєстрація; студент виконує кроки [`docs/manuals/deployment.md`](docs/manuals/deployment.md) §6 (`fly.toml` готовий);
- токен Telegram, перший адміністратор API, LOGIN-роль БД — креденшли вводить лише студент;
- скрінкаст демо і репетиція з вимкненим Wi-Fi — робить студент за сценарієм §15 брифінгу;
- ТЕО, WBS/Гант/сітьовий графік, IDEF0/BPMN, англомовний abstract, звіт (фаза 11) — артефакти автора для звіту.

## Документація

Карта всіх документів — [`docs/index.md`](docs/index.md). Головне:
[результати експериментів](docs/results.md) ·
[технічне завдання](docs/tz/technical_specification.md) ·
[розходження зі специфікацією](docs/deviations.md) ·
[журнал робіт](docs/journal.md) ·
[реєстр ризиків](docs/risk_register.md) ·
[таблиці звіту](docs/report_tables/index.md) ·
[модель даних](docs/db_schema.md) ·
[безпека](docs/security.md) ·
[керівництво користувача](docs/manuals/user_guide.md) ·
[розгортання](docs/manuals/deployment.md) ·
[API модулів](docs/api/).
