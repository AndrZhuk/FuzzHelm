# Розходження «спека ↔ реальність»: компонент wiring (фаза 10)

Автор: Андрій Жук, 2026. Формат: що в спеці / що насправді / що зробив / чим обґрунтовано. Усі числа — з названих
команд, виконаних 2026-09-19 на HEAD `ca1a6b0` з незакоміченими змінами цього компонента. Зона: `ingest/pipeline.py`,
`ingest/ws_client.py`, `workers/**`, `quality/{anomaly_mlp,dq_score}.py`, `backtest/manifest.py`,
`api/backtest_runner.py`, `scripts/train_anomaly_mlp.py`, `data/anomaly_mlp_*.json`, `.github/workflows/ci.yml`,
`fly.toml`, `Makefile`, `docs/manuals/*.md`, `tests/unit/test_wiring_*.py`.

## WIRE-01. MLP-автокодувальник у робочому контурі; рішення про архітектуру (закриває «Стан на HEAD» PLAT-05)
- **Що в спеці:** §4.1 — `QualityGate(Q, MLP-аномалії)` між агрегатором свічок і сховищем; §5.17 — `N_invalid` включає
  аномалії автокодувальника, вектор з 5 ознак; §3 — «MLPRegressor(8-3-8)».
- **Що насправді (HEAD):** `IngestPipeline(anomaly=…)` існував, але жоден воркер скорер не передавав; навчена мережа жила
  лише в пам'яті `quality/anomaly_eval.py`; `candle.anomaly_score` не заповнювався (read-only
  `SELECT count(*) FILTER (WHERE anomaly_score IS NOT NULL), count(*) FROM candle` на робочій БД → `0|130695`, повторено
  2026-09-19 — воркерів проти робочої БД я не запускав). Рішення про архітектуру стояло маркером TBD `mlp_architecture_decision` (PLAT-05) — тепер ухвалене (п. 1).
- **Що зробив:**
  1. **Рішення: 5-3-5.** Це вектор §5.17 (норматив) і його підтверджує замір на справжніх даних, зокрема на розбитті
     ВСЕРЕДИНІ IS, що не торкається відкладених днів. `uv run python scripts/train_anomaly_mlp.py --no-model --out …`
     (дні 1–15 → 16–20, 6 seed) відтворив `docs/figures/quality_mlp_rocauc.md` побайтово, крім рядків команди й часу
     (`diff`): ROC-AUC 5-3-5 **0.9718 ± 0.0021** проти 8-3-8 **0.9551 ± 0.0021**, 8-3-8 кращий у 0 з 6; FPR@q99
     (seed 20260918) 0.0233 проти 0.0375. `… --is-days 12 --holdout-days 3` (дні 1–12 → 13–15): **0.9792 ± 0.0045**
     проти **0.9567 ± 0.0038**, 0 з 6; FPR@q99 0.0127 проти 0.0433. Застереження: числа 5-3-5 на днях 16–20 отримано вже
     після вибору з двох архітектур, тож вони злегка оптимістичні; той самий порядок на внутрішньому розбитті IS показує,
     що вибір від днів 16–20 не залежить.
  2. **Артефакт `data/anomaly_mlp_BTCUSDT.json`** (JSON, не pickle; 6 734 байти): архітектура, 5 ознак, параметри
     екстрактора, `scaler.mean/scale`, `coefs_`/`intercepts_` обох шарів, активації tanh/identity, поріг q₉₉
     **2.865277120886994**, `params_sha256 98a2bebf0b7e8c141fe7f44941ed670f0ab437029079cff31d8f205e5f9dd1e4`
     (завантажувач перевіряє дайджест, форми шарів, активації і поріг), навчання: дні 1–15 `[2026-08-04, 2026-08-19)`,
     21 600 барів → 21 382 вектори (вилучено 0), `dataset_hash 9156616d…` = хеш вікна калібрування МФ
     (`same_bars_as_mf_calibration: true`), seed 20260918, 82 ітерації, збіжність true; числа оцінювання цього прогону;
     версії python 3.12.12 / numpy 2.5.3 / sklearn 1.9.1; git-провенанс (`git_dirty: true` — артефакт зроблено кодом
     цього компонента до коміту; коміт — не моя дія). Команда: `uv run python scripts/train_anomaly_mlp.py --no-report`
     (9.7 с). Перед записом скрипт перевіряє, що поріг = порогу тієї самої архітектури в оцінюванні і що мережа,
     відтворена з JSON, дає **побітово** ті самі скори на 21 382 навчальних векторах (`np.array_equal` — true).
  3. **Відтворення мережі:** `FrozenAutoencoder` повторює `StandardScaler.transform` і `MLPRegressor.predict`
     операція в операцію (`x −= μ; x /= σ; a @ W; a += b; tanh на місці`), тож скори збігаються до біта — пакетом і
     потоково (`test_model_artifact_roundtrip_reproduces_scores_bit_for_bit`). Без sklearn у рантаймі скорингу.
  4. **Проводка.** Конвеєр: перед першою свічкою — хук прогріву (218 барів; безперервний хвіст), далі кожна випущена
     свічка → `on_anomaly` → `on_candle` (порядок гарантує запис свічки і скору однією транзакцією), аномалія →
     `health.anomalies` і `anomaly_count`/`validity` години. Ingest-воркер: модель на інструмент, прогрів з БД (бракує —
     публічний REST, лише для живого WS), скор → `candle.anomaly_score` явним UPDATE у транзакції свічки (upsert уже
     закриту свічку не оновлює), health + підсумок. Торговий воркер: ті самі 523 бари прогріву TradingLoop — і в
     екстрактор; скор у `StepOutput` → `LivePersister.write_step(anomaly_score=…)`; health і `anomalies` у підсумку.
  5. **Колонка NUMERIC(10,6):** скор округлюється до 1e−6 і обрізається до 9999.999999 — стрибок ціни на десятки σ дає
     скор > 10⁴, і INSERT упав би з numeric overflow разом із записом свічки. Прапорець аномалії ставиться за
     неокругленим скором.
  6. **Немає файлу моделі** → попередження в журналі й контур без MLP; пошкоджений артефакт / чужий символ →
     `AnomalyModelError` на старті (мовчки вимкнути перевірку якості через зіпсований файл гірше, ніж не стартувати).
