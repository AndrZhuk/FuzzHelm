# API компонента `api` (+ `notify`, `scheduler`) — фактичний, хвиля 2

Автор: Андрій Жук, 2026. Джерело вимог — `docs/BRIEF.md` §8.1, §8.2, §6, §7, §10 N, §12 фаза 8, §14 п. 2.9.
Безпека (STRIDE, матриця доступу, секрети, pip-audit) — `docs/security.md`. Розходження — `docs/deviations.d/api.md`
(API-01…API-16). Журнал — `docs/journal.d/api.md`.

Запуск: `uvicorn fuzzhelm.api.main:app` (docker-compose, сервіс `api`, порт 8000). OpenAPI 3.1 англійською —
`/docs` (Swagger UI), `/redoc`, `/openapi.json`. Під час імпорту і старту застосунок **не підключається до БД**:
engine створюється ліниво, LISTEN запускається з першим SSE-клієнтом або `/market/health`. Тому `/healthz` і `/docs`
працюють і без PostgreSQL (перевірено тестом `test_default_services_do_not_touch_db_until_first_request`).

---

## 1. Ендпоінти (§8.1) і ролі

Кожен маршрут має рівно одну залежність `require(Permission.X)`. Роль перевіряється за явною таблицею
`fuzzhelm.api.auth.ACCESS_MATRIX` (дозвіл → множина ролей). Для дозволів зі зміною стану (позначено `*`)
роль додатково звіряється з `app_user` у БД: пониження ролі діє одразу, а не через 8 год, коли спливе токен.
Відповіді: немає/невалідний/прострочений токен → **401** з `WWW-Authenticate: Bearer`, роль без дозволу → **403**.

| Метод | Шлях | Дозвіл | op | an | au | ad | Відповідь |
|---|---|---|:-:|:-:|:-:|:-:|---|
| POST | `/auth/login` | — | ✓ | ✓ | ✓ | ✓ | `TokenResponse` (JWT HS256, 8 год) |
| GET | `/auth/me` | `self:read` | ✓ | ✓ | ✓ | ✓ | роль і перелік дозволів |
| GET | `/market/candles` | `market:read` | ✓ | ✓ | ✓ | ✓ | keyset-сторінка свічок |
| GET | `/market/health` | `market:read` | ✓ | ✓ | ✓ | ✓ | лаг, останній Q, прогалини, знімок `PipelineHealth` (`pipeline` — останній будь-якого видавця; `pipelines` — останній кожного `source`: `ingest_worker`, `trading_worker` із шапкою LiveView; workers W-22) |
| GET | `/dq/score` | `market:read` | ✓ | ✓ | ✓ | ✓ | погодинний Q + 4 компоненти + ваги AHP |
| GET | `/strategies` | `strategy:read` | ✓ | ✓ | ✓ | ✓ | імена, кількість версій, активна |
| GET | `/strategies/{name}` | `strategy:read` | ✓ | ✓ | ✓ | ✓ | метадані версій |
| GET | `/strategies/{name}/versions/{v}` | `strategy:read` | ✓ | ✓ | ✓ | ✓ | тексти YAML дослівно |
| POST | `/strategies` | `strategy:write` * | ✓ | ✗ | ✗ | ✓ | 201, версія 1 |
| PUT | `/strategies/{name}` | `strategy:write` * | ✓ | ✗ | ✗ | ✓ | нова версія `max+1` |
| POST | `/strategies/{name}/activate?version=` | `strategy:write` * | ✓ | ✗ | ✗ | ✓ | активна версія |
| POST | `/backtests` | `backtest:run` * | ✓ | ✓ | ✗ | ✓ | **202** + `run_id` |
| GET | `/runs` · `/runs/{id}` · `/runs/{id}/metrics` · `/runs/{id}/equity` | `run:read` | ✓ | ✓ | ✓ | ✓ | паспорт, метрики, капітал |
| GET | `/decisions/{id}/explain` | `decision:read` | ✓ | ✓ | ✓ | ✓ | формальне виведення рішення |
| GET | `/risk/state` · `/risk/events` · `/risk/limits` | `risk:read` | ✓ | ✓ | ✓ | ✓ | режим, журнал вердиктів, ліміти + sha256 |
| PUT | `/risk/limits` | `risk:limits:write` * | ✗ | ✗ | ✗ | ✓ | зміна файлу лімітів + аудит |
| POST | `/risk/killswitch/release` | `risk:killswitch:release` * | ✗ | ✗ | ✗ | ✓ | **202**: команда воркеру + аудит |
| GET | `/stream/live` | `stream:read` | ✓ | ✓ | ✓ | ✓ | SSE |
| GET | `/audit` | `audit:read` | ✗ | ✗ | ✓ | ✓ | журнал аудиту |
| GET | `/healthz` | — | ✓ | ✓ | ✓ | ✓ | liveness без БД |

