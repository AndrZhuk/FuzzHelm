# Керівництво користувача FuzzHelm

Автор: Андрій Жук, 2026. Для оператора, аналітика, аудитора й адміністратора стенда. Система **не є інвестиційною
рекомендацією**, працює лише з публічними read-only даними, виконання — симульоване (`PaperBroker`); шляху до mainnet
немає за побудовою (allowlist хостів, тест `test_mainnet_host_is_rejected_by_config`).

Усі команди — з кореня репозиторію `~/dev/fuzzhelm`; `uv` встановлює залежності з `uv.lock`. Розгортання і змінні
оточення — `docs/manuals/deployment.md`, резервні копії — `docs/manuals/backup_runbook.md`.

## 1. Перед першим запуском

1. БД піднята й мігрована (`docker compose up -d db`, `uv run alembic upgrade head`).
2. Історія свічок і специфікації інструментів: `uv run fuzzhelm backfill --days 45` (≈ 41 с, пише `data/dataset_window.json`),
   ставки фандингу: `uv run fuzzhelm fetch-funding`. Стан БД: `uv run fuzzhelm db-stats`.
3. Сценарій стрес-тесту вже в репозиторії (`fixtures/ws/scenarios/flash_crash.jsonl.gz`); перевірити, що він відтворюється
   побайтово: `uv run python scripts/make_demo_scenario.py --check`.
4. Модель аномалій (QualityGate §4.1) вже в репозиторії: `data/anomaly_mlp_BTCUSDT.json` — MLP-автокодувальник 5-3-5,
   навчений на днях 1–15 BTCUSDT (ті самі бари, що й калібрування МФ). Це JSON, а не pickle: його можна прочитати й
   порівняти в git. Перенавчити (≈ 10 с, лише SELECT з БД) і заново заміряти ROC-AUC:
   `make anomaly` (= `uv run python scripts/train_anomaly_mlp.py`; пише `docs/figures/quality_mlp_rocauc.md` і артефакт).
   Для ETHUSDT моделі немає: воркери пишуть попередження і працюють для ETH без MLP (`make anomaly SYMBOL=ETHUSDT`
   навчив би її на днях 1–15 ETHUSDT з окремим звітом `quality_mlp_rocauc_ETHUSDT.md`).

## 2. Реплей записаної сесії (демо, офлайн)

```bash
uv run python -m fuzzhelm.workers.trading_worker --profile replay            # темп ×30 з профілю, пише в БД + SSE
uv run python -m fuzzhelm.workers.trading_worker --profile replay --speed inf --no-db   # без БД, лише підсумок
```
Воркер прогріває ознаки 523 барами історії строго до першої свічки сесії (з БД; якщо їх там немає — з записаних рядків
REST `fixtures/rest/binance_klines.json.gz`, які при цьому записуються в БД), далі відтворює 45-хв сесію
`fixtures/ws/btcusdt_2026-09-18.jsonl.gz` через той самий конвеєр інжесту й той самий торговий цикл, що й бектест.
Кожна закрита свічка — рішення з повним трасуванням у `decision`, ордери, позиції, записи ризик-ланцюга, точка капіталу
і події SSE (`/stream/live`). Шапка LiveView приходить подією `health`:
`MODE: PAPER · FEED: REPLAY · NO MAINNET KEYS · SEED 20260918 · Q=0.98`.

Кожна закрита свічка ще й отримує скор MLP-автокодувальника (екстрактор ознак прогрівається тими самими 523 барами):
скор пишеться в `candle.anomaly_score`, аномалія (скор > q₉₉ навчання = 2.865…) зменшує `validity` скору якості Q своєї
години (§5.17), а подія `health` показує `anomaly_scored`, `anomalies` і модель. `--no-anomaly` вимикає скорер.

Вивід — JSON-підсумок (run_id, кількість барів і виконань, фінальний режим ризику, `equity_hash`, голова журналу).
На записаній сесії профіль replay (поріг входу 0.20, `docs/deviations.d/workers.md` W-03) дає одну угоду: вхід о 19:32,
вихід о 19:35 UTC, −7.28 USDT; з порогом брифінгу 0.25 угод немає (`--param u_enter=0.25`). Режим «Replay ×30» —
`--speed 30` (за замовчуванням з профілю), `--speed inf` — без пауз.

Живий paper-режим (публічний WS Binance, виконання — PaperBroker): `--profile paper --minutes 30` (потрібна мережа;
історія для прогріву за потреби добирається публічним REST).

## 3. Демо «ризик-контур стримує сам себе» (flash_crash → HALTED)