- **Виміряно (лише читання робочої БД, скрипт у scratchpad):** хук прогріву для BTCUSDT повертає 218 безперервних барів
  до 2026-09-18 00:00; модель на всьому 45-денному вікні: IS (дні 1–15) — 214 аномалій на 21 382 оцінених бари (1.00 %,
  як і має бути для q₉₉), дні 16–45 — 522 на 43 200 (1.21 %). Реплей (`--profile replay --speed inf --no-db`): чиста
  сесія — 0 аномалій на 45 свічок, `equity_hash d0f45aa3…` і `q_last 0.98055` такі самі, як з `--no-anomaly`;
  `--scenario flash_crash` — 10 аномалій на 45 свічок, HALTED у той самий момент, `equity_hash a5b8aa3a…` той самий,
  `q_last` 0.98026 проти 0.98055 без моделі (аномалії знижують validity години).
- **Чим обґрунтовано:** брифінг §4.1, §5.17; тести `tests/unit/test_wiring_anomaly.py` (11): побітове відтворення,
  відмова на підробку дайджесту/чужий документ/символ, попередження без файлу, закомічений артефакт = 5-3-5 на вікні
  калібрування і ловить стрибок +2 %, обрізка NUMERIC, безперервний хвіст, прогрів + порядок стоків + облік у Q,
  частковий/холодний старт, торгова сесія, стік ingest-воркера (одна транзакція, фіктивні репозиторії).
- **Відкрите:** (а) моделі для ETHUSDT немає (доказ — лише для BTCUSDT): воркери попереджають і працюють для ETH без MLP;
  `make anomaly SYMBOL=ETHUSDT` навчить її тим самим методом. (б) `scheduler.jobs.build_db_context` не передає
  `anomaly_threshold`, тож нічний `hourly_dq` для годин, яких не записав живий конвеєр, рахує 0 аномалій; до того ж поріг
  у `JobContext` один на всі інструменти (власник scheduler). (в) Образ Docker моделі не містить (`.dockerignore`
  виключає `data/`), сервіс `worker` не монтує `./data` — у контейнері скорер вимкнено з попередженням (власник
  Dockerfile/compose). (г) Хвилини, які агрегатор пропустив як недобірні, екстрактор бачить як сусідні бари — стрибок
  через дірку може бути позначено аномалією (консервативно).

## WIRE-02. WS-07: сторож тиші живого клієнта на монотонному годиннику
- **Що в спеці:** «no frame for N seconds → force reconnect».
- **Що насправді:** `BinanceWsClient` рахував тишу ін'єктованим годинником події (у воркерах — `SystemClock`): крок NTP
  уперед між міткою кадру й розрахунком залишку давав нульовий таймаут і хибний розрив, крок назад посеред справжньої тиші
  відсував дедлайн — розрив проґавлювався. `MonotonicClock` існував, але не використовувався (WS-07 «Стан на HEAD»).
