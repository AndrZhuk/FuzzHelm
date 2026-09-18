# FuzzHelm — брифінг для агента-виконавця

> **Проєктно-технологічна практика, 4 курс, кафедра АСУ НУ «Львівська політехніка».**
> ОПП F3 «Комп'ютерні науки (Обчислювальний інтелект смарт-систем)». Студент: Андрій Жук.
>
> **Тема:** Програмний сервіс агрегації біржових даних та автоматизації маржинальних торговельних
> операцій з нечітко-логічним ядром прийняття рішень та ієрархічною підсистемою автоматичного
> контролю ризиків і лімітів.

---

## 0. Як користуватися цим документом (читати першим)

Це **повна виконавча специфікація**. Вона самодостатня: усе потрібне для реалізації є тут, у
розмову з автором звертатися не потрібно.

### 0.1 Протокол роботи

1. Виконуй **фазами** з розділу 12, строго по порядку. Кожна фаза має **gate-критерій** — доки він
   не виконаний, наступну фазу не починати.
2. Після кожної фази: `make test` має бути зеленим, `git commit` з осмисленим повідомленням.
   Історія комітів по днях — сама є артефактом для захисту (комісія питає «коли ви це писали»).
3. **Усі числа, які потраплять у звіт, мусять бути ВИМІРЯНІ.** Заборонено вписувати «приблизно
   Sharpe 1.4» чи «покриття ~85%». Якщо числа ще немає — став маркер `<<TBD:назва_експерименту>>`
   і повертайся до нього після прогону. Жодного вигаданого числа.
4. Кожен модуль фіксуй у `docs/journal.md` одним абзацом того ж дня: що зроблено, яке рішення
   прийнято, чому. Це заготовка тексту звіту.
5. Якщо якесь місце спеки суперечить реальності (API змінився, бібліотека не ставиться) —
   **не вигадуй**: зафіксуй розходження в `docs/deviations.md` (що в спеці / що насправді / що
   зробив / чим обґрунтовано) і йди далі. Ці розходження теж ідуть у звіт.

### 0.2 Тверді заборони

- **Жодних реальних грошей і жодного mainnet.** У конфігурації жорсткий allowlist хостів
  (тільки testnet і публічні read-only ендпоінти), який перевіряється тестом
  `test_mainnet_host_is_rejected_by_config`. Ніякого коду, що вміє торгувати на реальному рахунку.
- **Ніяких секретів у репозиторії.** Тільки `.env` (у `.gitignore`) і `os.environ`. Не логувати
  значення ключів. Не комітити `fixtures` з приватними даними.
- **Ти не реєструєш акаунтів і не вводиш креденшли.** Кроки, що цього вимагають, позначені
  **[ЛЮДИНА]** — їх виконує студент сам (див. 12.9).
- **Не «підганяти» результат.** Якщо стратегія на OOS збиткова — так і пишемо. Тема роботи — метод
  і архітектура, а не прибутковість. Це не недолік, це наукова чесність, і вона окремо оцінюється.
- Система **не є** інвестиційною рекомендацією; це декларується у звіті (підрозділ 2.13) і в README.

### 0.3 Організаційне (зробити ДО першого `docker compose up`)

Робочий репозиторій **не має лежати в iCloud Drive** — синхронізація псує volume PostgreSQL і
породжує `.icloud`-заглушки. Робоче місце: `~/dev/fuzzhelm`. У теку практики в iCloud
складаються лише **результати** (звіт, рисунки, додатки, архів коду).

```bash
mkdir -p ~/dev/fuzzhelm && cd ~/dev/fuzzhelm && git init
```

---

## 1. Місія і межі системи

FuzzHelm — сервіс, який:

1. збирає котирування з **двох незалежних публічних read-only джерел** (Binance + Kraken) двома
   транспортами: **REST** (добір історії — backfill, довідник інструментів, funding) і **WebSocket**
   (свічки, угоди, снапшоти книги, mark price) у реальному часі;
2. **нормалізує** їх у канонічні DTO на `Decimal`, дедуплікує, виявляє й добирає прогалини,
   оцінює якість потоку числом `Q ∈ [0,1]` і **зберігає** у PostgreSQL 16 із ланцюгом хешів подій;
3. **приймає торговельні рішення за заданими алгоритмічними правилами** — нечіткий вивід Мамдані
   на 45 правилах над трьома лінгвістичними змінними, які, у свою чергу, зведені довірчо-зваженим
   консенсусом із 6 незалежних детекторів патернів; кожне рішення має формальне виведення
   (`/explain`) зі спрацьованими правилами та їх ступенями активації;
4. **автоматично контролює ризики і ліміти** — детермінований контур: сайзер із таргетуванням
   волатильності, ланцюг 6 лімітів з доказовим інваріантом монотонності, автомат режимів
   NORMAL→WARNING→COOLDOWN→HALTED з гістерезисом і витримкою, розрахунок ціни ліквідації,
   VaR/CVaR з тестом Купця, засувний kill-switch, аудит кожного відхилення;
5. **тестує стратегії на історичних даних** — подієвий бектест-рушій (той самий код рішень, що і
   в live), 17 метрик + PSR/DSR, walk-forward з embargo, паралельний grid-пошук із заміром за
   законом Амдала, Парето-фронт замість `argmax Sharpe`;
6. **автоматизує виконання** — порт `ExecutionVenue` із двома реалізаціями: `PaperBroker`
   (симуляція з моделлю витрат) і `BinanceTestnetVenue` (реальний повний цикл ордера на testnet);
7. **покрита модульним тестуванням** — 92 тест-кейси, з них 12 property-based, покриття ≥90% на
   `fuzzy`/`risk`/`decision`/`sizing`, ≥80% загалом.

### Керуюча теза всієї роботи

> **М'яке пропонує, жорстке вирішує.** Нечітке ядро видає лише *намір* `u ∈ [−1;1]`.
> Детермінований ризик-контур не має **жодного технічного способу** підвищити експозицію — це не
> заборона в коді, а властивість алгебри вердиктів, доведена property-тестом.

---

## 2. Дослівний текст індивідуального завдання і його трасування

> «Розробити програмний сервіс агрегації біржових даних та автоматизації торговельних операцій із
> модулем ризик-менеджменту. Розробити модулі підключення до API біржі за протоколами REST і
> WebSocket для збору, нормалізації та збереження ринкових котирувань у реальному часі, а також
> створити підсистему тестування стратегій на історичних даних (backtesting). Реалізувати модуль
> прийняття торговельних рішень за заданими алгоритмічними правилами, підсистему автоматичного
> контролю ризиків і лімітів, а також покрити ключову логіку модульним тестуванням.»

**Формулювання не змінюється.** Кожна його кома мусить мати підтвердження. Таблиця трасування —
обов'язковий артефакт (йде у висновки звіту):

| № | Фрагмент завдання | Модуль | Підтвердження |
|---|---|---|---|
| 1 | «агрегації біржових даних» | `ingest.rest`, `ingest.kraken_client`, `ingest.crosscheck` | два джерела, крос-звірка, таблиця розбіжностей |
| 2 | «автоматизації торговельних операцій» | `execution.router`, `execution.testnet_venue` | один реальний ордер на testnet зі скріншотом |
| 3 | «із модулем ризик-менеджменту» | `risk/*` | 6 лімітів, автомат, журнал `risk_event` |
| 4 | «REST … для збору» | `ingest.rest_client`, `ingest.backfill` | 45 днів свічок у БД, token-bucket |
| 5 | «WebSocket … у реальному часі» | `ingest.ws_client`, `ingest.reconnect` | записана сесія, 6 патологічних сценаріїв |
| 6 | «нормалізації» | `ingest.normalize`, `quantize`, `dedup` | таблиця мапінгу полів двох бірж |
| 7 | «збереження» | `storage/*`, Alembic | 12 таблиць, 3 рівні моделі БД |
| 8 | «backtesting на історичних даних» | `backtest/*` | walk-forward 6 фолдів, 17 метрик |
| 9 | «рішень за заданими алгоритмічними правилами» | `fuzzy/*`, `decision/*`, `detectors/*` | 45 правил у YAML, CRUD через UI, `/explain` |
| 10 | «автоматичного контролю ризиків і лімітів» | `risk/*`, `sizing/*` | statechart, інваріант монотонності |
| 11 | «покрити ключову логіку модульним тестуванням» | `tests/*` | 92 кейси, вивід `pytest --cov` |

---

## 3. Головна інженерна ідея + заготовлені відповіді комісії

