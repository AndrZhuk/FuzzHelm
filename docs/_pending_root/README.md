# FuzzHelm

> Чернетка кореневого `README.md` (лежить у `docs/_pending_root/` до завершення експериментів фази 7; переносить провідний
> розробник). English version — [`README.en.md`](README.en.md).

Сервіс агрегації біржових даних та автоматизації маржинальних торговельних операцій з нечітко-логічним ядром прийняття
рішень та ієрархічною підсистемою автоматичного контролю ризиків і лімітів.

> **М'яке пропонує, жорстке вирішує.** Нечітке ядро видає лише намір `u ∈ [−1; 1]`. Детермінований ризик-контур не має
> технічного способу підвищити експозицію: це властивість алгебри вердиктів, перевірена property-тестом
> (`test_risk_chain_never_increases_exposure`).

Проєктно-технологічна практика, кафедра АСУ ІКНІ НУ «Львівська політехніка», 2026. Автор: Андрій Жук. Версія 0.1.0.

## Безпека і відмова від відповідальності

- **Лише публічні read-only дані і paper/testnet-виконання.** Реальних коштів і mainnet немає за побудовою. `fuzzhelm.config`
  має жорсткий allowlist хостів: ордери йдуть лише на `testnet.binancefuture.com` / `demo-fapi.binance.com`, дані беруться лише з
  публічних хостів Binance і Kraken. Порушення зупиняє старт (`MainnetHostRejected`, тест `test_mainnet_host_is_rejected_by_config`).
  Типовий виконавець торгового циклу — симулятор `PaperBroker`.
- **Система не є інвестиційною рекомендацією.** Тема роботи — метод і перевірюваний стенд, а не прибутковість. Дефолтна
  стратегія на 45-денному вікні збиткова, і це чесно зафіксовано (`docs/deviations.md` §2, `docs/journal.md`).
- Секрети — лише в `.env` (у `.gitignore`; шаблон `.env.example` без жодного значення). Облікові записи, ключі testnet,
  токен Telegram створює людина: агент креденшлів не вводить.

## Архітектура коротко

```
REST (Binance klines/exchangeInfo/fundingRate, Kraken OHLC) ─┐
WS Binance: /market (kline, aggTrade, markPrice) + /public (depth20) ─┴→ IngestPipeline: нормалізація (Decimal),
   інваріанти, дедуплікація, прогалини → REST-добір, скор якості Q (AHP), MLP-аномалії (опційний скорер, у воркерах не підключено) → PostgreSQL + журнал з ланцюгом хешів
 → FeaturePipeline (інкрементально) → 6 детекторів (s, c) → консенсус T, R, V → Мамдані (45 правил як дані) → κ → u_final
 → тригер Шмітта → сайзер (vol-target, ATR-ризик, плече) → RiskGuard (6 лімітів + режим) → автомат NORMAL/WARNING/COOLDOWN/HALTED
 → OrderRouter → PaperBroker | BinanceTestnetVenue → Portfolio → паспорт прогону, /explain, SSE
```

- **Межа детермінізму:** пакети `core, features, detectors, fuzzy, decision, risk, sizing` не читають настінного годинника й не
  мають випадковості (AST-тест). Тому один і той самий `TradingLoop.step()` працює в бектесті, live/replay і в клітинках сітки.
- **Межа типів:** гроші, ціни й обсяги — лише `Decimal`/`NUMERIC(38,18)`. Конвертація у float і назад відбувається лише у двох
  точках (`features/convert.py`, `sizing/convert.py`).
- **Відтворюваність:** кожен прогін має паспорт (`config_hash`, `dataset_hash`, `git_sha`, `seed`, `engine`,
  `journal_head_hash`, `equity_hash`).
- Діаграми: [`docs/diagrams/`](../diagrams/) (компоненти, розгортання, statechart ризику, послідовності, ER, класи, діяльність).

