# CLI `fuzzhelm` (фактичний, хвиля 2)

Точка входу `fuzzhelm = fuzzhelm.cli:main` (`pyproject.toml`, `[project.scripts]`), розбір аргументів —
`argparse`, асинхронний код — `asyncio.run`. Скрипти `scripts/{backfill,calibrate_mf,crosscheck_report,
fetch_funding,replay_gap_to_db}.py` — тонкі обгортки, що передають аргументи відповідній підкоманді.
Автор: Андрій Жук, 2026.

```bash
uv run fuzzhelm [--database-url URL] COMMAND [опції]      # URL за замовчуванням: FUZZHELM_DATABASE_URL / .env
```

**Мережа.** Команди звертаються лише до read-only хостів з allowlist. Базові URL перевіряє `Settings`, а
клієнти ще раз — через `assert_readonly_url`. Ордерних ендпоінтів CLI не має.
**БД.** Використовується роль застосунку `fuzzhelm_app` (`make_engine(url, role=APP_ROLE, null_pool=True)`),
схема має бути мігрована щонайменше до `0003_auth_audit`. Кожна зміна стану — окрема транзакція (`session_scope`).
**`--dry-run`** (у `backfill`, `crosscheck`, `fetch-funding`, `replay-gap`, а також у `calibrate` з вікна БД)
друкує план і не створює ні HTTP-клієнта, ні engine БД. `calibrate --from-fixture --dry-run` рахує
калібрування, але файлів не пише.

## Коди виходу

| код | коли |
|---|---|
| 0 | успіх (для `backfill`: gate фази 1 `candle ≥ 60 000` виконано; для `replay-gap`: є FILLED, `zero_loss`, ланцюг цілий; для `verify-journal`: усі ланцюги цілі, якорі збігаються) |
| 1 | команда відпрацювала, але gate/перевірку не пройдено |
| 2 | помилка користувача або даних (`CliError`: немає вікна датасету, немає інструмента в БД, неповне вікно, вікно в майбутньому; для `user`: слабкий пароль, розбіжність паролів, немає термінала без `--password-stdin`, дубль логіна, невідомий логін, пониження останнього admin, помилка СУБД — лише клас і SQLSTATE) чи помилка розбору аргументів argparse |

## Підкоманди

### `backfill` — N повних UTC-днів 1m-свічок у PostgreSQL
```
--days 45  --symbols BTCUSDT,ETHUSDT  --end-date YYYY-MM-DD (кінець виключно; за замовчуванням —
північ поточного UTC-дня за часом біржі)  --limit 1500 (2…1500)  --report docs/figures/backfill_report.md
--window-json data/dataset_window.json  --timeout 15  --dry-run
```
Послідовність така. Зсув годинника визначається за 3 замірами `/time` (мінімальний RTT), далі береться
`exchangeInfo`, і `InstrumentRepo.upsert` записує інструменти. `ingest.backfill.backfill_klines` качає дані
з перекриттям в 1 бар і посторінково передає їх у `CandleRepo.upsert` (COPY). Знайдені прогалини
відкриваються в `GapRepo` (OPEN → FILLING), `fill_gaps` добирає бари, а рядок отримує кінцевий статус.
Після цього перераховуються `load_arrays`, `dataset_hash` і `find_gaps` у вікні, і пишуться звіт та
`data/dataset_window.json`. Прогін 2026-09-18 завантажив 2 × 64 800 барів за 40.8 с, 88 запитів klines,
вага 884 разом з `/time` і `exchangeInfo`, прогалин 0 (`docs/figures/backfill_report.md`).

### `crosscheck` — Binance BTC-USDT-PERP ↔ Kraken BTC-USD-SPOT
```
--threshold-bps 50  --report docs/figures/crosscheck_report.md  --figure docs/figures/crosscheck_divergence.png
--save-input data/crosscheck_input.json.gz  --from-input PATH (офлайн, без мережі)  --timeout 15  --dry-run
```
Команда бере останні ≤ 720 закритих 1m-барів Kraken (обмеження `/0/public/OHLC`) і klines Binance за той
самий час. Розбіжність рахує `ingest.crosscheck`, а `indexPriceKlines` Binance разом із Kraken `USDTUSD`
дають розклад на премію перпетуала, дисконт USDT і залишок. З `--from-input` звіт перераховується офлайн
побайтово (перевірено 2026-09-19). `--dry-run --from-input` друкує статистику JSON.

### `fetch-funding` — історія ставок фінансування
```
--symbols (за замовчуванням — символи вікна)  --window-json data/dataset_window.json  --out-dir data  --timeout 15  --dry-run
```
Запит `GET /fapi/v1/fundingRate` за `[start_ms, end_ms − 1]` вікна з пагінацією за часом. Результат пишеться
в `data/funding_<SYMBOL>.json` (формат — `docs/api/data.md`). Прогін 2026-09-19 в scratch-каталог дав
135 записів на символ, 1 запит на символ, рядки побайтово збіглися з закоміченими.

