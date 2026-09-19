# Фінальний чек-лист готовності (брифінг §17): стан пункт за пунктом

Автор: Андрій Жук, 2026. Стан: HEAD `415acbd` (2026-09-19 20:54 +03:00) + фінальний прохід документації (ще не закомічено).
Перевірку виконано 2026-09-19 близько 21:30 (+03:00) незалежним фінальним аудитом. Кожен статус спирається на команду, запущену
під час аудиту, на запит до робочої БД `fuzzhelm-db-1` (порт 5442, лише читання) або на названий файл. Контейнерів аудит не
запускав і не зупиняв.

Статуси: **виконано** — вимогу виконано дослівно; **частково** — виконано не все, решту названо; **не виконано** — з причиною і тим,
що потрібно зробити і ким. Кроки з позначкою **[ЛЮДИНА]** виконує лише студент (брифінг §12.9).

## Підсумок

| № | Пункт §17 | Статус |
|---|---|---|
| 1 | Репозиторій не в iCloud; `docker compose up` піднімає 4 сервіси з нуля однією командою | **частково** |
| 2 | `make test` → 92 passed, < 25 с, без мережі | **виконано** |
| 3 | `make cov` → загалом ≥ 80 %, `fuzzy`/`risk`/`decision`/`sizing` ≥ 90 % | **виконано** |
| 4 | Демо повністю працює з вимкненим Wi-Fi (офлайн-фікстура) | **частково** |
| 5 | `ExplainView` показує спрацьовані правила з α і україномовне речення | **не виконано** |
| 6 | Сценарій `flash_crash` доводить систему до `HALTED` без участі людини | **виконано** |
| 7 | `test_risk_chain_never_increases_exposure` і `test_equity_accounting_identity` зелені | **виконано** |
| 8 | У БД є прогін із повним паспортом (`config_hash`, `git_sha`, `seed`, `equity_hash`) | **виконано** |
| 9 | Жодного маркера TBD ні в `config/`, ні в `docs/` | **частково** |
| 10 | Жодного секрету в git-історії; `.env` у `.gitignore` | **виконано** |
| 11 | `docs/journal.md` заповнений по днях; `docs/deviations.md` містить усі розходження | **виконано** |
| 12 | Записаний скрінкаст демо лежить окремо як план Б | **не виконано** |
| 13 | Таблиця трасування з розділу 2 заповнена по всіх 11 рядках | **частково** |

Разом: виконано 7, частково 4, не виконано 2. Обидва «не виконано» і частина «частково» залежать від людини (скрінкаст,
репетиція без мережі, testnet-ордер) або від окремого етапу веб-панелі (D-06), а не від коду бекенду.

## Докази за пунктами

### 1. Репозиторій поза iCloud; `docker compose up` — 4 сервіси з нуля (частково)
- **Виконано:** робоча копія лежить у `/Users/andriizhuk/dev/fuzzhelm`, не в iCloud Drive (брифінг §0.3).
- **Виконано частково:** `docker compose config --services` → `db`, `api`, `worker`, тобто 3 сервіси. Сервіс `ui` оголошено лише в
  профілі `ui`, а каталогу `./ui` немає, тож четвертий сервіс не стартує. Збирання й запуск `docker compose up -d --build db api
  worker` перевірено 2026-09-19 о 12:45 UTC на тодішньому коміті (`docs/manuals/deployment.md` §3a). На `415acbd` аудит їх не
  перезапускав, бо робоча БД працює і її не можна зупиняти.
- **Не виконано «з нуля однією командою»:** на порожньому volume після `up` ще потрібні `uv run alembic upgrade head` і
  `uv run fuzzhelm backfill --days 45` (README, «Швидкий старт»). Команда `docker compose up` міграцій не запускає, бо `api`
  стартує лише `uvicorn`. Воркер у контейнері не бачить `data/anomaly_mlp_*.json`, бо `data/` у `.dockerignore`, а `./data`
  воркеру не змонтовано (`deployment.md` §3a, п. 2a).
- **Що потрібно:** автор реалізує веб-панель (D-06), після чого сервіс `ui` з'явиться без профілю. Для підйому «з нуля» —
  рішення провідного розробника: додати крок міграцій у старт `api` або окремий сервіс ініціалізації.

### 2. `make test` → ≥ 92 passed, < 25 с, без мережі (виконано)
- `uv run pytest -q -n 6` (= `make test`) під час аудиту: **979 passed in 12.28 s**. Записаний замір — 979 passed за 12.83 с
  (`docs/report_tables/raw/test_runs.txt`). 979 ≥ 92: заголовок §10 «92 кейси» — D-02; усі 133 названі в §10 тест-функції є
  (`docs/report_tables/test_groups.md`; повторна генерація `uv run python -m tests.helpers.brief_test_groups` у тимчасовий
  файл побайтово збіглася з цим файлом).