Таблицю згенеровано з коду (`iter_api_routes()` + `route_permission()` + `ACCESS_MATRIX`), тож вона не розходиться з
реалізацією. Тест `test_access_matrix_enforced_for_every_route` параметризований по **23 захищених парах
«метод × шлях» × 4 ролі = 92 випадки**: 403 ⇔ ролі немає в матриці, а жоден випадок не змінює стан.
Тест `test_every_route_is_protected_or_explicitly_public` падає, якщо з'явиться маршрут без дозволу.

Цілі параметри запитів обмежені типами колонок DDL §6 (`schemas.INT32_MAX` для `instrument_id`, `strategy_id`,
`version`, `user_id`; `schemas.INT64_MAX` для `decision_id`, `seed` і всіх `*_ns`): завелике число дає **422** зі
шляхом до параметра, а не 503 від драйвера чи 500 від `datetime` (`test_out_of_range_integers_are_422_not_500`).

Гроші, ціни й кількості у відповідях подаються **рядками** без експоненти (`core.money.dec_str`), бо JSON-число
втратило б точність NUMERIC(38,18). Метрики ядра (T/R/V, μ, α, u, κ) подаються як float. Неcкінченні float → `null`.
Час — наносекунди UTC (`*_ns`).

## 2. Автентифікація — `fuzzhelm.api.auth`

```python
class Permission(StrEnum): SELF_READ, MARKET_READ, STRATEGY_READ, STRATEGY_WRITE, BACKTEST_RUN, RUN_READ,
                           DECISION_READ, RISK_READ, RISK_LIMITS_WRITE, KILLSWITCH_RELEASE, STREAM_READ, AUDIT_READ
ACCESS_MATRIX: dict[Permission, frozenset[Role]];  MUTATING_PERMISSIONS: frozenset[Permission]
allowed(role, permission) -> bool
issue_token(*, uid, login, role, secret, ttl_hours, clock, jti=None) -> IssuedToken(token, expires_in_s, expires_at_s, jti)
decode_token(token, *, secret, clock) -> Principal(uid, login, role, jti, exp_s)   # AuthError → 401
LoginRateLimiter(max_failures=10, window_s=300, now_s=None, max_keys=10_000)
    .retry_after_s(*keys) -> float; .record_failure(*keys); .reset(key)
```
* Claims: `sub` (логін), `uid`, `role`, `iat`, `nbf`, `exp = iat + jwt_ttl_hours·3600`, `iss="fuzzhelm"`, `jti`.
  Алгоритм — лише HS256 (`algorithms=["HS256"]`): `alg=none` і підміна алгоритму відкидаються (тест).
* `exp`/`nbf` перевіряються за ін'єктованим `Clock`, а не за `time.time()` бібліотеки, тому тест прострочення
  детермінований і не потребує sleep. Допуск розсинхронізації годинників — 30 с для `nbf`, для `exp` допуску немає.
* `POST /auth/login` приймає OAuth2-форму (кнопка **Authorize** у `/docs`) або JSON `{username, password}`.
  bcrypt (вартість 12) рахується **в окремому потоці** і поза транзакцією (з'єднання пулу БД не тримається
  на час хешування; `routers.auth.check_password`). Для неіснуючого логіна
  виконується `dummy_verify`, тому відповідь однакова (401) і текстом, і часом. Після 10 невдалих спроб за 5 хв
  з одного IP або на один логін — **429** з `Retry-After`. Кожна спроба, успішна чи ні, пишеться в `audit_log`.
* Залежності (`api/deps.py`): `get_services(request) -> ApiServices`, `get_principal`, `require(permission)`
  (має атрибут `.permission` для тесту матриці), `client_ip(request) -> str | None`. `X-Forwarded-For` **не**
  довіряється; за reverse proxy uvicorn запускається з `--proxy-headers --forwarded-allow-ips=<ip проксі>`.

## 3. Сервісний шар — `fuzzhelm.api.services`

