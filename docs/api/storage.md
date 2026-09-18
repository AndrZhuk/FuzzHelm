# `fuzzhelm.storage`: фактичний публічний API

Сховище PostgreSQL 16 без розширень: 15 таблиць за нормативним DDL §6 (12 нумерованих + `run_metric`,
`app_user`, `audit_log`), 3 ревізії Alembic, асинхронні репозиторії поверх SQLAlchemy 2.0 + asyncpg.
Пакет є межею (boundary): архітектурні AST-тести його не сканують, але float → Decimal він робить лише через
`sizing.convert.float_to_decimal_exact`, а Decimal → float лише через `features.convert.to_float`.

**Головне правило:** межа транзакції завжди у викликача. Репозиторій отримує `AsyncSession`, виконує запити
і **ніколи не комітить сам**.

```python
from fuzzhelm.storage.session import make_engine, session_factory, session_scope
from fuzzhelm.storage.repositories import CandleRepo, InstrumentRepo

engine = make_engine()                          # URL з FUZZHELM_DATABASE_URL / .env (Settings)
factory = session_factory(engine)
async with session_scope(factory) as s:          # BEGIN … COMMIT (ROLLBACK при винятку)
    iid = await InstrumentRepo(s).upsert(instrument_dto)
    res = await CandleRepo(s).upsert(candles, iid)   # UpsertResult(inserted, updated, skipped)
```

Процесний engine API/воркерів (`get_default_engine()` / `get_session`) уже працює роллю `fuzzhelm_app`
(`make_engine(role="fuzzhelm_app")`): СУБД сама відхиляє UPDATE/DELETE журналу подій та аудиту. Схема мусить бути
мігрована до `0003_auth_audit` (роль існує). Це захист від помилкових записів, а не межа безпеки — див. «Безпека».

---

## Конвенції типів (однакові для всіх репозиторіїв)

| Домен | БД | Правило |
|---|---|---|
| час `*_ns: int` (нс UTC) | `TIMESTAMPTZ` (мкс) | `ns_to_dt` відкидає субмікросекундну частину (floor), `dt_to_ns` точний. Для міток, кратних 1 мкс, перетворення туди й назад тотожне. Уся арифметика цілочисельна. |
| `ts_event_ns`, `ts_ingest_ns` | `BIGINT` | лише в `event_journal`, наносекунди зберігаються без втрат |
| `Decimal` | `NUMERIC(p,s)` | пишеться як є; округлення до масштабу колонки робить PostgreSQL («половина від нуля», **не** HALF_EVEN). Виняток — гроші `equity_point` (`equity, cash, unrealized, gross_exposure, var95, cvar95`): `to_money18` квантує HALF_EVEN до 1e−18 ще до запису, як `core.money.quantize_internal` / `equity_hash` |
| `float` (κ, u, T/R/V, скор) | `NUMERIC(p,s)` | `to_numeric(x)` = `float_to_decimal_exact` (найкоротше `repr`), numpy-скаляр спершу `.item()` |
| неcкінченні | `NUMERIC` | `to_numeric` відкидає float **і Decimal** NaN/sNaN/±Infinity (`ValueError`): PostgreSQL прийняв би `NUMERIC 'NaN'` мовчки (ST-04). Колонки свічок пишуться з DTO, який NaN уже відкинув |
| хеші | `BYTEA` | приймаються `bytes` або hex-рядок (як у `RunManifest`) |
| enum | `TEXT` / `SMALLINT` | `.value` |
| JSON | `JSONB` | `json_dumps`: orjson; `Decimal` → рядок без експоненти (`Decimal('1E+2')` → `"100"`); numpy → списки |

Рядки читання — frozen dataclass-и `…Row`. У них поле `x_ns` відповідає TIMESTAMPTZ-колонці `x`,
гроші мають тип `Decimal` масштабу колонки (напр. `Decimal('60000.100000000000000000')`).

## `storage/session.py`