**Перша фраза на захисті (вивчити напам'ять):**

> «Пряма побудова нечіткого контролера над шістьма детекторами дала б 3⁶ = 729 правил —
> нечитабельну і неперевірювану базу знань. Я розв'язав це архітектурно: ієрархічна агрегація
> замінює комбінаторику двома рівнями — арифметичною довірчо-зваженою згорткою у три агреговані
> змінні та компактною базою 5×3×3 = 45 правил. Стиснення 16×, і я покажу, що саме при цьому
> втрачено. Друга теза: інтелектуальний компонент у цій системі принципово не може збільшити
> ризик — це не заборона в коді, а властивість алгебри вердиктів, доведена property-тестом.»

**Питання 1. «Де тут обчислювальний інтелект, а не інженерія знань? Функції належності задали ви.»**
> Межі термів `V` і точки зламу `T` **виведені з даних**: `KMeans(k=3)` на векторі (перцентиль
> волатильності Паркінсона, нормований нахил EMA, z-score обсягу) по IS-вікну; `k` обрано за
> силуетним коефіцієнтом; центроїди стали центрами гаусових МФ режиму волатильності, а межі
> трапецій `T` — 15/50/85 перцентилями емпіричного розподілу. Додатково в контурі якості даних
> працює автокодувальник `MLPRegressor(8-3-8)`: помилка реконструкції — детектор аномалій котирувань.
> Тобто в системі є і нечітка обробка, і кластерний аналіз, і нейромережевий компонент, і
> параметрична оптимізація з Парето-фронтом. Плюс окремий підрозділ — аналіз чутливості до 8 параметрів.

**Питання 2. «Ваша база правил логічно несуперечлива? Доведіть.»**
> Доведено машинно і з чесним обмеженням. При `w_r ≡ 1` і розбитті МФ з умовою Руспіні
> (`Σμ_i(x) = 1`) property-тест `test_u_nondecreasing_in_trend_input` на 500 згенерованих трійках
> перевіряє `u(T₂) ≥ u(T₁) − 1e−9` при `T₂ > T₁`. Але монотонність Мамдані в загальному випадку
> **не тверджується**: у роботі побудований **контрприклад** — при `w_r ≠ const` центроїд обрізаної
> агрегованої фігури немонотонний, і цей контрприклад знайдений тим самим тестом. Тому у фінальній
> конфігурації ваги правил зафіксовані одиницею. Знайдений контрприклад науково сильніший за
> недоведену теорему.

**Питання 3. «Це не автоматизація торговельних операцій, а симуляція.»**
> Порт `ExecutionVenue` має дві реалізації. `PaperBroker` — для бектесту, реплею і демонстрації, з
> моделлю витрат. `BinanceTestnetVenue` — реальний повний цикл: підпис HMAC-SHA256,
> `POST /fapi/v1/order`, отримання `FILLED`, звірка стану. Один такий ордер виконано, екранограма —
> у додатку. Реальних коштів немає **за побудовою**: конфігурація має інваріант
> `assert settings.venue_base_url in ALLOWED_TESTNET_HOSTS`, перевірений тестом.

**Інші заготовки:**
- «Чому не scikit-fuzzy?» → «Бо механізм виведення і є темою роботи.»
- «Чому не нейромережа для рішень?» → «Теза роботи — інтерпретованість; нейромережа працює там, де
  доречна — у детекторі аномалій котирувань.»
- «Звідки 45 правил?» → «З трьох декларованих політик за режимами волатильності; повна таблиця в
  Додатку А, параметри МФ виведені з кластеризації.»
- «Ви довели стійкість?» → «Ні, і я цього не тверджу. Є оцінка гіршого випадку швидкості наростання
  просадки за явно перерахованих припущень.»

---

## 4. Архітектура

### 4.1 Потік даних (один напрямок, без циклів)

```
REST(Binance klines/exchangeInfo/premiumIndex) ──┐
REST(Kraken OHLC — крос-звірка)  ────────────────┤
WS(kline_1m, aggTrade, depth20@100ms, markPrice)─┴→ Normalizer → EventJournal(hash-chain)
   → CandleAggregator → QualityGate(Q, MLP-аномалії) → CandleRepo(PostgreSQL)
   → FeaturePipeline(O(1) інкрементальні) → BarWindow[LookaheadGuard]
   → 6×Detector(s,c) → ConsensusAggregator(T,R,V) → MamdaniEngine(u_raw, FiredRule[])
   → AgreementMeter(κ) → u_final = clip(κ·u_raw)
   → PositionSizer(vol-target + ATR-risk, min) → HysteresisGate
   → RiskGuard(chain of 6) → RiskStateMachine(κ_mode) → Verdict
   → OrderRouter → PaperBroker | BinanceTestnetVenue → Fill → Portfolio → equity_curve
   → decisions/risk_events (аудит) → FastAPI(/explain, SSE) → Vue 3
```

### 4.2 Дві архітектурні межі (це ядро якості роботи)

**Межа детермінізму.** Усе від `EventJournal` до `PaperBroker` — чисті функції та ін'єктовані порти
`Clock`, `IdGenerator(seed)`, `MarketFeed`, `ExecutionVenue`. AST-тест забороняє `datetime.now`,
`time.time`, `random.*`, `uuid4` у пакетах `core, features, detectors, fuzzy, decision, risk, sizing`.
Саме це дає властивість «один і той самий код рішень працює і в live, і в бектесті».

**Межа типів.** `Decimal` — ціни, обсяги, гроші, все, що входить у хеш стану і в БД
(`NUMERIC(38,18)`). `float`/`numpy` — індикатори, нечітке ядро, статистика. Конвертація **тільки у
двох точках**: `features/convert.py::to_float(price, scale)` і `sizing/convert.py::to_decimal(qty)`
з квантуванням `ROUND_DOWN`. Тест `test_decimal_float_boundary_is_single_choke_point` (AST-скан).

### 4.3 Модулі

| Модуль | Відповідальність | Пункт завдання | Файли |
|---|---|---|---|
| `core` | канонічні DTO (frozen slots), порти `Clock`/`MarketFeed`/`ExecutionVenue`, журнал подій з ланцюгом BLAKE2b, канонічна серіалізація без `float` | базова архітектура, детермінізм | `core/dto.py`, `ports.py`, `clock.py`, `journal.py`, `digest.py`, `money.py`, `errors.py`, `enums.py` |
| `ingest.rest` | добір (backfill) klines з пагінацією і перекриттям 1 бар, `exchangeInfo` (tick/step/minNotional/mmr), `premiumIndex` (funding), token-bucket з вагами, retry з повним джитером, оцінка зсуву годинника | «REST … для збору» | `rest_client.py`, `ratelimit.py`, `retry.py`, `backfill.py`, `kraken_client.py` |
| `ingest.ws` | комбінований стрім, реконект з backoff, heartbeat-watchdog, детекція прогалин за `aggId`/`openTime`, запис і відтворення сирої сесії | «WebSocket … у реальному часі» | `ws_client.py`, `reconnect.py`, `gap_detector.py`, `recorder.py`, `replay.py` |
| `ingest.normalize` | venue-JSON → канонічні DTO, `Decimal`, квантування до tick/step, пара `ts_event`/`ts_ingest`, дедуплікація за `event_uid` (BLAKE2b-128), крос-звірка Binance↔Kraken | «нормалізації» | `normalize.py`, `symbols.py`, `quantize.py`, `dedup.py`, `crosscheck.py` |
| `quality` | інваріанти свічки, MLP-автокодувальник аномалій, погодинний скор `Q` (ваги за AHP, CR<0.1) | «нормалізації та збереження» | `invariants.py`, `anomaly_mlp.py`, `dq_score.py`, `ahp.py`, `health.py` |
| `storage` | 12 таблиць, ідемпотентні upsert, репозиторії, Alembic (3 ревізії), backup/restore | «збереження» | `models.py`, `session.py`, `repositories/*.py` |
| `features` | EMA, ATR/RSI за Уайлдером, Bollinger, Donchian (монотонний дек), волатильність Паркінсона, Welford, OLS slope+R², перцентильний ранг — усі O(1) | вхід модуля рішень | `indicators.py`, `pipeline.py`, `window.py`, `convert.py` |
| `detectors` | 6 детекторів, єдиний контракт `DetectorOutput(name, s, c, features)`, довіра з властивостей самих даних | «модуль прийняття рішень» (шар свідчень) | `base.py`, `ema_slope.py`, `donchian.py`, `rsi_exhaustion.py`, `bollinger_z.py`, `candle_geometry.py`, `vol_regime.py`, `registry.py` |
| `fuzzy` | МФ (три/трапеція/гаус), парсер бази правил з YAML, рушій Мамдані, дефазифікація центроїдом (3 схеми інтегрування), генератор поверхні керування, `LinearVoteEngine` як базова лінія | «за заданими алгоритмічними правилами» | `membership.py`, `rules.py`, `mamdani.py`, `defuzz.py`, `linear.py`, `surface.py` |
| `decision` | довірчо-зважений консенсус, ентропійна узгодженість і `κ`, `DecisionCore`, повний `DecisionTrace`, україномовне трасування | «модуль прийняття рішень» | `aggregator.py`, `agreement.py`, `core.py`, `trace.py`, `narrative_uk.py` |
| `regimes` | KMeans-кластеризація режимів ринку → калібрування параметрів МФ, силуетний коефіцієнт | обґрунтування параметрів ядра | `cluster.py`, `calibrate_mf.py` |
| `sizing` | таргетування волатильності (gain scheduling) + ATR-ризик, мінімум, фільтр 1-го порядку, тригер Шмітта | межа рішення/ризику | `vol_target.py`, `atr_risk.py`, `sizer.py`, `hysteresis.py`, `convert.py` |
| `risk` | алгебра вердиктів ALLOW/SHRINK(f)/VETO з монотонною композицією, 6 лімітів, автомат станів з гістерезисом і dwell, ціна ліквідації, VaR/CVaR + тест Купця, журнал відхилень, засувний kill-switch | «контроль ризиків і лімітів» | `verdict.py`, `guard.py`, `state.py`, `margin.py`, `var.py`, `kupiec.py`, `journal.py`, `killswitch.py`, `rules/*.py` |
| `execution` | `PaperBroker`, `CostModel`, `BinanceTestnetVenue` (HMAC), ідемпотентність за `client_order_id`, `Portfolio` | «автоматизації торговельних операцій» | `paper_broker.py`, `cost_model.py`, `testnet_venue.py`, `router.py`, `portfolio.py` |
| `backtest` | подієвий рушій bar-close→open(t+1), метрики (17 + PSR/DSR), walk-forward з embargo, паралельний grid, Парето-фронт, паспорт прогону | «підсистема backtesting» | `engine.py`, `metrics.py`, `walkforward.py`, `grid.py`, `parallel.py`, `pareto.py`, `manifest.py` |
| `api` | REST + SSE, `/explain`, CRUD правил з валідацією і версіонуванням, JWT + 4 ролі + аудит дій | інтеграційний шар, безпека | `main.py`, `auth.py`, `deps.py`, `schemas.py`, `routers/*.py` |
| `notify` | Telegram Bot API: сигнали, ризик-події, kill-switch, розриви WS; дедуплікація, rate-limit | експлуатаційний ефект | `telegram.py`, `templates.py` |
| `scheduler` | APScheduler: нічний добір прогалин, погодинний `Q`, щоденний звіт | «сервіс» як сервіс | `jobs.py` |
| `ui` | 3 екрани Vue 3 + i18n (uk/en) | екранограми, візуалізація | `views/{LiveView,ExplainView,BacktestView}.vue` |

---

## 5. Ключові алгоритми з формулами

### 5.1 Індикатори (O(1) на бар)

- EMA: `E_t = αp_t + (1−α)E_{t−1}`, `α = 2/(n+1)`, `n = 21`
- **ATR за Уайлдером:** `TR_t = max(h_t−l_t, |h_t−c_{t−1}|, |l_t−c_{t−1}|)`,
  `ATR_t = ATR_{t−1} + (TR_t − ATR_{t−1})/n`, тобто `α = 1/n` (**не** `2/(n+1)` — окремий golden-тест)
- **RSI за Уайлдером:** `Ḡ_t = Ḡ_{t−1} + (G_t − Ḡ_{t−1})/n`, `RSI = 100 − 100/(1 + Ḡ/L̄)`
- Bollinger: `SMA₂₀ ± 2σ₂₀`, `bw = 2σ/μ`; дисперсія **за Welford**:
  `M_k = M_{k−1} + (x_k−M_{k−1})/k`, `S_k = S_{k−1} + (x_k−M_{k−1})(x_k−M_k)`
- Donchian на монотонному деку — амортизовано O(1)
- Волатильність Паркінсона: `σ_P = √( (1/(4 ln2 · W))·Σ ln²(h_i/l_i) )`, `W = 24`
- Перцентильний ранг: `V_t = (1/L)·Σ 1[σ_{P,i} ≤ σ_{P,t}]`, `L = 500`

### 5.2 Детектори: довіра як властивість даних

| Детектор | Сила `s` | Довіра `c` |
|---|---|---|
| `EmaSlope` | `tanh(g_t/0.15)`, `g_t = (e_t − e_{t−5})/(5·ATR_t)` | `R²` OLS-апроксимації `e_{t−4..t}` за часом |
| `Donchian` | `tanh(d_t/0.5)`, `d_t = (p_t − U_t)/ATR_t` | `min(1, (U−L)/(6ATR))·exp(−0.3·bars_since_breakout)` |
| `RsiExhaustion` | `sign(z)·abs(z)^1.6`, `z = (50 − RSI)/50` | `min(1, 0.6·abs(z)^0.8 + 0.4·div_score)` |
| `BollingerZ` | `−tanh(z_t/2.0)`, `z = (p−SMA₂₀)/σ₂₀` | `1 − ρ_t`, `ρ` = перцентиль bandwidth за 200 барів |
| `CandleGeometry` | `clip(Σ sign_j·v_j·score_j / Σ v_j, −1, 1)` | `max_j score_j · exp(−dist_to_level/(2ATR))` |
| `VolRegime` | `0` (контекст) | `1.0`; вихід — `V_t` |

Геометричний скоринг патернів **без порогових `if`**: `σ_s(x) = 1/(1+e^{−3x})`;
бичачий пін-бар `π = σ_s(W_l/max(B,ε) − 2)·σ_s(1 − W_u/max(B,ε))·min(1, Rg/ATR)`,
де `B` — тіло, `W_l`/`W_u` — нижня/верхня тінь, `Rg` — діапазон.

### 5.3 Рівень 1: довірчо-зважений консенсус

```
A = {EmaSlope, Donchian}                            # трендові
B = {RsiExhaustion, BollingerZ, CandleGeometry}      # реверсійні
T_t = Σ_{k∈A} ω_k c_k s_k / max(ε, Σ_{k∈A} ω_k c_k)
R_t = Σ_{k∈B} ω_k c_k s_k / max(ε, Σ_{k∈B} ω_k c_k)
V_t = вихід VolRegime,  ε = 1e−9
```

Стиснення бази знань: `3⁶ = 729` → `5·3·3 = 45` правил, коефіцієнт **16.2×**.

### 5.4 Ентропійна узгодженість і коефіцієнт довіри

```
Z  = Σ_{k∈A∪B} ω_k c_k
p₊ = Σ ω_k c_k · max(s_k, 0) / Z
p₋ = Σ ω_k c_k · max(−s_k, 0) / Z
p₀ = Σ ω_k c_k · (1 − |s_k|) / Z        # незадіяна маса свідчень
```

Тотожність `max(s,0) + max(−s,0) + (1−|s|) = 1` ⇒ `p₊+p₋+p₀ ≡ 1` для **трьох** категорій, тому
нормування на `ln 3` коректне:

```
H   = −Σ_{i∈{+,−,0}} p_i ln p_i / ln 3 ∈ [0,1]      # 0·ln0 = 0
A_g = 1 − H
κ   = κ_min + (1 − κ_min)·A_g^ν,   κ_min = 0.35, ν = 1.0
u_final = clip(κ · u_raw, −1, 1)
```

Межі досяжні: `p = (1,0,0)` ⇒ `κ = 1.0`; `p = (⅓,⅓,⅓)` ⇒ `κ = 0.35` (максимальне гасіння ≈2.86×).

### 5.5 Функції належності (калібровані з даних)

Умова Руспіні: `Σ_i μ_i(x) = 1 ∀x` — перевіряється тестом покриття, без «мертвих зон».

- **T** ∈ [−1;1], 5 термів, точки зламу = перцентилі {8, 25, 50, 75, 92} емпіричного розподілу `T`
  на IS-вікні: `μ_ST− = trap(−1,−1,−0.70,−0.35)`, `μ_W− = tri(−0.70,−0.35,0)`,
  `μ_NEU = tri(−0.35,0,0.35)`, `μ_W+ = tri(0,0.35,0.70)`, `μ_ST+ = trap(0.35,0.70,1,1)`
- **R** ∈ [−1;1], 3 терми: `trap(−1,−1,−0.45,0)`, `tri(−0.45,0,0.45)`, `trap(0,0.45,1,1)`
- **V** ∈ [0;1], 3 гаусіани з **центрами = центроїдами KMeans(k=3)**: `m ≈ (0.17, 0.51, 0.88)`,
  `σ_i = 0.5·d(m_i, m_{i±1})` (реальні числа взяти з прогону калібрування, не з цієї спеки)
- **U** ∈ [−1;1], 5 термів: `trap(−1,−1,−0.80,−0.45)`, `tri(−0.80,−0.40,0)`, `tri(−0.40,0,0.40)`,
  `tri(0,0.40,0.80)`, `trap(0.45,0.80,1,1)`

### 5.6 Нечіткий вивід Мамдані

Для правила `r`: «ЯКЩО T є Ã_r І R є B̃_r І V є C̃_r ТО U є D̃_r»

```
α_r     = min(μ_{Ã_r}(T), μ_{B̃_r}(R), μ_{C̃_r}(V))    # терм "any" → внесок 1.0
μ'_r(u) = min(α_r, μ_{D̃_r}(u))                         # обрізання
μ_agg(u)= max_{r=1..45} μ'_r(u)                         # s-норма max
```

**Три декларовані політики побудови бази** (відповідь на «чому саме 45 правил»):
при `V=LO` домінує реверсія; при `V=MID` тренд перемагає реверсію у конфлікті; при `V=HI` усе
притягується до HOLD — ненульовий вихід дають лише узгоджені `T` і `R`.

### 5.7 Дефазифікація як задача чисельного інтегрування

```
u_raw = ∫ u·μ_agg(u) du / ∫ μ_agg(u) du
```

Реалізувати **три** схеми на сітці `u_j = −1 + jΔ`: прямокутників (`Σ u_j μ_j / Σ μ_j`),
трапецій, Сімпсона. Порівняти похибки при `Δ = 0.02 / 0.01 / 0.002 / 0.001`, оцінити порядок
збіжності `p = log₂(|u_Δ − u_{Δ/2}| / |u_{Δ/2} − u_{Δ/4}|)`. Робоча схема — **трапеції на 201
вузлі**; критерій `|u(0.01) − u(0.001)| < 1e−3`. Порожня активація ⇒ `u_raw = 0` (HOLD, не NaN).

### 5.8 Сайзинг: таргетування волатильності як компенсація коефіцієнта передачі

```
1) σ²_t = λσ²_{t−1} + (1−λ)r²_t,  λ = 0.94;   σ_ann = σ_t√A,  A = 525600  (1m, 24/7)
2) s*_t = clip(σ_target/σ_ann, 0.25, 3.0),    σ_target = 0.20
3) фільтр 1-го порядку:  s_t = s_{t−1} + γ(s*_t − s_{t−1}),  γ = 0.2 ⇒ T = 4Δt
4) ATR-ризик:   q_atr = ρ_base·|u_final|·E_t / (χ·ATR_t),   ρ_base = 0.005, χ = 2.0
5) vol-target:  q_vt  = s_t·E_t/p_t
6) плече:       q_lev = (E_t·L_max − N_gross)/p_t,          L_max = 3.0
7) q = floor_to_step(κ_mode · min(q_atr, q_vt, q_lev), step_size)
8) відхилити, якщо q·p_t < min_notional  (код BELOW_MIN_NOTIONAL, НЕ округляти вгору)
```

Множення на `σ_target/σ̂` — це **gain scheduling**: коефіцієнт підсилення замкненого контуру
(реалізована волатильність) тримається на завданні. Мінімум з трьох — перетин допустимих множин
керування; `SizingResult.binding_constraint` каже, хто став вузьким місцем.

### 5.9 Гістерезис (тригер Шмітта) з кількісною ціною відсутності

Вхід при `|u_final| ≥ 0.25`, вихід лише при `|u_final| < 0.12`. Ширина петлі 0.13.
Без гістерезису при `u ≈ 0.25` позиція перевідкривається щобару: 1440 барів/добу × 2 комісії ×
0.04% = **−1.15% капіталу на добу** лише на транзакційних витратах. Тест на детерміністичній
послідовності `u = 0.30, 0.20, 0.15, 0.11`.

### 5.10 Маржа та ціна ліквідації (виведення, не з документації)

Умова ліквідації `Equity = MM`. Для лонга: `W + q(P − P_e) = q·P·mmr − ma` ⇒

```
P_liq^long  = (q·P_e − W − ma) / (q·(1 − mmr))
P_liq^short = (W + q·P_e + ma) / (q·(1 + mmr))
```

де `W` — власні кошти, `P_e` — ціна входу, `mmr` — ставка підтримуваної маржі, `ma` — maintenance
amount. **Контрольний приклад для тесту:** `q=1, P_e=100, W=10` (плече 10×), `mmr=0.005, ma=0` ⇒
`P_liq = 90/0.995 = 90.4523` — вище наївних 90 саме на підтримуючу маржу.

Відстань до ліквідації в одиницях власного шуму: `DTL = |P_t − P_liq|/ATR_t`, ліміт `DTL ≥ 6`.
При порушенні правило **не відхиляє**, а зменшує плече до `L'_max = 1/(Δ_stop/(P_e(1−b)) + mmr)`, `b = 0.20`.

### 5.11 Алгебра вердиктів і доказовий інваріант

```
Verdict ∈ {ALLOW, SHRINK(f), VETO},  f ∈ (0;1)

compose(v₁..v_n) = VETO,           якщо ∃i: v_i = VETO
                 = SHRINK(Π f_i),  інакше якщо є SHRINK
                 = ALLOW,          інакше
```

**Інваріант:** `exposure(compose(V)) ≤ min_i exposure(v_i) ≤ exposure_requested`.
Оскільки всі `f_i ∈ (0;1)`, добуток монотонно незростаючий за додаванням правил ⇒ **жодне нове
правило не може збільшити експозицію**. Композиція комутативна (порядок правил у конфігурації не
впливає). Перевірка — `hypothesis` на послідовностях до 20 вердиктів.

**Шість реальних лімітів:** `MaxPositionNotional`, `MaxGrossLeverage`, `MaxDailyLoss`,
`MaxDrawdownHalt`, `LiquidationBufferGuard`, `StaleDataGuard(lag > 5 с ∨ Q < 0.90)`.

### 5.12 Автомат ризик-станів з гістерезисом і витримкою

```
DD_t = 1 − E_t / max_{τ≤t} E_τ

NORMAL   → WARNING:   DD ≥ 0.04 ∨ σ_ann/σ_base > 1.6
WARNING  → NORMAL:    DD ≤ 0.025 ∧ dwell ≥ 15 барів
WARNING  → COOLDOWN:  DD ≥ 0.08 ∨ PnL_day ≤ −0.02·E_open       (reduce-only)
COOLDOWN → WARNING:   DD ≤ 0.05 ∧ dwell ≥ 30 барів
*        → HALTED:    DD ≥ 0.12 ∨ PnL_day ≤ −0.03·E_open       (засувний)

κ_mode = {NORMAL: 1.0, WARNING: 0.5, COOLDOWN: 0.25, HALTED: 0.0}
```

Таблиця переходів 4×6 = 24 клітинки задана **тотально**, перевіряється параметризованим тестом.
`HALTED` знімається лише `POST /risk/killswitch/release` з роллю `admin`, із записом в `audit_log`.

**Оцінка гіршого випадку (не доведення стійкості!):** збиток за бар обмежений зверху
`|ΔE| ≤ κ_mode·ρ_base·E_t·(1+κ_slip)`; `κ_mode` монотонно незростаюча за `DD` ⇒ число барів до
пробиття `DD_max` обмежене знизу `n_min ≥ (DD_max − DD₀)/v_max(COOLDOWN)`. У звіті формулювати
саме як оцінку гіршого випадку за заданих припущень про виконання стопа.

### 5.13 VaR / CVaR + тест Купця

```
r^p_t = ΔE_t/E_{t−1};  вікно W = 500
VaR₉₅ = −Quantile_{0.05}({r^p});   CVaR₉₅ = −(1/m)Σ_{i≤m} r_(i),  m = ⌊0.05W⌋
Інваріант: CVaR ≥ VaR ≥ 0

Kupiec POF: LR = −2 ln[ ((1−p)^{W−x}·p^x) / ((1−x/W)^{W−x}·(x/W)^x) ],  p = 0.05
LR > χ²₁(0.95) = 3.841 ⇒ модель VaR відкидається
```

Паралельно параметричний `VaR = z_{0.95}·σ_p·√h·E` (`z` через `statistics.NormalDist`); у звіті —
порівняння двох оцінок і чесний висновок про заниження ризику через товсті хвости.
VaR/CVaR — **звітна метрика**, вона не блокує ордери.

### 5.14 Модель витрат виконання

```
P_fill = P_ref + side·( δ_spread/2 + k_s·σ_t·P_ref·√(Q/V_bar) ),   k_s = 0.5
fee     = N·τ,  τ_taker = 0.0004, τ_maker = 0.0002
funding = N·f_rate  кожні 00/08/16 UTC  (f_rate з /premiumIndex)
```

Неоднозначність «стоп і тейк в одному барі» розв'язується **песимістично** (спершу стоп) —
документоване припущення з окремим тестом. Три рівні деградації моделі
(`zero` / `sqrt_impact` / `sqrt_impact+fee+funding`) дають порівняльну таблицю Sharpe — окремий
обчислювальний експеримент, який показує, наскільки наївна модель завищує результат.

### 5.15 Метрики бектесту, PSR і DSR

```
Sharpe  = √P·(mean r − r_f)/std r;   Sortino = √P·(mean r − r_f)/√(mean min(r,0)²)
MaxDD   = max_t(1 − E_t/max_{τ≤t}E_τ);  Calmar = CAGR/MaxDD;  Ulcer = √(mean DD_t²)
PF      = Σприбутків/|Σзбитків|;     Expectancy = p·avg_win − (1−p)·avg_loss

PSR = Φ( (ŜR − SR*)·√(n−1) / √(1 − γ₃·ŜR + ((γ₄−1)/4)·ŜR²) ),  SR* = 0
DSR: SR₀ = √Var(SR_i)·[(1−γ_E)·Φ⁻¹(1−1/N) + γ_E·Φ⁻¹(1−1/(Ne))],  γ_E = 0.5772
```

`N` для DSR — **фактичне** число прогонів сітки, `Var(SR_i)` — вибіркова дисперсія Sharpe по цих
прогонах. Якщо `PSR < 0.95` — у звіті прямо зазначається, що перевага статистично не встановлена.

### 5.16 Walk-forward з embargo, Парето-фронт, закон Амдала

```
IS = 90 днів, embargo E = 2·max_lookback барів, OOS = 30 днів, крок 30 днів ⇒ k = 6 фолдів
Інваріант: (IS_i ∪ E_i) ∩ OOS_i = ∅  ∧  max(IS_i.t) + E ≤ min(OOS_i.t)
```

Сітка `Θ`: `n_ATR × χ × u_enter × ρ_base × λ` = 3·3·3·2·2 = **108 конфігурацій**.
Замість одновимірного `argmax Sharpe` — **недомінований (Парето) фронт** у критеріях
(`SR_OOS ↑`, `MaxDD_OOS ↓`, `Turnover ↓`); вибір робочої точки з фронту обґрунтовується текстом.

Паралелізм: `ProcessPoolExecutor(p)`, воркер — **чиста функція** над in-memory `numpy`-масивом
свічок, без БД і без async-рушія; `initializer` виставляє `decimal` context і seed.
Замір `S(p) = T(1)/T(p)`, `p ∈ {1,2,4,8}`, порівняння з межею Амдала `S ≤ 1/(f + (1−f)/p)`,
оцінка `f` методом найменших квадратів, аналіз розходження (витрати на pickle-серіалізацію).

### 5.17 Скор якості даних (ваги за AHP) і нейромережевий детектор аномалій

```
completeness = N_obs/N_exp;            validity   = 1 − N_invalid/N_total
timeliness   = exp(−lag_p95/τ₀), τ₀=1000 мс;  continuity = 1 − gap_sec/3600
Q = w₁·compl + w₂·valid + w₃·timel + w₄·contin
```

Ваги `w` **не «на око»**: матриця парних порівнянь 4×4 за шкалою Сааті, `w` = головний власний
вектор, перевірка узгодженості `CR = (λ_max − n)/((n−1)·CI_rand) < 0.1`. Очікуваний результат
≈ (0.35, 0.27, 0.20, 0.18) — **перерахувати і взяти фактичні**.

`N_invalid` включає аномалії, знайдені автокодувальником: `MLPRegressor(hidden=(3,), max_iter=500,
random_state=seed)` навчається на векторі `x = (Δlog p, log(Rg/ATR), log(v/v̄), bw_pct, |z|)`
нормальних барів IS-вікна; аномалія при `‖x − x̂‖² > q₉₉(train)`. ROC-AUC на розмічених ін'єкованих
аномаліях — таблиця у звіті.

---

## 6. Модель даних

PostgreSQL 16 **без розширень**. Гроші/обсяги — `NUMERIC(38,18)`, час — `TIMESTAMPTZ` UTC +
`ts_event_ns BIGINT` для впорядкування. **12 таблиць.** Alembic: 3 ревізії
(`0001_core`, `0002_trading`, `0003_auth_audit`).

Три рівні моделі БД для звіту: концептуальна (ER у нотації Чена, 7 сутностей) → логічна
(нормалізація до 3НФ, розв'язання M:N) → фізична (наведений нижче DDL).

```sql
-- 1. Довідник інструментів
CREATE TABLE instrument (
  id            SERIAL PRIMARY KEY,
  venue         TEXT NOT NULL,
  symbol_venue  TEXT NOT NULL,
  symbol_canon  TEXT NOT NULL,
  base_asset    TEXT, quote_asset TEXT,
  contract_type TEXT CHECK (contract_type IN ('SPOT','PERP')),
  tick_size     NUMERIC(38,18) NOT NULL,
  step_size     NUMERIC(38,18) NOT NULL,
  min_notional  NUMERIC(38,18) NOT NULL,
  mmr           NUMERIC(10,8)  NOT NULL DEFAULT 0.005,
  maint_amount  NUMERIC(38,18) NOT NULL DEFAULT 0,
  max_leverage  SMALLINT DEFAULT 3,
  active        BOOLEAN DEFAULT TRUE,
  spec_fetched_at TIMESTAMPTZ,
  UNIQUE (venue, symbol_venue), UNIQUE (symbol_canon)
);

-- 2. Свічки (ядро даних)
CREATE TABLE candle (
  instrument_id INT NOT NULL REFERENCES instrument,
  tf            TEXT NOT NULL,
  open_time     TIMESTAMPTZ NOT NULL,
  close_time    TIMESTAMPTZ NOT NULL,
  o NUMERIC(38,18), h NUMERIC(38,18), l NUMERIC(38,18), c NUMERIC(38,18),
  volume        NUMERIC(38,18) CHECK (volume >= 0),
  quote_volume  NUMERIC(38,18) CHECK (quote_volume >= 0),
  trades_count  INT CHECK (trades_count >= 0),
  vwap          NUMERIC(38,18),
  is_closed     BOOLEAN NOT NULL DEFAULT FALSE,
  is_synthetic  BOOLEAN NOT NULL DEFAULT FALSE,
  src           SMALLINT NOT NULL CHECK (src IN (1,2,3)),  -- 1=ws 2=rest 3=replay
  anomaly_score NUMERIC(10,6),
  ingested_at   TIMESTAMPTZ DEFAULT now(),
  PRIMARY KEY (instrument_id, tf, open_time),
  CONSTRAINT ck_hl   CHECK (h >= l),
  CONSTRAINT ck_h    CHECK (h >= GREATEST(o,c)),
  CONSTRAINT ck_l    CHECK (l <= LEAST(o,c)),
  CONSTRAINT ck_vwap CHECK (vwap IS NULL OR (vwap >= l AND vwap <= h))
);
CREATE INDEX ix_candle_time_brin ON candle USING BRIN (open_time) WITH (pages_per_range=32);
CREATE INDEX ix_candle_lookup ON candle (instrument_id, tf, open_time DESC);
-- ідемпотентність: ON CONFLICT DO UPDATE ... WHERE candle.is_closed = FALSE AND EXCLUDED.src <= candle.src

-- 3. Журнал подій з ланцюгом хешів
CREATE TABLE event_journal (
  run_id       UUID   NOT NULL,
  seq          BIGINT NOT NULL,
  ts_event_ns  BIGINT NOT NULL,
  ts_ingest_ns BIGINT NOT NULL,
  kind         TEXT   NOT NULL,
  payload      JSONB  NOT NULL,
  prev_hash    BYTEA  NOT NULL,
  hash         BYTEA  NOT NULL,
  PRIMARY KEY (run_id, seq)
);
-- REVOKE UPDATE, DELETE ON event_journal FROM fuzzhelm_app;  -- append-only

-- 4. Прогалини інжесту (доказ надійності конвеєра)
CREATE TABLE ingest_gap (
  id BIGSERIAL PRIMARY KEY,
  instrument_id INT REFERENCES instrument,
  stream TEXT CHECK (stream IN ('klines','trades','depth')),
  ts_lo TIMESTAMPTZ, ts_hi TIMESTAMPTZ,
  expected_count INT, filled_rows INT DEFAULT 0,
  detector TEXT CHECK (detector IN ('seq','bucket_count','time')),
  status TEXT CHECK (status IN ('OPEN','FILLING','FILLED','PARTIAL','UNFILLABLE')),
  attempts SMALLINT DEFAULT 0,
  detected_at TIMESTAMPTZ DEFAULT now(), closed_at TIMESTAMPTZ
);
CREATE INDEX ix_gap_open ON ingest_gap (instrument_id, detected_at DESC)
  WHERE status IN ('OPEN','FILLING');

-- 5. Погодинний скор якості
CREATE TABLE dq_score (
  instrument_id INT, hour_start TIMESTAMPTZ,
  expected_buckets INT, observed_buckets INT, invalid_count INT,
  anomaly_count INT, gap_seconds NUMERIC(10,2), lag_p95_ms NUMERIC(12,2),
  completeness NUMERIC(6,4), validity NUMERIC(6,4),
  timeliness NUMERIC(6,4), continuity NUMERIC(6,4),
  score NUMERIC(6,4) CHECK (score BETWEEN 0 AND 1),
  PRIMARY KEY (instrument_id, hour_start)
);

-- 6. Стратегії (правила як дані, версіоновані через UI)
CREATE TABLE strategy (
  id SERIAL PRIMARY KEY, name TEXT NOT NULL, version INT NOT NULL,
  rules_yaml TEXT NOT NULL, membership_yaml TEXT NOT NULL,
  rules_hash BYTEA NOT NULL, created_by TEXT, created_at TIMESTAMPTZ DEFAULT now(),
  is_active BOOLEAN DEFAULT FALSE,
  UNIQUE (name, version), UNIQUE (rules_hash)
);

-- 7. Паспорт прогону (відтворюваність)
CREATE TABLE run (
  id UUID PRIMARY KEY,
  kind TEXT CHECK (kind IN ('backtest','paper','replay','grid_cell','testnet')),
  strategy_id INT REFERENCES strategy, instrument_id INT REFERENCES instrument,
  tf TEXT, ts_from TIMESTAMPTZ, ts_to TIMESTAMPTZ,
  config JSONB NOT NULL, config_hash BYTEA NOT NULL,
  dataset_hash BYTEA NOT NULL, git_sha CHAR(40), seed BIGINT NOT NULL,
  engine TEXT CHECK (engine IN ('mamdani','linear')),
  journal_head_hash BYTEA, equity_hash BYTEA,
  status TEXT CHECK (status IN ('RUNNING','DONE','FAILED')), error TEXT,
  started_at TIMESTAMPTZ, finished_at TIMESTAMPTZ
);
CREATE UNIQUE INDEX ux_run_identity ON run (config_hash, dataset_hash, seed, engine, git_sha);

-- 8. Рішення з повним трасуванням (джерело /explain)
CREATE TABLE decision (
  id BIGSERIAL PRIMARY KEY,
  run_id UUID NOT NULL REFERENCES run, instrument_id INT, open_time TIMESTAMPTZ,
  t_in NUMERIC(8,5), r_in NUMERIC(8,5), v_in NUMERIC(8,5),
  agreement NUMERIC(6,4), kappa NUMERIC(6,4),
  u_raw NUMERIC(8,5), u_final NUMERIC(8,5),
  detector_outputs JSONB NOT NULL,   -- [{name,s,c,weight,features}]
  memberships      JSONB NOT NULL,   -- {"T":{"ST+":0.71,...},"R":{...},"V":{...}}
  fired_rules      JSONB NOT NULL,   -- [{"rule_id":"R07","alpha":0.62,"consequent":"SL"}]
  target_side SMALLINT CHECK (target_side IN (-1,0,1)),
  target_qty NUMERIC(38,18), binding_constraint TEXT,
  stop_price NUMERIC(38,18), tp_price NUMERIC(38,18), liq_price NUMERIC(38,18),
  UNIQUE (run_id, instrument_id, open_time)
);
CREATE INDEX ix_decision_rules_gin ON decision USING GIN (fired_rules jsonb_path_ops);

-- 9. Ордери (з полями виконання)
CREATE TABLE sim_order (
  id BIGSERIAL PRIMARY KEY,
  run_id UUID REFERENCES run, decision_id BIGINT REFERENCES decision,
  client_order_id UUID UNIQUE NOT NULL, venue_order_id TEXT,
  instrument_id INT, side SMALLINT, otype TEXT CHECK (otype IN ('MARKET','STOP_MARKET')),
  qty NUMERIC(38,18), filled_qty NUMERIC(38,18) DEFAULT 0,
  avg_fill_price NUMERIC(38,18), fee NUMERIC(38,18) DEFAULT 0,
  slippage_bps NUMERIC(12,4), liquidity TEXT CHECK (liquidity IN ('maker','taker')),
  status TEXT CHECK (status IN ('NEW','PARTIAL','FILLED','REJECTED','CANCELED')),
  reject_code TEXT, ts_created TIMESTAMPTZ, ts_filled TIMESTAMPTZ
);
-- FK на decision_id: жоден ордер не існує без рішення ядра

-- 10. Позиції
CREATE TABLE position (
  id BIGSERIAL PRIMARY KEY, run_id UUID, instrument_id INT,
  side SMALLINT, qty NUMERIC(38,18), avg_entry NUMERIC(38,18),
  leverage NUMERIC(8,3), allocated_margin NUMERIC(38,18),
  stop_price NUMERIC(38,18), tp_price NUMERIC(38,18), liq_price NUMERIC(38,18),
  realized_pnl NUMERIC(38,18) DEFAULT 0, funding_paid NUMERIC(38,18) DEFAULT 0,
  max_adverse_excursion NUMERIC(38,18),
  opened_at TIMESTAMPTZ, closed_at TIMESTAMPTZ,
  exit_reason TEXT CHECK (exit_reason IN ('SIGNAL','STOP','TP','RISK_VETO','HALT','LIQUIDATION'))
);

-- 11. Ризик: журнал вердиктів і переходів
CREATE TABLE risk_event (
  id BIGSERIAL PRIMARY KEY, run_id UUID, ts TIMESTAMPTZ, instrument_id INT,
  rule TEXT NOT NULL, verdict TEXT CHECK (verdict IN ('ALLOW','SHRINK','VETO')),
  factor NUMERIC(6,4), observed NUMERIC(38,18), limit_value NUMERIC(38,18),
  state_from TEXT, state_to TEXT, dwell_bars INT, actor TEXT, payload JSONB
);
CREATE INDEX ix_risk_run_ts ON risk_event (run_id, ts DESC);
CREATE INDEX ix_risk_veto   ON risk_event (run_id) WHERE verdict = 'VETO';

-- 12. Крива капіталу + метрики
CREATE TABLE equity_point (
  run_id UUID, ts TIMESTAMPTZ, equity NUMERIC(38,18), cash NUMERIC(38,18),
  unrealized NUMERIC(38,18), gross_exposure NUMERIC(38,18),
  leverage NUMERIC(8,4), drawdown NUMERIC(10,6), risk_state TEXT,
  kappa NUMERIC(6,4), var95 NUMERIC(38,18), cvar95 NUMERIC(38,18),
  PRIMARY KEY (run_id, ts)
);
CREATE TABLE run_metric (run_id UUID, name TEXT, value DOUBLE PRECISION,
  PRIMARY KEY (run_id, name));

-- Auth (ревізія 0003)
CREATE TABLE app_user (id SERIAL PRIMARY KEY, login TEXT UNIQUE, pwd_hash TEXT,
  role TEXT CHECK (role IN ('operator','analyst','auditor','admin')), created_at TIMESTAMPTZ);
CREATE TABLE audit_log (id BIGSERIAL PRIMARY KEY, ts TIMESTAMPTZ, user_id INT,
  action TEXT, target TEXT, before_json JSONB, after_json JSONB, ip INET);
```

---

## 7. Формати конфігурацій

Правила — **дані, а не код**. Усі магічні числа живуть у `config/` і мають вказане джерело
(`percentile` / `kmeans` / `ahp` / `expert`). Це прямо відповідає «за заданими алгоритмічними
правилами»: правила задає оператор через UI, а не програміст комітом.

### `config/membership.yaml`

```yaml
version: 3
variables:
  T:
    range: [-1.0, 1.0]
    source: percentile          # звідки взялися точки зламу
    source_run_id: <<TBD>>      # run_id прогону калібрування
    terms:
      STRONG_DOWN: {type: trap, points: [-1.0, -1.0, -0.70, -0.35]}
      WEAK_DOWN:   {type: tri,  points: [-0.70, -0.35, 0.0]}
      NEUTRAL:     {type: tri,  points: [-0.35, 0.0, 0.35]}
      WEAK_UP:     {type: tri,  points: [0.0, 0.35, 0.70]}
      STRONG_UP:   {type: trap, points: [0.35, 0.70, 1.0, 1.0]}
  R:
    range: [-1.0, 1.0]
    source: expert
    terms:
      SELL_PRESSURE: {type: trap, points: [-1.0, -1.0, -0.45, 0.0]}
      NO_PRESSURE:   {type: tri,  points: [-0.45, 0.0, 0.45]}
      BUY_PRESSURE:  {type: trap, points: [0.0, 0.45, 1.0, 1.0]}
  V:
    range: [0.0, 1.0]
    source: kmeans              # центри = центроїди KMeans(k=3)
    silhouette: <<TBD>>
    terms:
      LO:  {type: gauss, m: <<TBD>>, sigma: <<TBD>>}
      MID: {type: gauss, m: <<TBD>>, sigma: <<TBD>>}
      HI:  {type: gauss, m: <<TBD>>, sigma: <<TBD>>}
  U:
    range: [-1.0, 1.0]
    terms:
      STRONG_SHORT: {type: trap, points: [-1.0, -1.0, -0.80, -0.45]}
      SHORT:        {type: tri,  points: [-0.80, -0.40, 0.0]}
      HOLD:         {type: tri,  points: [-0.40, 0.0, 0.40]}
      LONG:         {type: tri,  points: [0.0, 0.40, 0.80]}
      STRONG_LONG:  {type: trap, points: [0.45, 0.80, 1.0, 1.0]}
defuzz:
  scheme: trapezoid             # rect | trapezoid | simpson
  grid_nodes: 201
```

### `config/rules_mamdani.yaml`

```yaml
version: 3
# Політика LO:  домінує реверсія (R переважує T)
# Політика MID: тренд перемагає реверсію у конфлікті
# Політика HI:  все притягується до HOLD; ненульовий вихід лише при узгоджених T і R
rules:
  - {id: R01, if: {T: STRONG_DOWN, R: SELL_PRESSURE, V: LO},  then: STRONG_SHORT, w: 1.0}
  - {id: R07, if: {T: STRONG_UP,   R: NO_PRESSURE,   V: MID}, then: STRONG_LONG,  w: 1.0}
  # ... рівно 45 правин: 5(T) × 3(R) × 3(V), кожна комбінація рівно один раз
```

Валідація при завантаженні (і тестами): рівно 45 правил; жодних дублікатів антецедентів; усі терми
існують у `membership.yaml`; усі `w == 1.0` у робочій конфігурації (варіант `w != 1.0` існує лише в
тесті-контрприкладі).

### `config/detectors.yaml`, `config/risk_limits.yaml`, `config/dq_weights.yaml`

```yaml
# detectors.yaml
detectors:
  ema_slope:      {weight: 1.0, n_ema: 21, horizon: 5, scale: 0.15}
  donchian:       {weight: 1.0, n: 20, decay: 0.3}
  rsi_exhaustion: {weight: 0.8, n: 14, power: 1.6}
  bollinger_z:    {weight: 0.8, n: 20, k: 2.0, z_scale: 2.0}
  candle_geometry:{weight: 0.6}
  vol_regime:     {weight: 1.0, window: 24, rank_window: 500}

# risk_limits.yaml
limits:
  max_position_notional: {value: 0.30, unit: equity_fraction, action: shrink}
  max_gross_leverage:    {value: 3.0,  action: shrink}
  max_daily_loss:        {value: 0.02, unit: equity_fraction, action: veto, reset: utc_midnight}
  max_drawdown_halt:     {value: 0.12, action: halt}
  liquidation_buffer:    {min_dtl_atr: 6.0, action: reduce_leverage, b: 0.20}
  stale_data:            {max_lag_s: 5.0, min_dq: 0.90, action: veto}
state_machine:
  warn_enter: 0.04   warn_exit: 0.025  warn_dwell: 15
  cool_enter: 0.08   cool_exit: 0.05   cool_dwell: 30
  halt_enter: 0.12
  kappa_mode: {NORMAL: 1.0, WARNING: 0.5, COOLDOWN: 0.25, HALTED: 0.0}

# dq_weights.yaml
ahp_matrix: [[1, 2, 3, 3], [0.5, 1, 2, 2], [0.333, 0.5, 1, 1], [0.333, 0.5, 1, 1]]
weights: <<TBD:головний власний вектор>>
consistency_ratio: <<TBD>>   # мусить бути < 0.1
```

---

## 8. API та веб-панель

### 8.1 Ендпоінти FastAPI

| Метод | Шлях | Роль | Призначення |
|---|---|---|---|
| POST | `/auth/login` | — | JWT (HS256, 8 год) |
| GET | `/market/candles` | analyst+ | свічки з пагінацією |
| GET | `/market/health` | analyst+ | лаг, кадри, реконекти, `Q`, відкриті прогалини |
| GET | `/dq/score` | analyst+ | погодинний `Q` з розкладкою 4 компонент |
| GET | `/strategies` · POST · PUT | analyst (чит.), operator (зміна) | CRUD правил з валідацією YAML і версіонуванням |
| POST | `/backtests` | analyst+ | запуск прогону, повертає `run_id` |
| GET | `/runs/{id}` · `/runs/{id}/metrics` · `/runs/{id}/equity` | analyst+ | результати |
| GET | `/decisions/{id}/explain` | analyst+ | **ядро демо**: МФ, спрацьовані правила з α, μ_agg, центроїд, україномовне трасування |
| GET | `/risk/state` · `/risk/events` | analyst+ | режим, журнал вердиктів |
| PUT | `/risk/limits` | **admin** | зміна лімітів → `audit_log` (before/after) |
| POST | `/risk/killswitch/release` | **admin** | зняття HALTED → `audit_log` |
| GET | `/stream/live` (SSE) | analyst+ | потік подій у браузер |

Матриця доступу «ендпоінт × роль» — таблиця у звіті (підрозділ 2.9). Тест
`test_put_risk_limits_requires_admin_role_403_for_analyst` обов'язковий.

### 8.2 Три екрани Vue 3 (ECharts 5.5, лише 2D)

1. **`LiveView`** — шапка стану (`MODE: PAPER · FEED: REPLAY · NO MAINNET KEYS · SEED · Q`), свічки
   з маркерами входів/виходів і лінією ліквідації, панель 6 детекторів (пари `s`/`c` різними
   кольорами), три стовпчики `T`/`R`/`V`, вихід FIS (`u_raw`, `κ`, `u_final`), вбудований блок
   ризик-панелі (режим, просадка, журнал відхилених ордерів), панель здоров'я конвеєра.
2. **`ExplainView`** — **робити ПЕРШОЮ і полірувати найбільше, це кульмінація захисту**: три
   графіки МФ із вертикалями поточних значень і заштрихованими активними термами; таблиця
   спрацьованих правил зі стовпцем `α`, відсортована за спаданням; агрегована фігура `μ_agg(u)` з
   позначеним центроїдом; згенероване українською речення; розкладка сайзера з `binding_constraint`.
3. **`BacktestView`** — конструктор/редактор правил (`RulesEditor` з валідацією), запуск прогону,
   6 фолдів walk-forward парними стовпчиками IS vs OOS, крива капіталу з drawdown і смугами
   режимів, таблиця 17 метрик + PSR, Парето-фронт, таблиця чутливості.

Компоненти: `CandleChart`, `DetectorGauge`, `MembershipPlot`, `RuleTable`, `RiskStatePanel`,
`RejectionLog`, `EquityCurve`, `MetricsTable`, `RulesEditor`, `DqPanel`.
i18n `uk`/`en` — **інтерфейс українською за замовчуванням**, бо екранограми йдуть в український звіт.

---

## 9. Стек і структура репозиторію

**Базовий:** Python 3.12, FastAPI 0.115, Pydantic 2.9 (`frozen=True`, `extra='forbid'` на канонічних
DTO), SQLAlchemy 2.0 async + asyncpg 0.30, Alembic, PostgreSQL 16, Vue 3.5 + Vite 5 + TS 5.6 +
Pinia, Docker Compose, PlantUML.

**Додано з обґрунтуванням (таблиця йде у звіт):**

| Пакет | Чому саме він |
|---|---|
| `websockets` 13 | явні коди закриття (класифікація розривів) і керований ping/pong; тягнути aiohttp заради одного сокета немає сенсу |
| `httpx` 0.27 + `respx` 0.21 | async REST + мокування на рівні транспорту → офлайн-тести без мережі |
| `numpy` 2.1 | сітка дефазифікації 201 вузол × 45 правил на бар; воркери grid-пошуку над in-memory масивами |
| `scikit-learn` 1.5 | `KMeans` (калібрування МФ) + `MLPRegressor` (автокодувальник аномалій) — одна залежність закриває дві компетентності |
| `hypothesis` 6.112 | інваріанти («ланцюг ризику не збільшує експозицію», «`s∈[−1;1]` для будь-якого OHLCV») прикладами не доводяться |
| `time-machine` 2.15 | детерміністичний час для `MaxDailyLoss`/`StaleDataGuard` |
| `orjson` 3.10 | канонічна серіалізація з `OPT_SORT_KEYS`; **float заборонений рекурсивним предвалідатором** `core/digest.py::assert_no_float()` (orjson серіалізує float нативно, `default` для нього не викликається) |
| `APScheduler` 3.10 | періодичні задачі сервісу |
| `python-jose` + `passlib[bcrypt]` | JWT і хеші паролів |
| `ECharts` 5.5 (2D) | свічки з маркерами, крива капіталу, бари належності |
| `vue-i18n` 10 | локалізація uk/en → українські екранограми |
| `ruff` 0.7 + `mypy` 1.13 (`--strict` лише на `core/`, `fuzzy/`, `risk/`) | статичний контроль без боротьби з типами numpy |

**Свідомо відкинуто (окремий підрозділ звіту):** `scikit-fuzzy`/`simpful` (механізм виведення і є
темою роботи), TimescaleDB (унікальний індекс на гіпертаблиці вимагає колонки партиціонування — це
блокер для дедуплікації; на ~10⁵ рядків BRIN достатньо), Kafka/Redis (один воркер +
`asyncio.Queue` покривають 2 інструменти; брокер тут — карго-культ), PyTorch (інтерпретованість
важливіша), приватний mainnet-API (жорсткий allowlist хостів).

```
fuzzhelm/
├── README.md                    # + README.en.md
├── Makefile                     # up down migrate ingest record replay backtest grid verify test cov lint report backup
├── docker-compose.yml           # db, api, worker, ui  (4 сервіси, без adminer/grafana)
├── docker-compose.test.yml
├── Dockerfile
├── fly.toml                     # деплой на free tier
├── pyproject.toml               # ruff, mypy, pytest, coverage --fail-under=80
├── .env.example                 # без жодного справжнього значення
├── .github/workflows/ci.yml     # ruff → mypy(core,fuzzy,risk) → pytest --cov → pip-audit
├── CHANGELOG.md                 # SemVer
├── config/
│   ├── membership.yaml  rules_mamdani.yaml  detectors.yaml  risk_limits.yaml  dq_weights.yaml
│   └── profiles/{backtest,paper,replay,grid}.yaml
├── fixtures/
│   ├── ws/btcusdt_<дата>.jsonl.gz                    # записана WS-сесія (~45 хв)
│   ├── ws/pathological/{gap,dup,reorder,clock_jump,stall,flash_crash}.jsonl.gz
│   ├── rest/{binance_klines.json.gz,exchange_info.json,kraken_ohlc.json}
│   └── golden/{rsi14.csv,atr14.csv,equity_reference.json,run_manifest.json}
├── alembic/versions/000{1,2,3}_*.py
├── src/fuzzhelm/
│   ├── config.py  cli.py  logging_setup.py
│   ├── core/       dto.py ports.py clock.py journal.py digest.py money.py errors.py enums.py
│   ├── ingest/     rest_client.py kraken_client.py ratelimit.py retry.py backfill.py
│   │               ws_client.py reconnect.py gap_detector.py recorder.py replay.py
│   │               normalize.py symbols.py quantize.py dedup.py crosscheck.py
│   ├── quality/    invariants.py anomaly_mlp.py dq_score.py ahp.py health.py
│   ├── storage/    models.py session.py repositories/{candle,decision,risk,run,equity,gap,dq,user}.py
│   ├── features/   indicators.py pipeline.py window.py convert.py
│   ├── detectors/  base.py ema_slope.py donchian.py rsi_exhaustion.py bollinger_z.py
│   │               candle_geometry.py vol_regime.py registry.py
│   ├── fuzzy/      base.py membership.py rules.py mamdani.py defuzz.py linear.py surface.py
│   ├── decision/   aggregator.py agreement.py core.py trace.py narrative_uk.py
│   ├── regimes/    cluster.py calibrate_mf.py
│   ├── sizing/     vol_target.py atr_risk.py sizer.py hysteresis.py convert.py
│   ├── risk/       verdict.py guard.py state.py margin.py var.py kupiec.py journal.py killswitch.py
│   │               rules/{max_position_notional,max_gross_leverage,max_daily_loss,
│   │                      max_drawdown_halt,liquidation_buffer,stale_data}.py
│   ├── execution/  paper_broker.py cost_model.py testnet_venue.py router.py portfolio.py
│   ├── backtest/   engine.py metrics.py walkforward.py grid.py parallel.py pareto.py manifest.py
│   ├── api/        main.py auth.py deps.py schemas.py routers/{market,strategies,runs,decisions,risk,dq,stream,auth}.py
│   ├── notify/     telegram.py templates.py
│   ├── scheduler/  jobs.py
│   └── workers/    ingest_worker.py trading_worker.py
├── ui/src/
│   ├── views/{LiveView,ExplainView,BacktestView}.vue
│   ├── components/{CandleChart,DetectorGauge,MembershipPlot,RuleTable,RiskStatePanel,
│   │               RejectionLog,EquityCurve,MetricsTable,RulesEditor,DqPanel}.vue
│   ├── stores/{market,decision,risk,backtest,auth}.ts
│   └── i18n/{uk.json,en.json}
├── tests/
│   ├── conftest.py
│   └── unit/ (21 файл)  property/ (5)  integration/ (6)  e2e/ (2)  arch/ (3)
├── docs/
│   ├── journal.md                         # щоденні записи (заготовка звіту)
│   ├── deviations.md                      # розходження спека/реальність
│   ├── tz/technical_specification.md      # ТЗ за ГОСТ 19.201-78
│   ├── teo/cost_estimate.md               # трудомісткість + кошторис + TCO
│   ├── risk_register.md                   # ризики ПРОЄКТУ (не ринкові!)
│   ├── manuals/{user_guide.md,deployment.md,backup_runbook.md}
│   └── diagrams/*.puml  idef0/*  bpmn/*  flowcharts/*  gantt/*  figures/*
└── scripts/
    record_ws_session.py  calibrate_mf.py  train_anomaly_mlp.py  run_backtest.py
    run_grid.py  bench_amdahl.py  plot_membership.py  plot_control_surface.py
    plot_pareto.py  sensitivity.py  export_report_tables.py  testnet_one_order.py
```

**Очікуваний обсяг:** `src` ≈ 4 200 рядків, `tests` ≈ 1 300, Vue/TS ≈ 900, SQL/міграції ≈ 300,
YAML ≈ 250 → **≈ 6 950 рядків**.

---

## 10. Тестова стратегія — 92 кейси

**Цілі:** покриття ≥ 90% для `fuzzy`/`risk`/`decision`/`sizing`, ≥ 80% загалом
(`--cov-fail-under=80`). Повний прогін unit+property **< 25 с**. Жодного мережевого виклику,
жодного `sleep`, жодних перф-ассертів (флейкають саме в день захисту).

### A. Архітектурні (3)
`test_no_wallclock_in_core` (AST-скан `core,features,detectors,fuzzy,decision,risk` на
`datetime.now/utcnow`, `time.time`, `random.*`, `uuid4`) · `test_decimal_float_boundary_is_single_choke_point` ·
`test_mainnet_host_is_rejected_by_config`

### B. Канонічна серіалізація і журнал (8)
`test_canonical_json_rejects_float` · `test_decimal_quantized_not_normalized` (`Decimal('100')` →
`"100.00"`, **не** `"1E+2"`) · `test_keys_sorted_lexicographically` ·
`test_state_digest_stable_across_processes` (subprocess з іншим `PYTHONHASHSEED`) ·
`test_hash_chain_links_prev_hash` · `test_tampered_payload_breaks_chain_at_exact_seq` ·
`test_event_uid_stable_across_restart` · `test_dedup_idempotent_under_permutation` [property]

### C. Нормалізація та інжест (13)
`test_kline_ms_to_ns_exact` · `test_price_quantized_to_tick_half_even` · `test_qty_floored_to_step` ·
`test_reject_below_min_notional_with_code` · `test_unknown_field_raises_normalization_error` ·
`test_event_and_ingest_time_never_mixed` · `test_paginator_stitches_segments_with_one_bar_overlap` ·
`test_paginator_detects_gap_in_overlap` · `test_token_bucket_blocks_on_weight_exhaustion` ·
`test_retry_after_header_honored` · `test_backoff_full_jitter_within_cap` [property] ·
`test_binance_kraken_price_crosscheck_flags_divergence_above_50bps` ·
`test_gap_detected_and_backfilled_idempotently`

### D. Якість даних (7)
`test_high_below_close_rejected` · `test_price_not_multiple_of_tick_rejected` ·
`test_dq_score_in_unit_interval` [property] · `test_perfect_hour_scores_one` ·
`test_timeliness_decays_exponentially` · `test_ahp_weights_sum_to_one_and_cr_below_0_1` ·
`test_mlp_autoencoder_flags_injected_spike_and_not_normal_bar`

### E. Індикатори (9)
`test_rsi14_matches_wilder_golden_csv` (atol 1e−9) · `test_atr14_matches_wilder_golden_csv` ·
`test_wilder_alpha_is_one_over_n_not_two_over_n_plus_one` · `test_ema_incremental_equals_batch`
(10 000 барів, atol 1e−10) · `test_welford_stable_on_1e9_offset_series` (наївна `Σx²` тут падає) ·
`test_donchian_deque_matches_naive_max` [property] · `test_parkinson_vol_exact_on_constant_range` ·
`test_ols_r2_near_one_on_clean_trend` · `test_indicators_return_none_before_warmup`

### F. Детектори (8)
`test_all_detectors_bounded_and_no_nan_on_any_ohlcv` [property, 6×200 прикладів] ·
`test_all_detectors_are_pure` · `test_all_detectors_registered` ·
`test_ema_slope_positive_on_linear_uptrend` · `test_donchian_confidence_decays_with_staleness` ·
`test_rsi_convex_map_weak_in_middle` (`|s(45)| < |s(25)|/4`) ·
`test_bollinger_confidence_drops_when_bandwidth_expands` · `test_pin_bar_score_zero_when_wicks_symmetric`

### G. Нечітке ядро (17) — найважливіший блок
`test_tri_mf_peak_equals_one` · `test_trap_plateau_is_flat` · `test_gauss_mf_symmetry` ·
`test_mf_partition_of_unity_ruspini` (2001 точка, `Σμ = 1 ± 1e−12`) ·
`test_mf_coverage_no_dead_zones` (`max_i μ ≥ 0.5`) · `test_rulebase_yaml_has_exactly_45_rules` ·
`test_rulebase_no_duplicate_antecedents` · `test_rulebase_all_terms_exist_in_membership_config` ·
`test_rule_firing_is_min_of_memberships` · `test_dont_care_term_contributes_one` ·
`test_clipped_consequent_never_exceeds_alpha` · `test_aggregation_is_pointwise_max` ·
`test_centroid_of_symmetric_aggregate_is_zero` · `test_empty_activation_returns_exactly_zero` ·
`test_defuzz_grid_convergence_order_is_two_for_trapezoid` ·
**`test_u_nondecreasing_in_trend_input`** [property, 500 прикладів, `w_r ≡ 1`] ·
**`test_weighted_rules_break_monotonicity_counterexample`** (документує контрприклад для `w=0.8`)

### H. Агрегація і κ (8)
`test_consensus_zero_when_all_confidences_zero` · `test_consensus_between_min_and_max_contribution`
[property] · `test_membership_probabilities_sum_to_one` (`p₊+p₋+p₀ ≡ 1`) [property] ·
`test_agreement_is_one_when_all_same_sign` · **`test_kappa_reaches_kmin_on_uniform_split`**
(`κ = 0.35 ± 1e−6`) · `test_kappa_monotone_in_agreement` [property] ·
`test_entropy_handles_zero_probability` · `test_decision_trace_contains_every_fired_rule`

### I. Сайзинг (9)
`test_position_size_inverse_to_atr` [20 значень ATR, грошовий ризик = const] ·
`test_vol_target_halves_notional_when_vol_doubles` · `test_vol_target_clipped_at_bounds` ·
`test_first_order_filter_reaches_63pct_in_T` · `test_final_qty_is_min_of_three_constraints` ·
`test_sizer_reports_binding_constraint` · `test_qty_floored_never_rounded_up` [property] ·
`test_hysteresis_prevents_flip_flop` (0.30→0.20→0.15→0.11) ·
`test_size_zero_in_halted_regardless_of_signal`

### J. Ризик (16)
**`test_risk_chain_never_increases_exposure`** [property, до 20 вердиктів] ·
`test_veto_absorbs_everything` · `test_compose_is_order_independent` [property] ·
`test_risk_fsm_transition_table_is_total` [parametrize 4×6=24] ·
`test_hysteresis_blocks_recovery_inside_band` · `test_dwell_time_blocks_premature_recovery` ·
`test_halted_requires_manual_release_by_admin` · `test_max_daily_loss_resets_at_utc_midnight`
[time-machine] · `test_drawdown_uses_running_peak` · `test_stale_data_vetoes_on_lag_and_on_low_dq` ·
`test_liq_price_long_10x_equals_90_4523` · `test_liq_price_short_symmetry` ·
`test_margin_ratio_is_one_at_liq_price` · `test_liquidation_guard_reduces_leverage_before_veto` ·
`test_every_verdict_written_to_risk_event_with_observed_and_limit` ·
`test_cvar_ge_var_always` [property] (+ `test_kupiec_lr_zero_when_breaches_equal_expected`,
`test_kupiec_rejects_at_20_breaches_of_500`)

### K. Виконання і бектест (13)
`test_fill_on_next_bar_open_not_current_close` · `test_sqrt_impact_scales_with_sqrt_of_qty` ·
`test_taker_fee_higher_than_maker` · `test_funding_charged_at_00_08_16_utc` ·
`test_intrabar_pessimism_resolves_stop_before_tp` ·
**`test_equity_accounting_identity`** (`cash + Σq·p − Σfees == equity`, 1e−9, на кожному кроці) ·
`test_lookahead_guard_raises_on_future_index` ·
`test_shuffling_future_bars_does_not_change_past_decisions` ·
`test_backtest_deterministic_same_seed_same_equity_sha256` ·
`test_different_seed_changes_equity` (негативний контроль) · `test_zero_signal_yields_flat_equity` ·
`test_walkforward_embargo_no_overlap` [parametrize k=6 × E∈{0,60,120,240}] ·
`test_grid_results_independent_of_worker_count`

### L. Метрики (6)
`test_sharpe_matches_reference_series` · `test_sortino_penalizes_only_downside` ·
`test_max_drawdown_on_known_curve` · `test_ulcer_zero_on_monotone_curve` ·
`test_psr_below_threshold_on_short_sample` · `test_pareto_front_contains_only_nondominated`

### M. Інтеграційні (6, маркер `integration`, Postgres із docker-compose)
`test_candle_upsert_idempotent` · `test_upsert_does_not_overwrite_closed_candle` ·
`test_check_constraint_rejects_invalid_candle` · `test_journal_append_only_revoked_update` ·
`test_order_requires_decision_fk` · `test_migrations_up_and_down_clean`

### N. API та e2e (8)
`test_login_returns_jwt_and_role` · `test_put_risk_limits_requires_admin_role_403_for_analyst` ·
`test_limit_change_written_to_audit_log_with_before_after` ·
`test_get_explain_returns_fired_rules_and_memberships` ·
`test_explain_narrative_is_ukrainian_and_non_empty` ·
`test_strategy_post_invalid_yaml_returns_422_with_field_path` ·
`test_replay_session_end_to_end` (≥1 угода, 0 `LookaheadError`, тотожність капіталу, всі угоди мають
`fired_rules`, `run.status='DONE'`) ·
`test_all_pathological_sessions_recover_with_zero_lost_events` [parametrize по 6 фікстурах]

**Приймальне тестування (окремо, у додаток):** таблиця «вимога ТЗ (FR-01…FR-24, NFR-01…NFR-09) →
критерій приймання → тест-кейс / ручна процедура → результат».

---

## 11. Що НЕ робимо (свідомо відрізане)

| Відрізано | Обґрунтування |
|---|---|
| TimescaleDB, гіпертаблиці, continuous aggregates, компресія | унікальний індекс на гіпертаблиці вимагає колонки партиціонування — блокер для дедуплікації; при ~10⁵ рядків BRIN достатній. У звіті — абзац «чому партиціонування тут не потрібне» |
| Синхронізація L2 diff-потоку (U/u/pu), CRC32, таблиця `order_book_events` | 3–4 дні на біржу; `depth20@100ms` дає готові снапшоти, з яких OBI рахується без алгоритму ресинку |
| Модель позиції в черзі лімітної заявки, лімітні ордери | без L3/MBO це необґрунтоване наближення, яке завищує результат; лишаються `MARKET` і `STOP_MARKET` |
| Рушій Сугено, порівняння трьох рушіїв | Сугено потребує окремого набору синглтонів на 45 правил — порівняння було б нечесним; `LinearVoteEngine` (50 рядків) як базова лінія несе весь аргумент ablation |
| Генетичний алгоритм (1000 оцінювань, графік збіжності) | гарантоване перенавчання на одному активі; паралелізм і замір Амдала повністю зберігаються на grid-пошуку по 108 клітинках |
| ECharts GL, 3D-поверхня керування в браузері | WebGL-конфлікти у Vite і ризик порожньої панелі в аудиторії; поверхня `u(T,R` за фіксованого `V)` генерується matplotlib-скриптом |
| `CorrelationClusterCap`, параметричний VaR портфеля, EWMA-коваріація | на 1–2 інструментах це театр; історичний VaR/CVaR лишається звітною метрикою з тестом Купця |
| `MaxConsecutiveLosses`, `OrderRateLimit`, `FatFinger`, `PriceBand` | перше поглинається `MaxDailyLoss`+`MaxDrawdownHalt`, решта безпредметна у paper-брокері; 10 правил → 6 реальних |
| Fractional Kelly | циклічна залежність (`p` і `b` з бектесту, який залежить від `ρ_base`, що залежить від `f*`); описати у звіті як розглянутий і свідомо відкинутий варіант |
| Відсоток за позичені кошти | фандинг уже несе маржинальну механіку; третя сутність у моделі витрат не дає нового висновку |
| 5 Vue-екранів → 3 | `SurfaceView` → статичні PNG, `RiskView` → блок у `LiveView` |
| `testcontainers`, перф-ассерти, `EXPLAIN`-тести | флейкають на навантаженому ноутбуці |
| 174 тести → 92 | комісія питає «який інваріант перевіряє цей тест», а не рахує їх |

---

## 12. Порядок виконання: фази і gate-критерії

Кожна фаза завершується: зелений `make test`, коміт, абзац у `docs/journal.md`, артефакт у `docs/`.

### Фаза 0 — Плацдарм (вечір перед)
Репо з iCloud на `~/dev/fuzzhelm`; каркас пакетів; `docker compose up` (db+api); Alembic `0001`;
**запис живої WS-сесії** `scripts/record_ws_session.py --minutes 45` + генерація 6 патологічних
варіантів з неї (`gap`, `dup`, `reorder`, `clock_jump`, `stall`, `flash_crash`).
**Gate:** `fixtures/ws/*.jsonl.gz` існують і `replay` їх читає.
**Критично:** без фікстури далі не працює ніщо. Якщо живий WS недоступний — синтезувати сесію з
REST-klines (`scripts/record_ws_session.py --synthesize-from-rest`) і зафіксувати це в `deviations.md`.

### Фаза 1 — `core` + REST-інжест
DTO, порти, `Clock`, журнал з ланцюгом BLAKE2b, канонічна серіалізація з предвалідатором float;
`rest_client` (пагінація з перекриттям 1 бар, token-bucket з вагами, retry з повним джитером),
`backfill` 45 днів klines у БД, `kraken_client` для крос-звірки.
**Gate:** у `candle` ≥ 60 000 рядків; тести A, B, C зелені; `ТЗ` (FR/NFR) чернетка готова.
**Артефакти:** ТЗ, IDEF0 A-0/A0, діаграма компонентів, ER Чена.

### Фаза 2 — WebSocket-інжест, нормалізація, якість
`ws_client` + `reconnect` + `heartbeat` + `gap_detector` + `recorder`/`replay`;
`normalize`/`quantize`/`dedup`/`crosscheck`; `quality` (інваріанти, AHP-ваги, `Q`).
**Gate:** усі 6 патологічних сесій відтворюються з нульовою втратою подій
(`test_all_pathological_sessions_recover_with_zero_lost_events`); у `ingest_gap` є реальні рядки зі
статусом `FILLED`; тест D зелений.
**Артефакти:** sequence «розрив → прогалина → REST-backfill», таблиця мапінгу полів двох бірж.

### Фаза 3 — Ознаки і детектори
9 індикаторів O(1) з golden-тестами Уайлдера; `BarWindow` з `LookaheadGuard`; 6 детекторів +
`registry`; `regimes.cluster` → `calibrate_mf` (KMeans, силуетний коефіцієнт) → **записати
фактичні числа в `membership.yaml`**.
**Gate:** тести E, F зелені; `membership.yaml` не містить `<<TBD>>` у секції `V`.
**Артефакти:** таблиця «індикатор → формула → складність → тест», рисунок кластерів, діаграма класів.

### Фаза 4 — Нечітке ядро
МФ, парсер YAML (45 правил), Мамдані, три схеми дефазифікації + аналіз порядку збіжності,
`LinearVoteEngine`; `decision` (консенсус, ентропія, `κ`, `DecisionTrace`, `narrative_uk`).
**Gate:** усі 17 тестів G і 8 тестів H зелені, включно з property-тестом монотонності **і**
тестом-контрприкладом; `plot_membership.py` і `plot_control_surface.py` генерують рисунки.
**Артефакти:** 4 панелі МФ, таблиця бази правил, блок-схема ДСТУ ISO 5807, таблиця збіжності.

### Фаза 5 — Сайзинг і ризик
`vol_target` + `atr_risk` + `sizer` + `hysteresis`; алгебра вердиктів, 6 правил, автомат станів,
`margin`, `var`, `kupiec`, `journal`, `killswitch`.
**Gate:** тести I (9) і J (16) зелені; `test_risk_chain_never_increases_exposure` і
`test_liq_price_long_10x_equals_90_4523` **обов'язково**; таблиця переходів тотальна.
**Артефакти:** statechart, BPMN ланцюга перевірок, таблиця 6 лімітів, виведення `P_liq`.

### Фаза 6 — Виконання і бектест
`PaperBroker`, `CostModel`, `Portfolio`, `backtest.engine`, 17 метрик + PSR; **перший повний прогін
на 45 днях**.
**Gate:** тести K (13) і L (6) зелені; `test_equity_accounting_identity` тримається на кожному
кроці; у БД є `run` зі статусом `DONE` і повним паспортом.
**Артефакти:** крива капіталу з drawdown і смугами режимів, таблиця метрик, таблиця 3 моделей витрат.

### Фаза 7 — Обчислювальний експеримент
`walkforward` + embargo, `grid` 108 клітинок + `parallel` + `bench_amdahl`, `pareto`,
`sensitivity.py` (8 параметрів), `anomaly_mlp` (навчання + ROC-AUC).
**Gate:** у БД 108 `grid_cell`-прогонів; `S(p)` заміряно для `p ∈ {1,2,4,8}`; усі `<<TBD>>` числа
експерименту заповнені.
**Артефакти:** таблиця IS/OOS по 6 фолдах, графік `S(p)` проти Амдала, Парето-фронт, таблиця
чутливості, ROC-AUC.

### Фаза 8 — API, безпека, сервісність
Роутери, `/explain`, JWT + 4 ролі + `audit_log`, CRUD стратегій з валідацією і версіонуванням,
`notify.telegram`, `scheduler.jobs`.
**Gate:** тести N (без e2e-UI) зелені; `/docs` віддає повну OpenAPI; зміна ліміту пише before/after.
**Артефакти:** OpenAPI, sequence автентифікації, STRIDE-таблиця, матриця доступу.

### Фаза 9 — Веб-панель
3 екрани Vue + i18n. **`ExplainView` робиться ПЕРШОЮ і полірується найбільше.** `plot_*.py` → 9
рисунків. Деплой на Fly.io. `backup`/`restore` runbook. `pip-audit`.
**Gate:** `test_replay_session_end_to_end` зелений; 14 екранограм зняті; посилання на розгорнутий
екземпляр працює.

### Фаза 10 — Добивання
Тести до 92 і покриття ≥80%; ТЕО; WBS + діаграма Ганта + сітьовий графік; **реєстр ризиків
проєкту**; керівництво користувача; інструкція з розгортання; англомовний abstract; `CHANGELOG`.
**Gate:** `make cov` показує ≥80% загалом і ≥90% на чотирьох ключових пакетах; у `docs/` немає
жодного `<<TBD>>`.

### Фаза 11 — Звіт **[ЗАПУСКАТИ ОКРЕМО, ЗА ЯВНОЮ КОМАНДОЮ]**
Не починати разом з кодом. Див. розділ 14. На вхід ідуть: `docs/journal.md`, `docs/deviations.md`,
усі таблиці з `scripts/export_report_tables.py`, 9 рисунків, 14 екранограм, вивід `pytest --cov`.

### 12.9 Кроки **[ЛЮДИНА]** — агент їх не виконує

1. **Реєстрація на Binance Futures Testnet** і отримання тестових API-ключів (потрібен акаунт).
   Агент лише читає `BINANCE_TESTNET_KEY`/`BINANCE_TESTNET_SECRET` з `.env`, який заповнює студент.
2. **Створення Telegram-бота** через `@BotFather`, отримання токена.
3. **Реєстрація на Fly.io** для деплою.
4. Підписання бланків практики, отримання відгуку від керівника.

Якщо якогось із цих ключів немає — відповідний модуль пишеться повністю, покривається тестами з
моками, а живий прогін позначається в `deviations.md` як «не виконано: немає креденшлів».
Це не блокує жодну іншу фазу. Порядок аварійного відрізання — розділ 13.

---

## 13. Ризики проєкту і порядок аварійного відрізання

**Порядок відрізання при відставанні** (згори — відрізається першим):
MLP-автокодувальник → крос-звірка з Kraken → Telegram → Fly.io → i18n → grid зі 108 до 36 клітинок.

**Не відрізати ніколи:** `ExplainView`; інваріант монотонності ризик-ланцюга; тотожність обліку
капіталу; `ReplayFeed` з офлайн-фікстурою; ТЗ; діаграма Ганта; таблиця трасування завдання.

Реєстр ризиків проєкту (`docs/risk_register.md`, ≥9 позицій) — саме це, а не ринковий VaR,
відповідає ФК15 «методи оцінювання ризиків **їх проектування**». Приклади рядків:

| Ризик | Ймовірн. | Вплив | Мітигація |
|---|---|---|---|
| Біржа змінила формат WS-повідомлення | середня | висока | усе демо працює з `ReplayFeed` на записаній фікстурі; live — опційний |
| Docker-volume псується через iCloud | висока | висока | репозиторій на локальному шляху `~/dev` (фаза 0) |
| Немає testnet-ключів до захисту | середня | середня | `PaperBroker` покриває демо; testnet-ордер позначається як не виконаний |
| Немає інтернету в аудиторії | середня | висока | офлайн-фікстура + записаний скрінкаст як план Б |
| Витік ключів у репозиторій | низька | висока | `.env` у `.gitignore`, `pip-audit`, заборона логувати секрети |
| Перенавчання на grid-пошуку | висока | середня | walk-forward з embargo, PSR/DSR, Парето-фронт замість argmax |

---

## 14. План звіту на 25–30 сторінок (фаза 11)

Times New Roman 14 pt, інтервал 1,5, поля 25/20/20/25 мм. Основний текст — **28 сторінок**.
Повні тексти програм і екранограми — **тільки в додатках** (вони не входять в обсяг).

**ВСТУП — 1,5 с.** Актуальність (частка алгоритмічної торгівлі, проблема інтерпретованості
автоматичних рішень). **Об'єкт** — процес автоматизованого прийняття торговельних рішень за
неповних даних. **Предмет** — методи та засоби нечіткого виведення і автоматичного контролю
ризиків у складі програмного сервісу. Мета і 7 задач. Методи дослідження (нечітка логіка, кластерний
аналіз, теорія автоматичного керування, статистичне оцінювання, property-based верифікація).
Практичне значення. Перелік ЗК/ФК/ПР, що формуються. Структура звіту.

**1 ОСНОВНА ЧАСТИНА — 8 с.**
- **1.1 Характеристика бази практики — 1,5 с.** Кафедра АСУ ІКНІ НУ «Львівська політехніка»:
  завідувач проф. Теслюк В.М., директор ІКНІ Олег Матвійків, наукові напрями, лабораторії; місце
  ОПП «Комп'ютерні науки (Обчислювальний інтелект смарт-систем)» у структурі інституту.
  *Таблиця структури кафедри, рисунок організаційної схеми.*
- **1.2 Основне виробниче завдання і обов'язки на робочому місці — 1 с.** *(Розділ 4 програми
  практики вимагає ДВА пункти: основне виробниче завдання і індивідуальне завдання — не забути.)*
  Обов'язки стажиста-програміста, участь у розробці й експлуатації ПЗ, ведення проєктної
  документації, щоденні записи. *Таблиця «обов'язки → виконані роботи».*
- **1.3 Інформаційні технології, з якими ознайомився — 3 с.** Асинхронний Python (asyncio,
  структурована конкурентність, backpressure); протоколи біржових API (REST з ваговим лімітуванням
  vs WebSocket зі станом); PostgreSQL 16 (BRIN, `ON CONFLICT`, три рівні моделі БД); методології
  тестування (unit/property/golden/інтеграційне); контейнеризація і CI/CD; нечітке моделювання;
  **апаратне забезпечення і методика вибору технічних засобів** — характеристики робочої станції,
  розрахунок обсягу даних (10⁵ свічок ≈ 38 МБ + індекси), вимоги до RAM/IOPS для PostgreSQL.
  *Таблиця «технологія → де застосована → чому саме вона», таблиця розрахунку техзасобів.*
- **1.4 Заняття, консультації, ознайомлення з виробництвом — 0,5 с.**
- **1.5 Охорона праці та безпечні умови роботи ІТ-фахівця — 1 с.** Вступний інструктаж і на
  робочому місці; ДСанПіН 3.3.2.007-98 (робота з ВДТ); ергономіка, освітлення, режим праці й
  відпочинку, електро- і пожежна безпека. *Рисунок організації робочого місця.*
- **1.6 Календарний план-графік — 1 с.** WBS, діаграма Ганта, сітьовий графік з критичним шляхом.

**2 ІНДИВІДУАЛЬНЕ ЗАВДАННЯ — 17 с.**
- **2.1 Постановка задачі, аналіз предметної області, огляд аналогів — 2 с.** Функціональна модель
  as-is (ручна маржинальна торгівля) і to-be (автоматизована з ризик-контуром) — 2 BPMN +
  порівняльна таблиця. Порівняння Freqtrade / Hummingbot / Jesse / Backtrader / NautilusTrader за
  6 критеріями (інтерпретованість рішення, нечітке ядро, модель ризик-лімітів, якість моделі витрат,
  відтворюваність прогонів, аудит відхилень) → висновок про нішу. Огляд 5 статей 2020–2025
  (Scopus/IEEE) про нечітку логіку в алгоритмічній торгівлі.
- **2.2 Технічне завдання і вимоги — 1,5 с.** ТЗ за ГОСТ 19.201-78: призначення, вимоги до функцій,
  надійності, складу техзасобів, сумісності; стадії розробки; порядок контролю і приймання.
  Нумеровані FR-01…FR-24, NFR-01…NFR-09 з критеріями приймання. Ролі: оператор / аналітик / аудитор
  / адміністратор. Дерево цілей. **Реєстр ризиків проєкту** (9 позицій). Елементи ТЕО:
  трудомісткість, кошторис, TCO. *Use-case + специфікації 4 сценаріїв, дерево цілей, 3 таблиці.*
- **2.3 Архітектура сервісу — 1,5 с.** Порти й адаптери, межа детермінізму, межа `Decimal↔float`.
  *Діаграма компонентів UML, IDEF0 A-0 + декомпозиція A0 (A1 «Агрегувати дані» … A5 «Виконати і
  облікувати»), DFD рівня 1, діаграма розгортання, діаграма пакетів, таблиця модулів.*
- **2.4 Підсистема агрегації, нормалізації та збереження — 2,5 с.** REST з ваговим бюджетом і
  стиком сегментів; WebSocket з реконектом і детекцією прогалин; крос-звірка двох джерел;
  Decimal-дисципліна і квантування; скор `Q` з вагами за AHP; MLP-детектор аномалій; схема БД у
  трьох рівнях; запис і відтворення сесій. *Sequence «розрив → backfill», ER Чена + фізична схема,
  таблиця індексів, ROC-AUC, гістограма затримки.*
- **2.5 Ансамбль детекторів патернів — 2 с.** Контракт `DetectorOutput`, формули 6 детекторів,
  шість різних способів обчислення довіри з властивостей даних. *Діаграма класів (Strategy), таблиця.*
- **2.6 Нечітке ядро прийняття рішень — 3,5 с. — ЦЕНТРАЛЬНИЙ ПІДРОЗДІЛ.** Ієрархічна агрегація
  729 → 45; лінгвістичні змінні і калібрування МФ кластеризацією (рисунок кластерів, силуетний
  коефіцієнт); база правил і три політики; алгоритм Мамдані покроково; дефазифікація як чисельне
  інтегрування — три схеми і порядок збіжності; ентропійна узгодженість і `κ` з виведенням `p₀`;
  поверхня керування при `V = 0.2/0.5/0.9`; **результат про монотонність** (перевірена для `w ≡ 1`,
  побудований контрприклад для `w ≠ const`); порівняння з лінійним голосуванням.
  *4 панелі МФ, 3 панелі поверхні, блок-схема ДСТУ ISO 5807, таблиця схем інтегрування.*
- **2.7 Сайзинг і підсистема автоматичного контролю ризиків і лімітів — 2,5 с.** Ланцюжок від `u`
  до кількості; таргетування волатильності як gain scheduling з перехідною характеристикою;
  гістерезис з кількісною ціною відсутності (−1.15%/добу); виведення ціни ліквідації і DTL в ATR;
  алгебра вердиктів і доказ монотонності; автомат станів з гістерезисом і dwell; VaR/CVaR + тест
  Купця; оцінка гіршого випадку швидкості просадки. *Statechart, BPMN, блок-схема композиції
  вердиктів, таблиця 6 лімітів, гістограма доходностей з VaR/CVaR.*
- **2.8 Симулятор виконання та підсистема бектестингу — 1,5 с.** Правило `t → t+1`,
  `LookaheadGuard` як тип, модель витрат з корінним впливом, песимістичне розв'язання
  неоднозначності, walk-forward з embargo. *Рисунок фолдів, таблиця 3 моделей витрат, таблиця метрик.*
- **2.9 Інформаційна безпека сервісу — 1,5 с.** STRIDE-модель загроз (8 рядків); JWT + 4 ролі +
  матриця доступу; аудит дій з before/after; керування секретами; валідація вхідних даних як межа
  довіри; `pip-audit`; HTTPS/reverse proxy; allowlist хостів біржі. *Sequence автентифікації, 2 таблиці.*
- **2.10 Розгортання, відтворюваність і експлуатація — 1 с.** Docker Compose, Fly.io, тріада
  `config_hash + git_sha + seed`, планувальник періодичних задач, backup/restore, нотифікації.
- **2.11 Обчислювальний експеримент, аналіз чутливості та результати — 1,5 с.** Таблиця IS/OOS по
  6 фолдах; PSR і DSR з фактичним `N`; ablation по детекторах і Мамдані vs лінійне; чутливість до
  8 параметрів; Парето-фронт; `S(p)` проти межі Амдала; таблиця 6 патологічних сесій
  «втрачено / продубльовано / час відновлення». *6 рисунків, 5 таблиць.*
- **2.12 Тестування — 1,5 с.** Піраміда, розподіл 92 кейсів, три цитовані інваріанти, приймальне
  тестування за ТЗ. *Таблиця «група → кейсів → інваріант», таблиця трасування «FR → модуль → тест»,
  скріншот покриття.*
- **2.13 Правові та етичні аспекти — 0,5 с.** Система не є інвестиційною рекомендацією; лише
  публічні дані згідно з ToS; відсутність шляху до mainnet; обробка персональних даних оператора.

**ВИСНОВКИ ТА ПРОПОЗИЦІЇ — 1,5 с.** Таблиця «пункт індивідуального завдання → що зроблено → яким
артефактом підтверджено» (11 рядків із розділу 2 цього брифінгу). Кількісні результати. Що
виявилося складнішим за очікуване. Пропозиції кафедрі. Напрями продовження в бакалаврській
кваліфікаційній роботі: ANFIS-налаштування МФ градієнтним методом, портфель 10+ активів,
калібрування `k_s` на тикових даних.

**СПИСОК ДЖЕРЕЛ — 1,5 с., ≥24 позиції.** Zadeh (1965); Mamdani & Assilian (1975); Ross T.J. Fuzzy
Logic with Engineering Applications; Рутковський Л. Методи і технології штучного інтелекту;
Wilder (1978); Parkinson (1980); Welford (1962); Saaty (AHP); Almgren & Chriss (2000); Kupiec
(1995); Bailey & López de Prado, The Deflated Sharpe Ratio (2014); Jorion, Value at Risk; Amdahl
(1967); MacQueen (1967); **5 статей 2020–2025 зі Scopus/IEEE** про fuzzy-logic trading і risk
control; **≥2 українські джерела** (посібники кафедри з теорії керування і обчислювального
інтелекту); ISO/IEC 25010:2011; ISO 31000:2018; ДСТУ ISO 5807; ГОСТ 19.201-78;
ДСанПіН 3.3.2.007-98; документація Binance API, PostgreSQL 16, FastAPI, Hypothesis.

**ДОДАТКИ (поза обсягом).** А — повна таблиця 45 правил і параметри всіх МФ із джерелом кожного
числа. Б — повні тексти програм (`fuzzy/`, `risk/`, `decision/`, `detectors/`, `quality/`) із
заголовком кожного файлу (найменування, час створення, автор) і коментарями до базових змінних та
логічних фрагментів — **так вимагає програма практики**. В — екранограми (14): 3 екрани дашборду,
`ExplainView`, поверхня керування, Q-панель, `/docs`, testnet-ордер, Telegram, вивід `pytest`,
покриття, історія комітів. Г — таблиці результатів і приймальне тестування. Д — ТЗ повністю, ТЕО,
керівництво користувача, інструкція з розгортання, backup runbook. Е — DDL і міграції.
Ж — англомовний abstract. З — титульна сторінка (Додаток Д програми), бланк «Завдання та
результати проходження практики» (Додаток В програми), скерування, довідка-згода бази практики.

---

## 15. Сценарій демонстрації на захисті (5 хвилин)

**Підготовка (за 20 хв, не на очах комісії):** `make up` виконано, БД засіяна 45 днями свічок,
готовий walk-forward прогін у БД, браузер на `LiveView`, другий термінал, третя вкладка —
`BacktestView`. **Записаний скрінкаст усього сценарію лежить на робочому столі як план Б.**

**0:00–0:30 — Рамка і безпека.** У шапці: `MODE: PAPER · FEED: REPLAY · NO MAINNET KEYS · SEED · Q=1.00`.
«Система працює лише з публічними read-only даними, виконання симульоване, а шляху до реальних
грошей у ній немає за побудовою — конфігурація має allowlist хостів, і це перевіряється тестом.
Тема роботи — не стратегія, а інтерпретоване нечітке ядро рішень у доказово безпечній ризик-оболонці.»
**Демонстративно вимкнути Wi-Fi.**

**0:30–1:20 — Живий конвеєр і якість даних.** «Replay ×30». Свічки наростають, зліва 6 індикаторів
детекторів, нижче три стовпчики `T/R/V`, праворуч `u_raw`, `κ`, `u_final`. Панель здоров'я:
`frames · lag · gaps: 2 (both FILLED) · Q`. Відкрити рядок `ingest_gap`: «Два розриви послідовності
система знайшла і добрала через REST. А скор якості даних не декоративний — він є ПЕРШИМ входом
ланцюга ризик-перевірок.»

**1:20–2:30 — КУЛЬМІНАЦІЯ: формальне виведення.** Спрацьовує угода → клік на маркер → `ExplainView`.
Прочитати вголос: **«Спрацювало правило R07 з α = 0.62: ЯКЩО тренд СИЛЬНЕ_ЗРОСТАННЯ І реверсія
НЕМА_ТИСКУ І волатильність ПОМІРНА ТО сигнал СИЛЬНИЙ_ЛОНГ.»** Показати розкладку сайзера:
`atr_risk | vol_target | leverage → binding: VOL_TARGET → ×κ_mode ×κ → кількість`, і червону лінію
`Liq · DTL в ATR`. «Кожна угода має формальний вивід, який можна прочитати. На питання "чому саме
тут" я відповідаю не вагами, а правилами.» **Витратити повну хвилину, не поспішати.**

**2:30–3:20 — Ризик-контур стримує сам себе.** Перемкнути `Scenario: flash_crash`. **Далі не
торкатися клавіатури:** просадка 4% → `NORMAL → WARNING`, `κ_mode = 0.5`; 8% → `COOLDOWN`,
`κ = 0.25`, reduce-only, у журналі `MaxDailyLoss · VETO · observed=−2.10% · limit=−2.00%`;
12% → `HALTED`, flatten-all, червоний банер. Натиснути «Continue» — не працює. «Стан засувний,
знімається лише адміністратором через окремий ендпоінт, і кожне зняття пишеться в `audit_log`.
Відновлення: просадка впала до 3.1% — система НЕ повертається, бо повернення при 2.5% і після
15 барів витримки. Це не налаштування, це усунення чатерингу релейної ланки.» І головне:
«Композиція будь-якої кількості ризик-правил може лише зменшити експозицію — доведено
property-тестом, а не покладається на мою уважність.»

**3:20–4:10 — Бектест чесно.** 6 фолдів walk-forward, парні стовпчики IS vs OOS. Сказати прямо:
«OOS гірший за IS — це нормально, і саме тому walk-forward з embargo, а не 80/20. PSR = <факт>,
DSR при N=108 нижче порогу — на 45-денній вибірці одного активу перевага статистично не встановлена,
і я цього не тверджу.» Парето-фронт: «Робочу точку обрано з недомінованої множини за трьома
критеріями, а не argmax Sharpe.» Таблиця чутливості: «Ось як результат змінюється при варіації
восьми параметрів.» Таблиця трьох моделей витрат: «Наївна модель без ковзання завищує Sharpe у
кілька разів — я це рахую, а не приховую.»

**4:10–4:40 — Автоматизація виконання і тести.** `python scripts/testnet_one_order.py --confirm` →
`client_order_id`, `venue_order_id`, `status=FILLED`; поруч екранограма testnet-інтерфейсу.
«Заміна одного об'єкта в композиції — і той самий код ставить реальний ордер на testnet. Mainnet
недосяжний за конструкцією.» Telegram-нотифікація на телефон. Термінал: `make test` → `92 passed`;
`make cov` → покриття по пакетах.

**4:40–5:00 — Відтворюваність.** `psql -c "select id, config_hash, git_sha, seed, equity_hash from
run order by started_at desc limit 3"`. Фінальна фраза: **«Будь-яке число у моєму звіті
відтворюється однією командою за run_id, бо разом із результатом збережено хеш конфігурації, SHA
коміту, seed і хеш кривої капіталу. Результат цієї роботи — не прибутковість, а метод, архітектура
і перевірюваний стенд.»**

---

## 16. Мапа на компетентності ОПП

| Компетентність / ПР | Чим підтверджується |
|---|---|
| ЗК1, ЗК17, ЗК18 | ієрархічна декомпозиція 729 → 45, дерево цілей, IDEF0/DFD, алгоритмічне структурування конвеєра |
| ЗК4 | звіт українською, `narrative_uk` у `/explain`, i18n uk |
| ЗК5, ФК21 | `README.en.md`, англомовні описи в OpenAPI, англомовний abstract |
| ЗК10 | PSR/DSR і пряма відмова твердити прибутковість; підрозділ «обмеження застосовності»; знайдений контрприклад до власної тези |
| ЗК11, ЗК12 | обґрунтування 12 технологічних рішень; тріада `config_hash+git_sha+seed`; CI з порогом покриття |
| ЗК13 | підрозділ 2.13 (правові й етичні аспекти, ToS, відсутність інвестрекомендацій) |
| ФК1 | виведення `P_liq` з умови `Equity = MM`; формалізація нечіткого виведення; AHP як задача на власний вектор |
| ФК2, ФК17 | нечіткий вивід Мамдані з нуля; ентропійна міра узгодженості ансамблю; емпіричні розподіли |
| ФК3 | формальні мови (YAML-DSL → AST правил), скінченні автомати з тотальною таблицею переходів, блок-схеми ДСТУ ISO 5807 |
| ФК4 | дефазифікація як чисельне інтегрування: три схеми, похибка, порядок збіжності; Welford проти катастрофічного скорочення; Decimal-дисципліна |
| ФК5, ФК6 | мінімум трьох сайзерів як перетин допустимих множин керування; **Парето-фронт** замість argmax; AHP-зважування |
| ФК7 | обчислювальні експерименти: 3 моделі витрат, ablation, чутливість до 8 параметрів, walk-forward, замір Амдала |
| ФК8 | функціональне ядро (чисті reducer-и) + об'єктна периферія; Protocol-и, Strategy, Chain of Responsibility, Adapter |
| ФК9, ФК16 | клієнт-серверна багаторівнева модель; `ProcessPoolExecutor` із замірами `S(p)`; розгортання на хмарному free tier, CI/CD |
| ФК10 | **ТЗ за ГОСТ 19.201-78**, ТЕО, WBS, діаграма Ганта, сітьовий графік, SemVer + CHANGELOG |
| ФК11 | 3 екрани дашборду; `MembershipPlot` і `RuleTable` як інструменти верифікації моделі; 9 розрахункових рисунків |
| ФК12 | профілювання, аналіз GIL і event loop, обмеження ресурсів контейнера, замір CPU/RAM |
| ФК13 | власний WebSocket-клієнт з реконектом і heartbeat; token-bucket з вагами; аналіз якості мережі (гістограма RTT/джитера) |
| ФК14 | підрозділ 2.9: STRIDE, JWT + 4 ролі + матриця доступу, `audit_log` з before/after, керування секретами, `pip-audit`, allowlist хостів |
| ФК15 | **as-is / to-be BPMN**; IDEF0 A-0/A0; DFD; use-case; **реєстр ризиків ПРОЄКТУ** (правильне читання ФК15 — ризики проектування, а не ринковий VaR) |
| ФК18 | автокодувальник `MLPRegressor(8-3-8)` як нейромережевий детектор аномалій котирувань; ROC-AUC |
| ФК19, ФК20 | смарт-система як єдиний контур: якість даних → нечітке рішення → детермінований ризик → виконання → зворотний зв'язок по капіталу |
| ФК22 | підрозділ 1.5 (ДСанПіН 3.3.2.007-98, ергономіка ВДТ, режим праці, електро- і пожежна безпека) |
| ПР10 | **три рівні моделі БД** (концептуальна ER Чена → логічна 3НФ → фізична DDL), індекси, backup/restore runbook |
| ПР12 | `KMeans(k=3)` кластеризація режимів ринку з силуетним коефіцієнтом → калібрування МФ з даних |

---

## 17. Фінальний чек-лист готовності

- [ ] Репозиторій **не** в iCloud; `docker compose up` піднімає 4 сервіси з нуля однією командою
- [ ] `make test` → 92 passed, < 25 с, без мережі
- [ ] `make cov` → загалом ≥80%, `fuzzy`/`risk`/`decision`/`sizing` ≥90%
- [ ] Демо повністю працює **з вимкненим Wi-Fi** (офлайн-фікстура)
- [ ] `ExplainView` показує спрацьовані правила з `α` і україномовне речення
- [ ] Сценарій `flash_crash` доводить систему до `HALTED` без участі людини
- [ ] `test_risk_chain_never_increases_exposure` і `test_equity_accounting_identity` зелені
- [ ] У БД є прогін із повним паспортом (`config_hash`, `git_sha`, `seed`, `equity_hash`)
- [ ] Жодного `<<TBD>>` ні в `config/`, ні в `docs/`
- [ ] Жодного секрету в git-історії; `.env` у `.gitignore`
- [ ] `docs/journal.md` заповнений по днях; `docs/deviations.md` містить усі розходження
- [ ] Записаний скрінкаст демо лежить окремо як план Б
- [ ] Таблиця трасування з розділу 2 заповнена по всіх 11 рядках
