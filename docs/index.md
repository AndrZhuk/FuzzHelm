# Карта документації FuzzHelm

Автор: Андрій Жук, 2026. Стан: HEAD `b933802` + документація, додана 2026-09-19 (ще не закомічена). Один рядок на файл або групу
однотипних файлів.

## Вимоги, план, трасування

| Файл | Зміст |
|---|---|
| [`BRIEF.md`](BRIEF.md) | нормативна виконавча специфікація (§0–§17): місія, архітектура, формули, модель даних, тести, фази, план звіту |
| [`contracts.md`](contracts.md) | публічні інтерфейси між модулями хвилі 0: DTO, порти, правила меж детермінізму й типів |
| [`tz/technical_specification.md`](tz/technical_specification.md) | технічне завдання за ГОСТ 19.201-78: FR-01…FR-24, NFR-01…NFR-09, ролі, стадії, таблиця приймальних випробувань |
| [`risk_register.md`](risk_register.md) | реєстр ризиків **проєкту** (ФК15): 20 ризиків з імовірністю, впливом, мітигацією і фактичним статусом |
| [`deviations.md`](deviations.md) | зведені розходження «спека ↔ реальність»: таблиця всіх ідентифікаторів, наукові результати-розходження, пункти за модулями |
| [`deviations.d/`](deviations.d/) | первинні записи розходжень по модулях (повні числа і команди): `ingest_rest`, `ingest_ws`, `data`, `storage`, `features`, `decision`, `fuzzy`, `risk`, `riskfix`, `execution`, `engine`, `api`, `workers`, `platform`, `exp_search`, `exp_analysis` |
| [`journal.md`](journal.md) | щоденний журнал робіт (чернетка звіту): хронологія комітів, 2026-09-18 і 2026-09-19 за модулями |
| [`journal.d/`](journal.d/) | первинні журнали модулів (ті самі 16 компонентів, що й у `deviations.d/`), з командами й замірами |

## Архітектура і модулі

| Файл | Зміст |
|---|---|
| [`api/api.md`](api/api.md) | FastAPI: ендпоінти й ролі, JWT, сервісний шар, стратегії, бектести, `/explain`, ризик, SSE, Telegram, планувальник |
| [`api/backtest.md`](api/backtest.md) | аналітика бектесту: 17 метрик, PSR/DSR, walk-forward, Парето, паспорт прогону, паралельний запуск, сітка |
| [`api/cli.md`](api/cli.md) | CLI `fuzzhelm`: підкоманди, коди виходу, `--dry-run`, керування користувачами |
| [`api/data.md`](api/data.md) | артефакти даних (`data/*`), нові REST-ендпоінти, фандинг, розширення калібрування |
| [`api/decision.md`](api/decision.md) | шар рішень: консенсус, узгодженість κ, `DecisionTrace`, `DecisionCore`, україномовне трасування |
| [`api/engine.md`](api/engine.md) | `Dataset`, `BacktestConfig`, `TradingLoop.step()`, `run_backtest`, воркери сітки, заміри швидкодії |
| [`api/execution.md`](api/execution.md) | `CostModel`, `PaperBroker`, `Portfolio` і тотожність обліку, `BinanceTestnetVenue`, `OrderRouter` |
| [`api/exp_analysis.md`](api/exp_analysis.md) | інструменти аналітичних експериментів фази 7: моделі витрат, ablation, VaR/Купець, гістерезис, таблиці звіту |
| [`api/exp_search.md`](api/exp_search.md) | інструменти пошукових експериментів фази 7: walk-forward, сітка, правило вибору з фронту, DSR, Амдал, чутливість |
| [`api/features.md`](api/features.md) | індикатори, `FeaturePipeline`, `BarWindow`/`LookaheadGuard`, 6 детекторів, KMeans-калібрування |
| [`api/fuzzy.md`](api/fuzzy.md) | функції належності, база правил, `MamdaniEngine`, дефазифікація, лінійна базова лінія, аудит монотонності |
| [`api/ingest_rest.md`](api/ingest_rest.md) | REST-клієнти, нормалізація, квантування, дедуплікація, token bucket, повтори, добір, крос-звірка |
| [`api/ingest_ws.md`](api/ingest_ws.md) | запис/реплей сесій, WS-клієнт, backoff і сторож тиші, детектор прогалин, агрегатор свічок, `IngestPipeline` |
| [`api/quality.md`](api/quality.md) | інваріанти, AHP, скор Q, MLP-автокодувальник і його оцінювання, здоров'я конвеєра |
| [`api/risk.md`](api/risk.md) | сайзинг і ризик: конфігурація, алгебра вердиктів, 6 лімітів, `RiskGuard`, автомат станів, маржа, VaR, Купець |
| [`api/storage.md`](api/storage.md) | сховище: конвенції типів, сесії, моделі, Alembic, репозиторії, права ролі застосунку |
| [`api/workers.md`](api/workers.md) | `workers.persist`, торговий і ingest-воркери, скрипти прогону й демо |
| [`db_schema.md`](db_schema.md) | модель даних у трьох рівнях (ER Чена, 3НФ, фізичний DDL), індекси з замірами, права |
| [`field_mapping.md`](field_mapping.md) | таблиця мапінгу полів Binance/Kraken на канонічні DTO |
| [`security.md`](security.md) | межі довіри, STRIDE, матриця доступу, sequence автентифікації, аудит, секрети, `pip-audit`, залишкові ризики |