- Без мережі: типовий відбір `addopts` виключає маркери `integration`, `live`, `slow` (`pyproject.toml`). Аудит додатково
  прогнав набір з усіма проксі (`HTTP(S)_PROXY`, `ALL_PROXY`) на мертвий порт `127.0.0.1:9` і `UV_OFFLINE=1`: **979 passed in
  16.23 s**. Це непрямий доказ: фікстури блокування сокетів у тестах немає, а проксі поважають не всі клієнти. Wi-Fi аудит не
  вимикав, бо це зміна системних налаштувань.
- Застереження: послідовно (`uv run pytest -q`) набір триває 29.90 с, тож ціль < 25 с виконується лише з pytest-xdist (QP-03).
  Unit + property послідовно — 807 passed за 23.37 с, ціль §10 виконано (`test_runs.txt`).

### 3. `make cov` → ≥ 80 % загалом, ≥ 90 % на чотирьох ключових пакетах (виконано)
- Офлайн, як `make cov` (аудит, файл даних у тимчасовому каталозі): `uv run pytest -q -n 6 --cov --cov-report=term` → **83.72 %**,
  979 passed; `uv run python -m tests.helpers.cov_packages --data-file <той файл>` → fuzzy 99.4 %, risk 99.9 %, decision 98.4 %,
  sizing 100.0 %, порушень порогів немає (код виходу 0).