- **Що зробив:** параметр `monotonic: Clock | None` клієнта (None → як раніше, `clock`; реплей і старі тести не
  змінились); сторож (`beat/remaining_s/expired`) — лише за ним, мітки `ts_ingest_ns` і записів керування — за `clock`.
  Обидва воркери створюють клієнт через `make_ws_client(...)` з `MonotonicClock()`.
- **Чим обґрунтовано:** `tests/unit/test_wiring_ws_monotonic.py`: крок +40 с між кадрами — з монотонним годинником 0
  спрацювань і всі 12 кадрів, з настінним (поведінка до виправлення) — 1 хибне спрацювання і лише 6 кадрів; крок −40 с
  посеред 15-секундної тиші — монотонний сторож рве з'єднання (1 спрацювання), настінний — 0 (тишу проґавлено); воркери
  передають `MonotonicClock`, мітки — `SystemClock`. Сторож реплею в `IngestPipeline` навмисно лишився на віртуальному
  часі кадрів (для записаної `clock_jump` 2 спрацювання — властивість запису, не live).

## WIRE-03. Одне визначення «брудного» коду для паспортів (XS-11, XA-19, W-11)
- **Що в спеці (постановка фази):** «code dirty» = зміни поза `docs/` і `artifacts/`, реалізувати в
  `backtest.manifest.read_git_sha` (+ шляхи змін для провенансу); `DbBacktestRunner` і `scripts/run_backtest.py` —
  через нього.
- **Що насправді:** `read_git_sha` рахував сирий `git status --porcelain`; exp_search/exp_analysis звужували самі;
  `run_backtest.py` і POST /backtests писали `git_dirty = 1` через редагування документації (W-11, RR-19).
- **Що зробив:** `manifest.read_git_state() -> GitState(sha, dirty, dirty_any, dirty_paths, ignored, source)` з
  `GIT_DIRTY_IGNORED = ("artifacts", "docs")` — єдина реалізація; `build_manifest`, `DbBacktestRunner._git` (шляхи — у
  `runner.git_dirty_paths` і результаті задачі), торговий воркер (шляхи — у `session.start` журналу) і
  `scripts/run_backtest.py` (друкує шляхи, звіт пише «незакомічені зміни коду») беруть `dirty` звідти. Контейнерний
  шлях `FUZZHELM_GIT_SHA` збережено (тепер `dirty = None`, `source = "env"`).
- **Розходження з постановкою:** `read_git_sha` лишився `(sha, dirty_any)` — СИРИЙ прапорець, а не «код». На ньому
  стоять `experiments_search.git_state` і `experiments_analysis.git_state` (не моя зона): вони беруть із нього
  `dirty_any`, а їхній тест `test_git_state_ignores_outputs_and_docs_but_not_code_and_commit_is_refused_when_dirty`
  вимагає `dirty_any = True` при змінах лише в docs/artifacts. Зміна семантики `read_git_sha` зламала б цей тест і
  підписане поле паспортів. Натомість `test_wiring_git_state.py` перевіряє в тимчасовому репозиторії, що всі три
  реалізації дають однакові `sha/dirty/dirty_any/dirty_paths`, а AST-тест — що паспорти прогонів не імпортують
  `read_git_sha`.
- **Чим обґрунтовано:** `tests/unit/test_wiring_git_state.py` (4). **Відкрите:** власникам exp_search/exp_analysis —
  делегувати `git_state()` у `read_git_state()`; тоді `read_git_sha` можна прибрати.

## WIRE-04. CI (§9)
- **Що в спеці:** `.github/workflows/ci.yml`: ruff → mypy(core, fuzzy, risk) → pytest --cov → pip-audit.
- **Що зробив:** задачі `lint`, `test` (після lint; поріг 80 % — з `[tool.coverage.report] fail_under`, pytest-cov бере
  його з конфігурації coverage, коли `--cov-fail-under` не задано — перевірено в `pytest_cov/plugin.py`), `integration`
  (сервіс `postgres:16` на 5432 у раннері, `FUZZHELM_TEST_DATABASE_URL`, `-m integration`), `audit`; `astral-sh/setup-uv`,
  `uv sync --frozen`. Розходження: conftest інтеграційних тестів *пропускає* їх, якщо БД недосяжна, — у CI це дало б
  зелений прогін без жодного тесту, тож задача падає, якщо у виводі є «test PostgreSQL unreachable».
