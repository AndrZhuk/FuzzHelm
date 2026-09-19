# Перелік розходжень «спека ↔ реальність»

Згенеровано `scripts/export_report_tables.py`. Автор: Андрій Жук, 2026.

Усього розходжень з ідентифікатором: **227**.

| ID | назва | файл |
|---|---|---|
| API-01 | Матриця доступу — явна таблиця, роль auditor, додаткові маршрути | `docs/deviations.d/api.md` |
| API-02 | Четверта ревізія Alembic `0004_decision_trace_extras` | `docs/deviations.d/api.md` |
| API-03 | Точний множник `risk_event.factor` у `payload.factor_exact` | `docs/deviations.d/api.md` |
| API-04 | `PUT /risk/limits` переписує `config/risk_limits.yaml` (файл — джерело правди) | `docs/deviations.d/api.md` |
| API-05 | Цільові правки storage заради API і планувальника | `docs/deviations.d/api.md` |
| API-06 | Зняття kill-switch через API — команда воркеру (202), а не синхронне зняття | `docs/deviations.d/api.md` |
| API-07 | Джерело SSE — PostgreSQL LISTEN/NOTIFY; авторизація лише заголовком | `docs/deviations.d/api.md` |
| API-08 | `POST /backtests`: 202, черга в процесі, запис результатів робить API | `docs/deviations.d/api.md` |
| API-09 | `/explain` перераховує μ_agg і κ з конфігурації прогону й показує узгодженість | `docs/deviations.d/api.md` |
| API-10 | Деталі JWT | `docs/deviations.d/api.md` |
| API-11 | Вхід: bcrypt у потоці, обмежувач спроб у пам'яті процесу | `docs/deviations.d/api.md` |
| API-12 | Планувальник — окремий процес; погодинний Q не перезаписує живий | `docs/deviations.d/api.md` |
| API-13 | `pip-audit`: `ecdsa` 0.19.2 (PYSEC-2026-1325) через `python-jose` — запит, а не зміна | `docs/deviations.d/api.md` |
| API-14 | Telegram: API надсилає лише запит на зняття kill-switch | `docs/deviations.d/api.md` |
| API-15 | Набір даних прогону через API будується канонічним шляхом рушія (виправлено на рецензії) | `docs/deviations.d/api.md` |
| API-16 | Межі вводу на HTTP-межі: цілі за типами колонок, обсяг обчислень стратегії, текст помилок (рецензія) | `docs/deviations.d/api.md` |
| DATA-01 | «45 днів свічок» — межі вікна не визначені; прогалин добір не знайшов | `docs/deviations.d/data.md` |
| DATA-02 | Ваги REST-ендпоінтів хвилі 2 — виміряно, а не з документації | `docs/deviations.d/data.md` |
| DATA-03 | WS-04: REST-добір угод перенесено в модулі REST | `docs/deviations.d/data.md` |
| DATA-04 | Крос-звірка: перпетуал у USDT ↔ спот у USD; лише 720 хвилин | `docs/deviations.d/data.md` |
| DATA-05 | σ гаусіан V: буквальне «0.5·d до сусіда» лишає мертву зону — правило `cover` | `docs/deviations.d/data.md` |
| DATA-06 | T-точки — перцентилі симетризованої вибірки T ∪ −T | `docs/deviations.d/data.md` |
| DATA-07 | Монотонність на робочій (каліброваній) конфігурації: «груба» з кроком 1.0 не виконується | `docs/deviations.d/data.md` |
| DATA-08 | KMeans відтворюється до ~1e−15, а не побітово | `docs/deviations.d/data.md` |
| DATA-09 | Кластер MID виділено сплесками обсягу, а не «середньою волатильністю» | `docs/deviations.d/data.md` |
| DATA-10 | Калібрування на днях 1–15 разом із зоною embargo фолду 0; лише BTCUSDT | `docs/deviations.d/data.md` |
| DATA-11 | `--dry-run` CLI не торкається мережі й БД; мітка часу у звіті добору | `docs/deviations.d/data.md` |
| DATA-12 | Міграція основної БД (тепер 0004); MLP за §5.17 навчено не на IS-вікні | `docs/deviations.d/data.md` |
| DEC-01 | «Узгодженість = 1, якщо всі детектори одного знаку» — неточне твердження | `docs/deviations.d/decision.md` |
| DEC-02 | Узгодженість при нульовій / мізерній масі свідчень (Z < ε) | `docs/deviations.d/decision.md` |
| DEC-03 | V до прогріву VolRegime | `docs/deviations.d/decision.md` |
| DEC-04 | Запит до фундаменту (необов'язковий) | `docs/deviations.d/decision.md` |
| ENG-01 | Правило тейк-профіту | `docs/deviations.d/engine.md` |
| ENG-02 | σ_base для тригера `σ_ann/σ_base > 1.6` | `docs/deviations.d/engine.md` |
| ENG-03 | Стоп і TP прив'язані до ціни рішення `c_t`, а не до фактичної ціни входу | `docs/deviations.d/engine.md` |
| ENG-04 | Розмір фіксується на вході; ризик-ланцюг — лише на намір заявки | `docs/deviations.d/engine.md` |
| ENG-05 | Стан тригера Шмітта синхронізується з фактичною позицією | `docs/deviations.d/engine.md` |
| ENG-06 | Семантика `exit_reason` | `docs/deviations.d/engine.md` |
| ENG-07 | Фандинг у бектесті — історичний ряд ставок | `docs/deviations.d/engine.md` |
| ENG-08 | Прогрів, вікно оцінки, embargo | `docs/deviations.d/engine.md` |
| ENG-09 | Якість даних у бектесті: Q = 1, лаг = 0 | `docs/deviations.d/engine.md` |
| ENG-10 | Обсяг трасування `record_traces` | `docs/deviations.d/engine.md` |
| ENG-11 | Ціна ліквідації | `docs/deviations.d/engine.md` |
| ENG-12 | Відмова сайзера не йде в ризик-ланцюг (знайдено під час аудиту WIP-коду) | `docs/deviations.d/engine.md` |
| ENG-13 | Специфікація інструмента не входить ні в `config_hash`, ні в `dataset_hash` | `docs/deviations.d/engine.md` |
| ENG-14 | ЗНАХІДКА: стан COOLDOWN поглинаючий для пласкої книги (виміряно на реальних даних) | `docs/deviations.d/engine.md` |
| ENG-15 | Кеш Decimal-барів ключувався лише хешем свічок (знайдено під час аудиту WIP-коду) | `docs/deviations.d/engine.md` |
| ENG-16 | Правки в чужих пакетах (цільові, для інтеграції) | `docs/deviations.d/engine.md` |
| ENG-17 | Інтеграція з API: рушій чистий, запис у БД — у воркері/API | `docs/deviations.d/engine.md` |
| ENG-18 | Seed у сітці | `docs/deviations.d/engine.md` |
| ENG-19 | Виконавець циклу — `PaperBroker` | `docs/deviations.d/engine.md` |
| ENG-20 | Kill-switch, спрацьований між барами, не скасовував вхід у черзі (знайдено рецензією) | `docs/deviations.d/engine.md` |
| ENG-21 | Вибір клітинки на IS у walk-forward — argmax Шарпа, а не «фронт замість argmax» | `docs/deviations.d/engine.md` |
| EXE-01 | Тотожність обліку капіталу для безстрокового ф'ючерса | `docs/deviations.d/execution.md` |
| EXE-02 | Seeded-шум ковзання (на нього посилається `config/cost_model.yaml` як на «D-07») | `docs/deviations.d/execution.md` |
| EXE-03 | Тейк-профіт без окремого типу заявки | `docs/deviations.d/execution.md` |
| EXE-04 | Оцінки σ_t і V_bar у формулі імпакту | `docs/deviations.d/execution.md` |
| EXE-05 | Ставка фандингу в бектесті | `docs/deviations.d/execution.md` |
| EXE-06 | Ліквідація в симуляції | `docs/deviations.d/execution.md` |
| EXE-07 | Testnet: лише MARKET, живий прогін не виконано | `docs/deviations.d/execution.md` |
| EXE-08 | Embargo в walk-forward вирізається з кінця IS | `docs/deviations.d/execution.md` |
| EXE-09 | Перелік 17 метрик і умовності обчислення | `docs/deviations.d/execution.md` |
| EXE-10 | PSR/DSR: одиниці і малі N | `docs/deviations.d/execution.md` |
| EXE-11 | Сітка: тлумачення `n_ATR` і значення осей | `docs/deviations.d/execution.md` |
| EXE-12 | Ідемпотентність маршрутизатора vs брокера | `docs/deviations.d/execution.md` |
| EXE-13 | Дрібниці виконання, що впливають на числа | `docs/deviations.d/execution.md` |
| XA-01 | VaR на вироджених доходностях: «усі бари» і «бари в позиції» — обидва | `docs/deviations.d/exp_analysis.md` |
| XA-02 | Параметричний VaR як прогноз: σ̂ того самого вікна W, середнє 0 | `docs/deviations.d/exp_analysis.md` |
| XA-03 | Зведення OOS: зчеплена крива з конкатенованих дохідностей фолдів | `docs/deviations.d/exp_analysis.md` |
| XA-04 | «Наївна модель завищує Sharpe у кілька разів» — лише як вимір, і відношення лише для додатних Шарпів | `docs/deviations.d/exp_analysis.md` |
| XA-05 | Ablation без VolRegime: V ≡ 0.5 | `docs/deviations.d/exp_analysis.md` |
| XA-06 | κ ≡ 1 через конфігурацію (κ_min = 1), а не обхід коду | `docs/deviations.d/exp_analysis.md` |
| XA-07 | Ціна гістерезису: компаратор u_exit = u_enter; виміряне число замість «−1.15 %/добу» | `docs/deviations.d/exp_analysis.md` |
| XA-08 | `--risk-loop relaxed` — ізоляція ефекту від засувки ризику | `docs/deviations.d/exp_analysis.md` |
| XA-09 | Розбиття `--smoke` | `docs/deviations.d/exp_analysis.md` |
| XA-10 | VaR збереженого прогону (`--run-id`): прогрів відкидається за типовою конфігурацією | `docs/deviations.d/exp_analysis.md` |
| XA-11 | «Ковзання ≈» — відносно P_ref із `Fill.slippage_bps`, з шумом | `docs/deviations.d/exp_analysis.md` |
| XA-12 | DSR у зведенні — лише проти прогонів сітки на тому самому наборі | `docs/deviations.d/exp_analysis.md` |
| XA-13 | Групи тестів A–N: дослівні назви + евристика файлів | `docs/deviations.d/exp_analysis.md` |
| XA-14 | Таблиця трасування: «підтвердження» — лише наявне | `docs/deviations.d/exp_analysis.md` |
| XA-15 | Запис обраної конфігурації в БД — через `DbBacktestRunner`; у цій хвилі не виконано | `docs/deviations.d/exp_analysis.md` |
| XA-16 | Бари в позиції можуть не вмістити W = 500 | `docs/deviations.d/exp_analysis.md` |
| XA-17 | Робоча точка сітки exp_search обрана за OOS — застереження в кожному виводі зі `--selection fixed` | `docs/deviations.d/exp_analysis.md` |
| XA-18 | `--selection is_grid`: вибір на IS за правилом фронту Парето, а не argmax SR | `docs/deviations.d/exp_analysis.md` |
| XA-19 | `git_dirty` паспортів — зміни коду, а не виводи; commit у БД лише із закоміченого коду | `docs/deviations.d/exp_analysis.md` |
| XA-20 | Оборот зведення OOS — формула `compute_metrics` на зчепленій кривій | `docs/deviations.d/exp_analysis.md` |
| XA-21 | DSR: N — усі прогони ОДНІЄЇ сітки на тому самому наборі | `docs/deviations.d/exp_analysis.md` |
| XA-22 | Повне вікно BTCUSDT містить вікно калібрування МФ | `docs/deviations.d/exp_analysis.md` |
| XS-01 | Вибір робочої точки на IS кожного фолду — з фронту Парето правилом ε-обмеження, а не argmax SR | `docs/deviations.d/exp_search.md` |
| XS-02 | Конкатенований OOS — ланцюг доходностей свіжих фолдових прогонів | `docs/deviations.d/exp_search.md` |
| XS-03 | DSR: N = число клітинок, оцінених у ЦЬОМУ прогоні; простір — конкатенований OOS | `docs/deviations.d/exp_search.md` |
| XS-04 | `grid_cell` без покрокової кривої; OOS-метрики — у `run_metric` того самого рядка; колізія ідентичності | `docs/deviations.d/exp_search.md` |
| XS-05 | T(1) — пул з одним процесом; T(p) включає старт пулу; гетерогенні ядра | `docs/deviations.d/exp_search.md` |
| XS-06 | Чутливість: λ через пам'ять EWMA, n_ATR цілим, ціль — ланцюг OOS з фіксованими параметрами | `docs/deviations.d/exp_search.md` |
| XS-07 | Запис фолдів walk-forward: IS і OOS обраної клітинки як kind=backtest з тегами в `run_metric` | `docs/deviations.d/exp_search.md` |
| XS-08 | Прогони для запису повторюються в головному процесі | `docs/deviations.d/exp_search.md` |
| XS-09 | Мамдані vs лінійне на тих самих фолдах — кожне ядро обирає свою клітинку | `docs/deviations.d/exp_search.md` |
| XS-10 | Перевірка запису на головній БД — транзакцією з ROLLBACK | `docs/deviations.d/exp_search.md` |
| XS-11 | `git_dirty` у паспортах експерименту не рахує `artifacts/` і `docs/`; commit у БД — лише із закоміченого коду | `docs/deviations.d/exp_search.md` |
| XS-12 | Калібрування МФ і фолди walk-forward: OOS жодного фолду не використано | `docs/deviations.d/exp_search.md` |
| XS-13 | Робоча точка `run_grid.py` обирається ЗА метриками конкатенованого OOS | `docs/deviations.d/exp_search.md` |
| F-01 | Пін-бар «нуль при симетричних тінях» | `docs/deviations.d/features.md` |
| F-02 | Donchian: означення U_t і «пробою» | `docs/deviations.d/features.md` |
| F-03 | RsiExhaustion: `div_score` не визначено | `docs/deviations.d/features.md` |
| F-04 | CandleGeometry: набір патернів j, голоси v_j, dist_to_level | `docs/deviations.d/features.md` |
| F-05 | Ініціалізація Уайлдера/EMA і пласкі ряди | `docs/deviations.d/features.md` |
| F-06 | Golden-дані: незалежна реалізація + один зовнішній опублікований приклад | `docs/deviations.d/features.md` |
| F-07 | Велфорд на ряді зі зсувом 1e9: точність ~1e−6, не машинна | `docs/deviations.d/features.md` |
| F-08 | «O(1) на бар» для перцентильного рангу | `docs/deviations.d/features.md` |
| F-09 | Bandwidth = 2σ/μ | `docs/deviations.d/features.md` |
| F-10 | T-точки: 15/50/85 (§3) vs {8,25,50,75,92} (§5.5) | `docs/deviations.d/features.md` |
| F-11 | Сигнатура `calibrate` | `docs/deviations.d/features.md` |
| F-12 | KMeans: стандартизація і силует на підвибірці | `docs/deviations.d/features.md` |
| F-13 | VolRegime до прогріву | `docs/deviations.d/features.md` |
| F-14 | Запити до конфігурації (файл не належить цьому модулю) | `docs/deviations.d/features.md` |
| F-15 | Перцентильний ранг із нічиїми: повністю пласке вікно дає V = 1 | `docs/deviations.d/features.md` |
| FIN-01 | Чистий перезапуск прогону фази 6 (закриває RR-19, W-11, «Відкрите» W-10) | `docs/deviations.d/final_docs.md` |
| FIN-02 | `export_report_tables.py` з вкладеними `--results-dir` дублює таблиці exp_search | `docs/deviations.d/final_docs.md` |
| FIN-03 | `docs/report_tables/test_groups.md`: два генератори, одне ім'я (закриває «Відкрите» QP-04) | `docs/deviations.d/final_docs.md` |
| FIN-04 | Три маркери TBD у згенерованій таблиці трасування закодовано жорстко | `docs/deviations.d/final_docs.md` |
| FIN-05 | Провенанс MLP-моделі ETHUSDT і шаблонний текст її звіту | `docs/deviations.d/final_docs.md` |
| FIN-06 | Позитивна валова перевага без витрат — лише PSR, без DSR | `docs/deviations.d/final_docs.md` |
| FIN-07 | Маркери TBD: що зроблено з кожним | `docs/deviations.d/final_docs.md` |
| FIN-08 | Час набору тестів і RAM (закриває NFR-08, `ram_profile`) | `docs/deviations.d/final_docs.md` |
| FIN-09 | Перевірка команд README: `make backup` без каталогу і дампи поза `.gitignore` | `docs/deviations.d/final_docs.md` |
| FZ-01 | Монотонність u за T при w ≡ 1 не виконується (Мамдані max-min + центроїд) | `docs/deviations.d/fuzzy.md` |
| FZ-02 | Умова Руспіні для V (гаусіани) і U | `docs/deviations.d/fuzzy.md` |
| FZ-03 | Приклад `R07` у §7 не узгоджується з жодною нумерацією | `docs/deviations.d/fuzzy.md` |
| FZ-04 | Порядок збіжності схем дефазифікації: Сімпсон ≈ 2, а не 4; оцінка p з одного відношення шумна | `docs/deviations.d/fuzzy.md` |
| FZ-05 | Рішення реалізації, яких у спеці немає | `docs/deviations.d/fuzzy.md` |
| ING-01 | Межі таблиці ваг `/fapi/v1/klines` | `docs/deviations.d/ingest_rest.md` |
| ING-02 | Token bucket «2400/хв» не гарантує ліміту 2400 за хвилину | `docs/deviations.d/ingest_rest.md` |
| ING-03 | Строгість схеми: ринкові payload-и — строго, довідники — за обов'язковим підмножиною | `docs/deviations.d/ingest_rest.md` |
| ING-04 | У WS-кадрах 2026 року є поля, яких немає в описі потоків | `docs/deviations.d/ingest_rest.md` |
| ING-05 | Kraken OHLC не має часу закриття й обсягу в котирувальній валюті | `docs/deviations.d/ingest_rest.md` |
| ING-06 | Спостереження щодо core (не баг, запит до власника core) | `docs/deviations.d/ingest_rest.md` |
| WS-01 | Очікувані AHP-ваги ≈ (0.35, 0.27, 0.20, 0.18) з матрицею брифінгу недосяжні | `docs/deviations.d/ingest_ws.md` |
| WS-02 | MLP-автокодувальник: вектор з 5 ознак у §5.17 проти «8-3-8» у §3 | `docs/deviations.d/ingest_ws.md` |
| WS-03 | Порядок видачі ReplayFeed: «за ts_ingest_ns» проти «за надходженням» | `docs/deviations.d/ingest_ws.md` |
| WS-04 | REST-добір угод: у `rest_client` немає `/fapi/v1/aggTrades` | `docs/deviations.d/ingest_ws.md` |
| WS-05 | Детектор `bucket_count` не реалізовано | `docs/deviations.d/ingest_ws.md` |
| WS-06 | Джерела REST-відповідей у патологічних фікстурах | `docs/deviations.d/ingest_ws.md` |
| WS-07 | Хибне спрацювання сторожа тиші при стрибку годинника | `docs/deviations.d/ingest_ws.md` |
| WS-08 | Час відновлення — у відтвореному часі, без RTT REST | `docs/deviations.d/ingest_ws.md` |
| WS-09 | Параметри, яких брифінг не задає (обрані й обґрунтовані вимірами) | `docs/deviations.d/ingest_ws.md` |
| WS-10 | Примітка для інтеграції: запас годинника в `backfill_klines` | `docs/deviations.d/ingest_ws.md` |
| PLAT-01 | Керування користувачами: `fuzzhelm user add \| list \| set-role` | `docs/deviations.d/platform.md` |
| PLAT-01a | Незалежне рев'ю PLAT-01: відлуння пароля через `--password-stdin` і гонка «останнього admin» | `docs/deviations.d/platform.md` |
| PLAT-02 | docker-compose: спільний `./config` для api і worker; збирання й запуск у контейнерах | `docs/deviations.d/platform.md` |
| PLAT-03 | `GET /risk/events`: `until_ns` і keyset-курсор (ts, id) замість «найновіші ≤ 2 000» | `docs/deviations.d/platform.md` |
| PLAT-04 | JWT: python-jose → PyJWT | `docs/deviations.d/platform.md` |
| PLAT-05 | MLP-автокодувальник (§5.17): навчання на справжньому IS-вікні замість 3 000-барної фікстури | `docs/deviations.d/platform.md` |
| QP-01 | Покриття: що рахується і де гейт | `docs/deviations.d/quality_pass.md` |
| QP-02 | mypy: 13 → 0 помилок на всьому `src/fuzzhelm` без зміни поведінки | `docs/deviations.d/quality_pass.md` |
| QP-03 | Швидкість: < 25 с для unit+property досягнуто; для всього `make test` — ні, і без послаблення не буде | `docs/deviations.d/quality_pass.md` |
| QP-04 | Інвентар тестів брифінгу: генератор і захист від перейменування | `docs/deviations.d/quality_pass.md` |
| QP-05 | Налаштування hypothesis: єдине правило | `docs/deviations.d/quality_pass.md` |
| QR-01 | Шаблон виключення покриття `"\.\.\."` ховав справжній код | `docs/deviations.d/quality_pass.md` |
| QR-02 | `pragma: no cover` на досяжній гілці ключового пакета fuzzy | `docs/deviations.d/quality_pass.md` |
| QR-03 | «До 20 вердиктів» (§10 J) не було захищено від тихого зменшення | `docs/deviations.d/quality_pass.md` |
| QR-04 | Швидкість — переміряно на дереві з тестами wiring | `docs/deviations.d/quality_pass.md` |
| QR-05 | Проводка wiring — перевірено наскрізно (зона wiring, правок не вносив) | `docs/deviations.d/quality_pass.md` |
| R-01 | Стала часу фільтра 1-го порядку: «γ = 0.2 ⇒ T = 4Δt» | `docs/deviations.d/risk.md` |
| R-02 | Тест Купця: «відкидає при 20 пробоях із 500» | `docs/deviations.d/risk.md` |
| R-03 | Ціна відсутності гістерезису: «−1.15% капіталу на добу» | `docs/deviations.d/risk.md` |
| R-04 | «Інваріант: CVaR ≥ VaR ≥ 0» і оцінювач квантиля | `docs/deviations.d/risk.md` |
| R-05 | Інтерпретація `max_position_notional: {value: 0.30, unit: equity_fraction}` | `docs/deviations.d/risk.md` |
| R-06 | LiquidationBufferGuard: `L'_max` сам по собі не відновлює DTL ≥ 6 | `docs/deviations.d/risk.md` |
| R-07 | Сцена демо 2:30–3:20: «4% → WARNING, 8% → COOLDOWN, 12% → HALTED» при flash_crash | `docs/deviations.d/risk.md` |
| R-08 | Автомат станів: уточнення, яких немає в §5.12 | `docs/deviations.d/risk.md` |
| R-09 | Гістерезис: «вихід лише при \|u\| < 0.12» | `docs/deviations.d/risk.md` |
| R-10 | Відхилення сигнатур від `docs/contracts.md` §10–§11 | `docs/deviations.d/risk.md` |
| R-11 | `action: shrink` без місця для приросту | `docs/deviations.d/risk.md` |
| R-12 | Оцінка швидкості просадки | `docs/deviations.d/risk.md` |
| RF-01 | COOLDOWN: `scaled_entries` (типово, рішення автора) замість буквального reduce-only (закриває ENG-14) | `docs/deviations.d/riskfix.md` |
| RF-02 | Специфікація інструмента — у паспорті прогону через `dataset_hash` (закриває ENG-13) | `docs/deviations.d/riskfix.md` |
| RF-03 | VaR₉₅/CVaR₉₅ у `equity_point`: гроші, W = 500, лише повні вікна; рушій заповнює сам | `docs/deviations.d/riskfix.md` |
| RF-04 | Оцінка гіршого випадку: κ входу, а не поточного стану; відкат нереалізованого прибутку (ENG-04, R-12) | `docs/deviations.d/riskfix.md` |
| RF-05 | Рецензія riskfix (2026-09-19): що перевірено, що виправлено | `docs/deviations.d/riskfix.md` |
| ST-01 | `sim_order.decision_id` — NOT NULL | `docs/deviations.d/storage.md` |
| ST-02 | Append-only і для `audit_log`; роль застосунку NOLOGIN | `docs/deviations.d/storage.md` |
| ST-03 | `ux_run_identity` не ловить дублікати, коли `git_sha IS NULL` | `docs/deviations.d/storage.md` |
| ST-04 | CHECK-обмеження свічки не відкидають NaN | `docs/deviations.d/storage.md` |
| ST-05 | Частина посилань не має FK у нормативному DDL | `docs/deviations.d/storage.md` |
| ST-06 | «12 таблиць» ↔ 15 у DDL | `docs/deviations.d/storage.md` |
| ST-07 | Мікросекундна роздільність TIMESTAMPTZ | `docs/deviations.d/storage.md` |
| ST-08 | Свічка з БД ≠ байт-у-байт DTO з нормалізатора | `docs/deviations.d/storage.md` |
| ST-09 | (фундамент, не змінено) `sizing.convert.*` падає на numpy-2-скалярах | `docs/deviations.d/storage.md` |
| ST-10 | Спостереження щодо docker-compose (файли не мої, не змінював) | `docs/deviations.d/storage.md` |
| ST-11 | Розміщення тестів без БД і покриття | `docs/deviations.d/storage.md` |
| ST-12 | `ix_candle_lookup` дублює первинний ключ (замір) | `docs/deviations.d/storage.md` |
| ST-13 | Режим округлення до масштабу NUMERIC(38,18): PostgreSQL ≠ `core.money` | `docs/deviations.d/storage.md` |
| ST-14 | Аргумент брифінгу проти TimescaleDB не стосується `candle` | `docs/deviations.d/storage.md` |
| WIRE-01 | MLP-автокодувальник у робочому контурі; рішення про архітектуру (закриває «Стан на HEAD» PLAT-05) | `docs/deviations.d/wiring.md` |
| WIRE-02 | WS-07: сторож тиші живого клієнта на монотонному годиннику | `docs/deviations.d/wiring.md` |
| WIRE-03 | Одне визначення «брудного» коду для паспортів (XS-11, XA-19, W-11) | `docs/deviations.d/wiring.md` |
| WIRE-04 | CI (§9) | `docs/deviations.d/wiring.md` |
| WIRE-05 | `fly.toml` (free tier) — не задеплоєно | `docs/deviations.d/wiring.md` |
| WIRE-06 | Makefile | `docs/deviations.d/wiring.md` |
| WIRE-07 | Незалежна рецензія проводки: два дефекти скорера в торговому воркері; перезаміри | `docs/deviations.d/wiring.md` |
| W-01 | Торговий воркер споживає лише kline + markPrice | `docs/deviations.d/workers.md` |
| W-02 | `dataset_hash` live/replay-прогону — ідентичність СЕСІЇ, а не лише даних | `docs/deviations.d/workers.md` |
| W-03 | Профіль replay: `u_enter = 0.20` (клітинка сітки), а не 0.25 | `docs/deviations.d/workers.md` |
| W-04 | Сценарій flash_crash: дизайн і фактична послідовність станів (≠ «4 % → 8 % → 12 %» §15) | `docs/deviations.d/workers.md` |
| W-05 | Прогрів з історії, що передує потоку | `docs/deviations.d/workers.md` |
| W-06 | Лаг для StaleDataGuard у live | `docs/deviations.d/workers.md` |
| W-07 | VaR₉₅/CVaR₉₅ у `equity_point` | `docs/deviations.d/workers.md` |
| W-08 | Журнал подій прогону зберігається (бектест, API, воркер) | `docs/deviations.d/workers.md` |
| W-09 | Ingest-воркер: журнал без рядка `run`, Q лише завершених годин | `docs/deviations.d/workers.md` |
| W-10 | Колізія `client_order_id` між збереженими прогонами з тим самим seed (знайдено на робочій БД) | `docs/deviations.d/workers.md` |
| W-11 | Прапорець «брудного» дерева git | `docs/deviations.d/workers.md` |
| W-12 | Правки `backtest/engine.py` (цільові) | `docs/deviations.d/workers.md` |
| W-13 | Правки storage (адитивні) | `docs/deviations.d/workers.md` |
| W-14 | Зняття kill-switch: команда з API → воркер | `docs/deviations.d/workers.md` |
| W-15 | Синтетичні ціни не пишуться в `candle` | `docs/deviations.d/workers.md` |
| W-16 | POST /backtests → справжній рушій | `docs/deviations.d/workers.md` |
| W-17 | Шапка «MODE: PAPER · FEED: … · NO MAINNET KEYS · SEED · Q» | `docs/deviations.d/workers.md` |
| W-18 | Перечитування `config/risk_limits.yaml` посеред прогону | `docs/deviations.d/workers.md` |
| W-19 | Резервне копіювання: `docker compose exec` працює без `.env` | `docs/deviations.d/workers.md` |
| W-20 | Ingest-воркер: зупинка могла розірвати ланцюг журналу в БД | `docs/deviations.d/workers.md` |
| W-21 | Аудит воркера про зняття HALTED: час бару реплею і без автора | `docs/deviations.d/workers.md` |
| W-22 | `/market/health`: health торгового й ingest-воркера затирали один одного | `docs/deviations.d/workers.md` |
| W-23 | `run.ts_to` live/replay-прогону був NULL | `docs/deviations.d/workers.md` |
| W-24 | `scripts/run_backtest.py`: чесність повторного запуску | `docs/deviations.d/workers.md` |
| W-25 | `make_demo_scenario.py --check` перезаписував закомічений сценарій | `docs/deviations.d/workers.md` |
| W-26 | Інтеграційні тести воркерів без sleep | `docs/deviations.d/workers.md` |
| W-27 | Дрібні правки надійності | `docs/deviations.d/workers.md` |