- Офіційне число (об'єднаний прогін з інтеграційними, QP-01): **89.38 %**; fuzzy 99.4, risk 99.9, decision 98.7, sizing 100.0 %
  (`docs/report_tables/raw/coverage_combined.txt`, `coverage_packages.md`).

### 4. Демо з вимкненим Wi-Fi (частково)
- **Працює офлайн (перевірено аудитом):** `uv run python -m fuzzhelm.workers.trading_worker --profile replay --speed inf --no-db` —
  45 свічок, `equity_hash d0f45aa3cd5ea81e…`, 0 аномалій. Те саме з `--scenario flash_crash` — `final_state HALTED`. Тести
  `test_replay_session_end_to_end` і `test_flash_crash_reaches_halted_without_human_input` мережі не потребують і входять до
  979 passed. Мережу під час цих запусків аудит не вимикав.
- **Не виконано:** (а) ручна репетиція сценарію §15 з вимкненим Wi-Fi. Причина: це крок людини біля машини. Що потрібно: студент
  **[ЛЮДИНА]** один раз проганяє `make replay`, `scripts/demo_flash_crash.py`, `uvicorn` + `/docs` без мережі. (б) Частина
  сценарію §15 (0:30–3:20) спирається на екрани Vue, яких немає (пункт 5).

### 5. `ExplainView` (не виконано)
- Каталогу `ui/` немає (`ls ui` → «No such file or directory»). Причина — порядок робіт D-06: спершу бекенд.
- Дані для екрана вже віддає API: `GET /decisions/{id}/explain` повертає спрацьовані правила з α, належності, μ_agg, центроїд і
  україномовне речення. Тести `test_get_explain_returns_fired_rules_and_memberships` і
  `test_explain_narrative_is_ukrainian_and_non_empty` (`tests/e2e/test_api.py`) входять до 979 passed.
- **Що потрібно:** автор реалізує Vue 3 `ExplainView` першою поверх `/decisions/{id}/explain`. План Б на захисті — Swagger `/docs`
  і JSON-відповідь `/explain` (`docs/risk_register.md` RR-12).

### 6. `flash_crash` → `HALTED` без участі людини (виконано)
- Аудит: `uv run python -m fuzzhelm.workers.trading_worker --profile replay --speed inf --no-db --scenario flash_crash` →
  `final_state: HALTED`, `halted_at_ns 1789760039999000000`, подія `HALT_BREACH`, `equity_hash a5b8aa3a744842cc…`, 10 аномалій MLP.
- Тести: `test_flash_crash_reaches_halted_without_human_input` (офлайн) і
  `test_flash_crash_halts_on_db_and_only_admin_release_via_api_unlatches` (інтеграційний, у прогоні 1034 passed).

### 7. Ключові інваріанти зелені (виконано)
- Аудит: `uv run pytest -q tests/property/test_risk_property.py::test_risk_chain_never_increases_exposure
  tests/property/test_portfolio_property.py::test_equity_accounting_identity tests/unit/test_risk.py::test_risk_fsm_transition_table_is_total`
  → 26 passed (з параметризацією). Обидва тести §17 входять і до повного прогону 979 passed.

### 8. Прогін із повним паспортом у БД (виконано)
- `select … from run where id = '4e5be0de-a4b7-4ef1-b84e-4a563b248be3'` → backtest, DONE, mamdani, `config_hash 48e2a5d8…`,
  `dataset_hash 99382675…`, `git_sha 415acbd2…`, seed 20260918, `equity_hash 3b5009cd…`, `journal_head_hash 028bc57a…`;
  `run_metric.git_dirty = 0`.
- Аудит перерахував `equity_hash` з 64 800 рядків `equity_point` функцією `backtest.manifest.equity_hash`: він збігся з паспортом
  і з прогоном `a848fa56` на `b933802`. `uv run fuzzhelm verify-journal --run-id 4e5be0de-…` → «5959 entries, chain OK, anchor OK».

### 9. Жодного маркера TBD у `config/` і `docs/` (частково)
- `grep -rnE '<<TB[D]' config README.md README.en.md CHANGELOG.md` → порожньо.
- `grep -rnE '<<TB[D]' docs` → 12 рядків, **усі в `docs/BRIEF.md`**: правило протоколу §0.1 п. 3, шаблони конфігурацій §7 (фактичні
  числа записано в `config/membership.yaml` і `config/dq_weights.yaml`) і текст gate-критеріїв §12/§17. Це текст нормативної
  специфікації, а не незаповнені числа проєкту. У згенерованих і написаних документах маркерів немає (FIN-07).
- **Що потрібно:** рішення автора, чи редагувати `docs/BRIEF.md` (вхідну специфікацію; її розбирають
  `tests/helpers/brief_test_groups.py`, `tests/unit/test_experiments_analysis.py` і `tests/integration/test_migrations.py`) або прийняти ці 12 рядків як цитати специфікації.

### 10. Секрети поза git (виконано)
- `git check-ignore -v .env` → `.gitignore:1:.env`.
- Історія: `git log --all --name-only` не містить `.env`, `*.pem`, `*.key`. `git grep` по всіх комітах (`git rev-list --all`)
  шукав значення `BINANCE_TESTNET_KEY/SECRET`, `TELEGRAM_BOT_TOKEN`, `FUZZHELM_JWT_SECRET` довжиною ≥ 8 символів. Знайшов лише
  заглушку `FUZZHELM_JWT_SECRET=change-me` у `.env.example`.
- Відкритий ризик: `backups/` і `*.dump` не внесено в `.gitignore`, тож дамп з bcrypt-хешами `app_user` можна закомітити
  випадково (FIN-09). Рішення — за провідним розробником.

### 11. Журнал по днях і всі розходження (виконано)
- `docs/journal.md`: хронологія комітів і розділи «2026-09-18 — фаза 0 і хвиля 1», «2026-09-19 — хвиля 2, хвиля 3a, хвиля 3b»,
  «2026-09-19 (вечір) — фінальні хвилі».
- `docs/deviations.md`: зведена таблиця — 235 рядків (234 пункти + псевдонім D-07). Кожен із 227 ідентифікаторів фрагментів
  `docs/deviations.d/*.md` (`docs/report_tables/deviations.md`) присутній у зведеній таблиці. Під час аудиту додано пропущений QR-05.

### 12. Скрінкаст демо як план Б (не виконано)
- Відеофайлів (`*.mp4`, `*.mov`, `*.webm`, `*.mkv`) у репозиторії немає (`find`). Причина: запис робить людина біля машини.
- **Що потрібно:** студент **[ЛЮДИНА]** записує скрінкаст за сценарієм §15 (реплей, flash_crash, `/docs` і `/explain`) і зберігає
  його окремо від репозиторію як план Б (`docs/risk_register.md` RR-06).

### 13. Таблиця трасування по 11 рядках (частково)
- Таблицю заповнено по всіх 11 рядках: `docs/results.md` §18 і згенерована `docs/report_tables/traceability.md`. Модулі й
  артефакти, на які вона посилається, аудит перевірив: файли існують, `risk_fsm.puml` збігається з повторною генерацією з
  `risk/state.py::TRANSITIONS`, числа рядків БД збігаються з запитами.
- Підтвердження за брифінгом неповне у двох рядках:
  - Рядок 2: немає живого testnet-ордера зі скріншотом. Причина — немає testnet-ключів (EXE-07). Що потрібно: студент
    **[ЛЮДИНА]** реєструється на Binance Futures Testnet, вписує ключі в `.env`, запускає
    `uv run python scripts/testnet_one_order.py --confirm` і знімає екранограму.
  - Рядок 9: немає CRUD правил через веб-UI, бо UI — окремий етап (D-06). CRUD через API `/strategies` є.

## Gate фази 10 (для довідки)

`make cov` ≥ 80 % загалом і ≥ 90 % на ключових пакетах — виконано (пункт 3). «У `docs/` немає жодного маркера TBD» — частково
(пункт 9). Артефакти фази 10, яких немає: ТЕО, WBS / діаграма Ганта / сітьовий графік, англомовний abstract. Причина і що
потрібно — `docs/tz/technical_specification.md` розділи 5–7, `docs/index.md` «Що не виконано (фази 9–11)».
