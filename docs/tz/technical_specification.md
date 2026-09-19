# Технічне завдання на програмний сервіс FuzzHelm

Структура за ГОСТ 19.201-78 «Техническое задание. Требования к содержанию и оформлению». Автор: Андрій Жук, 2026.
Редакція: до звіту з практики, стан HEAD `415acbd` (2026-09-19) + фінальний прохід документації. Нормативне джерело вимог — `docs/BRIEF.md`.
Розходження вимог із реалізацією — `docs/deviations.md`. Колонку результатів приймання заповнено на HEAD `415acbd` (2026-09-19).
Кроки людини й невиконані артефакти названо прямо: «не виконано: причина; що зробити і ким».

---

## 1. Вступ

### 1.1 Найменування програми

**FuzzHelm** — програмний сервіс агрегації біржових даних та автоматизації маржинальних торговельних операцій з
нечітко-логічним ядром прийняття рішень та ієрархічною підсистемою автоматичного контролю ризиків і лімітів.
Позначення пакета — `fuzzhelm`, версія 0.1.0 (`pyproject.toml`).

### 1.2 Коротка характеристика галузі застосування

Галузь — автоматизоване прийняття торговельних рішень на ринку безстрокових ф'ючерсів криптовалют (Binance USDⓈ-M) за неповних і
зашумлених даних. Сервіс працює **лише** з публічними read-only даними і симульованим (paper) або тестовим (testnet) виконанням.
Шляху до реальних коштів немає за побудовою (allowlist хостів). Сервіс **не є інвестиційною рекомендацією**. Призначення — навчальна
і дослідницька демонстрація методу: інтерпретованого нечіткого ядра рішень у детермінованій ризик-оболонці («м'яке пропонує, жорстке
вирішує»).

---

## 2. Підстави для розробки

- **Документ-підстава:** індивідуальне завдання проєктно-технологічної практики студента 4 курсу ОПП F3 «Комп'ютерні науки
  (Обчислювальний інтелект смарт-систем)» (дослівний текст — брифінг §2):
  > «Розробити програмний сервіс агрегації біржових даних та автоматизації торговельних операцій із модулем ризик-менеджменту.
  > Розробити модулі підключення до API біржі за протоколами REST і WebSocket для збору, нормалізації та збереження ринкових
  > котирувань у реальному часі, а також створити підсистему тестування стратегій на історичних даних (backtesting). Реалізувати
  > модуль прийняття торговельних рішень за заданими алгоритмічними правилами, підсистему автоматичного контролю ризиків і лімітів,
  > а також покрити ключову логіку модульним тестуванням.»
- **Організація, що затвердила завдання:** кафедра автоматизованих систем управління (АСУ) ІКНІ НУ «Львівська політехніка».
- **Виконавець:** Андрій Жук.
- **Найменування теми розробки:** «Програмний сервіс агрегації біржових даних та автоматизації маржинальних торговельних операцій з
  нечітко-логічним ядром прийняття рішень та ієрархічною підсистемою автоматичного контролю ризиків і лімітів».

---

## 3. Призначення розробки

**Функціональне призначення.** Сервіс виконує:
1. збирання котирувань з двох незалежних публічних джерел (Binance, Kraken) через REST і WebSocket;
2. нормалізацію, дедуплікацію, контроль якості та збереження котирувань у PostgreSQL із журналом подій з ланцюгом хешів;
3. прийняття торговельних рішень нечітким виводом Мамдані за базою правил, що зберігається як дані;
4. автоматичний контроль ризиків і лімітів детермінованим контуром, який не може збільшити експозицію;
5. автоматизоване виконання (paper / testnet) і тестування стратегій на історичних даних тим самим кодом рішень;
6. пояснення кожного рішення (`/explain`) і аудит кожної дії.

**Експлуатаційне призначення.** Навчальний стенд для демонстрації на захисті практики (сценарій брифінгу §15) і відтворюваних
обчислювальних експериментів (фаза 7). Будь-яке число звіту відтворюється за `run_id` через паспорт прогону.

---

## 4. Вимоги до програми

### 4.1 Вимоги до функціональних характеристик

#### 4.1.1 Ролі користувачів

За `docs/security.md` §3 (матриця «дозвіл × роль» `fuzzhelm.api.auth.ACCESS_MATRIX`, 12 дозволів):