```python
@dataclass
class ApiServices:
    uow: Callable[[], AsyncContextManager[Repos]]  # одна транзакція; COMMIT до відповіді
    limits: LimitsStore; backtests: BacktestService; live: LiveHub; settings: Settings
    clock: Clock; ids: IdGenerator; login_limiter: LoginRateLimiter
    password_context: CryptContext = PWD_CONTEXT
    detectors_cfg: Mapping | None = None; live_source: PgLiveSource | None = None
    engine: AsyncEngine | None = None; notifier: TelegramNotifier | None = None
    def ensure_live(self) -> None; def spawn(self, coro) -> Task; async def drain(self); async def aclose(self)

build_db_services(settings=None, *, engine=None, limits_path=None, backtests=None, clock=None, ids=None,
                  listen=True, own_engine=False) -> ApiServices   # робоча збірка, роль fuzzhelm_app
create_app(services=None, *, cors_origins=("http://localhost:5173", ...), build_default=True) -> FastAPI
```
`Repos` — Protocol-підмножина репозиторіїв storage (`users, audit, strategies, runs, equity, decisions, candles,
instruments, dq, gaps, risk`) плюс `notify(channel, kind, payload)`. Цей метод виконує `pg_notify` **у тій самій
транзакції**, тож подію буде доставлено лише після COMMIT. Маршрут сам відкриває `async with services.uow()`, щоб
COMMIT (а з ним і запис аудиту) стався **до** відповіді. Вихідний код yield-залежності FastAPI виконується вже після
відповіді. Тести підміняють `ApiServices` через `app.dependency_overrides[get_services]` реалізацією в пам'яті
`tests/helpers/api_fakes.py` з тими самими методами й типами рядків. Код маршрутів у e2e і на PostgreSQL однаковий.

Відображення помилок (`main._install_error_handlers`): `LookupError` → 404, `IntegrityError` 23505/23503 → 409,
інші порушення обмежень → 422, 42501 (роль СУБД) → 403, SQLSTATE класу 22 (data exception, напр. число поза
типом колонки) → 422, `OperationalError`/`OSError` та інші `DBAPIError` → 503. Деталі драйвера й
файлової системи потрапляють лише в журнал сервера. `SecurityHeadersMiddleware` додає заголовки `nosniff`, `DENY`,
`no-referrer`, `no-store`. Middleware написано на чистому ASGI, тому SSE він не буферизує.

## 4. Стратегії — правила як дані (§7)

`POST /strategies {name, rules_yaml, membership_yaml, activate}` і `PUT /strategies/{name} {rules_yaml, membership_yaml, activate}`.
Валідація (`api/validation.py`):
```python
safe_yaml_mapping(text, field) -> Mapping           # SafeLoader без якорів/псевдонімів, лише мапінг зверху
validate_strategy_texts(rules_yaml, membership_yaml) -> ValidatedStrategy(membership, rulebase, rules, membership_tree)
check_resource_limits(membership)                   # defuzz.grid_nodes ≤ MAX_DEFUZZ_NODES (2001), ≤ 15 термів на змінну
class StrategyValidationError(field, path, message, *, kind="config_validation"|"yaml_syntax", line=None, column=None)
```
Тексти перевіряються тими самими завантажувачами, що й у рушія: `fuzzy.membership.load_membership` і
`fuzzy.rules.load_rulebase(production=True)`. Це означає рівно 45 правил, кожну комбінацію антецедентів рівно
один раз, усі терми визначені, `w == 1.0`. Помилка дає **422** з точним шляхом:
```json
{"detail": [{"loc": ["body", "rules_yaml"], "path": "rules[3].if.T", "msg": "unknown term 'SIDEWAYS' ...",
             "type": "config_validation"}]}
```
Синтаксична помилка YAML має `type: "yaml_syntax"`, `line`, `column`. Розмір тексту обмежено 256 KiB. Окремо
обмежено обсяг обчислень (`check_resource_limits`): завантажувач fuzzy перевіряє `grid_nodes` лише знизу, а
`grid_nodes: 1000000000` вичерпав би пам'ять на першому `/explain` чи бектесті. Межа 2001 вузол — найдрібніша сітка
дослідження збіжності (Δ = 0.001), 15 термів на змінну (T/R/V і так фіксує база 5×3×3). Порушення → 422 з
`path` `defuzz.grid_nodes` / `variables.U.terms` (`test_strategy_compute_budget_is_bounded`). Кожна зміна
створює **нову незмінну версію**: старі версії лишаються, бо на них посилаються паспорти прогонів. Ідентичний набір
текстів відхиляється з **409** через `UNIQUE (rules_hash)`. Зміни пишуться в `audit_log` (`strategy.create`,
`strategy.update`, `strategy.activate`) зі станом до й після і публікуються на `fuzzhelm_live` (kind `strategy`).