Стек: Python 3.12 (`uv`), FastAPI, Pydantic 2, SQLAlchemy 2 async + asyncpg, Alembic, PostgreSQL 16 (без розширень), numpy,
scikit-learn, websockets, httpx, PyJWT, APScheduler, Docker Compose. Точні версії — `uv.lock`.

## Швидкий старт

Репозиторій має лежати **не** в iCloud (наприклад `~/dev/fuzzhelm`), бо синхронізація псує volume PostgreSQL.

```bash
cp .env.example .env                      # заповнити самостійно; для локального стенда достатньо значень за замовчуванням
docker compose up -d db                   # PostgreSQL 16 на порту хоста 5442
uv sync                                   # залежності з uv.lock
uv run alembic upgrade head               # ревізії 0001…0004, роль fuzzhelm_app, append-only журнал і аудит
uv run fuzzhelm backfill --days 45        # 45 повних UTC-днів 1m-свічок BTCUSDT, ETHUSDT (+ data/dataset_window.json)
uv run fuzzhelm fetch-funding             # історія ставок фандингу за вікном
uv run fuzzhelm db-stats                  # стан БД
uv run uvicorn fuzzhelm.api.main:app --port 8000     # API; документація — http://localhost:8000/docs
uv run python -m fuzzhelm.workers.trading_worker --profile replay --speed inf --no-db   # офлайн-реплей записаної сесії
```

У контейнерах: `docker compose up -d --build db api worker` (api — `127.0.0.1:8000`, worker — реплей профілю `replay`).
Сервіс `ui` винесено в профіль `ui`; веб-панель ще не реалізовано. Докладно: [`docs/manuals/deployment.md`](../manuals/deployment.md).

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

Інші точки входу:
`python -m fuzzhelm.workers.trading_worker --profile replay|paper`,
`python -m fuzzhelm.workers.ingest_worker [--symbols …] [--minutes N]`, `python -m fuzzhelm.scheduler.jobs`,
`python -m fuzzhelm.backtest.runner [--db SYMBOL] [--report]`, `scripts/run_backtest.py`, `scripts/demo_flash_crash.py`,
`scripts/run_all_experiments.sh` (фаза 7). Живий testnet-ордер `scripts/testnet_one_order.py --confirm` запускає лише людина з
ключами testnet у `.env`. Повний опис — [`docs/api/cli.md`](../api/cli.md), [`docs/manuals/user_guide.md`](../manuals/user_guide.md).

## Цілі Makefile

| Ціль | Команда |
|---|---|
| `make up` / `make down` | `docker compose up -d --build` / `docker compose down` |
| `make migrate` | `uv run alembic upgrade head` |
| `make ingest` | `uv run python -m fuzzhelm.cli backfill --days 45` |
| `make record` | `scripts/record_ws_session.py --minutes 45` (запис живої WS-сесії) |
| `make replay` | `python -m fuzzhelm.workers.trading_worker --profile replay` |
| `make backtest` | `scripts/run_backtest.py` |
| `make grid` | `scripts/run_grid.py` |
| `make verify` | `python -m fuzzhelm.cli verify-journal` |
| `make test` | `uv run pytest -q` (unit, property, arch, e2e — офлайн) |
| `make test-int` | тестова БД `docker-compose.test.yml` (порт 5443) + `pytest -m integration` |
| `make cov` | `pytest --cov --cov-report=term-missing` |
| `make lint` | `ruff check src tests scripts` + `mypy` (`core`, `fuzzy`, `risk`) |
| `make report` | `scripts/export_report_tables.py` |
| `make backup` / `make restore FILE=…` | `pg_dump -Fc` / `pg_restore --clean --if-exists` ([runbook](../manuals/backup_runbook.md)) |

## Дані

- Вікно: `[2026-08-04T00:00Z, 2026-09-18T00:00Z)` — 45 повних UTC-днів, 64 800 закритих 1m-барів на символ (BTCUSDT, ETHUSDT),
  Binance USDⓈ-M. Машиночитна копія з хешами — `data/dataset_window.json`.