### `calibrate` — МФ T/V на ПЕРШОМУ IS-вікні
```
--symbol BTCUSDT  --window-json data/dataset_window.json  --is-days 15 (з config/profiles/backtest.yaml)
--trim-end-bars 0 (перевірка чутливості: без останніх N барів IS)  --seed (FUZZHELM_SEED)
--membership [config/membership.yaml]  --manifest [data/calibration_manifest.json]
--report [docs/figures/calibration_report.md]  --figure [docs/figures/regimes_clusters.png]
--from-fixture PATH (офлайн-бари)  --no-write  --dry-run
```
Бари днів 1–15 з БД (вимагає повного вікна: 21 600 закритих барів) проходять `calibration_series`
(конвеєр ознак → 6 детекторів → консенсус), потім `regimes.calibrate` (KMeans k=3 для V, силует для
k=2…6, T-перцентилі {8,25,50,75,92} симетризованої вибірки, σ за правилом `cover`). Далі пишуться
`membership.yaml` (`provisional: false`, `source_run_id = cal-…`), маніфест, звіт і рисунок. Повтор із тими
самими даними дає той самий id і той самий YAML побайтово.

**Куди пише (`cli.resolve_calibration_outputs`).** Чотири робочі артефакти в квадратних дужках — одне
узгоджене ціле: `source_run_id` у YAML = id маніфесту (gate фази 3). Тому шляхи за замовчуванням пише
**лише** калібрування з БД без `--no-write`. `--from-fixture` і `--no-write` пишуть тільки ті з
`--membership/--manifest/--report/--figure`, що задані явно; без жодного друкують числа й
`no files written`. `--no-write` не пише YAML навіть за явним шляхом. Перевірка відтворюваності без
ризику для закомічених файлів: `uv run fuzzhelm calibrate --no-write --manifest /tmp/m.json`.

### `replay-gap` — реальні рядки `ingest_gap` зі статусом FILLED
```
--session fixtures/ws/pathological/gap.jsonl.gz  --reference fixtures/ws/sample_btcusdt_4m.jsonl.gz
--report docs/figures/backfill_gap_replay.md  --timeout 15  --dry-run
```
Команда відтворює сесію через `IngestPipeline` із БД-стоками: `GapRepo` на кожну зміну стану прогалини,
`CandleRepo`, а події йдуть у `event_journal`. Добір робить **справжній** `BinanceRestClient`. `run_id`
журналу — `uuid5(sha256 файлу)`, тож повторний запуск — ідемпотентний no-op. Прогін 2026-09-18 дав 3 рядки
FILLED, 4 REST-запити, `zero_loss = True` і 6 801 запис журналу. `detected_at`/`closed_at` рядків — віртуальний
час кадрів реплею (`FrameClock`), тож у БД вони збігаються; звіт це зазначає.

### `verify-journal` — перерахунок ланцюга хешів
```
--run-id UUID (за замовчуванням — усі run_id у event_journal)
```
Для кожного run рахує кількість записів, першу зіпсовану `seq` (`JournalRepo.verify`) і голову ланцюга. Якщо
є рядок `run` з `journal_head_hash`, звіряє з ним голову як із зовнішнім якорем. Приклад 2026-09-19:
`7d441046-…: 6801 entries, head 3ef051408da8fa01…, chain OK, no run row`.

### `db-stats` — стан БД
```
--json
```
Показує кількість рядків у 15 таблицях, покриття свічок за інструментом, tf і src, статуси `ingest_gap` і
журнали. Приклад 2026-09-19: `candle=129603`, `ingest_gap` = 3 × FILLED, `event_journal=6801`.

### `user` — користувачі API (add / list / set-role), хвиля 3 (PLAT-01)
```
fuzzhelm user add --login L --role operator|analyst|auditor|admin [--password-stdin]
fuzzhelm user list [--json]
fuzzhelm user set-role --login L --role R
```
* **Пароль ніколи не приходить з argv** (прапорця `--password` немає: він був би видний у `ps` та історії оболонки).
  Без `--password-stdin` — `getpass` двічі (без відлуння; розбіжність → exit 2); якщо термінала немає, команда
  відмовляє з підказкою `--password-stdin`, а не читає stdin мовчки. З `--password-stdin` — перший рядок stdin
  (зрізається лише кінцеве `\n`/`\r\n`); stdin має бути каналом (pipe/файл): якщо це термінал, команда відмовляє
  (exit 2), бо `readline()` з термінала показав би набраний пароль на екрані. Скорочення прапорців у `user add|list|
  set-role` вимкнено (`allow_abbrev=False`): `--password` чи `--pass` — помилка розбору, а не мовчазне
  `--password-stdin`. Пароль читається й перевіряється **до** підключення до БД.
