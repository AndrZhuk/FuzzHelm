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
Пише закриті свічки (src = WS), прогалини з REST-добором, журнал подій і Q завершених годин; знімок здоров'я конвеєра
— подією `health` для `/market/health` (поле `pipelines.ingest_worker`; шапка торгового воркера — у
`pipelines.trading_worker`). Ctrl-C або `--minutes` зупиняють воркер лише між записами: запис, що обробляється, завжди
доходить до БД, тож ланцюг журналу сесії лишається цілим (`uv run fuzzhelm verify-journal --run-id <journal_run_id>`). Нічні задачі (добір PARTIAL/UNFILLABLE, погодинний Q, щоденний звіт) —
`uv run python -m fuzzhelm.scheduler.jobs`.