## Діаграми (`diagrams/`, PlantUML; поруч — відрендерені `.svg` і `.png`)

| Файл | Зміст |
|---|---|
| [`diagrams/component.puml`](diagrams/component.puml) | компоненти: пакети, порти `core/ports.py`, межа детермінізму і межа типів |
| [`diagrams/deployment.puml`](diagrams/deployment.puml) | розгортання: compose-сервіси db/api/worker/ui, порти 5442/8000/5443, volume, теки, зовнішні хости |
| [`diagrams/risk_fsm.puml`](diagrams/risk_fsm.puml) | statechart автомата ризик-станів, **згенерований** з `risk/state.py::TRANSITIONS` і `config/risk_limits.yaml` |
| [`diagrams/gen_risk_fsm.py`](diagrams/gen_risk_fsm.py) | генератор `risk_fsm.puml` (лише читає код і конфігурацію) |
| [`diagrams/seq_ws_gap_backfill.puml`](diagrams/seq_ws_gap_backfill.puml) | sequence «розрив WS → прогалина → REST-добір» за `ingest/pipeline.py` |
| [`diagrams/seq_auth_jwt.puml`](diagrams/seq_auth_jwt.puml) | sequence автентифікації (JWT HS256, PyJWT) і авторизації за `ACCESS_MATRIX` |
| [`diagrams/er_conceptual_chen.puml`](diagrams/er_conceptual_chen.puml) | концептуальна ER-модель у нотації Чена (7 сутностей) |
| [`diagrams/er_physical.puml`](diagrams/er_physical.puml) | фізична схема: 15 таблиць, ключі, FK, індекси, append-only |
| [`diagrams/class_detectors_strategy.puml`](diagrams/class_detectors_strategy.puml) | класи детекторів і рушіїв виводу як стратегій у `DecisionCore` |
| [`diagrams/class_verdict_algebra.puml`](diagrams/class_verdict_algebra.puml) | алгебра вердиктів: `RiskRule`, `RuleVerdict`, `Verdict`, `compose`, `RiskGuard` |
| [`diagrams/activity_trading_loop.puml`](diagrams/activity_trading_loop.puml) | діяльність одного бару `TradingLoop`: виконання → облік → рішення → сайзинг → ризик-ланцюг → заявки |

## Посібники (`manuals/`)