## 5. Бектести — `api/backtests.py`, `api/backtest_runner.py`

```python
class BacktestService(Protocol):
    async def submit(self, run_id: UUID, spec: Mapping, *, actor: str) -> BacktestJob
    def job(self, run_id: UUID) -> BacktestJob | None
    async def aclose(self) -> None
EngineBacktestService(runner: Runner | None = None, *, max_concurrent=1, max_pending=8, max_jobs=256)
    # Runner = Callable[[UUID, Mapping], Awaitable[Any]]; .wait(run_id) — для тестів/скриптів
BacktestJob(run_id, spec, actor, status: JobStatus(PENDING|RUNNING|DONE|FAILED), error, result)
load_engine_module() -> ModuleType     # лінивий імпорт fuzzhelm.backtest.engine (BacktestConfig, run_backtest)
public_error(exc) -> str               # текст помилки для job.error / run.error; для DBAPIError — лише клас і SQLSTATE

DbBacktestRunner(factory, *, data_dir, clock=None, git=True)   # типовий Runner робочої збірки
prepare_backtest(session, spec, engine_mod, *, data_dir) -> PreparedBacktest
build_config(engine_mod, spec, trees=None) -> BacktestConfig   # профіль backtest + engine + params + дерева стратегії
funding_rates(path, instrument) -> Sequence[FundingRate] | None   # увесь файл; вікно обрізає Dataset.with_funding
plan_persistence(result, *, run_id, instrument_id) -> PersistPlan          # чисте відображення BacktestResult → рядки
persist_backtest_result(session, run_id, result, *, instrument_id, symbol) -> PersistCounts
run_config_json(cfg) -> dict           # cfg.identity_dict() → run.config (з нього /explain відтворює рушій)
PARAM_KEYS = {n_atr, chi, u_enter, u_exit, rho_base, lam, tp_multiple, cost_mode, initial_equity}
```
`POST /backtests {symbol, tf="1m", ts_from_ns, ts_to_ns, engine="mamdani"|"linear", strategy_id?, seed?, params?}`
одразу повертає **202** `{run_id, status, status_url}`. Параметри з `params` валідує схема `BacktestParams`
(`extra=forbid`, межі значень), невідомий ключ дає 422. Запуск пишеться в аудит (`backtest.submit`). Черга живе в
процесі API: не більше 8 незавершених задач (далі **429**), один прогін одночасно, рушій працює в потоці
(`asyncio.to_thread`), тож event loop не блокується.

`DbBacktestRunner` виконує задачу так:
1. Читає інструмент, версію стратегії (якщо є `strategy_id`) і закриті свічки `[ts_from, ts_to)`
   через `CandleRepo.load_arrays`. Ставки фінансування бере з `data/funding_<SYMBOL>.json` і будує набір
   **канонічним шляхом рушія** `Dataset.from_candle_arrays(arrays, instrument, funding=rates)` (той самий, що
   `backtest.runner.load_db_window`): ставки обрізає рушій за правилом [t₀ − 1 доба, t_last + 1 хв]. Тому ті самі
   свічки дають той самий `dataset_hash` через API і через CLI/сітку/walk-forward, зокрема для під-вікна (API-15,
   `test_api_dataset_hash_equals_engine_canonical_path_for_subwindow`).
2. Будує `BacktestConfig.from_profile("backtest", engine, trees={membership, rules зі стратегії}, **params)`.
   Якщо `strategy_id = null`, беруться `config/membership.yaml` і `config/rules_mamdani.yaml`.
3. Перевіряє ідентичність `(config_hash, dataset_hash, seed, engine, git_sha)` через `find_by_identity`
   (`IS NOT DISTINCT FROM`, ST-03). Дубль → задача **FAILED** «identical run already stored as <id>».
4. Записує паспорт `run` зі статусом RUNNING (config = `identity_dict`, хеші, git HEAD, strategy_id, вікно).
5. Викликає `run_backtest(dataset, cfg, seed, run_id=run_id, git=False, kind=BACKTEST)` у потоці.
6. Однією транзакцією пише результат: рішення з трасуванням (`DecisionRecord.from_trace` + `narrate()` у
   `decision.narrative`), ордери (`OrderRepo.create` + `apply_fill` кожного виконання; FK на рішення-джерело),
   позиції, криву капіталу (COPY), записи ризик-контуру, метрики (17 + `psr`, `sr_period`, …) і
   `finish(DONE, journal_head_hash, equity_hash)`. Будь-яка помилка дає `finish(FAILED, public_error(e))`: текст
   помилки СУБД (SQL і параметри) до користувача не потрапляє, лише клас і SQLSTATE.