```bash
uv run python scripts/demo_flash_crash.py                     # офлайн, у пам'яті: таблиця барів, переходи, спроби зняття
uv run python scripts/demo_flash_crash.py --db --speed 30 --linger 600   # через воркер у БД, з очікуванням адміністратора
```
Сценарій: на межі хвилини 19:33:00, коли стратегія тримає лонг 0.066 BTC, ціна розривається на −6.5 % (далі до −11 % і
відновлення до −3 %). Фактична поведінка (виміряно): стоп виконується за ціною відкриття бару розриву, денний збиток
−3.52 % ≥ порогу −3 % → **NORMAL → HALTED** без участі людини; наступні 17 барів — HALTED, жодного виконання («Continue» не
працює). Зняти засувку може лише адміністратор:
```bash
TOKEN=$(curl -s -X POST localhost:8000/auth/login -d "username=<admin>&password=<пароль>" | jq -r .access_token)
curl -X POST localhost:8000/risk/killswitch/release -H "Authorization: Bearer $TOKEN" \
     -H "Content-Type: application/json" -d '{"reason": "demo review"}'
```
API пише `audit_log` і надсилає команду воркеру (канал `fuzzhelm_control`), воркер знімає HALTED → COOLDOWN
(reduce-only) і пише власний аудит (стан до/після) та запис `risk_event` переходу з актором. Запит від ролі не-admin
API відхиляє з 403; навіть підроблений запис в `audit_log` воркер перевіряє за роллю з `app_user` і відхиляє.
Чому послідовність не «4 % → 8 % → 12 %», як у сценарії брифінгу, і чому в цій сцені немає запису VETO MaxDailyLoss —
`docs/deviations.d/workers.md` W-04. Сам запис «MaxDailyLoss · VETO · observed · limit» система пише, коли після
денного збитку ≥ 2 % є намір входу — його видно в першому повному бектесті (82 записи за 2026-08-04…06, перший:
observed −2.058 %, limit −2.00 %):
```bash
docker exec fuzzhelm-db-1 psql -U fuzzhelm -d fuzzhelm -c "select ts, rule, verdict, observed, limit_value from risk_event
  where run_id = '89619416-720b-4880-b4e4-26d2c74bca68' and rule = 'max_daily_loss' and verdict = 'VETO' order by ts limit 3"
```
Через API ці ранні записи довгого прогону досяжні напряму (хвиля 3, PLAT-03): `GET /risk/events?run_id=<id>&only=veto&
rule=max_daily_loss&until_ns=1786060800000000000` (до 2026-08-07) або гортанням усього журналу курсором `next_cursor`
(сторінки ≤ 2 000). Перевірено на робочій БД: прогін `89619416…` має 26 979 записів `risk_event` — 14 сторінок, кожен
запис рівно раз; вибірка з `until_ns` дала ті самі 82 VETO `max_daily_loss`, перший — observed −2.058 %.
Аудит зняття: запис запиту API (`risk.killswitch.release`, автор — admin) і два записи воркера (`killswitch.release`,
`risk.release` — стан до/після) з тим самим `user_id`, настінним часом застосування і `after.request_audit_id`.

## 4. Бектест