- **Виміряно локально:** YAML — `yaml.safe_load` (4 задачі); `uv run mypy src/fuzzhelm/core src/fuzzhelm/fuzzy
  src/fuzzhelm/risk` → «Success: no issues found in 35 source files»; `uv run pytest -q --cov` → 937 passed, TOTAL
  86.46 % («Required test coverage of 80.0% reached»); `uv run pip-audit --skip-editable --progress-spinner off` →
  «No known vulnerabilities found» (цим можна закрити маркер TBD `pip_audit_after_jose_removal` у зведеному документі — він не в моїй зоні).
- **Не перевірено:** workflow ні разу не виконувався на GitHub (`git remote -v` порожній; пуш — не моя дія); інтеграційні
  тести я не запускав — тестова БД 5443 належить компоненту QUALITY.

## WIRE-05. `fly.toml` (free tier) — не задеплоєно
- **Що в спеці:** `fly.toml — деплой на free tier`; реєстрація на Fly.io — **[ЛЮДИНА]**.
- **Що насправді:** у `Dockerfile` немає CMD (команди задає compose); образ не містить `data/`.
- **Що зробив:** `app = "fuzzhelm-change-me"`, `[processes] app = "uvicorn fuzzhelm.api.main:app --host 0.0.0.0 --port
  8000"`, `internal_port = 8000`, перевірка `GET /healthz` (маршрут існує, не торкається БД), `release_command = "alembic
  upgrade head"`, `shared-cpu-1x`/256 МБ з автозупинкою; секрети — лише `fly secrets set`. Імпорт застосунку API — 95 731 712
  байт max RSS (`/usr/bin/time -l uv run python -c "import fuzzhelm.api.main"`), тож 256 МБ для API без важких бектестів
  достатньо з запасом; POST /backtests на 45 днях не міряв. TOML — `tomllib`, тест `test_wiring_ci_deploy.py`.

## WIRE-06. Makefile
- **Що в спеці:** §9 — `up down migrate ingest record replay backtest grid verify test cov lint report backup`;
  contracts §0 п. 8 — «не редагувати Makefile» (для модулів хвилі 1; у цій фазі Makefile — зона wiring).
- **Що насправді:** `grid` запускав `run_grid.py` без аргументів; `report` — `export_report_tables.py` без `--db` і без
  `--results-dir`, тобто лише таблиці із заглушками TBD; цілей експериментів фази 7 не було.
- **Що зробив:** додано `experiments` (`scripts/run_all_experiments.sh`), `walkforward`, `audit`, `anomaly`, `users`
  (лише підказка — пароль вводить людина); `grid`/`walkforward`/`backtest`/`anomaly` — з `SYMBOL`/`WORKERS`; `report` —
  `--db --results-dir docs/report_tables/raw --results-dir artifacts/exp_search --out docs/report_tables` (команда з
  `docs/api/exp_analysis.md` крок 9 без `--run-id`/`--cov-file`). `make -n` розгортається для всіх 21 цілей (20 — у
  `test_makefile_targets_expand`, `restore FILE=x.dump` — вручну).

## WIRE-07. Незалежна рецензія проводки: два дефекти скорера в торговому воркері; перезаміри
- **Що в спеці:** WIRE-01 — скор кожної закритої свічки потоку → `candle.anomaly_score`; стан екстрактора ознак —
  з безперервного ряду, що зростає за часом (ATR Уайлдера, σ20, ранг ширини, середні — рекурсивні).
- **Що насправді (знайдено рецензією; обидва відтворено тестами ДО виправлення):**
  1. `TradingSession._step` забирав очікуваний вердикт на кроці *будь-якої* свічки й скидав його, якщо час не
     збігся. `_fill_discontinuity` крокує добрані REST-бари дірки між прогрівом і першою свічкою потоку ПЕРЕД самою
     свічкою, а її `on_anomaly` уже прийшов — тож скор свічки потоку губився (не доходив до `StepOutput` →
     `candle.anomaly_score`). Тест до виправлення: `assert o.anomaly is not None` → `AssertionError: None`.
  2. `AnomalyScorer` подавав у екстрактор бари, не новіші за вже подані. Свічку потоку, що перекривається з прогрівом,
     `TradingSession.on_candle` свідомо пропускає («бар уже враховано»), але конвеєр до того вже прогнав її через
     екстрактор удруге, видав їй скор і міг зарахувати її в `health.anomalies` і `anomaly_count` години Q. Тест до
     виправлення: після прогріву `BARS[:300]` виклик `update(BARS[299])` повертав вердикт (score 0.00268), а не None.