Замір і незалежна перевірка (рецензія 2026-09-19). Тестова БД (порт 5443): `instrument` і 64 800 свічок
BTC-USDT-PERP вікна 2026-08-04…2026-09-18 скопійовано з робочої БД командою `psql \copy … to stdout | psql \copy …
from stdin` (робоча БД лише читалась). Далі скрипт у робочій теці сесії (не в репозиторії) через ASGI-клієнт
виконав `POST /backtests` → `EngineBacktestService.wait` → `/decisions/{id}/explain` для **кожного** рішення, і
незалежно перерахував хеші. Load average ≈ 5, два прогони (до і після виправлення API-15):

| величина | прогін 1 | прогін 2 |
|---|---|---|
| `POST /backtests` → 202 | 17.3 мс | 19.6 мс |
| задача DONE (рушій + запис) | 6.72 с | 7.01 с |
| `/explain`, усі 180 рішень: медіана / максимум | 6.9 / 27.2 мс | 4.8 / 14.4 мс |

Обидва прогони записали однакове: 180 рішень, 295 ордерів (0 без рішення-джерела), 211 з виконаннями, 113 позицій,
64 800 точок капіталу, 26 979 записів ризику, 25 метрик; `config_hash 7f91fc34…`, `dataset_hash 3e58da26…`,
`equity_hash 8a82f814…`. Незалежні перевірки: `equity_hash`, перерахований з рядків `equity_point`, дорівнює
паспорту; прямий `run_backtest` на наборі `Dataset.from_candle_arrays` дає ті самі `config_hash` і `equity_hash`;
хеш свічок збігається з `data/dataset_window.json`; тотожність брифінгу `equity = cash ± gross_exposure − Σfee`
(EXE-01, комісії з `sim_order.fee`) виконується на всіх 64 800 рядках з нульовим залишком; усі 180 `/explain` мають
`consistency.ok = true`, `strategy.source = run_config`, `narrative_source = stored`. `journal_head_hash` залежить
від `run_id` (однаковий для того самого `run_id`, різний для різних), тож два API-прогони мають різні голови журналу.
Чистий рушій на тому самому наборі: 4.90–5.39 с (решта — читання свічок і запис). Попередній замір будівника
(load average ≈ 50) дав ті самі кількості рядків і 7.60 с.

## 6. Прогони — `/runs*`

`GET /runs?kind=&status=&limit=`. `GET /runs/{id}` повертає паспорт (hex-хеші, `config`), а для задачі з черги,
паспорта якої ще немає, віддає стан задачі в `job`. `GET /runs/{id}/metrics` → `{run_id, metrics: {name: float|null}}`.
`GET /runs/{id}/equity?from_ns&to_ns&max_points=5000` → `{n_total, stride, items[…]}`: проріджування зі сталим кроком,
остання точка лишається завжди.

## 7. `/decisions/{id}/explain` — ядро демо (§8.1, §8.2)

`api/explain.py: explain_decision(row, *, run, strategy, detectors_cfg=None, mf_points=101) -> dict`. Поля відповіді:

| Поле | Зміст |
|---|---|
| `inputs` {T,R,V}, `inputs_source` | входи FIS, перераховані `consensus()` з точних float JSONB `detector_outputs`. Якщо вони неповні, беруться з колонок (`stored_columns`) |
| `memberships` | {T:{5 термів}, R:{3}, V:{3}}, μ кожного терму в точці входу |
| `variables.{T,R,V,U}` | для `MembershipPlot`: діапазон, значення, терми (тип, параметри, μ, `active`, крива на `mf_points` точках), назви українською |
| `fired_rules[]` | за **спаданням α**: `rule_id, alpha, antecedent, consequent`, `*_uk`, `text_uk` = «R22: ЯКЩО тренд … І реверсія … І волатильність … ТО сигнал …» |
| `aggregate` | `grid` (201 вузол), `mu` = μ_agg(u), `centroid` = u_raw, `scheme`, `area`, `height`, сила кожного консеквента β |
| `u_raw, kappa, u_final, agreement` | ентропійна узгодженість (p+, p−, p0, H, A_g, κ) |
| `sizing` | розкладка сайзера (`q_atr, q_vt, q_lev, s_t, sigma_ann, kappa_mode, stop_distance, notional, binding_constraint`) |
| `risk` | вердикт ланцюга, записи правил, `approved_qty`, стан автомата |
| `target`, `prices` | бік, кількість, `binding_constraint`; `stop`, `tp`, `liq` рядками |
| `narrative_uk`, `narrative_source` | збережений текст (`stored`), інакше `narrate()` (`recomputed`) або скорочений (`recomputed(partial)`) |
| `strategy` | `{id, name, version, source: run_config | strategy | default_config | linear}` |
| `consistency` | `u_raw_stored/recomputed/abs_diff`, `fired_rules_match`, `max_alpha_abs_diff`, `ok` |