- Хеш свічок (BLAKE2b-256): BTCUSDT `41bc9f0b20a8d4d2…`, ETHUSDT `d9f11c7e9f1e718f…` (`docs/figures/backfill_report.md`).
- Перше IS-вікно (дні 1–15) — єдине, на якому калібруються функції належності (`cal-727167b30ad655a5`, `config/membership.yaml`).
- Ставки фандингу: `data/funding_BTCUSDT.json`, `data/funding_ETHUSDT.json` (по 135 записів). Записані WS-сесії:
  `fixtures/ws/btcusdt_2026-09-18.jsonl.gz` (45 хв, 92 101 кадр), шість патологічних сценаріїв у `fixtures/ws/pathological/`.

## Як відтворити прогін за `run_id`

1. Прочитати паспорт:
   ```bash
   docker exec fuzzhelm-db-1 psql -U fuzzhelm -d fuzzhelm -c \
     "select id, kind, config_hash, dataset_hash, git_sha, seed, engine, equity_hash from run where id = '<run_id>'"
   ```
   Прапорець незакомічених змін — `select value from run_metric where run_id = '<run_id>' and name = 'git_dirty'`.
2. Перейти на той самий код: `git checkout <git_sha>`.
3. Перевірити дані: хеш свічок вікна має збігтися з `data/dataset_window.json` (скрипти звіряють його самі).
4. Повторити прогін тим самим профілем і seed: для профілю `backtest` — `uv run python scripts/run_backtest.py`. Якщо ідентичний
   прогін уже збережено, скрипт проганяє рушій поточним кодом і пише у звіт, чи відтворюються `equity_hash` і голова журналу.
   Для довільного символу — `uv run python -m fuzzhelm.backtest.runner --db <SYMBOL> --report`.
5. Перевірити журнал і якір: `uv run fuzzhelm verify-journal --run-id <run_id>` (очікувано «chain OK, anchor OK»).

Будь-яке рішення прогону пояснює `GET /decisions/{id}/explain`: рушій перераховується з `run.config`, а блок `consistency`
показує збіг зі збереженим.

## Тести

```bash
uv run pytest -q                                   # типовий відбір: без integration/live/slow
docker compose -f docker-compose.test.yml up -d --wait
FUZZHELM_TEST_DATABASE_URL=postgresql+asyncpg://fuzzhelm:fuzzhelm@localhost:5443/fuzzhelm_test \
  uv run pytest -q -m integration -p no:randomly
docker compose -f docker-compose.test.yml down
uv run pytest --collect-only -q                    # на HEAD b933802: 918/973 зібрано, 55 відібрано маркерами
```

Підсумок прогону й покриття — `<<TBD:final_test_run>>`, `<<TBD:final_coverage>>`.

## Стан робіт

Реалізовано Python-бекенд (фази 0–8 брифінгу), інструменти обчислювального експерименту фази 7 і документацію. Не реалізовано або
не виконано: веб-панель Vue (`<<TBD:ui_views>>`), деплой Fly.io (`<<TBD:fly_deploy>>`), живий testnet-ордер (немає ключів,
`<<TBD:testnet_one_order>>`). Результати фази 7: `<<TBD:phase7_results>>`.

## Документація

Карта всіх документів — [`docs/index.md`](../index.md). Головне:
[технічне завдання](../tz/technical_specification.md) ·
[розходження зі специфікацією](../deviations.md) ·
[журнал робіт](../journal.md) ·
[реєстр ризиків](../risk_register.md) ·
[модель даних](../db_schema.md) ·
[безпека](../security.md) ·
[керівництво користувача](../manuals/user_guide.md) ·
[розгортання](../manuals/deployment.md) ·
[API модулів](../api/) ·
[CHANGELOG](CHANGELOG.md).

> Відносні посилання в цій чернетці вказують із `docs/_pending_root/`. Після перенесення в корінь репозиторію заміни `../` на
> `docs/`.