* Правила пароля (`check_new_password`): ≥ 8 символів, ≤ 72 байтів UTF-8 (bcrypt мовчки обрізав би решту), без
  керівних символів, не дорівнює логіну (без урахування регістру). Логін — `^[A-Za-z0-9][A-Za-z0-9_.@-]{0,63}$`.
  Повідомлення про помилки пароль не містять.
* Хеш — bcrypt (вартість 12) через `UserRepo.create`; у БД, виводі, журналах і `audit_log` немає ні пароля, ні хеша
  (`user list` друкує лише `id, login, role, created_at`).
* Кожна дія пише `audit_log` у **тій самій транзакції**, що й зміна: `user.create` (after = `{id, login, role}`),
  `user.set_role` (before/after = роль), `user.list` (after = `{count}`); `user_id = NULL` (дію виконав не користувач
  API), `after._actor = {"login": "cli", "role": null, "os_user": <обліковий запис ОС або null>}`, `target =
  "user/<login>"`. `set-role` на ту саму роль — «nothing changed», без запису.
* Останнього `admin` понизити не можна (exit 2): інакше ліміти ризику й зняття kill-switch стали б недоступні.
  Інваріант тримається й під конкуренцією: пониження admin блокує рядки всіх адміністраторів до COMMIT
  (`UserRepo.admins_for_update()`, `SELECT … FOR UPDATE ORDER BY id`), тож два паралельні `set-role` не можуть обидва
  побачити «лишається ще один admin»; якщо роль цілі змінила інша транзакція, поки ця чекала, — exit 2
  «changed concurrently», без зміни й без аудиту.
* Дубль логіна — exit 2 «already exists» (перевірка до INSERT; гонку ловить `UNIQUE(login)` СУБД, і тоді назовні
  йде лише «already exists», без SQL і параметрів драйвера).
* Роль СУБД — `fuzzhelm_app` (як в усіх командах CLI): INSERT/UPDATE `app_user`, лише INSERT в `audit_log`.

Створення першого адміністратора — крок **[ЛЮДИНА]** (`docs/manuals/user_guide.md` §5):
```bash
uv run fuzzhelm user add --login <логін> --role admin          # пароль двічі з клавіатури, без відлуння
```
Функції для тестів і скриптів: `user_add(uow, *, login, role, password) -> (UserRow, audit_id)`,
`user_set_role(uow, *, login, role) -> (old_role, audit_id | None)`, `user_list(uow) -> (rows, audit_id)`,
`read_new_password(*, from_stdin, stdin=None, prompt=None)`, `check_new_password(password, login)`,
`UserStores(users, audit)` + `UserUow` (одиниця роботи; робоча — `_db_user_stores(args)`).

## Тести

`tests/unit/test_cli.py` покриває `fuzzhelm user` на репозиторіях у пам'яті з транзакційною семантикою (пароль зі stdin
і з getpass не потрапляє ні у вивід, ні в журнал, ні в аудит; getpass двічі й розбіжність; без термінала — вимога
`--password-stdin`; 6 слабких/нехешованих паролів; дубль; set-role з before/after і захистом останнього admin;
list без хешів; `--password`, `--pass` в argv — помилка розбору; `--password-stdin` на терміналі — відмова без
читання stdin (`test_user_add_password_stdin_on_terminal_is_refused_not_echoed`); повторна перевірка цілі після
блокування admin — `test_user_set_role_rechecks_target_after_locking_admins`). `tests/integration/test_cli_db.py` —
ті самі команди на PostgreSQL (порт 5443): bcrypt `$2b$12$`, аудит з актором `cli`, вхід створеного користувача через
`/auth/login`, гонка UNIQUE без деталей драйвера, блокування рядків admin (друга сесія з `lock_timeout` 200 мс
отримує SQLSTATE 55P03 — `test_user_set_role_locks_admin_rows_against_concurrent_demotion`). Крім того, `tests/unit/test_cli.py` покриває розбір аргументів, dry-run без мережі й БД (HTTP-клієнт і engine
підмінено «бомбою»), вікно й план, крос-звірку зі збережених сирих відповідей, нормалізацію і файли
фінансування, нові REST-ендпоінти (respx), шлях «прогалина → ingest_gap → FILLED» на фейкових репозиторіях,
звіт добору і внесок фінансування в `dataset_hash`. `tests/unit/test_calibration_pipeline.py` покриває
калібрування на фікстурі: детермінізм, причинність, правила σ, симетричні T, gate фази 3 для закоміченого
конфігу й обґрунтування DATA-06. Мережевих викликів і `sleep` немає.