```bash
uv run python scripts/run_backtest.py                          # BTCUSDT, профіль backtest, 45 днів з БД
uv run python scripts/run_backtest.py --symbol ETHUSDT
```
Скрипт звіряє свічки з `data/dataset_window.json`, запускає рушій (Мамдані, seed профілю), пише прогін з повним
паспортом (`config_hash`, `dataset_hash`, `git_sha` + прапорець `git_dirty`, `seed`, `engine`, `journal_head_hash`,
`equity_hash`) і будує `docs/figures/first_run_metrics.md` та `first_run_equity.png` з рядків БД. Ідентичний прогін
(та сама п'ятірка ідентичності) повторно не пишеться — звіт перебудовується з наявного. Через API:
`POST /backtests {symbol, ts_from_ns, ts_to_ns, engine?, seed?, params?}` → 202 з `run_id`, стан — `GET /runs/{id}`.

`git_dirty = 1` у паспорті означає незакомічені зміни **коду** — усього, крім `docs/` і `artifacts/` (одне визначення для
`run_backtest.py`, POST /backtests, торгового воркера і скриптів експериментів; `backtest.manifest.read_git_state`).
Редагування документації чи виводи попереднього кроку прогін «брудним» не роблять; список змінених файлів коду
скрипт друкує, а воркер пише в `session.start` журналу (`git_dirty_paths`).

Обчислювальний експеримент фази 7 (усе — з БД, лише закомічений код для `--persist commit`):
```bash
make grid                     # 108 клітинок + OOS, фронт Парето, PSR/DSR   (SYMBOL=ETHUSDT, WORKERS=4 — параметри)
make walkforward              # walk-forward 15/5/5 × 6 з embargo, Мамдані і лінійна база
make experiments              # увесь оркестратор scripts/run_all_experiments.sh (години; підсумок — artifacts/exp_logs)
make report                   # таблиці звіту docs/report_tables/*.md з виводів і фактів БД
```

Відтворюваність будь-якого числа: `psql -c "select id, config_hash, git_sha, seed, equity_hash from run order by
started_at desc limit 3"` і `uv run fuzzhelm verify-journal --run-id <id>` (ланцюг хешів + звірка з паспортом).

## 5. Вхід в API і пояснення рішення

Перший адміністратор створюється людиною **[ЛЮДИНА]** (агент креденшлів не вводить і користувачів у робочій БД не
створює). Пароль вводиться з клавіатури двічі, без відлуння, і ніколи не передається в аргументах командного рядка:
```bash
uv run fuzzhelm user add --login <логін> --role admin           # ≥ 8 символів, ≤ 72 байти, ≠ логіну
uv run fuzzhelm user list                                        # id, логін, роль, час створення (без хешів)
uv run fuzzhelm user set-role --login <логін> --role analyst     # останнього admin понизити не можна
```
У скриптах пароль передається через stdin: `printf '%s\n' "$PW" | uv run fuzzhelm user add --login <логін> --role
analyst --password-stdin`. Кожна команда пише `audit_log` (`user.create`, `user.set_role`, `user.list`, актор `cli`);
пароль і його bcrypt-хеш не друкуються й в аудит не потрапляють (`docs/api/cli.md`). У контейнерах compose:
`docker compose run --rm -it api fuzzhelm user add --login <логін> --role admin`.
Ролі: `operator` (зміна стратегій, бектести), `analyst` (читання, бектести), `auditor` (читання + `/audit`), `admin`
(ліміти ризику, зняття kill-switch). Матриця доступу — `docs/api/api.md` §1.

```bash
TOKEN=$(curl -s -X POST localhost:8000/auth/login -d "username=<login>&password=<пароль>" | jq -r .access_token)
curl -s localhost:8000/runs?kind=replay -H "Authorization: Bearer $TOKEN"            # прогони
curl -s "localhost:8000/risk/state" -H "Authorization: Bearer $TOKEN"                # режим ризику live-прогону
curl -s "localhost:8000/decisions/<id>/explain" -H "Authorization: Bearer $TOKEN"    # формальне виведення
```
`/decisions/{id}/explain` — ядро демо: входи T/R/V, ступені належності, спрацьовані правила за спаданням α з
українським текстом правила, агрегована фігура μ_agg і центроїд, κ, розкладка сайзера з `binding_constraint`, вердикт
ризик-ланцюга і україномовне трасування. id рішень — з події SSE `decision` або `GET /runs/{id}`. Інтерактивна
документація — `http://localhost:8000/docs` (кнопка **Authorize**).

## 6. Живий інжест

```bash
uv run python -m fuzzhelm.workers.ingest_worker --symbols BTCUSDT,ETHUSDT            # до Ctrl-C
uv run python -m fuzzhelm.workers.ingest_worker --minutes 1.5                         # димовий прогін
```
Пише закриті свічки (src = WS) разом з їх MLP-скором (`candle.anomaly_score`), прогалини з REST-добором, журнал подій
і Q завершених годин (аномалії — у `anomaly_count` і `validity` рядка `dq_score`); знімок здоров'я конвеєра — подією
`health` для `/market/health` (поле `pipelines.ingest_worker`: `anomalies`, `anomaly_scored`, `anomaly_model`; шапка
торгового воркера — у `pipelines.trading_worker`). Перед першою свічкою скорер прогрівається 218 попередніми барами з
БД (яких бракує — публічним REST); без файлу моделі інструмента — попередження в журналі й робота без MLP. Сторож тиші
WebSocket рахує тишу монотонним годинником: крок NTP системного годинника не рве здорове з'єднання (WS-07). Ctrl-C або `--minutes` зупиняють воркер лише між записами: запис, що обробляється, завжди
доходить до БД, тож ланцюг журналу сесії лишається цілим (`uv run fuzzhelm verify-journal --run-id <journal_run_id>`). Нічні задачі (добір PARTIAL/UNFILLABLE, погодинний Q, щоденний звіт) —
`uv run python -m fuzzhelm.scheduler.jobs`.

## 7. Make-цілі

| ціль | що робить |
|---|---|
| `make test` / `make cov` / `make lint` / `make audit` | тести (офлайн) / покриття (поріг 80 % з `pyproject.toml`) / ruff + mypy / pip-audit |
| `make test-int` | інтеграційні тести на тестовій БД 5443 (піднімає і гасить `docker-compose.test.yml`) |
| `make backtest` · `make grid` · `make walkforward` · `make experiments` · `make report` | бектест, сітка, walk-forward, увесь оркестратор фази 7, таблиці звіту (`SYMBOL=…`, `WORKERS=…`) |
| `make anomaly` | перенавчання MLP-моделі аномалій + звіт ROC-AUC |
| `make replay` · `make ingest` · `make record` · `make verify` | реплей сесії, 45-денний добір, запис WS-сесії, перевірка ланцюгів журналу |
| `make users` | лише підказка, як створити користувача API (пароль вводить людина) |
| `make ui` · `make ui-build` · `make ui-check` | веб-панель: dev-сервер Vite на 5173 · продакшн-збірка · перевірка типів (vue-tsc) |
| `make screens UI_LOGIN=… UI_PASSWORD=…` | 14 екранограм панелі у `docs/figures/screens/` (потрібні API, Vite і воркер реплею) |
| `make up` / `down` / `migrate` / `backup` / `restore FILE=…` | стенд Docker Compose, міграції, резервні копії |

Перевірити, що виконає ціль, не запускаючи її: `make -n <ціль>`.


## 8. Веб-панель

`make ui` підіймає Vite на `http://localhost:5173`. Панель ходить до API через проксі `/api`, адреса бекенда —
змінна `FUZZHELM_API` (типово `http://127.0.0.1:8000`, у контейнері `http://api:8000`), тож CORS не потрібен.
Інтерфейс українською; перемикач `UK/EN` — у шапці, вибір запам'ятовується в браузері.

**Вхід.** Потрібен користувач API (`uv run fuzzhelm user add --login <логін> --role admin|operator|analyst|auditor`).
На цій машині створено чотирьох користувачів **локального стенда**: `demo_admin`, `demo_operator`, `demo_analyst`,
`demo_auditor` зі спільним паролем `fuzzhelm-demo-2026`. Це стендові креденшли для демонстрації на цій робочій станції
(БД теж локальна, `fuzzhelm:fuzzhelm` на 5442). **Перед будь-яким показом поза цією машиною — створіть власних
користувачів і видаліть демонстраційних** (`fuzzhelm user list`, `fuzzhelm user add`).

**Три екрани.**

1. **Виведення** (`/explain/{id}`) — кульмінація демонстрації. Сім пронумерованих кроків згори вниз: входи T/R/V із трьома
   графіками функцій належності (вертикаль — поточне значення, заштриховані активні терми) → таблиця спрацьованих правил
   за спаданням α → агрегована фігура μ_agg(u) з центроїдом → ланцюг `u_raw × κ = u_final` → розкладка сайзера з
   `binding_constraint` → ризик-ланцюг «запитано → затверджено» → україномовне трасування і підсумок. Угорі — паспорт
   рішення і звірка перерахунку зі збереженим слідом. Номер рішення вводиться у полі «Рішення №».
2. **Онлайн** (`/live`) — свічки, шість детекторів (сила `s` і довіра `c`), вихід нечіткого ядра, режим ризику, журнал
   відхилених ордерів, здоров'я конвеєра і потік подій. Наповнюється з SSE, тож потрібен запущений воркер:
   `uv run python -m fuzzhelm.workers.trading_worker --profile replay`. Зняти `HALTED` може лише роль `admin` і лише
   вказавши причину — вона йде в `audit_log` разом зі станом до/після.
3. **Бектест** (`/backtest`) — список прогонів і запуск нового, крива капіталу з просадкою і смугою режимів ризику,
   паспорт відтворюваності (`git_sha`, `config_hash`, `dataset_hash`, `equity_hash`, `journal_head_hash`) і 29 метрик.
   Вкладка «Редактор правил» стартує з робочих `config/membership.yaml` і `config/rules_mamdani.yaml`, перевіряє YAML у
   браузері й віддає їх на семантичну перевірку серверу; кожне збереження створює нову версію стратегії. Сервер відхиляє
   неповну базу правил (потрібні всі 5×3×3 = 45 комбінацій антецедента) з переліком незакритих комбінацій.

**Що робити, якщо панель порожня.** «Немає зв'язку з API» — не запущено `uvicorn`. «Даних ще немає» на «Онлайн» — не
запущено воркер реплею. «Прогонів ще немає» — виконайте `make backtest`. Порожній графік якості Q — погодинний скор для
цього вікна ще не рахували (нічна задача `fuzzhelm.scheduler.jobs`).