- **Що зробив:** (1) `_step` забирає вердикт лише тоді, коли його `t_ns` = open_time свічки кроку; добрані бари мають
  `anomaly = None`. (2) `AnomalyScorer` пропускає бар з `t_ns ≤` останнього поданого (прогрів або потік): `update` →
  None, `warm_up` його не рахує, лічильник `stale_skipped` (також у `describe()`). Звичайний шлях (час строго зростає)
  не змінився: ті самі скори, ті самі реплеї.
- **Чим обґрунтовано:** `tests/unit/test_wiring_anomaly_edges.py` (2): послідовність скорів після повтору/старшого бару
  = послідовність без нього (порівняння вердиктів 1:1 на 120 барах); після 3-хвилинної дірки добрані бари без скору,
  кожна свічка потоку — зі своїм скором, `scorer.scored` = кількість свічок потоку.
- **Лишилось (не виправлялось):** добрані REST-бари дірки в торговому воркері в екстрактор не потрапляють (скор свічки
  потоку конвеєр рахує до `on_candle`), тож перша свічка після такої дірки скориться відносно несуміжної історії —
  консервативно, як п. (г) WIRE-01.
- **Перезаміри рецензії (2026-09-19, машина розробника; усі числа — з цих команд):**
  * `uv run python scripts/train_anomaly_mlp.py --out <scratch>/rocauc.md --model-out <scratch>/model.json` (10.4 с):
    звіт = `docs/figures/quality_mlp_rocauc.md` без рядків команди й часу (`diff` порожній); `model` і
    `params_sha256 98a2bebf…` артефакту — ті самі, що в `data/anomaly_mlp_BTCUSDT.json` (відрізняється лише
    `provenance`); поріг 2.865277120886994, 21 382 вектори, 82 ітерації, round trip побітово — true.
  * `… --is-days 12 --holdout-days 3 --no-model --out <scratch>`: 5-3-5 0.9792 ± 0.0045, 8-3-8 0.9567 ± 0.0038,
    FPR@q99 0.0127 / 0.0433 — як у WIRE-01.
  * Лише SELECT з робочої БД: `anomaly_score` заповнено в 0 із 130 695 рядків `candle`; модель на 45 днях — 214/21 382
    (1.0008 %) днів 1–15 і 522/43 200 (1.2083 %) днів 16–45; `anomaly_warmup_hook(rest=None)` перед 2026-09-18 00:00 —
    218 барів, безперервний хвіст 218.
  * `IngestPipeline.run` над записаними сесіями з артефактом і прогрівом із REST-фікстури:
    `sample_btcusdt_4m` — 3 свічки, 3 скори (0.032629, 0.038478, 0.099604), 0 аномалій; `pathological/flash_crash` —
    2 з 3 (скори 3.79·10⁶ і 2.40·10⁶ → у БД 9999.999999), validity години 0.99971; `btcusdt_2026-09-18` — 45 скорів,
    0 аномалій; `scenarios/flash_crash` — 10 аномалій (скори 36.7…62 584). На 19 хвилинах, для яких є REST-бар, скор
    потоку після 218 барів прогріву відрізняється від пакетного скору по всій історії фікстури на 2.4·10⁻¹⁰…1.75·10⁻⁸
    (ATR Уайлдера має нескінченну пам'ять — різний старт), прапорці однакові.
  * Реплеї `--profile replay --speed inf --no-db` після виправлень: чистий — 0 аномалій на 45, `equity_hash d0f45aa3…`,
    `q_last` 0.98055 (так само з `--no-anomaly`); `--scenario flash_crash` — 10 аномалій, HALTED о 1789760039999000000,
    `equity_hash a5b8aa3a…` (так само з `--no-anomaly`), `q_last` 0.98026 проти 0.98055.
  * Набір за замовчуванням `uv run pytest -q -p no:randomly`: HEAD `ca1a6b0` (копія `git archive` у scratch) — 918 passed
    за 31.53 с; дерево після рецензії — 978 passed за 32.65 с (ціль брифінгу < 25 с не досягнута й на HEAD).
    Покриття `pytest --cov` (конфіг без omit) — 83.56 %; ті самі дані з колишнім omit (cli, workers, infra) — 87.38 %.
    ruff — «All checks passed!», mypy core/fuzzy/risk — «no issues found in 35 source files», pip-audit — «No known
    vulnerabilities found», `make -n` — усі 21 ціль розгортаються, YAML CI — 4 задачі (`yaml.safe_load`).