| Роль | Код | Права |
|---|---|---|
| Оператор | `operator` | читання всіх даних, стратегій, прогонів, рішень, ризику, потоку; **зміна стратегій**; запуск бектестів |
| Аналітик | `analyst` | читання; запуск бектестів |
| Аудитор | `auditor` | читання; **журнал аудиту** `GET /audit`; бектестів не запускає (розділення обов'язків) |
| Адміністратор | `admin` | усі права оператора; **зміна лімітів ризику** `PUT /risk/limits`; **зняття kill-switch** `POST /risk/killswitch/release`; журнал аудиту |

Облікові записи створює людина командою `fuzzhelm user add` (PLAT-01). Для дозволів, що змінюють стан, роль перечитується з БД.

#### 4.1.2 Склад функцій

Джерело кожної вимоги — розділ брифінгу. Критерії приймання й тести — у розділі 8. Пріоритет: **О** — обов'язкова за
індивідуальним завданням, **Б** — обов'язкова за брифінгом, **Д** — бажана.

| Код | Вимога | Джерело | Пр. |
|---|---|---|---|
| FR-01 | Добір історії 1m-свічок Binance через REST з пагінацією, перекриттям сторінок в один бар і перевіркою стику; фіксоване відтворюване вікно даних | §1 п. 1, §4.3 `ingest.rest`, §12 ф. 1 | О |
| FR-02 | Лімітування REST-запитів token bucket з вагами біржі (доказово в межах ліміту), повтори з повним джитером і пошаною до `Retry-After` | §4.3, §10 C | Б |
| FR-03 | Довідник інструментів (tick, step, minNotional, mmr) з `exchangeInfo`, історія ставок фандингу, оцінка зсуву годинника | §4.3 | Б |
| FR-04 | WebSocket-клієнт реального часу (kline, aggTrade, depth20, markPrice): реконект з backoff, сторож тиші (heartbeat), класифікація розривів | §1 п. 1, §4.3 `ingest.ws` | О |
| FR-05 | Запис сирих WS-сесій у `jsonl.gz` і їх відтворення як `MarketFeed` (`ReplayFeed`) з керованим темпом | §4.3, §12 ф. 0 | Б |
| FR-06 | Виявлення прогалин за біржовими ідентифікаторами і часом, REST-добір з нульовою втратою подій, журнал `ingest_gap` | §4.3, §12 ф. 2 | О |
| FR-07 | Нормалізація JSON бірж у канонічні DTO на `Decimal`: строга схема, квантування до tick/step, розділення `ts_event`/`ts_ingest` | §1 п. 2, §4.3 `ingest.normalize` | О |
| FR-08 | Дедуплікація за `event_uid`, незалежна від порядку; крос-звірка Binance ↔ Kraken з порогом 50 б.п. | §2 рядок 1, §4.3 | О |
| FR-09 | Контроль якості даних: інваріанти свічки, погодинний скор Q з вагами за AHP (CR < 0.1), нейромережевий детектор аномалій (MLP-автокодувальник) | §4.3 `quality`, §5.17 | Б |
| FR-10 | Збереження в PostgreSQL 16 без розширень: 15 таблиць нормативного DDL, міграції Alembic, ідемпотентний upsert свічок, append-only журнал і аудит | §1 п. 2, §6 | О |
| FR-11 | Журнал подій з ланцюгом хешів BLAKE2b і перевірка ланцюга; паспорт прогону (`config_hash`, `dataset_hash`, `git_sha`, `seed`, `engine`, `journal_head_hash`, `equity_hash`) | §4.2, §6 `run`, `event_journal` | Б |
| FR-12 | Інкрементальні індикатори O(1) на бар (EMA, ATR і RSI Уайлдера, Боллінджер-Велфорд, Дончіан, Паркінсон, перцентильний ранг, OLS) і шість детекторів з контрактом `(s, c)`. Відхилення: перцентильний ранг — пошук O(log L) і вставка O(L) (F-08) | §5.1, §5.2 | О |
| FR-13 | Довірчо-зважений консенсус T, R, V і ентропійна узгодженість з коефіцієнтом довіри κ | §5.3, §5.4 | О |
| FR-14 | Нечіткий вивід Мамдані за базою 45 правил, що зберігається як дані (YAML) з валідацією; функції належності, калібровані з даних (перцентилі, KMeans); дефазифікація центроїдом, три схеми інтегрування | §5.5–§5.7, §7 | О |
| FR-15 | Пояснюваність рішення: повне трасування (`DecisionTrace`), `GET /decisions/{id}/explain` зі спрацьованими правилами, α, μ_agg, центроїдом, розкладкою сайзера і україномовним текстом | §1 п. 3, §8.1 | О |
| FR-16 | Сайзинг: таргетування волатильності + ATR-ризик + ліміт плеча (мінімум трьох), квантування лише вниз, відмова нижче minNotional; гістерезис (тригер Шмітта) | §5.8, §5.9 | О |
| FR-17 | Ризик-ланцюг із шести лімітів з алгеброю вердиктів ALLOW/SHRINK/VETO, яка не може збільшити експозицію; журнал кожної перевірки в `risk_event` | §5.11 | О |
| FR-18 | Автомат ризик-станів NORMAL/WARNING/COOLDOWN/HALTED з гістерезисом і витримкою, тотальна таблиця 4×6, засувний kill-switch, зняття лише адміністратором з аудитом | §5.12 | О |
| FR-19 | Розрахунок ціни ліквідації з умови Equity = MM; VaR/CVaR (історичний і параметричний) і тест Купця як звітні метрики | §5.10, §5.13 | Б |
| FR-20 | Автоматизоване виконання через порт `ExecutionVenue`: `PaperBroker` з моделлю витрат (виконання t → open t+1, песимізм «стоп раніше тейку»), `BinanceTestnetVenue` (HMAC-SHA256), `OrderRouter` з ідемпотентністю за `client_order_id` | §1 п. 6, §5.14 | О |
| FR-21 | Бектестинг тим самим кодом рішень, що в live: 17 метрик + PSR/DSR, walk-forward з embargo, паралельна сітка 108 клітинок, Парето-фронт, замір за законом Амдала, аналіз чутливості | §1 п. 5, §5.15, §5.16 | О |
| FR-22 | REST API і безпека: JWT HS256 (8 год), 4 ролі і матриця доступу, аудит з before/after, CRUD стратегій з валідацією і версіонуванням, ліміти ризику, kill-switch, SSE-потік подій | §8.1, §14 п. 2.9 | Б |
| FR-23 | Сервісна експлуатація: торговий (replay/paper) та ingest-воркер, планувальник періодичних задач, Telegram-нотифікації, CLI, резервне копіювання і відновлення | §4.3 `notify`, `scheduler`, §12 ф. 8, 9 | Б |
| FR-24 | Веб-панель із трьох екранів Vue 3 (`LiveView`, `ExplainView`, `BacktestView`), i18n uk/en | §8.2 | Б |

#### 4.1.3 Вхідні і вихідні дані

- **Вхід:** публічні відповіді Binance USDⓈ-M (`/fapi/v1/klines`, `exchangeInfo`, `premiumIndex`, `fundingRate`, `aggTrades`,
  `indexPriceKlines`; WS `/market/stream`, `/public/stream`) і Kraken (`/0/public/OHLC`, `AssetPairs`). Файли конфігурацій `config/*.yaml`
  (правила й МФ, детектори, ліміти, ваги якості, модель витрат, профілі запуску). Записані сесії `fixtures/ws/*.jsonl.gz`. HTTP-запити
  користувачів.
- **Вихід:** таблиці PostgreSQL (§6 брифінгу, `docs/db_schema.md`), JSON-відповіді API й SSE-події, звіти й рисунки в `docs/figures/`,
  виводи експериментів (`artifacts/`, `docs/report_tables/`), Telegram-повідомлення.

#### 4.1.4 Часові характеристики (виміряно, машина розробника)

Норм швидкодії брифінг не задає, крім часу тестів (NFR-08). Виміряні значення наведено для орієнтира з джерелами:

| Величина | Значення | Джерело |
|---|---|---|
| REST-добір 45 днів × 2 символи (129 600 барів) | 40.8 с | `docs/figures/backfill_report.md` |
| Повний крок торгового циклу, 3 000 барів фікстури, Мамдані | 44.5 мкс/бар (load ≈ 7) | `docs/api/engine.md` §4 |
| Одна клітинка сітки на 45-денному вікні (64 800 барів) | 57.1 мкс/бар | `docs/api/engine.md` §4 |
| Уся сітка 108 × 64 800 барів, 8 воркерів | 123.4 с (рецензія: 96.7 с) | `docs/api/engine.md` §4 |
| `MamdaniEngine.infer_u` | 7.7 мкс | `docs/api/fuzzy.md` |
| `POST /backtests` → 202 / задача DONE (64 800 барів) | 17.3–19.6 мс / 6.72–7.01 с | `docs/api/api.md` §5 |
| `/decisions/{id}/explain`, 180 рішень: медіана / максимум | 4.8 / 14.4 мс (прогін 2) | `docs/api/api.md` §5 |

### 4.2 Вимоги до надійності

#### 4.2.1 Нефункціональні вимоги

| Код | Вимога | Джерело |
|---|---|---|
| NFR-01 | **Безпека виконання:** лише paper/testnet; мережеві з'єднання лише з хостами allowlist (testnet для ордерів, read-only для даних, `api.telegram.org`); порушення зупиняє побудову конфігурації | §0.2, §3 |
| NFR-02 | **Детермінізм і відтворюваність:** у пакетах `core, features, detectors, fuzzy, decision, risk, sizing` немає настінного годинника й випадковості; той самий `(config, dataset, seed, engine, git_sha)` дає той самий `equity_hash`; результат сітки не залежить від кількості воркерів | §4.2, §6 `run` |
| NFR-03 | **Точність грошових обчислень:** гроші, ціни й обсяги — лише `Decimal`/`NUMERIC(38,18)`; конвертація Decimal↔float лише у двох точках; тотожність обліку капіталу на кожному кроці | §4.2, §10 K |
| NFR-04 | **Надійність інжесту й журналу:** відновлення після розриву, дублікатів, перестановок, стрибка годинника, «напівмертвого» з'єднання з нульовою втратою подій; зупинка воркера не рве ланцюг журналу; журнал і аудит — лише дописування | §12 ф. 2, §6 |
| NFR-05 | **Інформаційна безпека:** модель загроз STRIDE з контрзаходами (bcrypt, JWT лише HS256, обмеження спроб входу, аудит у тій самій транзакції, межі вводу, секрети лише в оточенні), аудит залежностей `pip-audit` | §14 п. 2.9, `docs/security.md` |
| NFR-06 | **Офлайн-працездатність демо:** увесь сценарій захисту працює з вимкненою мережею на записаній фікстурі | §15, §17 |
| NFR-07 | **Тестованість і якість коду:** покриття ≥ 80 % загалом і ≥ 90 % для `fuzzy/risk/decision/sizing`; `ruff`; `mypy --strict` для `core/fuzzy/risk`; назви тестів брифінгу — дослівно | §10, §9 |
| NFR-08 | **Продуктивність тестів:** unit + property < 25 с, без мережі і `sleep` | §10 |
| NFR-09 | **Переносність і розгортання:** Docker Compose (db, api, worker, ui), залежності зафіксовано в `uv.lock`, PostgreSQL 16 без розширень; процедура резервного копіювання й відновлення | §9, §12 ф. 9 |

#### 4.2.2 Заходи забезпечення надійності

- Засувка HALTED знімається лише адміністратором. Kill-switch, спрацьований між барами, скасовує заявки на збільшення позиції
  (ENG-20).
- Відмови через некоректні дії користувача: помилка вводу дає 422 з точним шляхом до поля, а не 5xx (API-16). Невалідна
  конфігурація ризику не записується (API-04). Невалідний файл лімітів у воркері відхиляється без зміни стану (W-18).
- Відновлення після рестарту: пропущені команди керування воркер дочитує з `audit_log` (API-06, W-14). Черга бектестів рестарту не
  переживає, і це задокументоване обмеження (API-08).

### 4.3 Умови експлуатації

- Середовище — робоча станція розробника (macOS або Linux) з Docker і `uv`. Репозиторій лежить **не** в iCloud (`~/dev/fuzzhelm`,
  брифінг §0.3).
- Персонал: один оператор-студент; ролі API — розділ 4.1.1. Спеціальної кваліфікації понад інструкцію
  `docs/manuals/user_guide.md` не потрібно.
- Кліматичні умови — звичайні для офісної ПЕОМ. Охорона праці — підрозділ 1.5 звіту (ДСанПіН 3.3.2.007-98).
- Живий режим потребує доступу до публічних хостів Binance. Демо й тести працюють офлайн.

### 4.4 Вимоги до складу і параметрів технічних засобів

Мінімальні вимоги окремо не вимірювались. Нижче виміряні параметри стенда і даних з джерелами:

| Параметр | Значення | Джерело |
|---|---|---|
| Процесор стенда | Apple M3, 4 продуктивні + 4 енергоефективні ядра | `docs/deviations.d/exp_search.md` XS-05 |
| Таблиця `candle` з індексами на 130 000 свічок | 29 941 760 байт (≈ 28.6 МіБ), BRIN — 24 576 байт | `docs/db_schema.md` §3.2 |
| Резервна копія робочої БД (`pg_dump -Fc`) | 9 206 764 / 9 332 499 байт | `docs/manuals/backup_runbook.md` §4 |
| Образ `api`/`worker` | 852 МБ кожен, контекст збирання 15.57 МБ | `docs/manuals/deployment.md` §3a |
| Оперативна пам'ять: бектест 45 днів BTCUSDT у пам'яті (64 800 барів) | max RSS 243 023 872 байти (≈ 232 МіБ), рушій 4.22 с | `docs/report_tables/raw/ram_profile.txt` |
| Оперативна пам'ять: процес API після імпорту застосунку | max RSS 95 846 400 байт | там само (узгоджується з WIRE-05: 95 731 712) |
| Оперативна пам'ять: типовий набір тестів послідовно | max RSS 732 692 480 байт | там само |
| PostgreSQL 16 у спокої | контейнер 185.6 МіБ; БД 462 584 855 байт; `shared_buffers` 128 МБ | там само |

### 4.5 Вимоги до інформаційної і програмної сумісності

- **Мова і середовище:** Python 3.12 (`.python-version`), керування залежностями `uv` з `uv.lock`. Основні бібліотеки: FastAPI, Pydantic 2,
  SQLAlchemy 2.0 async + asyncpg, Alembic, httpx, websockets, numpy, scikit-learn, PyJWT, passlib/bcrypt, APScheduler, orjson.
  Фактичні версії — D-05 і `uv.lock`.
- **СУБД:** PostgreSQL 16 (образ `postgres:16-alpine`) без розширень; схема — міграції `0001_core` … `0004_decision_trace_extras`.
- **Протоколи й формати:** HTTPS REST і WebSocket бірж; JSON; REST API з описом OpenAPI 3.1 (`/docs`, `/openapi.json`); SSE; JWT (RFC 7519,
  HS256); YAML-конфігурації; сесії WS — gzip JSON Lines (`docs/contracts.md` §7).
- **Розгортання:** Docker Compose (`docker-compose.yml`: `db` на порту хоста 5442, `api` на `127.0.0.1:8000`, `worker`, `ui` у профілі);
  тестова БД — `docker-compose.test.yml` (порт 5443, tmpfs).
- **ОС:** macOS / Linux (перевірено на macOS, машина розробника).

### 4.6 Вимоги до маркування та пакування (скорочено)

Версіонування за SemVer (`pyproject.toml`: `version = "0.1.0"`; журнал змін — `CHANGELOG.md`). Паспорт кожного прогону несе `git_sha`
(з `git rev-parse` або з build-arg `GIT_SHA` → `FUZZHELM_GIT_SHA` в образі). Пакування — Docker-образ з `Dockerfile` і Python-пакет
(`uv_build`).

### 4.7 Вимоги до транспортування та зберігання (скорочено)

Код зберігається в git-репозиторії поза iCloud. Резервні копії БД — `pg_dump -Fc` за `docs/manuals/backup_runbook.md`. У теку практики
в iCloud складаються лише результати: звіт, рисунки, архів коду. Секрети передаються лише через `.env`, який не комітиться.

### 4.8 Спеціальні вимоги

1. Жодних реальних грошей і жодного mainnet (allowlist, тест `test_mainnet_host_is_rejected_by_config`).
2. Система не є інвестиційною рекомендацією. Це декларується в README і звіті (підрозділ 2.13).
3. Кроки, що потребують креденшлів (testnet-ключі, Telegram-бот, Fly.io, адміністратор API, LOGIN-роль БД), виконує лише студент
   **[ЛЮДИНА]**.
4. Усі числа звіту мусять бути виміряні. До виміру відсутнє позначається маркером TBD. На момент приймання маркерів у `docs/` немає, крім
   12 рядків нормативної специфікації `docs/BRIEF.md` (правило §0.1, шаблони §7, текст gate-критеріїв), яку не редагують (FIN-07). Розходження зі специфікацією — у `docs/deviations.md`.
5. Правила рішень — дані, а не код: база правил і функції належності змінюються через API/YAML з валідацією і версіонуванням.

---

## 5. Вимоги до програмної документації

| Документ | Файл | Стан |
|---|---|---|
| Технічне завдання | `docs/tz/technical_specification.md` | цей документ |
| Опис архітектури і контрактів модулів | `docs/contracts.md`, `docs/api/*.md` | є |
| Модель даних (3 рівні) | `docs/db_schema.md`, `docs/diagrams/er_*.puml` | є |
| Інформаційна безпека (STRIDE, матриця доступу) | `docs/security.md` | є |
| Керівництво користувача | `docs/manuals/user_guide.md` | є |
| Інструкція з розгортання | `docs/manuals/deployment.md` | є |
| Runbook резервного копіювання | `docs/manuals/backup_runbook.md` | є |
| Розходження спеки й реалізації | `docs/deviations.md` | є |
| Журнал робіт | `docs/journal.md` | є |
| Реєстр ризиків проєкту | `docs/risk_register.md` | є |
| Діаграми UML (PlantUML) | `docs/diagrams/*.puml` | є |
| ТЕО (трудомісткість, кошторис, TCO) | — | не виконано: потрібні вхідні дані автора (ставка, тривалість практики за бланком, вартість ПЕОМ і хмари); виміряні ресурсні параметри — 4.4; що потрібно: автор складає `docs/teo/cost_estimate.md` |
| WBS, діаграма Ганта, сітьовий графік | — | не виконано: це план-графік практики для звіту (§14 п. 1.6); фактична хронологія є в `docs/journal.md` і розділі 7; що потрібно: автор будує WBS, Гант і сітьовий графік з датами практики |
| README (uk/en), CHANGELOG | `README.md`, `README.en.md`, `CHANGELOG.md` у корені репозиторію | є |
| Англомовний abstract | — | не виконано: це додаток Ж звіту фази 11, який запускається окремою командою; англомовний опис системи — `README.en.md` |

---

## 6. Техніко-економічні показники

Розрахунок трудомісткості, кошторису і сукупної вартості володіння (TCO) виконується окремим документом фази 10 (брифінг §9,
`docs/teo/cost_estimate.md`). **Не виконано:** для ТЕО потрібні вхідні дані автора (ставка, тривалість практики, вартість обладнання
і хмарного ресурсу), яких у репозиторії немає; документ складає автор. Цей розділ цифр не містить, щоб не вводити невиміряних значень. Фактичні
ресурсні параметри стенда (обсяг даних, розмір копій і образів, швидкодія) наведено в 4.1.4 і 4.4 як вхідні дані для ТЕО.

---

## 7. Стадії та етапи розробки

Дати — з історії комітів (`git log --format='%h %ad %s' --date=iso`, пояс +03:00). Фази — за брифінгом §12, хвилі — фактична
організація робіт (`docs/journal.md`).

| Етап | Зміст робіт | Фази брифінгу | Коміти / дата | Стан |
|---|---|---|---|---|
| 0. Плацдарм | каркас пакетів, контракти `core`, архітектурні тести, рекордер WS, конфігурації; перенесення портів БД; запис живої 45-хв сесії | 0 | `e5fbefb` 2026-09-18 22:14; `0b0cda4` 22:16; `f838b99` 22:52 | виконано |
| 1. Хвиля 1 — модулі | REST- і WS-інжест, нормалізація, якість, сховище (Alembic 0001–0003), ознаки й детектори, нечітке ядро, рішення, сайзинг і ризик, виконання, аналітика бектесту | 1–6 (модулі) | `287b086` 2026-09-18 23:56 | виконано |
| 2. Хвиля 2 — дані й інтеграція | 45-денний добір, фандинг, крос-звірка, калібрування МФ; торговий цикл і рушій бектесту; API, безпека, SSE, планувальник, Telegram; воркери; перший повний прогін; сценарій flash_crash | 1–3 (gate), 6, 8 | `5fed0fd` 2026-09-19 12:58 (WIP, перерив); `8b94897` 15:13 | виконано |
| 3a. Хвиля 3a — виправлення і платформа | політика COOLDOWN, специфікація в паспорті, ковзні VaR/CVaR, CLI користувачів, PyJWT, MLP на справжньому IS, compose | 5, 8, 9 (частково) | `4ef69ab` 2026-09-19 16:08 | виконано |
| 3b. Хвиля 3b — обчислювальний експеримент | інструменти walk-forward, сітки, Парето, DSR, Амдала, чутливості, моделей витрат, ablation, VaR/Купця, гістерезису, таблиць; оркестратор; повні прогони | 7 | `bd0a22a` 17:17; `b933802` 17:18; прогони з 14:18:14 UTC; результати — `ca1a6b0` 19:23 | виконано: 37 кроків OK, результати — `docs/results.md` |
| 4. Веб-панель і деплой | 3 екрани Vue, i18n, Fly.io | 9 | `fly.toml` — `2f77c82` | не виконано: веб-панель — порядок робіт D-06 (робить автор окремим етапом); деплой Fly.io — крок [ЛЮДИНА] (реєстрація), інструкція `docs/manuals/deployment.md` §6 |
| 5. Добивання | покриття, CI, mypy, MLP у контурі, ТЕО, WBS/Гант, реєстр ризиків, abstract, CHANGELOG, ТЗ | 10 | `ca1a6b0` 19:23 (ТЗ, реєстр ризиків); `2f77c82` 20:51; `415acbd` 20:54; фінальний прохід документації після `415acbd` | частково: покриття 89.38 %, CI, mypy, реєстр ризиків, ТЗ, README/CHANGELOG — виконано; ТЕО, WBS/Гант, abstract — не виконано (розділ 5) |
| 6. Звіт | звіт на 25–30 сторінок | 11 | за окремою командою | не розпочато |

---

## 8. Порядок контролю та приймання

### 8.1 Види випробувань

| Вид | Засіб | Команда |
|---|---|---|
| Модульні, property-based, архітектурні, e2e (офлайн) | pytest + hypothesis, без мережі й `sleep` | `make test` (`uv run pytest -q`) |
| Інтеграційні (PostgreSQL 16) | маркер `integration`, тестова БД 5443 | `make test-int` |
| Покриття | pytest-cov | `make cov` |
| Статичний контроль | ruff; mypy `--strict` для `core`, `fuzzy`, `risk` | `make lint` |
| Ручні процедури | CLI, скрипти, API | див. таблицю 8.3, колонка «тест/процедура» |

Обсяг набору на HEAD `415acbd`: 1034 вузли, у типовому відборі 979 (55 відібрано маркерами `integration`/`slow`). Результати:
`make test` → 979 passed за 12.83 с; разом з інтеграційними → 1034 passed, 0 failed; покриття 89.38 %; `make lint` чистий
(`docs/report_tables/raw/`).

### 8.2 Порядок приймання

1. Приймання проводиться на стенді за `docs/manuals/deployment.md` з мігрованою робочою БД і даними вікна `data/dataset_window.json`.
2. Спершу автоматичні випробування (8.1), потім ручні процедури з таблиці 8.3.
3. Вимога приймається, якщо її критерій виконано, усі вказані тести пройдено і (за наявності) ручну процедуру виконано з
   результатом, записаним у колонку «результат». Невиконані кроки [ЛЮДИНА] фіксуються як «не виконано: немає креденшлів»
   (брифінг §12.9) і не блокують інших вимог.
4. Акт приймання — ця таблиця із заповненою колонкою «результат» і виводом `pytest`/`pytest --cov` у додатку Г звіту.

### 8.3 Таблиця приймальних випробувань «вимога → критерій → тест/процедура → результат»

Тести наведено за дослівними назвами функцій з `tests/`. `(int)` — інтеграційний тест (потрібна БД). Колонку «результат» заповнено
за прогоном на HEAD `415acbd` 2026-09-19: разом з інтеграційними 1034 passed, 0 failed. Усі 212 тест-функцій, названих у цій таблиці,
є серед зібраних вузлів. Перевірено зіставленням імен з цього файлу з `uv run pytest --collect-only -q -m 'integration or not
integration'`. Факти ручних процедур мають джерело.

| Вимога | Критерій приймання | Тест / процедура | Результат |
|---|---|---|---|
| FR-01 | у `candle` ≥ 60 000 рядків; вікно — 45 повних UTC-днів; стики сторінок звірено | `test_paginator_stitches_segments_with_one_bar_overlap`, `test_paginator_detects_gap_in_overlap`, `test_dataset_window_is_45_full_utc_days_ending_at_midnight`, `test_backfill_gaps_become_ingest_gap_rows_and_end_filled`; процедура `uv run fuzzhelm backfill --days 45` + `db-stats` | процедура: після добору `candle` = 129 603 рядки (`docs/deviations.d/data.md`, рецензія); на момент приймання — 130 695, з них у вікні по 64 800 закритих барів на символ (`uv run fuzzhelm db-stats`, 2026-09-19); тести пройдено: 1034 passed, 0 failed (`415acbd`, `docs/report_tables/raw/test_runs.txt`) |
| FR-02 | жодного 429 від власного бюджету: за будь-які 60 с ≤ 2 400 ваги; `Retry-After` виконується; джитер у межах cap | `test_token_bucket_blocks_on_weight_exhaustion`, `test_bucket_window_bound_default_is_provably_within_binance_limit`, `test_klines_weight_table_matches_measured_headers`, `test_retry_after_header_honored`, `test_backoff_full_jitter_within_cap`, `test_agg_trades_and_index_klines_endpoints_charge_measured_weights` | процедура: максимум заголовка ваги 734 з 2 400 при доборі (`docs/figures/backfill_report.md`); тести пройдено: 1034 passed, 0 failed (`415acbd`, `docs/report_tables/raw/test_runs.txt`) |
| FR-03 | довідник і ставки фандингу покривають вікно; специфікація інструмента змінює `dataset_hash` | `test_funding_history_paginates_by_time_and_filters_window`, `test_committed_funding_files_are_valid_and_cover_the_dataset_window`, `test_funding_columns_enter_the_backtest_dataset_hash`, `test_changing_mmr_changes_the_run_passport_hash` | тести пройдено: 1034 passed, 0 failed (`415acbd`, `docs/report_tables/raw/test_runs.txt`) |
| FR-04 | два з'єднання (market/public); сторож тиші форсує реконект; фатальні розриви зупиняють клієнт | `test_ws_client_two_connections_from_settings`, `test_ws_client_heartbeat_watchdog_forces_reconnect`, `test_ws_client_close_codes_drive_backoff_and_fatal_stops`, `test_backoff_exponential_capped_jittered_and_reproducible`, `test_close_code_classification` | процедура: живий ingest 1 хв — 1 088 кадрів, 0 реконектів, 0 прогалин (`docs/deviations.d/workers.md` W-20); тести пройдено: 1034 passed, 0 failed (`415acbd`, `docs/report_tables/raw/test_runs.txt`) |
| FR-05 | формат сесії за контрактом; запис відтворюваний побайтово; реплей — `MarketFeed` | `test_recorder_writes_contract_format_and_replay_reads_it_back`, `test_write_session_is_byte_reproducible`, `test_replay_feed_is_market_feed_in_ingest_order`, `test_replay_pacing_uses_injected_clock_and_sleep` | тести пройдено: 1034 passed, 0 failed (`415acbd`, `docs/report_tables/raw/test_runs.txt`) |
| FR-06 | усі 6 патологічних сесій — 0 втрачених і 0 дубльованих подій; у `ingest_gap` є рядки FILLED | `test_all_pathological_sessions_recover_with_zero_lost_events`, `test_gap_detected_and_backfilled_idempotently`, `test_gap_detector_seq_confirms_wide_hole_and_heals_reordered_ids`, `test_gap_detector_time_detects_lost_close_from_other_streams`, `test_late_close_after_skip_does_not_mark_gap_filled`; процедура `uv run fuzzhelm replay-gap` | процедура: 3 рядки FILLED, `zero_loss = True` (`docs/figures/backfill_gap_replay.md`); таблиця 6 сценаріїв — `docs/figures/ingest_pathological.md`; тести пройдено: 1034 passed, 0 failed (`415acbd`, `docs/report_tables/raw/test_runs.txt`) |
| FR-07 | мс → нс точно; ціна HALF_EVEN до tick; кількість вниз до step; невідоме поле → помилка з шляхом; `ts_event` і `ts_ingest` не змішуються | `test_kline_ms_to_ns_exact`, `test_price_quantized_to_tick_half_even`, `test_qty_floored_to_step`, `test_reject_below_min_notional_with_code`, `test_unknown_field_raises_normalization_error`, `test_event_and_ingest_time_never_mixed` | тести пройдено: 1034 passed, 0 failed (`415acbd`, `docs/report_tables/raw/test_runs.txt`) |
| FR-08 | результат дедуплікації не залежить від перестановки; розбіжність > 50 б.п. позначається | `test_dedup_idempotent_under_permutation`, `test_event_uid_stable_across_restart`, `test_binance_kraken_price_crosscheck_flags_divergence_above_50bps`, `test_crosscheck_on_recorded_binance_and_kraken_data`; процедура `uv run fuzzhelm crosscheck --from-input data/crosscheck_input.json.gz` | процедура: 720/720 хвилин, max \|d\| 7.75 б.п., 0 хвилин > 50 б.п. (`docs/figures/crosscheck_report.md`); тести пройдено: 1034 passed, 0 failed (`415acbd`, `docs/report_tables/raw/test_runs.txt`) |
| FR-09 | невалідна свічка відхиляється; Q ∈ [0, 1]; ідеальна година → Q = 1; Σw = 1 і CR < 0.1; MLP ловить ін'єкцію і не ловить нормальний бар | `test_high_below_close_rejected`, `test_price_not_multiple_of_tick_rejected`, `test_dq_score_in_unit_interval`, `test_perfect_hour_scores_one`, `test_timeliness_decays_exponentially`, `test_ahp_weights_sum_to_one_and_cr_below_0_1`, `test_mlp_autoencoder_flags_injected_spike_and_not_normal_bar` | процедура: CR = 0.003639 (`config/dq_weights.yaml`); ROC-AUC 0.9531 (8-3-8) / 0.9729 (5-3-5) (`docs/figures/quality_mlp_rocauc.md`); робоча архітектура 5-3-5 підключена в ingest- і торговому воркерах з `2f77c82` (WIRE-01), моделі BTCUSDT і ETHUSDT — `data/anomaly_mlp_*.json`; тести пройдено: 1034 passed, 0 failed (`415acbd`, `docs/report_tables/raw/test_runs.txt`) |
| FR-10 | повторний upsert не змінює даних; закрита свічка не перезаписується; CHECK відхиляє невалідну свічку; UPDATE/DELETE журналу → 42501; ордер без рішення → помилка FK; міграції up/down чисті; схема = DDL брифінгу (крім ST-01) | `test_candle_upsert_idempotent` (int), `test_upsert_does_not_overwrite_closed_candle` (int), `test_check_constraint_rejects_invalid_candle` (int), `test_journal_append_only_revoked_update` (int), `test_order_requires_decision_fk` (int), `test_migrations_up_and_down_clean` (int), `test_migrated_schema_matches_brief_ddl` (int) | тести пройдено: 1034 passed, 0 failed (`415acbd`, `docs/report_tables/raw/test_runs.txt`); з них 54 інтеграційні на PostgreSQL 16 (`docker-compose.test.yml`, порт 5443) |
| FR-11 | float у канонічних даних відхиляється; зміна payload ламає ланцюг рівно на своєму seq; дайджест стабільний між процесами; голова ланцюга = паспорт | `test_canonical_json_rejects_float`, `test_decimal_quantized_not_normalized`, `test_keys_sorted_lexicographically`, `test_state_digest_stable_across_processes`, `test_hash_chain_links_prev_hash`, `test_tampered_payload_breaks_chain_at_exact_seq`, `test_rehashed_tampering_is_caught_at_next_seq_or_by_head_anchor`, `test_replay_worker_persists_run_with_full_passport` (int); процедура `uv run fuzzhelm verify-journal` | процедура `uv run fuzzhelm verify-journal` (2026-09-19, `415acbd`): 61 ланцюг — 57 «chain OK, anchor OK», 3 журнали інжесту «chain OK, no run row», 1 «chain BROKEN at seq 26» — FAILED-прогін `fcf44bb4…`, записаний до виправлення W-10 (`docs/report_tables/raw/verify_journal.txt`); тести пройдено: 1034 passed, 0 failed (`415acbd`, `docs/report_tables/raw/test_runs.txt`) |
| FR-12 | RSI14/ATR14 збігаються з golden (atol 1e−9); α Уайлдера = 1/n; інкрементальне = пакетне; s ∈ [−1, 1], c ∈ [0, 1], без NaN для будь-якого OHLCV; детектори чисті | `test_rsi14_matches_wilder_golden_csv`, `test_atr14_matches_wilder_golden_csv`, `test_wilder_alpha_is_one_over_n_not_two_over_n_plus_one`, `test_ema_incremental_equals_batch`, `test_welford_stable_on_1e9_offset_series`, `test_donchian_deque_matches_naive_max`, `test_parkinson_vol_exact_on_constant_range`, `test_ols_r2_near_one_on_clean_trend`, `test_indicators_return_none_before_warmup`, `test_all_detectors_bounded_and_no_nan_on_any_ohlcv`, `test_all_detectors_are_pure`, `test_all_detectors_registered`, `test_ema_slope_positive_on_linear_uptrend`, `test_donchian_confidence_decays_with_staleness`, `test_rsi_convex_map_weak_in_middle`, `test_bollinger_confidence_drops_when_bandwidth_expands`, `test_pin_bar_score_zero_when_wicks_symmetric` | тести пройдено: 1034 passed, 0 failed (`415acbd`, `docs/report_tables/raw/test_runs.txt`) |
| FR-13 | усі c = 0 ⇒ T = R = 0; p₊ + p₋ + p₀ ≡ 1; рівномірний розподіл ⇒ κ = 0.35 ± 1e−6; κ монотонна за узгодженістю; трасування містить кожне спрацьоване правило | `test_consensus_zero_when_all_confidences_zero`, `test_consensus_between_min_and_max_contribution`, `test_membership_probabilities_sum_to_one`, `test_agreement_is_one_when_all_same_sign`, `test_kappa_reaches_kmin_on_uniform_split`, `test_kappa_monotone_in_agreement`, `test_entropy_handles_zero_probability`, `test_decision_trace_contains_every_fired_rule` | тести пройдено: 1034 passed, 0 failed (`415acbd`, `docs/report_tables/raw/test_runs.txt`) |
| FR-14 | рівно 45 правил без дублікатів, усі терми існують, w ≡ 1; Руспіні для T і R; без мертвих зон; порожня активація → u = 0; порядок трапецій ≈ 2; конфіг МФ — записане калібрування | `test_tri_mf_peak_equals_one`, `test_trap_plateau_is_flat`, `test_gauss_mf_symmetry`, `test_mf_partition_of_unity_ruspini`, `test_mf_coverage_no_dead_zones`, `test_rulebase_yaml_has_exactly_45_rules`, `test_rulebase_no_duplicate_antecedents`, `test_rulebase_all_terms_exist_in_membership_config`, `test_rule_firing_is_min_of_memberships`, `test_dont_care_term_contributes_one`, `test_clipped_consequent_never_exceeds_alpha`, `test_aggregation_is_pointwise_max`, `test_centroid_of_symmetric_aggregate_is_zero`, `test_empty_activation_returns_exactly_zero`, `test_defuzz_grid_convergence_order_is_two_for_trapezoid`, `test_u_nondecreasing_in_trend_input`, `test_weighted_rules_break_monotonicity_counterexample`, `test_rule_table_satisfies_declared_constraints`, `test_committed_membership_is_the_recorded_calibration_phase3_gate` | тести пройдено: 1034 passed, 0 failed (`415acbd`, `docs/report_tables/raw/test_runs.txt`); зміст G16 звужено (FZ-01) |
| FR-15 | `/explain` повертає спрацьовані правила з α, належності, μ_agg, центроїд, розкладку сайзера і непорожній український текст; розбіжність перерахунку не приховується | `test_get_explain_returns_fired_rules_and_memberships`, `test_explain_narrative_is_ukrainian_and_non_empty`, `test_explain_flags_inconsistency_when_strategy_config_differs`, `test_decision_core_end_to_end_with_real_detectors_and_mamdani` | процедура: 180 з 180 `/explain` мають `consistency.ok = true` (`docs/api/api.md` §5); тести пройдено: 1034 passed, 0 failed (`415acbd`, `docs/report_tables/raw/test_runs.txt`) |
| FR-16 | розмір обернено пропорційний ATR при сталому грошовому ризику; ціль = мінімум трьох обмежень; квантування лише вниз; тригер Шмітта не дає брязкоту; HALTED ⇒ 0 | `test_position_size_inverse_to_atr`, `test_vol_target_halves_notional_when_vol_doubles`, `test_vol_target_clipped_at_bounds`, `test_first_order_filter_reaches_63pct_in_T`, `test_final_qty_is_min_of_three_constraints`, `test_sizer_reports_binding_constraint`, `test_qty_floored_never_rounded_up`, `test_hysteresis_prevents_flip_flop`, `test_size_zero_in_halted_regardless_of_signal` | тести пройдено: 1034 passed, 0 failed (`415acbd`, `docs/report_tables/raw/test_runs.txt`); зміст I4 уточнено (R-01) |
| FR-17 | композиція будь-якої кількості вердиктів не збільшує експозицію; VETO поглинає; порядок правил не впливає; кожна перевірка — запис з `observed` і `limit` | `test_risk_chain_never_increases_exposure`, `test_veto_absorbs_everything`, `test_compose_is_order_independent`, `test_max_daily_loss_resets_at_utc_midnight`, `test_drawdown_uses_running_peak`, `test_stale_data_vetoes_on_lag_and_on_low_dq`, `test_liquidation_guard_reduces_leverage_before_veto`, `test_every_verdict_written_to_risk_event_with_observed_and_limit`, `test_guard_approved_position_satisfies_every_limit`, `test_risk_chain_never_exceeds_requested_or_kappa_scaled_size` | тести пройдено: 1034 passed, 0 failed (`415acbd`, `docs/report_tables/raw/test_runs.txt`) |
| FR-18 | таблиця переходів тотальна (24 клітинки); гістерезис і витримка блокують передчасне повернення; HALTED знімає лише admin; flash_crash → HALTED без участі людини; засувка скасовує вхід у черзі | `test_risk_fsm_transition_table_is_total`, `test_hysteresis_blocks_recovery_inside_band`, `test_dwell_time_blocks_premature_recovery`, `test_halted_requires_manual_release_by_admin`, `test_state_machine_path_is_independent_of_cooldown_policy`, `test_flash_crash_reaches_halted_without_human_input`, `test_halted_latches_until_admin_release`, `test_killswitch_tripped_between_bars_cancels_queued_increase_but_keeps_open_stop`, `test_flash_crash_halts_on_db_and_only_admin_release_via_api_unlatches` (int); процедура `uv run python scripts/demo_flash_crash.py` | процедура: NORMAL → HALTED о 19:33:59, 17 барів HALTED без виконань; analyst/operator — відмова, admin → COOLDOWN (`docs/deviations.d/workers.md`, рецензія); тести пройдено: 1034 passed, 0 failed (`415acbd`, `docs/report_tables/raw/test_runs.txt`) |
| FR-19 | P_liq лонга 10× = 90.4523; margin ratio = 1 на P_liq; CVaR ≥ VaR завжди; LR = 0 при очікуваній кількості пробоїв; 20/500 не відкидається (R-02); VaR у кривій з 500-ї дохідності | `test_liq_price_long_10x_equals_90_4523`, `test_liq_price_short_symmetry`, `test_margin_ratio_is_one_at_liq_price`, `test_cvar_ge_var_always`, `test_kupiec_lr_zero_when_breaches_equal_expected`, `test_kupiec_rejects_at_20_breaches_of_500`, `test_engine_equity_points_carry_money_var_cvar_from_the_500th_return` | тести пройдено: 1034 passed, 0 failed (`415acbd`, `docs/report_tables/raw/test_runs.txt`); валідація VaR на даних (конкатенований OOS, бари в позиції): історичний VaR₉₅ Купець не відкидає (LR 2.254 BTCUSDT, 3.338 ETHUSDT), параметричний відкинуто (LR 164.4 / 80.1) (`docs/report_tables/var_kupiec.md`, `docs/results.md` §8) |
| FR-20 | MARKET виконується за open t+1; імпакт ∝ √Q; taker > maker; фандинг о 00/08/16 UTC; стоп раніше тейку; тотожність обліку на кожному кроці; HMAC-підпис точного запиту; не-testnet хост відхиляється; повтор із тим самим `client_order_id` не дублює ордер | `test_fill_on_next_bar_open_not_current_close`, `test_sqrt_impact_scales_with_sqrt_of_qty`, `test_taker_fee_higher_than_maker`, `test_funding_charged_at_00_08_16_utc`, `test_intrabar_pessimism_resolves_stop_before_tp`, `test_equity_accounting_identity`, `test_order_is_signed_with_hmac_sha256_over_exact_query`, `test_non_testnet_base_url_refused`, `test_router_idempotent_retry_returns_same_ack_without_second_venue_call`, `test_duplicate_client_order_id_rejected_without_double_fill`; процедура **[ЛЮДИНА]** `uv run python scripts/testnet_one_order.py --confirm` | тести пройдено: 1034 passed, 0 failed (`415acbd`, `docs/report_tables/raw/test_runs.txt`); не виконано: живий testnet-ордер, бо немає testnet-ключів (EXE-07); що потрібно: студент [ЛЮДИНА] вписує ключі в `.env` і запускає `uv run python scripts/testnet_one_order.py --confirm` зі скріншотом |
| FR-21 | майбутні бари не змінюють минулих рішень; той самий seed → той самий SHA кривої, інший seed → інша крива; нульовий сигнал → пласка крива; embargo без перетину; сітка не залежить від кількості воркерів; метрики на еталонних рядах; Парето-фронт містить лише недоміновані; вибір — ε-обмеження, а не argmax | `test_lookahead_guard_raises_on_future_index`, `test_shuffling_future_bars_does_not_change_past_decisions`, `test_backtest_deterministic_same_seed_same_equity_sha256`, `test_different_seed_changes_equity`, `test_zero_signal_yields_flat_equity`, `test_walkforward_embargo_no_overlap`, `test_grid_results_independent_of_worker_count`, `test_sharpe_matches_reference_series`, `test_sortino_penalizes_only_downside`, `test_max_drawdown_on_known_curve`, `test_ulcer_zero_on_monotone_curve`, `test_psr_below_threshold_on_short_sample`, `test_pareto_front_contains_only_nondominated`, `test_front_rule_picks_max_sharpe_within_dd_cap_not_global_argmax`, `test_amdahl_fit_recovers_known_serial_fraction_from_synthetic_speedups`, `test_sensitivity_space_has_eight_admissible_parameters_around_defaults`; процедура `scripts/run_all_experiments.sh` (фаза 7) і gate `select count(*) from run where kind = 'grid_cell' and status = 'DONE'` = 108 | процедура: чистий прогін фази 6 `4e5be0de-a4b7-4ef1-b84e-4a563b248be3` на `415acbd` — DONE, `run_metric.git_dirty = 0`, `equity_hash 3b5009cd…` = прогону `a848fa56` на `b933802` (крива відтворена побітово; `docs/figures/first_run_metrics.md`, FIN-01); експерименти фази 7 на `b933802`: сітка 2 × 108 `grid_cell`, walk-forward 6 фолдів × 2 символи × 2 ядра, Амдал S(8) = 3.855, чутливість 8 параметрів (`docs/results.md` §3–§5, §10); тести пройдено: 1034 passed, 0 failed (`415acbd`, `docs/report_tables/raw/test_runs.txt`) |
| FR-22 | логін повертає JWT і роль; analyst на `PUT /risk/limits` → 403; зміна ліміту → аудит before/after; кожен маршрут захищений за матрицею (92 випадки); `alg=none`/чужі алгоритми відхиляються; понижена роль втрачає право зміни до спливу токена; невалідний YAML → 422 з шляхом до поля | `test_login_returns_jwt_and_role`, `test_put_risk_limits_requires_admin_role_403_for_analyst`, `test_limit_change_written_to_audit_log_with_before_after`, `test_strategy_post_invalid_yaml_returns_422_with_field_path`, `test_access_matrix_enforced_for_every_route`, `test_every_route_is_protected_or_explicitly_public`, `test_token_with_other_algorithm_or_alg_none_is_rejected`, `test_demoted_user_loses_write_access_before_token_expiry`, `test_login_rate_limited_after_repeated_failures`, `test_strategy_crud_versions_and_audit`, `test_killswitch_release_admin_only_writes_audit_and_command`, `test_sse_stream_delivers_published_events_with_ids`, `test_openapi_is_complete_and_english`, `test_risk_events_keyset_pages_reach_every_record_with_until_ns` | процедура: `/docs` 200, `/openapi.json` 200 (OpenAPI 3.1.0, 22 шляхи), без токена — 401 (`docs/manuals/deployment.md` §3a); тести пройдено: 1034 passed, 0 failed (`415acbd`, `docs/report_tables/raw/test_runs.txt`) |
| FR-23 | реплей записаної сесії: ≥ 1 угода, 0 `LookaheadError`, тотожність капіталу, `fired_rules` у кожної угоди, `run.status = 'DONE'`; шапка «MODE: PAPER … NO MAINNET KEYS»; планувальник реєструє три UTC-задачі; нотифікатор без токена — no-op; користувач, створений CLI, входить через API; backup → restore дає ідентичні кількості рядків | `test_replay_session_end_to_end`, `test_replay_header_says_paper_replay_no_mainnet`, `test_ingest_worker_writes_candles_gaps_journal_and_health` (int), `test_build_scheduler_registers_three_utc_cron_jobs`, `test_retry_gaps_fills_partial_and_gives_up_after_max_attempts`, `test_hourly_dq_scores_previous_hour_from_stored_rows`, `test_daily_report_summarizes_previous_utc_day_and_sends_telegram`, `test_notifier_is_noop_without_token_or_chat`, `test_signal_sent_as_plain_ukrainian_text`, `test_user_cli_creates_bcrypt_user_audits_and_user_can_log_in` (int), `test_trading_loop_hot_reloads_limits_and_state_machine_only`; процедура `docs/manuals/backup_runbook.md` | процедура: backup/restore виконано 2026-09-19 — кількості 15 таблиць ідентичні (`docs/manuals/backup_runbook.md` §4); реплей у контейнері — DONE, `equity_hash d0f45aa3…` = реплей з хоста (`docs/manuals/deployment.md` §3a); тести пройдено: 1034 passed, 0 failed (`415acbd`, `docs/report_tables/raw/test_runs.txt`) |
| FR-24 | 3 екрани відкриваються; `ExplainView` показує МФ, правила з α, μ_agg з центроїдом, текст українською; інтерфейс українською за замовчуванням | ручна процедура за сценарієм §15 (0:30–3:20) | не виконано: веб-панель (3 екрани Vue, i18n) — порядок робіт D-06, каталогу `ui/` немає; що потрібно: автор реалізує `ExplainView` першою поверх `/decisions/{id}/explain`; план Б для захисту — Swagger `/docs` і JSON `/explain` |
| NFR-01 | mainnet-хост у конфігурації зупиняє старт; типові налаштування вказують на testnet; Telegram — лише `api.telegram.org` | `test_mainnet_host_is_rejected_by_config`, `test_default_settings_point_to_testnet`, `test_non_testnet_base_url_refused`, `test_base_url_must_be_telegram_https_host` | тести пройдено: 1034 passed, 0 failed (`415acbd`, `docs/report_tables/raw/test_runs.txt`) |
| NFR-02 | AST-скан не знаходить настінного часу й випадковості в межі детермінізму; дайджест стабільний між процесами; SHA кривої стабільний; сітка не залежить від кількості воркерів; режим трасування не змінює кривої | `test_no_wallclock_in_core`, `test_state_digest_stable_across_processes`, `test_backtest_deterministic_same_seed_same_equity_sha256`, `test_grid_results_independent_of_worker_count`, `test_record_traces_mode_does_not_change_equity`, `test_api_dataset_hash_equals_engine_canonical_path_for_subwindow` (int) | процедура: `equity_hash`, перерахований з 64 800 рядків `equity_point` чистого прогону `4e5be0de…`, збігся з паспортом; той самий `equity_hash 3b5009cd…` дав прогін `a848fa56` на іншому коміті (`docs/figures/first_run_metrics.md`, FIN-01); тести пройдено: 1034 passed, 0 failed (`415acbd`, `docs/report_tables/raw/test_runs.txt`) |
| NFR-03 | AST-скан: конвертація Decimal↔float лише у двох точках; тотожність обліку ≤ 1e−18 на кожному кроці; хеш кривої з БД = хеш вхідної на рівно-половинних хвостах | `test_decimal_float_boundary_is_single_choke_point`, `test_equity_accounting_identity`, `test_qty_floored_never_rounded_up`, `test_equity_hash_from_db_matches_on_half_ties` (int) | процедура: 0 порушень тотожності на 64 800 барах кожного символу (`docs/api/engine.md` §4); тести пройдено: 1034 passed, 0 failed (`415acbd`, `docs/report_tables/raw/test_runs.txt`) |
| NFR-04 | 0 втрачених подій у 6 сценаріях; зупинка воркера не перериває запис, що обробляється; розрив фіксується навіть для з'єднання, що не повернулося | `test_all_pathological_sessions_recover_with_zero_lost_events`, `test_ingest_pump_stop_never_cancels_a_record_in_progress`, `test_ingest_pump_cancels_only_the_wait_for_the_next_record`, `test_pipeline_watchdog_fires_on_virtual_time_even_if_connection_never_returns` | тести пройдено: 1034 passed, 0 failed (`415acbd`, `docs/report_tables/raw/test_runs.txt`) |
| NFR-05 | контрзаходи STRIDE `docs/security.md` §2 перевірено тестами; журнал і аудит незмінні для ролі застосунку; секрети не потрапляють у журнали; `pip-audit` без неприйнятих уразливостей | `test_expired_tampered_and_alg_none_tokens_are_rejected`, `test_limit_change_audit_before_after_is_append_only` (int), `test_token_never_logged_or_in_errors`, `test_out_of_range_integers_are_422_not_500`, `test_strategy_yaml_aliases_and_path_like_text_rejected`, `test_strategy_compute_budget_is_bounded`, `test_user_add_reads_password_from_stdin_never_echoes_or_logs`; процедура `uv run pip-audit` | тести пройдено: 1034 passed, 0 failed (`415acbd`, `docs/report_tables/raw/test_runs.txt`); `uv run pip-audit` на `415acbd` — «No known vulnerabilities found» (`docs/report_tables/raw/pip_audit.txt`) |
| NFR-06 | увесь сценарій §15 працює з вимкненим Wi-Fi | `test_replay_session_end_to_end`, `test_flash_crash_reaches_halted_without_human_input`; ручна репетиція без мережі | тести пройдено: 1034 passed, 0 failed (`415acbd`, `docs/report_tables/raw/test_runs.txt`) (`test_replay_session_end_to_end`, `test_flash_crash_reaches_halted_without_human_input` — офлайн); не виконано: ручна репетиція демо з вимкненим Wi-Fi, бо це крок людини біля машини; що потрібно: студент [ЛЮДИНА] один раз проганяє сценарій §15 без мережі і записує скрінкаст як план Б |
| NFR-07 | `make cov`: ≥ 80 % загалом, ≥ 90 % `fuzzy/risk/decision/sizing`; `make lint` без помилок | `make cov` (з інтеграційними, ST-11), `make lint` | `make cov` / об'єднаний прогін з інтеграційними: 89.38 % загалом (≥ 80 %); fuzzy 99.4, risk 99.9, decision 98.7, sizing 100.0 % (≥ 90 %); офлайн-набір 83.72 % (`docs/report_tables/raw/coverage_combined.txt`, `coverage_packages.md`, `coverage_default.txt`); `make lint`: ruff «All checks passed!», mypy «Success: no issues found in 35 source files» (`docs/report_tables/raw/lint.txt`) |
| NFR-08 | unit + property < 25 с | `make test` (час з виводу pytest) | unit + property послідовно: 807 passed за 23.37 с (real 24.56 с) — ціль < 25 с виконано; весь типовий набір: `make test` (pytest-xdist, 6 воркерів) 979 passed за 12.83 с, послідовно 29.90 с (`docs/report_tables/raw/test_runs.txt`; QP-03, FIN-08) |
| NFR-09 | `docker compose up` піднімає сервіси з нуля; `uv sync --frozen`; backup → restore відтворює БД | процедури `docs/manuals/deployment.md` §3–§3a, `docs/manuals/backup_runbook.md` | процедура: збирання й запуск `db api worker` виконано 2026-09-19 (`docs/manuals/deployment.md` §3a); не виконано: підйом сервісу `ui`, бо веб-панелі ще немає (D-06, FR-24) |

### 8.4 Трасування індивідуального завдання

Рядки таблиці трасування брифінгу §2 (фрагмент завдання → модуль → підтвердження) відповідають вимогам так. Підтвердження — у
колонці «результат» таблиці 8.3.

| № (§2) | Фрагмент завдання | Вимоги |
|---|---|---|
| 1 | «агрегації біржових даних» | FR-08 |
| 2 | «автоматизації торговельних операцій» | FR-20 |
| 3 | «із модулем ризик-менеджменту» | FR-17, FR-18, FR-19 |
| 4 | «REST … для збору» | FR-01, FR-02, FR-03 |
| 5 | «WebSocket … у реальному часі» | FR-04, FR-05, FR-06 |
| 6 | «нормалізації» | FR-07, FR-08, FR-09 |
| 7 | «збереження» | FR-10, FR-11 |
| 8 | «backtesting на історичних даних» | FR-21 |
| 9 | «рішень за заданими алгоритмічними правилами» | FR-12, FR-13, FR-14, FR-15 |
| 10 | «автоматичного контролю ризиків і лімітів» | FR-16, FR-17, FR-18 |
| 11 | «покрити ключову логіку модульним тестуванням» | NFR-07, NFR-08, розділ 8 |