μ_agg не зберігається: це 201 вузол на кожен бар. Він **перераховується детерміновано** тим самим `MamdaniEngine`.
Джерело рушія обирається в порядку достовірності: дерева МФ і правил з `run.config.trees`, тобто саме те, що
виконував рушій; інакше тексти версії стратегії прогону; інакше `config/`. κ_min і ν так само беруться з дерева
detectors прогону. Блок `consistency` чесно показує, чи перерахунок збігся зі збереженим (допуски: u_raw — 5e−6,
тобто округлення NUMERIC(8,5); α — 1e−9). Розбіжність означає, що конфігурацію змінили після прогону, і її не
приховано (`test_explain_flags_inconsistency_when_strategy_config_differs`). Рішення, яке неможливо пояснити
(немає ні виходів детекторів, ні T/R/V), дає 409.

## 8. Ризик — `/risk/*`, `api/limits.py`

* `GET /risk/state?run_id=`. Без `run_id` береться найсвіжіший RUNNING live-прогін (paper/replay/testnet), інакше
  найсвіжіший live-прогін. Відповідь: `state` з останнього переходу `risk_state` (або `equity_point.risk_state`,
  або `NORMAL`), `kappa_mode` за поточними лімітами, просадка, останній запит на зняття kill-switch.
* `GET /risk/events?run_id&rule&only=all|veto|transitions&since_ns&limit`. `factor` точний: береться з
  `payload.factor_exact`, якщо NUMERIC(6,4) його округлила (API-03).
* `PUT /risk/limits` (лише admin) приймає тіло у формі файлу (`limits`, `state_machine`, опційно `sizing`,
  `hysteresis`) і `expected_sha256` з `GET /risk/limits` (оптимістичне блокування, 409 при розбіжності).
  Порядок дій: `risk.config.load_risk_config(tree)` (помилка → 422 з `path`), далі в **одній транзакції**
  `audit_log` (`risk.limits.update`, before = повна стара конфігурація, after = нова + `_changed` + `_sha256_before`,
  `user_id`, `ip`), атомарна заміна `config/risk_limits.yaml` (tmp → fsync → `os.replace` → fsync теки, `flock`),
  NOTIFY `fuzzhelm_control` (`risk.limits.changed`) і COMMIT. Якщо COMMIT не вдався, файл повертається до
  попереднього тексту. Стану «ліміт змінено без сліду в аудиті» не буває (`test_limits_file_restored_when_audit_commit_fails`).
  ```python
  FileLimitsStore(path).read() -> LimitsSnapshot(text, sha256, config); .replace(text, *, expected_sha256); .restore(text)
  config_json(cfg) -> dict  # Decimal → рядок; diff_paths(before, after) -> ["limits.max_daily_loss.value", ...]
  ```
* `POST /risk/killswitch/release {run_id?, reason}` (лише admin) → **202**. API пише `audit_log`
  (`risk.killswitch.release`, before = спостережений стан) і надсилає NOTIFY `fuzzhelm_control` kind
  `killswitch.release` `{audit_id, run_id, actor, role}`. Якщо Telegram налаштовано, у фоні йде повідомлення оператору.
  Сам HALTED знімає власник автомата, торговий воркер: `RiskStateMachine.release(Role.ADMIN)` (API-06).

**Протокол каналу керування для воркерів** (`LISTEN fuzzhelm_control`, payload `{"kind": …, "data": …}`):
`risk.limits.changed {sha256, audit_id}` — перечитати `config/risk_limits.yaml`;
`killswitch.release {audit_id, run_id, actor, role}` — зняти HALTED. Пропущені під час простою команди воркер
дочитує з `audit_log` (`action='risk.killswitch.release' AND id > останнього обробленого`).

## 9. SSE — `GET /stream/live`, `api/live.py`

