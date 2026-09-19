# Розходження «спека ↔ реальність»: фінальний прохід документації (фаза 10, звітні числа)

Автор: Андрій Жук, 2026. Формат: що в спеці / що насправді / що зробив / чим обґрунтовано. Усі числа — з названих команд, виконаних
2026-09-19 між 20:55 і 21:10 +03:00 на чистому HEAD `415acbd`. Код, тести, конфігурацію і `data/` не змінював. Зміни — лише в
`docs/` і кореневих `README.md`, `README.en.md`, `CHANGELOG.md`.

## FIN-01. Чистий перезапуск прогону фази 6 (закриває RR-19, W-11, «Відкрите» W-10)
- **Що в спеці:** §12 фаза 6 — у БД є `run` DONE з повним паспортом; §17 — прогін з паспортом `config_hash`, `git_sha`, `seed`,
  `equity_hash`. Кожне число звіту — з закоміченого коду.
- **Що насправді:** попередні прогони фази 6 мали `run_metric.git_dirty = 1`. Прогін `89619416` записано на `5fed0fd` із
  незакоміченим кодом. Прогін `a848fa56` записано на `b933802` під час паралельного редагування `docs/`, коли `run_backtest.py` ще брав
  сирий прапорець (RR-19).
- **Що зробив:** `FUZZHELM_DATABASE_URL=…5442… uv run python scripts/run_backtest.py` на чистому дереві `415acbd`. Скрипт уже бере
  прапорець з `manifest.read_git_state` (WIRE-03). Результат — run `4e5be0de-a4b7-4ef1-b84e-4a563b248be3`, DONE, `git_dirty = 0`.
  Інші поля: `config_hash 48e2a5d8…`, `dataset_hash 99382675…`, seed 20260918, `equity_hash 3b5009cd…`, `journal_head_hash 028bc57a…`.
  `verify-journal --run-id …` дає «chain OK, anchor OK».
- **Чим обґрунтовано:** `equity_hash` збігся з `a848fa56` (`b933802`), тобто крива капіталу відтворюється побітово через три коміти.
  Голова журналу інша (`67f0e130…` → `028bc57a…`), бо `client_order_id` залежить від `run_id` (W-10). Числа — `docs/results.md` §2.

## FIN-02. `export_report_tables.py` з вкладеними `--results-dir` дублює таблиці exp_search
- **Що в спеці (постановка):** `--results-dir docs/report_tables/raw --results-dir docs/report_tables/raw/exp_search`.
- **Що насправді:** `discover()` обходить кожен каталог рекурсивно (`rglob`) і не прибирає повторів. Другий каталог лежить усередині
  першого, тож кожен JSON exp_search знайдено двічі: «amdahl x2, grid x4, sensitivity x4, wf_folds x12». Таблиці `amdahl.md`,
  `wf_folds.md`, `pareto.md`, `sensitivity.md`, `metrics.md` вийшли з дубльованими секціями: `amdahl.md` мав 78 рядків замість 41.
  Ціль `make report` має той самий дефект. У ній `--results-dir artifacts/exp_search` поряд із `docs/report_tables/raw`, а
  `diff -rq artifacts/exp_search docs/report_tables/raw/exp_search` порожній, тобто це ті самі файли.
- **Що зробив:** запустив експорт повторно з одним каталогом `--results-dir docs/report_tables/raw`, який рекурсивно охоплює і
  `exp_search`. Знайдено «amdahl x1, grid x2, sensitivity x2, wf_folds x6». Команда записана в `docs/report_tables/index.md`.
  Код не змінював.
- **Рекомендація (власник `scripts/export_report_tables.py` / Makefile):** прибирати повтори в `discover()` за
  `path.resolve()` або вмістом. Інший варіант — лишити в `make report` один каталог.

## FIN-03. `docs/report_tables/test_groups.md`: два генератори, одне ім'я (закриває «Відкрите» QP-04)
- **Що насправді:** експорт перезаписав поіменний перелік (`tests/helpers/brief_test_groups.py`) своєю короткою версією (XA-13).
- **Що зробив:** поіменний перелік згенеровано заново на чистому HEAD командою `uv run python -m tests.helpers.brief_test_groups`.
  Відносно закоміченої версії змінився лише рядок заголовка: `HEAD ca1a6b0 + незакомічені зміни tests/` → `HEAD 415acbd`. Короткий
  підсумок експорту збережено як `docs/report_tables/test_groups_summary.md`. Обидва дають ті самі числа: 1034 / 979 / 54, 133 з
  133 назв.