| Файл | Зміст |
|---|---|
| [`manuals/user_guide.md`](manuals/user_guide.md) | керівництво користувача: реплей, демо flash_crash, бектест, вхід в API і `/explain`, живий інжест |
| [`manuals/deployment.md`](manuals/deployment.md) | інструкція з розгортання: сервіси, змінні оточення, перший запуск, збирання образів, тести |
| [`manuals/backup_runbook.md`](manuals/backup_runbook.md) | резервне копіювання і відновлення PostgreSQL з протоколом перевірки |

## Рисунки й звіти вимірів (`figures/`)

| Файл | Зміст |
|---|---|
| [`figures/backfill_report.md`](figures/backfill_report.md) | 45-денний REST-добір: вікно, хеші, ваги, стики сторінок |
| [`figures/backfill_gap_replay.md`](figures/backfill_gap_replay.md) | реальні рядки `ingest_gap` FILLED з реплею `gap.jsonl.gz` і справжнього REST |
| [`figures/ingest_pathological.md`](figures/ingest_pathological.md) | шість патологічних WS-сесій: втрати, дублікати, час відновлення |
| [`figures/crosscheck_report.md`](figures/crosscheck_report.md), `crosscheck_divergence.png` | крос-звірка Binance ↔ Kraken і розклад розбіжності |
| [`figures/calibration_report.md`](figures/calibration_report.md), `regimes_clusters.png` | калібрування МФ: KMeans, силует, правило σ `cover`, T-перцентилі |
| [`figures/fuzzy_rules_table.md`](figures/fuzzy_rules_table.md) | таблиця 45 правил (Додаток А) |
| [`figures/fuzzy_monotonicity.md`](figures/fuzzy_monotonicity.md) | аудит монотонності u(T) на робочій конфігурації |
| [`figures/fuzzy_defuzz_convergence.md`](figures/fuzzy_defuzz_convergence.md), `.png` | збіжність трьох схем дефазифікації |
| `figures/fuzzy_membership.png`, `figures/fuzzy_control_surface.png` | функції належності (4 панелі) і поверхня керування |
| [`figures/quality_mlp_rocauc.md`](figures/quality_mlp_rocauc.md) | ROC-AUC MLP-автокодувальника на справжньому IS-вікні (8-3-8 проти 5-3-5) |
| [`figures/riskfix_cooldown_policy.md`](figures/riskfix_cooldown_policy.md) | політики COOLDOWN на 45 днях: `scaled_entries` проти `reduce_only` |
| [`figures/first_run_metrics.md`](figures/first_run_metrics.md), `first_run_equity.png` | прогін фази 6 з рядків БД; переписується кроком `run_backtest_btc` оркестратора фази 7 (див. `risk_register.md` RR-19) |
| `figures/plot_*`, `figures/equity_*`, `figures/var_hist_*` | виводи фази 7 (VaR, криві капіталу), які пише оркестратор; зміст — `<<TBD:phase7_results>>` |
| `report_tables/raw/*` | сирі виводи аналітичних експериментів фази 7 (моделі витрат, ablation, гістерезис, VaR); зведені таблиці — `<<TBD:report_tables>>` |

## Чернетки кореневих файлів (`_pending_root/`, переносить провідний розробник)

| Файл | Зміст |
|---|---|
| [`_pending_root/README.md`](_pending_root/README.md) | README українською: безпека, архітектура, швидкий старт, CLI, make, дані, відтворення за `run_id`, тести |
| [`_pending_root/README.en.md`](_pending_root/README.en.md) | англомовна версія README (ЗК5) |
| [`_pending_root/CHANGELOG.md`](_pending_root/CHANGELOG.md) | журнал змін за SemVer: 0.1.0 (Додано / Змінено / Безпека) за хвилями |

## Чого ще немає (план фаз 9–11)

`teo/cost_estimate.md` (ТЕО) — `<<TBD:teo_cost_estimate>>`; WBS, діаграма Ганта, сітьовий графік — `<<TBD:gantt>>`; IDEF0, BPMN,
блок-схеми ДСТУ ISO 5807 — `<<TBD:idef0_bpmn_flowcharts>>`; англомовний abstract — `<<TBD:abstract_en>>`; звіт (фаза 11) — за
окремою командою.