```python
def make_engine(url: str | None = None, *, role: str | None = None, echo: bool = False,
                null_pool: bool = False, pool_size: int = 5, max_overflow: int = 5, **kwargs) -> AsyncEngine
def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]   # expire_on_commit=False
@asynccontextmanager
async def session_scope(factory) -> AsyncIterator[AsyncSession]                   # одна транзакція
def get_default_engine() -> AsyncEngine          # лінивий процесний engine за Settings, role=APP_ROLE
def get_default_factory() -> async_sessionmaker[AsyncSession]
async def dispose_default_engine() -> None       # у lifespan-shutdown FastAPI
async def get_session() -> AsyncIterator[AsyncSession]   # Depends(get_session): транзакція на запит
def json_dumps(obj) -> str; def json_loads(s) -> Any     # серіалізатори JSONB, вже підключені до engine
APP_ROLE = "fuzzhelm_app"
```
* `role=` працює лише з asyncpg (стартовий параметр з'єднання `role`, еквівалент `SET ROLE`). Для інших драйверів — `ValueError`.
  Користувач з'єднання (у compose — власник `fuzzhelm`) може виконати `SET ROLE fuzzhelm` і повернути собі всі права,
  тож `role=` захищає від помилкових UPDATE/DELETE, але не від скомпрометованого застосунку (тест
  `test_set_role_is_not_a_security_boundary_documented`).
* `get_default_engine()` / `get_session` створюють engine з `role=APP_ROLE`; для задач, яким потрібні права власника
  (DDL, TRUNCATE), створюйте окремий `make_engine(url)` без `role`.
* `null_pool=True` вимикає пул. Потрібно, коли кожен тест має власний event loop, і для одноразових скриптів.

## `storage/models.py`

ORM-моделі (`DeclarativeBase`) дзеркалять DDL: `InstrumentModel, CandleModel, EventJournalModel, IngestGapModel,
DqScoreModel, StrategyModel, RunModel, DecisionModel, SimOrderModel, PositionModel, RiskEventModel,
EquityPointModel, RunMetricModel, AppUserModel, AuditLogModel`. Також модуль експортує `metadata`, `table_of(Model) -> Table`,
`CORE_TABLES`, `TRADING_TABLES`, `AUTH_TABLES`, `ALL_TABLES` (15 імен у порядку створення) і `APP_ROLE`.
Схему створюють лише міграції. Моделі потрібні для типізованих запитів, а тест
`test_models_match_migrated_schema` (autogenerate-порівняння) гарантує, що вони не розійшлися з міграціями.

## Alembic

`alembic.ini` + `alembic/env.py` (async, asyncpg). Порядок вибору URL: `alembic -x url=…` → `sqlalchemy.url`,
виставлений програмно → `Settings().database_url` (`FUZZHELM_DATABASE_URL` / `.env`). Ревізії:
`0001_core` (instrument, candle, event_journal, ingest_gap, dq_score) → `0002_trading` (strategy, run, decision,
sim_order, position, risk_event, equity_point, run_metric) → `0003_auth_audit` (app_user, audit_log, роль
`fuzzhelm_app`, гранти, `REVOKE UPDATE, DELETE ON event_journal/audit_log`). DDL у ревізіях — дослівний текст §6.
Кожна ревізія має чистий downgrade. `env.py` можна викликати і з уже запущеного event loop: тоді він
запускає власний цикл в окремому потоці. Offline-режим (`--sql`) підтримується.

```bash
FUZZHELM_DATABASE_URL=postgresql+asyncpg://fuzzhelm:fuzzhelm@localhost:5442/fuzzhelm uv run alembic upgrade head
```

## Репозиторії (`storage/repositories/*`, реекспорт у `storage.repositories`)

### `candle.CandleRepo(session)`
```python
async def upsert(candles: Sequence[Candle], instrument_id: int | None = None, *, use_copy: bool | None = None) -> UpsertResult
async def upsert_records(records: Sequence[Sequence[Any]], *, use_copy: bool | None = None) -> UpsertResult
async def set_anomaly_scores(instrument_id, tf, scores: Sequence[tuple[int, Decimal | float]]) -> int
async def get(instrument_id, tf, open_time_ns) -> CandleRow | None
async def range(instrument_id, tf, ts_from_ns=None, ts_to_ns=None, *, after_ns=None, limit=1000,
                closed_only=False, descending=False) -> CandlePage          # [from, to), keyset за open_time
async def iter_range(instrument_id, tf, ts_from_ns=None, ts_to_ns=None, *, page_size=5000, closed_only=False) -> list[CandleRow]
async def load_arrays(instrument_id, tf, ts_from_ns=None, ts_to_ns=None, *, closed_only=True) -> CandleArrays
async def load_bars(instrument_id, tf, ts_from_ns=None, ts_to_ns=None) -> list[Bar]
async def count(instrument_id=None, tf=None) -> int
async def latest_open_time_ns(instrument_id, tf, *, closed_only=True) -> int | None
async def find_gaps(instrument_id, tf, step_ns, ts_from_ns=None, ts_to_ns=None) -> list[tuple[int, int, int]]
    # (перший відсутній open_time_ns, наступний наявний open_time_ns, кількість пропущених)
UpsertResult(inserted, updated, skipped).affected
CandlePage(items: list[CandleRow], next_after_ns: int | None)   # None — остання сторінка
CandleArrays(t_ns:int64, o,h,l,c,v,qv: float64, n:int64); .columns(names=("t_ns","o","h","l","c","v")); .bars()
CandleRow(...).to_dto(symbol_canon, venue, *, tick_size=None) -> core.dto.Candle
candle_record(c: Candle, instrument_id, anomaly_score=None) -> tuple   # порядок WRITE_COLUMNS
```
Інваріанти:
* Upsert іде через `INSERT … ON CONFLICT (instrument_id, tf, open_time) DO UPDATE … WHERE candle.is_closed = FALSE AND EXCLUDED.src <= candle.src`.
  Закриту свічку не змінює жодне джерело, а відкриту змінює лише джерело не нижчого пріоритету (`Src.WS=1 < REST=2 < REPLAY=3`).
  Повтор пачки дає `inserted=0`, а всі колонки, крім `ingested_at` відкритих свічок, лишаються побітово тими самими.
* Якщо в пачці кілька рядків з одним ключем, вони застосовуються послідовно (раунди `split_unique_rounds`),
  як при порядковій вставці.
* `anomaly_score` при оновленні — `COALESCE(EXCLUDED, поточний)`, тобто добір без скору не стирає вже порахований.
  `ingested_at` при оновленні стає `now()`.
* `instrument_id=None` означає, що id шукається за `Candle.instrument` (symbol_canon). Невідомий символ дає `LookupError`.
* COPY (бінарний) через тимчасову таблицю вмикається для раундів ≥ `COPY_THRESHOLD=500` рядків (лише asyncpg).
  Для менших пачок — `INSERT … VALUES` по 1000 рядків. Семантика обох шляхів ідентична, бо обидва будує `_upsert_stmt`.
* `load_arrays`: час рахується в СУБД (`EXTRACT(EPOCH)` у PG ≥ 14 повертає numeric, тому ×10⁶ і `::bigint` точні).
  Ціни конвертуються через `features.convert.to_float`, тож масиви побітово рівні `to_float(Decimal)`.
  `.columns()` можна передати прямо в `backtest.manifest.dataset_hash`.
* `to_dto` відновлює те, чого немає в таблиці: `event_uid` з природного ключа, `ts_event_ns := close_time_ns`,
  `ts_ingest_ns := ingested_at`. Ціни з `tick_size` квантуються до tick (це точно), решта Decimal зводиться до
  мінімального масштабу. Числово DTO дорівнює вхідному, але може відрізнятися масштабом (див. ST-08).

### `instrument.InstrumentRepo(session)`
`upsert(inst: Instrument, *, spec_fetched_at_ns=None, active=True) -> int` (ON CONFLICT (venue, symbol_venue)),
`get(id)`, `get_by_canon(symbol_canon)`, `get_by_venue_symbol(venue, symbol_venue)`, `list(*, active_only=False)`,
`id_map() -> dict[symbol_canon, id]`, `resolve_id(symbol_canon) -> int` (`LookupError`, якщо символу немає).
`InstrumentRow.to_dto() -> core.dto.Instrument`.

### `journal.JournalRepo(session)`
```python
async def append(entry: JournalEntry) -> None
async def append_many(entries: Sequence[JournalEntry]) -> int
async def read_chain(run_id, *, from_seq=0, limit=None) -> list[JournalEntry]
async def verify(run_id) -> int | None        # seq першого зіпсованого/відсутнього запису, None — ланцюг цілий
async def head(run_id) -> ChainHead(run_id, next_seq, head)    # GENESIS для порожнього
async def resume(run_id, sink=None) -> EventJournal            # продовжити ланцюг після рестарту
async def count(run_id=None) -> int; async def kinds(run_id) -> dict[str, int]
```
Payload зберігається в канонічній формі `core.digest.to_canonical` (Decimal → рядок). Хеш, перерахований із
прочитаного payload, збігається з записаним (перевірено тестами). Репозиторій лише дописує: повтор `seq` дає `IntegrityError`.
Міст зі синхронним sink `EventJournal`:
```python
buf = BufferedSink[JournalEntry](); j = EventJournal(run_id, sink=buf, keep=False)
... j.append(...) ...
await JournalRepo(s).append_many(buf.drain())
```

### `gap.GapRepo(session)`
`open(instrument_id, stream, ts_lo_ns, ts_hi_ns, *, expected_count=None, detector=GapDetectorKind.TIME, detected_at_ns=None) -> int`
(статус OPEN), `update_status(gap_id, status, *, filled_rows=None, count_attempt=True, at_ns=None) -> GapRow`.
Кінцевий статус FILLED/PARTIAL/UNFILLABLE ставить `closed_at` (`at_ns` або `now()`), OPEN/FILLING його знімає.
Також `get(id)`, `list_open(instrument_id=None, *, limit=1000)` (предикат збігається з частковим індексом `ix_gap_open`)
і `stats() -> {status: count}`.

### `dq.DqRepo(session)`
`upsert(DqRow)` — ON CONFLICT (instrument_id, hour_start), перерахунок тієї самої години її замінює; float → NUMERIC.
Також `range(instrument_id, ts_from_ns=None, ts_to_ns=None) -> list[DqRow]` і `latest(instrument_id)`.
Скор поза [0, 1] відхиляє `CHECK`.

### `strategy.StrategyRepo(session)`
```python
async def create_version(name, rules_yaml, membership_yaml, *, created_by=None, activate=False) -> StrategyRow
async def activate(strategy_id) -> StrategyRow       # решта версій того ж name стають неактивними (один UPDATE)
get(id), get_by_hash(h), get_version(name, v), latest(name), get_active(name=None), list_versions(name),
list_names(), delete(id) -> bool                     # FK з run не дає видалити використану версію
def rules_hash(rules_yaml, membership_yaml) -> bytes # BLAKE2b-256(canonical_json([rules, membership]))
class StrategyConflictError(FuzzHelmError): existing_id, name, version   # той самий набір текстів уже є → 409
```
Версія дорівнює `max(version)+1` у межах name. Конкурентні створення серіалізує `pg_advisory_xact_lock(hashtextextended(name))`.
Тексти YAML зберігаються дослівно. Валідація (`fuzzy.rules.load_rulebase`) — завдання API до запису.

### `run.RunRepo(session)`
```python
async def create(run_id, *, kind, config, config_hash, dataset_hash, seed, engine, git_sha=None, strategy_id=None,
                 instrument_id=None, tf=None, ts_from_ns=None, ts_to_ns=None, started_at_ns=None) -> RunRow  # RUNNING
async def create_from_manifest(run_id, manifest: RunManifest-like, *, config, ...) -> RunRow
async def finish(run_id, status=DONE, *, journal_head_hash=None, equity_hash=None, error=None, finished_at_ns=None) -> RunRow
async def get(run_id); async def list(*, kind=None, status=None, limit=100)
async def find_by_identity(*, config_hash, dataset_hash, seed, engine, git_sha) -> RunRow | None  # git_sha через IS NOT DISTINCT FROM
async def put_metrics(run_id, metrics: Mapping[str, float | int | None]) -> int   # upsert у run_metric
async def get_metrics(run_id) -> dict[str, float | None]
```
`finish(..., RUNNING)` дає `ValueError`. Дубль ідентичності (з ненульовим `git_sha`) відхиляє `ux_run_identity` (`IntegrityError`, 23505).

### `decision.DecisionRepo(session)`
```python
DecisionRecord(run_id, instrument_id, open_time_ns, detector_outputs, memberships, fired_rules,
               t_in, r_in, v_in, agreement, kappa, u_raw, u_final, target_side, target_qty,
               binding_constraint, stop_price, tp_price, liq_price)
DecisionRecord.from_trace(trace | trace.to_dict(), *, run_id, instrument_id, target_side=None, target_qty=None,
                          binding_constraint=None, stop_price=None, tp_price=None, liq_price=None)
async def insert(rec) -> int
async def insert_many(recs) -> list[int]           # id у порядку входу (FK для ордерів)
async def get(decision_id) -> DecisionRow | None   # усе для /explain
async def get_at(run_id, instrument_id, open_time_ns); async def list_for_run(run_id, *, after_id=None, limit=500, nonzero_only=False)
async def find_by_rule(run_id, rule_id, *, limit=500)   # fired_rules @> '[{"rule_id": …}]', GIN jsonb_path_ops
async def count(run_id) -> int
```
`from_trace` бере з `DecisionTrace.to_dict()` такі поля: `inputs.T/R/V`, `agreement.A_g` (у колонку `agreement`), `kappa`, `u_raw`, `u_final`,
`detector_outputs[{name,group,s,c,weight,features}]`, `memberships`, `fired_rules[{rule_id,alpha,consequent,antecedent}]`.
NUMERIC-колонки округлює PostgreSQL (`t_in` NUMERIC(8,5): 0.6123456789 → 0.61235). У JSONB float зберігається точно.
`UNIQUE (run_id, instrument_id, open_time)` не дозволяє записати два рішення на один бар.

### `order.OrderRepo(session)`
`create(req: OrderRequest, *, decision_id: int, run_id=None, instrument_id=None, status=NEW, reject_code=None) -> int`,
`apply_ack(OrderAck) -> OrderRow`, `apply_fill(Fill) -> OrderRow`, `cancel(client_order_id, *, at_ns=None)`, `get(id)`,
`get_by_client_id(cid)`, `list_for_decision(decision_id)`, `list_for_run(run_id, *, limit=10000)`.
`apply_fill` атомарно (одним UPDATE) накопичує: `filled += q`, `avg = (avg·filled + p·q)/filled'`, `fee += f`;
статус стає FILLED, якщо `filled' ≥ qty`, інакше PARTIAL. `decision_id` — NOT NULL + FK (ST-01), `client_order_id` — UNIQUE.

### `position.PositionRepo(session)`
`open(*, run_id, instrument_id, side, qty, avg_entry, opened_at_ns, leverage=None, allocated_margin=None, stop_price=None,
tp_price=None, liq_price=None) -> int`, `update(position_id, **fields)` (лише `MUTABLE_NUMERIC`, для інших полів `ValueError`),
`close(position_id, *, closed_at_ns, exit_reason, realized_pnl=None, funding_paid=None)`, `get`, `open_positions(run_id, instrument_id=None)`,
`list_for_run(run_id)`.

### `risk.RiskEventRepo(session)`
`insert_many(records: Sequence[RiskEventRecord-like], *, run_id=None, instrument_ids: Mapping[symbol, id] | None=None) -> int`,
`insert(record, …) -> int`, `list_for_run(run_id, *, since_ns=None, limit=500, rule=None)` (новіші першими, `ix_risk_run_ts`),
`vetoes(run_id, *, limit=500)` (частковий `ix_risk_veto`), `transitions(run_id)` (`rule == "risk_state"`).
Приймає `risk.journal.RiskEventRecord` структурно (Protocol). `instrument` (symbol_canon) перетворюється в id за мапою.
Без мапи або для невідомого символу — `LookupError`. Міст із `RiskJournal(run_id, sink=BufferedSink())` такий самий, як для журналу.

### `equity.EquityRepo(session)`
`insert_many(run_id, points: Sequence[EquityPoint], *, use_copy=None) -> int` (COPY від 500 точок; дубль `(run_id, ts)` дає помилку PK),
`curve(run_id, ts_from_ns=None, ts_to_ns=None) -> list[EquityRow]`, `equity_series(run_id) -> (ts_ns, equity)`, `count(run_id)`.
`EquityPoint(ts_ns, equity, cash, unrealized, gross_exposure, leverage, drawdown, risk_state, kappa, var95, cvar95)`.
float-поля можна передавати напряму. Гроші (`equity, cash, unrealized, gross_exposure, var95, cvar95`) перед записом
квантуються `to_money18` (HALF_EVEN, 1e−18). Інваріант: після `ts, eq = await repo.equity_series(run_id)` маємо
`eq == [quantize_internal(x) for x in вхід]`, отже `equity_hash(eq, ts)` дорівнює хешу вхідної кривої для будь-яких
Decimal, включно з рівно-половинними хвостами, на яких округлення самого PostgreSQL розійшлося б
(тест `test_equity_hash_from_db_matches_on_half_ties`, обидва шляхи — COPY і VALUES).

### `user.UserRepo(session, context=PWD_CONTEXT)`
`create(login, password, role) -> UserRow`, `get(id)`, `get_by_login(login)`, `authenticate(login, password) -> UserRow | None`
(для невідомого логіна виконується `dummy_verify`, щоб час відповіді не залежав від причини), `set_password`, `set_role`, `list()`.
Модуль також містить `hash_password(pw, context=PWD_CONTEXT)` і `verify_password(pw, hash, context=PWD_CONTEXT)` для перевірки у шарі API.
`PWD_CONTEXT` — bcrypt із вартістю 12. Пароль > 72 байт UTF-8 дає `ValueError`, бо bcrypt мовчки обрізав би решту.
Роль поза `core.enums.Role` відкидає і `Role(...)` (`ValueError`), і `CHECK` СУБД.

### `audit.AuditRepo(session)`
`append(action, target, *, before=None, after=None, user_id=None, ip=None, ts_ns=None) -> int` (ts = `now()`, якщо `ts_ns` не передано),
`append_record(AuditRecord-like, *, user_id=None, ip=None)` (роль і логін актора пишуться в `after_json["_actor"]`),
`list(*, target=None, action=None, user_id=None, limit=200)` (новіші першими; `ip` повертається рядком).

### `common` (утиліти)
`ns_to_dt, ns_to_dt_opt, dt_to_ns, dt_to_ns_opt, to_numeric, to_money18, trim_decimal, as_bytes, enum_value, from_mapping,
split_unique_rounds, copy_records, supports_copy, chunks, BufferedSink[T], sqlstate(exc) -> str | None,
constraint_name(exc) -> str | None` і константи `SQLSTATE_UNIQUE_VIOLATION="23505"`, `…FOREIGN_KEY…="23503"`,
`…NOT_NULL…="23502"`, `…CHECK…="23514"`, `…INSUFFICIENT_PRIVILEGE="42501"` (для мапінгу помилок у HTTP-коди в API).

## Безпека (ревізія 0003)

* Роль `fuzzhelm_app` (NOLOGIN) має SELECT/INSERT/UPDATE/DELETE на робочі таблиці і USAGE/SELECT на послідовності.
  На **`event_journal` і `audit_log`** у неї є **лише INSERT/SELECT**, TRUNCATE не видано ніде.
* Застосунок отримує ці права одним із двох способів, і гарантії в них різні:
  * `make_engine(url, role="fuzzhelm_app")` (так працює `get_default_engine`): власник підключається і працює як роль.
    Це **захист від помилкових записів**: будь-який UPDATE/DELETE журналу з коду застосунку дає 42501. Але власник
    з'єднання може `SET ROLE fuzzhelm` назад, тож **від скомпрометованого застосунку це не захищає**;
  * окрема логін-роль `CREATE ROLE … LOGIN PASSWORD … IN ROLE fuzzhelm_app`, яку створює адміністратор, а не агент
    **[ЛЮДИНА]**. Це **межа безпеки**: `SET ROLE` до власника і DELETE журналу дають 42501.
  Обидві властивості перевіряє тест `test_set_role_is_not_a_security_boundary_documented` (ST-02).
* Тест `test_app_role_has_sufficient_grants_for_repositories` показує, що цих прав достатньо для COPY-upsert свічок,
  запису рішень і ордерів.

## Тести

* `tests/integration/test_candles.py`, `test_journal.py`, `test_trading.py`, `test_migrations.py`, `test_auth_audit.py`
  мають маркер `integration`. Вони потребують `docker compose -f docker-compose.test.yml up -d --wait` (порт 5443)
  і запускаються командою `uv run pytest -m integration tests/integration -q`. Якщо БД недосяжна, тести пропускаються (skip).
* `tests/integration/test_storage_offline.py` працює без БД і входить у звичайний `make test`.
* 6 назв групи M узято з §10 дослівно, решта тестів додаткові (зокрема `test_equity_hash_from_db_matches_on_half_ties`,
  `test_set_role_is_not_a_security_boundary_documented`; офлайн — `test_to_numeric_rejects_non_finite_decimal`,
  `test_to_money18_rounds_half_even_like_core_money`, `test_default_engine_runs_as_app_role`).