- **Рекомендація:** перейменувати вихід експорту в коді, інакше `make report` знову перезапише поіменний перелік.

## FIN-04. Три маркери TBD у згенерованій таблиці трасування закодовано жорстко
- **Що насправді:** `section_traceability` завжди пише маркери `testnet_order_screenshot`, `ui_rules_crud`, `statechart_diagram`.
  Statechart при цьому існує: `docs/diagrams/risk_fsm.puml`, згенерований з `risk/state.py::TRANSITIONS`.
- **Що зробив:** у `docs/report_tables/traceability.md` три клітинки після генерації переписав вручну. Statechart — посилання на
  файл. Два кроки [ЛЮДИНА]/UI — «не виконано: причина; що зробити і ким». Про ручну правку сказано в `docs/report_tables/index.md`.
- **Рекомендація:** `make report` поверне маркери. Потрібно навчити скрипт перевіряти `docs/diagrams/risk_fsm.puml` і писати явне
  «не виконано» для кроків людини.

## FIN-05. Провенанс MLP-моделі ETHUSDT і шаблонний текст її звіту
- **Що насправді:** у `data/anomaly_mlp_ETHUSDT.json` (коміт `415acbd`) `provenance.git_dirty = true`, змінений шлях —
  `data/anomaly_mlp_BTCUSDT.json`. BTC-модель перезаписали за 11 с до ETH (17:51:55Z і 17:52:06Z), і `data/` рахується як код.
  На навчання ETH цей файл не впливає. Модель BTCUSDT має `git_dirty = false`, її `params_sha256 98a2bebf…` той самий, що у WIRE-01.
  Ще два дрібні місця. У звіті `docs/figures/quality_mlp_rocauc_ETHUSDT.md` є рядок «Хеш НЕ збігається з хешем вікна калібрування МФ
  … це ті самі бари». Друга половина хибна для ETH: МФ калібровано на барах BTCUSDT, тому хеш і не мав збігтися. Поля `decision` і
  `evaluation.report` ETH-артефакту посилаються на BTC-звіт `docs/figures/quality_mlp_rocauc.md`.
- **Що зробив:** згенерованих файлів і артефакту не змінював: зміна `data/` зробила б код «брудним». Числа ETH у
  `docs/results.md` §11 узяв з ETH-звіту. Прапорець провенансу там названо прямо.
- **Рекомендація (власник `scripts/train_anomaly_mlp.py`):** умовний текст для символу, відмінного від символу калібрування;
  посилання на власний звіт символу; перенавчання ETH після коміту BTC-моделі.

## FIN-06. Позитивна валова перевага без витрат — лише PSR, без DSR
- **Що в спеці:** §5.15 — PSR/DSR з фактичним N; «якщо PSR < 0.95 — перевага статистично не встановлена».
- **Що насправді:** варіант `zero` чистої оцінки BTCUSDT (`cost_models_BTCUSDT_isgrid`) має PSR 0.9727 ≥ 0.95. Параметри при цьому
  обирались на IS кожного фолду з 108 клітинок, і DSR для цього варіанта вивід не рахує. ETHUSDT з тією самою процедурою має
  PSR 0.5612.
- **Що зробив:** у `docs/results.md` і README формулюю так: «слабка позитивна валова перевага, PSR 0.97 без поправки на множинний
  вибір». Встановленою перевагу не називаю.
- **Чим обґрунтовано:** §0.2 брифінгу — не підганяти результат.