```python
LIVE_CHANNEL = "fuzzhelm_live"; CONTROL_CHANNEL = "fuzzhelm_control"
encode_notify_payload(kind, payload) -> str     # {"kind":…,"data":…}; > 7900 байт → ValueError (шліть посилання)
decode_notify_payload(raw) -> (kind, data) | None
LiveHub(queue_size=256, replay_size=512): publish(kind, payload) -> LiveEvent(seq, kind, payload); subscribe(kinds, last_event_id)
    last(kind); replay_after(seq); close(); stats(); await wait_subscribers(n)
PgLiveSource(dsn, hub, *, channel, role): ensure_started(); await wait_connected(); await stop()   # одне з'єднання LISTEN
```
Джерело подій — **PostgreSQL LISTEN/NOTIFY**. Опитування БД і брокер відкинуто, обґрунтування в API-07.
Воркер публікує подію так:
`SELECT pg_notify('fuzzhelm_live', <encode_notify_payload("decision", {"id": 123, ...})>)` у своїй транзакції.
Браузер отримує лише закомічені факти. Великі об'єкти передаються посиланням, деталі читаються через REST.
Формат SSE: `event: <kind>`, `id: <seq процесу>`, `data: <JSON>`. Keep-alive-коментар надсилається кожні 15 с.
`Last-Event-ID` відновлює пропущене з кільцевого буфера (512 подій), `kinds=` фільтрує, `max_events=` закриває потік.
Авторизація — лише заголовок `Authorization: Bearer` (як і в решти API). Тому браузерний клієнт використовує
`fetch` + `ReadableStream`, а не `EventSource`. Повільний клієнт втрачає найстаріші події своєї черги (лічильник
`dropped` у `stats()`) і не гальмує інших. Тестованість: `LiveHub` не знає про PostgreSQL (офлайн-тест
`test_sse_stream_delivers_published_events_with_ids`). Транзакційність NOTIFY перевіряє інтеграційний тест
`test_sse_streams_only_committed_notifications`: подія з відкоченої транзакції не доставляється.

## 10. Telegram — `fuzzhelm.notify.telegram`, `fuzzhelm.notify.templates`

```python
TelegramNotifier(token: SecretStr | None, chat_id: str | None, *, http=None, base_url="https://api.telegram.org",
                 dedup_window_s=300, rate_capacity=20, rate_per_s=20/60, now_s=None, timeout_s=5)
TelegramNotifier.from_settings(settings)          # TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID; немає → no-op (DISABLED)
await .send(Notification(kind, key, text, priority)) -> SendResult(status: SENT|DISABLED|DUPLICATE|RATE_LIMITED|FAILED, ...)
await .signal(symbol, side, u_final, top_rule, alpha, consequent, qty, price, binding_constraint)
await .risk_event(rule, verdict, symbol, observed, limit, factor); await .risk_state(state_from, state_to, drawdown, reason)
await .killswitch(action="tripped"|"release_requested"|"released", reason, actor, run_id)
await .ws_disconnect(conn, cls, detail, reconnect_in_s); await .daily_report(day, candles, q_min, gaps, vetoes, ...)
RateBucket(capacity, refill_per_s, now_s).try_take(); Deduplicator(window_s, now_s).is_duplicate/mark
```
`send()` ніколи не кидає винятку і не чекає: нотифікація — best effort, торговий цикл вона не зупиняє. Той самий
`key` у межах вікна вважається дублікатом. Token bucket без очікування: надлишок відкидається з `RATE_LIMITED`.
`CRITICAL` (kill-switch, перехід у HALTED) оминає bucket, але не дедуплікацію. Відповідь 429 від Telegram блокує
відправку на `retry_after`. Текст іде без `parse_mode`, тож ін'єкції розмітки немає; обрізка — до 4096 символів.
Токен присутній лише в URL запиту. На логери `httpx`/`httpcore` ставиться фільтр, що замінює токен на `***`, а
тексти помилок містять тільки тип винятку. Хост — лише `https://api.telegram.org` (allowlist). Шаблони
(`templates.py`) українською й використовують ті самі словники, що й `decision.narrative_uk`. Тести — тільки
`respx` (`tests/unit/test_notify.py`).

## 11. Планувальник — `fuzzhelm.scheduler.jobs`