## FIN-07. Маркери TBD: що зроблено з кожним
- **Що в спеці:** §12 фаза 10, §17 — жодного маркера TBD ні в `config/`, ні в `docs/`.
- **Що зробив:**
  1. Виміряні величини вписано з джерелом. Це підсумки тестів і покриття, lint, pip-audit, числа фази 7, чистий прогін фази 6,
     профіль RAM (`docs/report_tables/raw/ram_profile.txt`), рішення MLP.
  2. Кроки [ЛЮДИНА] переписано як «не виконано: причина; що зробити і ким»: testnet-ордер і скріншот, токен Telegram, деплой Fly.io,
     LOGIN-роль БД, адміністратор API, скрінкаст, офлайн-репетиція, підписання бланків.
  3. Артефакти фази 10–11 без виміряних чисел, які агент не створював, так само позначено «не виконано». Це ТЕО, WBS/Гант/сітьовий
     графік, IDEF0/BPMN/блок-схеми ДСТУ ISO 5807, англомовний abstract, веб-панель Vue (D-06).
  4. Згадки самої домовленості про маркер у `docs/contracts.md`, `docs/api/exp_analysis.md` і фрагментах переформульовано без
     буквального маркера.
- **Що лишилось свідомо:** `docs/BRIEF.md` — нормативна специфікація, її не редагую. Там маркер є в правилі §0.1 п. 3, у шаблонах
  конфігурацій §7 (фактичні числа — у `config/membership.yaml` і `config/dq_weights.yaml`, у `config/` маркерів нема) і в
  gate-критеріях §12/§17. `tests/arch/test_brief_test_inventory.py` і `tests/integration/test_migrations.py` розбирають `BRIEF.md`.
  Перевірка: `grep -rnE '<<TB[D]' docs config` знаходить рядки лише в `docs/BRIEF.md`.

## FIN-08. Час набору тестів і RAM (закриває NFR-08, `ram_profile`)
- **Що в спеці:** §10 — unit+property < 25 с; §17 — `make test` < 25 с. ТЗ §4.4 — пам'ять під навантаженням.
- **Виміряно** (`docs/report_tables/raw/test_runs.txt`, `ram_profile.txt`; load average 2.4–3.6):
  - `make test` (`pytest -n 6`) — 979 passed за 12.83 с;
  - послідовно — 29.90 с;
  - unit + property послідовно — 807 passed за 23.37 с (real 24.56 с);
  - разом з інтеграційними — 1034 passed за 108.17 с.
  - Max RSS: бектест 45 днів у пам'яті — 243 023 872 байти; імпорт API — 95 846 400; послідовний набір тестів — 732 692 480.
  - Контейнер PostgreSQL у спокої займає 185.6 МіБ, БД — 462 584 855 байт.
- **Чим обґрунтовано:** ціль §17 виконано лише завдяки паралельним воркерам (pytest-xdist, `2f77c82`). Послідовний прогін її не
  виконує (QP-03). Запас unit+property до 25 с — близько 1.6 с pytest-часу.

## FIN-09. Перевірка команд README: `make backup` без каталогу і дампи поза `.gitignore`
- **Що перевірено** (неруйнівні команди, 2026-09-19):
  - `uv lock --check` — «Resolved 100 packages».
  - `uv run alembic current` = `heads` = `0004_decision_trace_extras`.
  - `uv run fuzzhelm db-stats` — `candle` 130 695.
  - `uv run uvicorn fuzzhelm.api.main:app --port 8000`: `/healthz` → `{"status":"ok",…}`, `/docs` → 200, `/openapi.json` —
    OpenAPI 3.1.0, 22 шляхи, 25 операцій, `/risk/state` без токена → 401.
  - Офлайн-реплей `--profile replay --speed inf --no-db`: 45 свічок, `equity_hash d0f45aa3…` — як у W-03.
  - `make -n` для 21 цілі.
  - `docker compose config --services` → `db`, `api`, `worker` (`ui` — у профілі `ui`).
  - `docker compose up` і `backfill` не запускав: робоча БД уже працює, а добір пише в неї.
- **Що насправді:** `make backup` пише в `backups/…dump`, але каталогу `backups/` немає, тож перенаправлення shell впаде. Runbook
  (`docs/manuals/backup_runbook.md`) каже спершу `mkdir -p backups`. Крім того, `.gitignore` не містить `backups/` і `*.dump`.
  Дамп з `app_user` (bcrypt-хеші) і `audit_log` легко закомітити випадково.
- **Що зробив:** README у таблиці цілей Makefile каже виконати `mkdir -p backups` перед першою копією. Makefile і `.gitignore` не змінював: це конфігурація
  репозиторію, рішення за провідним розробником.
- **Рекомендація:** додати `backups/` у `.gitignore` і `mkdir -p backups` у ціль `backup`.