```python
JobContext(uow, clock, weights, tau0_ms=1000, backfill=None, notifier=None, anomaly_threshold=None, max_attempts=5)
await retry_gaps(ctx, *, statuses=(PARTIAL, UNFILLABLE), limit=100) -> list[GapOutcome]   # 02:30 UTC
await hourly_dq(ctx, hour_start_ns=None, *, force=False) -> list[DqRow]                  # hh:05 UTC, попередня година
await daily_report(ctx, day_start_ns=None) -> DailyReport                                # 00:10 UTC, попередня доба → Telegram
RestGapBackfiller(client)          # REST klines → CandleRepo.upsert (ідемпотентно)
expected_bars(gap), next_status(present, expected, attempts_after, max_attempts), dq_inputs_for_hour(...)
job_specs(ctx) -> [JobSpec]; build_scheduler(ctx, scheduler=None) -> AsyncIOScheduler  # max_instances=1, coalesce
build_db_context(settings=None, *, backfill=None, notifier=None, clock=None) -> JobContext
await serve(ctx, stop: asyncio.Event, scheduler=None)     # процес: python -m fuzzhelm.scheduler.jobs
```
Кожна задача — звичайна async-функція від `JobContext` і явного моменту часу, тому тести викликають її напряму
без справжнього часу й без sleep. APScheduler лише викликає її за розкладом. Прогалина переходить у FILLED, коли
всі бари наявні; інакше в PARTIAL; після `max_attempts` (5) — в UNFILLABLE. Прогалини угод і книги через REST
не добираються. Погодинний Q рахується лише для годин **без** рядка `dq_score`: живий конвеєр пише власний Q, і
його не перезаписуємо. Своєчасність рахується тільки для WS-свічок, бо REST-добір «запізнілий» за визначенням.
Щоденний звіт показує кількість свічок і мінімальний Q на інструмент, прогалини за статусом, VETO і зміни режиму
live-прогону, капітал і просадку.

## 12. Міграція 0004 і зміни storage (API-02, API-03, API-05)

`alembic/versions/0004_decision_trace_extras.py`:
`ALTER TABLE decision ADD COLUMN sizing JSONB, ADD COLUMN risk JSONB, ADD COLUMN narrative TEXT` (nullable, з чистим
downgrade). `DecisionRecord`/`DecisionRow` мають поля `sizing`, `risk`, `narrative`.
`DecisionRecord.from_trace(..., narrative=None)` бере `sizing`/`risk` із трасування, а якщо `binding_constraint`
не передано, то й його з `sizing`. `RiskEventRepo` пише точний `factor` у `payload.factor_exact`, коли
NUMERIC(6,4) його округлила б (`RiskEventRow.factor_exact`). `GapRepo.list_by_status(statuses, *, instrument_id,
max_attempts, limit)` і `list_overlapping(instrument_id, lo, hi, *, stream)` використовує планувальник.
Робочу БД (порт 5442) мігровано до `0004_decision_trace_extras` командою
`FUZZHELM_DATABASE_URL=…:5442/fuzzhelm uv run alembic upgrade head` (2026-09-19).

## 13. Тести

| Файл | Що перевіряє |
|---|---|
| `tests/e2e/test_api.py` (офлайн, `MemoryDb`) | 6 назв групи N дослівно + матриця (92 випадки), токени (підробка, `alg=none`, прострочення), пониження ролі, rate limit, CRUD і версії стратегій, YAML-псевдоніми, `/explain` (узгодженість, fallback), свічки, health, прогони, бектест, kill-switch + Telegram, точний factor, ліміти (422/409/відновлення файлу), SSE, OpenAPI; межі цілих (422 замість 500/503), SQLSTATE 22 → 422, межі обсягу обчислень стратегії, текст помилки прогону без деталей драйвера |
| `tests/unit/test_auth.py` | JWT, матриця, rate limiter, `check_password` |
| `tests/unit/test_api_backtest_runner.py` | конфігурація рушія, файл фандингу цілком (вікно — рушія), `plan_persistence` на справжньому рушії, `/explain` рішення рушія з `run.config` |
| `tests/unit/test_notify.py` | no-op, дедуплікація, bucket, 429, некоректна відповідь Telegram → FAILED без винятку, токен не потрапляє в журнал, шаблони |
| `tests/unit/test_scheduler.py` | добір прогалин, погодинний Q, щоденний звіт, розклад, `serve()` |
| `tests/integration/test_api_db.py` (маркер `integration`) | вхід + INET, аудит before/after append-only (42501), CRUD на БД, `/explain` на колонках 0004, точний factor, SSE лише після COMMIT, канал керування, планувальник на БД, API без БД, **повний бектест через API на БД**, `dataset_hash` під-вікна = канонічний шлях рушія |
