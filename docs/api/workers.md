# API компонента `workers` (торговий і ingest-воркери, запис прогонів) — фактичний, хвиля 2

Автор: Андрій Жук, 2026. Файли: `src/fuzzhelm/workers/{persist,trading_worker,ingest_worker}.py`,
`scripts/{run_backtest,make_demo_scenario,demo_flash_crash}.py`, профілі `config/profiles/{replay,paper}.yaml`
(ключі `feed`, `params`), фікстура `fixtures/ws/scenarios/flash_crash.jsonl.gz` (+ `.meta.json`).
Вимоги — `docs/BRIEF.md` §1, §4.1, §6, §8.1, §12 (фази 6, 8, 9), §15, §17. Розходження — `docs/deviations.d/workers.md`
(W-01…W-27; W-20…W-27 — незалежне рецензування), журнал — `docs/journal.d/workers.md`.

**Головне.** Рушій (`backtest.engine`) чистий і БД не знає; воркери — межа: вони читають потік/БД, крутять той самий
`TradingLoop.step()`, що й бектест, і пишуть рядки DDL §6 1:1. Уся мережа — лише read-only хости з allowlist,
виконання — лише `PaperBroker` (MODE: PAPER).

---

## 1. `workers.persist` — адаптер «рушій → сховище»

```python
VAR_WINDOW = 500; VAR_ALPHA = 0.05; VAR_MIN_OBS = VAR_WINDOW (= 500, RF-03); GIT_DIRTY_METRIC = "git_dirty"

# VaR₉₅/CVaR₉₅ для equity_point (звітна метрика §5.13, у ГРОШАХ: E_t · оцінка risk.var на останніх min(t, W) дохідностях)
var_cvar_fractions(returns, *, window=500, alpha=0.05, min_obs=500, chunk=4096) -> (var[N+1], cvar[N+1])  # NaN до min_obs
var_cvar_money(equity: Sequence[Decimal], ...) -> (list[Decimal | None], list[Decimal | None])
RollingVar(window=500, alpha=0.05, min_obs=500).update(E_t) -> (var | None, cvar | None)   # live, ті самі числа

# паспорт прогону
Passport(run_id, kind, config, config_hash, dataset_hash, seed, engine, git_sha=None, git_dirty=None,
         instrument_id=None, tf=None, ts_from_ns=None, ts_to_ns=None, strategy_id=None, started_at_ns=None)
await create_run(session, Passport) -> RunRow          # RUNNING; git_dirty → run_metric "git_dirty" (1.0/0.0)
await finish_run(session, run_id, status=DONE, *, journal_head_hash, equity_hash, error, finished_at_ns, metrics,
                 ts_to_ns=None)                 # ts_to_ns — кінець вікна live-прогону (W-23)

# відображення записів рушія → рядки
decision_record(DecisionEntry, *, run_id, instrument_id) -> DecisionRecord   # + narrative_uk (колонка 0004)
order_row(OrderRecord, *, decision_id, run_id, instrument_id) -> dict        # накопичений стан виконань
position_row(PositionRecord, *, run_id, instrument_id) -> dict
equity_point(EquityPointRecord, var95, cvar95) -> EquityPoint

# пакетний запис готового BacktestResult (в транзакції викликача)
plan_backtest(result, *, run_id, instrument_id) -> BacktestPlan              # чисте, без БД
await write_plan(session, run_id, plan, *, instrument_id, symbol, journal=()) -> PersistCounts
await persist_backtest(session, run_id, result, *, instrument_id, symbol, journal=()) -> PersistCounts
PersistCounts(decisions, decisions_skipped, orders, orders_skipped, fills, positions, equity_points,
              risk_events, metrics, journal_entries, candles).as_dict()

# покроковий запис live/replay
LivePersister(run_id, instrument_id, symbol, *, var=RollingVar())
    await .write_step(session, StepResult, *, candle=None, journal=(), extra_risk=()) -> StepWrite
    await .write_out_of_band(session, *, risk=(), audits=(), journal=(), user_id=None) -> int
        # зняття HALTED між барами; audit_log воркера — з user_id автора запиту API (W-21)
    .decision_id(open_time_ns); .totals; .orders_skipped
await pg_notify(session, channel, payload)             # NOTIFY у транзакції викликача (подія лише після COMMIT)
```
Порядок `write_plan`: рішення (`insert_many`, id у порядку входу) → ордери з FK на рішення-джерело (`OrderRepo.insert_many`,
executemany частинами по 2 000) → позиції (`PositionRepo.insert_many`) → капітал (бінарний COPY) → `risk_event`
(точний множник у `payload.factor_exact` пише `RiskEventRepo`) → метрики → `event_journal`. `write_step` на кожен бар:
свічка (опційно, upsert) → рішення з трасуванням → нові ордери (стан «до виконань») → `apply_fill` кожного виконання
(СУБД накопичує avg/fee) → скасування/відхилення з `order_updates` → позиції (відкриття/закриття, MAE) → ризик →
точка капіталу з `RollingVar` → журнал. Ордер без рішення-джерела не пишеться (лічильник `orders_skipped`; ST-01).

## 2. `workers.trading_worker`

```bash
python -m fuzzhelm.workers.trading_worker --profile replay|paper [--speed X|inf] [--scenario NAME] [--session PATH]
       [--warmup-bars N] [--minutes M] [--linger S] [--param KEY=VALUE]... [--no-db] [--no-candles] [--no-notify]
       [--database-url URL]
```
Коди виходу: 0 — прогін DONE; 1 — не DONE; 2 — `WorkerError`/`ConfigValidationError` (немає інструмента, файлу, історії).
Вивід — JSON `SessionSummary.as_dict()` (run_id, bars, fills, closed_trades, final_state, halted_at_ns, transitions,
equity_first/last, equity_hash, journal_head, journal_entries, q_last, max_abs_u_final, candles_released, frames, gaps).

```python
WorkerProfile.load(name) -> WorkerProfile(name, kind, instrument, tf, seed, raw, params, session, history, speed,
                                          streams, heartbeat_timeout_s);  .backtest_config(**overrides)
TradingSession(instrument, cfg, *, seed, run_id, kind, feed, warmup_bars, sink=None, streams=("kline","markPrice"),
               heartbeat_timeout_s=10.0, pipeline_clock=None (FrameClock), wall_clock=None, backfill=None,
               src=Src.REPLAY, commands=None, gap_filler=None, limits_path=None)
    await .begin(meta)                 # перший запис журналу "session.start" + sink.on_start (паспорт RUNNING)
    .warm_up(history: Dataset)         # рівно warmup_bars барів СТРОГО до потоку; рішень немає (trade_start)
    await .run(items, *, stop=None)    # IngestPipeline.run над відфільтрованими потоками → PipelineReport
    await .on_candle(candle)           # стік конвеєра: команди → (добір розриву) → "candle" у журнал → крок → sink
    await .process_commands(); await .apply(ControlCommand)
    await .apply_release(*, actor, role, audit_id=None, user_id=None)   # аудит: настінний час, user_id, request_audit_id
    .reload_limits_if_changed(force=False) -> bool      # config/risk_limits.yaml → TradingLoop.apply_risk_limits
    .q() -> float | None               # Q поточної години конвеєра → StaleDataGuard
    .health() -> dict                  # подія `health` (source = trading_worker): header "MODE: PAPER · FEED: … · NO MAINNET KEYS · SEED … · Q=…"
    .summary(status) / await .finish(status, error) -> SessionSummary   # "session.end" + sink.on_finish
SessionSink (Protocol): on_start / on_step(StepOutput) / on_out_of_band / on_finish
MemorySink()                           # тести, --no-db: steps, journal, risk_events, audits, out_of_band, summary
DbSink(factory, *, instrument_id, symbol, passport, write_candles=True, publish=True)
ControlChannel(factory, dsn)           # LISTEN fuzzhelm_control + опитування audit_log (джерело правди)
await run_worker(WorkerOptions, *, stop=None, settings=None, sink=None) -> SessionSummary
load_warmup(factory, instrument_id, instrument, first_open_ns, n, *, fallback, fallback_name) -> (Dataset, source)
fixture_history_filler(path, instrument) / rest_history_filler(settings, instrument) -> GapFiller
offline_warmup_history(path, instrument, first_open_ns, n) -> Dataset
history_window(ds, first_open_ns, n); first_kline_open_ns(path); scenario_path(name); header_line(*, feed, seed, q)
session_dataset_hash(*, feed, source, source_sha256, scenario, streams, warmup_dataset_hash, started_ns) -> hex
```

**Потік replay:** `ReplayFeed(path, speed, streams=feed.streams)` (фільтр потоків до розбору JSON, W-01) → `IngestPipeline`
(FrameClock — віртуальний час кадрів, сторож тиші 10 с) → закриті свічки → `TradingLoop.on_candle(c, dq_score=Q,
last_data_ns=close − лаг)` (W-06). **Потік paper:** `BinanceWsClient` (два з'єднання, фільтр `feed.streams`) + REST-добір
прогалин (`RestBackfiller`) + добір розриву між прогрівом і першою свічкою (`rest_history_filler`). Прогрів (W-05):
`resolved_warmup()` = 523 бари з БД; бракує — фікстура `fixtures/rest/binance_klines.json.gz` (replay) або публічний REST
(paper), свічки upsert у `candle` (src = REST) і читаються знову з БД. Свічки потоку replay пишуться з src = REPLAY; свічки
стрес-сценарію — **ніколи** (синтетичні ціни не потрапляють у `candle`, W-15).

**Паспорт live-прогону** (W-02): `config_hash` = `BacktestConfig.config_hash` профілю (порівнянний із бектестом), `dataset_hash`
= `session_dataset_hash(...)` (джерело + sha256 файлу + сценарій + потоки + хеш прогріву + момент старту), `git_sha` + метрика
`git_dirty`, `seed`, `engine`; на `finish` — `journal_head_hash` (голова ланцюга: session.start, кожна свічка, заявки,
виконання, рішення із заявками, кожен `risk_event`, команди керування, session.end) і `equity_hash` живих барів.
FAILED-прогін хешів не отримує і журнал після кроку, що впав, не дописує (W-10).

**NOTIFY `fuzzhelm_live`** (у транзакції кроку; payload ≤ 7 900 байт): `run` {run_id, kind|status, header|final_state},
`candle` {run_id, symbol, open_time_ns, o,h,l,c,v}, `decision` {id, run_id, open_time_ns, u_raw, kappa, u_final, action,
verdict, target_side, target_qty, top_rule, alpha, orders}, `fill`, `rejection` {items: [{rule, observed, limit}]},
`risk` (перехід {state_from, state_to, event, drawdown, day_return} або команда {kind, outcome, actor, role, state_before,
state_after}), `equity` {equity, drawdown, risk_state, kappa, position_qty, var95, cvar95}, `health` (див. вище).
GET /market/health віддає останній `health` у полі `pipeline`, а останній знімок кожного видавця — у `pipelines`
(`trading_worker` із шапкою, `ingest_worker`; W-22). Подія, довша за межу NOTIFY, пропускається з попередженням у журнал —
запис кроку через неї не падає.

**Канал керування `fuzzhelm_control`** (протокол API §8): команда `killswitch.release` береться з `audit_log`
(`action = risk.killswitch.release`, id > останнього на старті сесії, `after.run_id` ∈ {null, run_id}); роль актора
перечитується з `app_user`; не-admin → відмова (`outcome = denied`, запис у журнал, стан не змінюється); admin →
`TradingLoop.release_halt(ADMIN)` → HALTED → COOLDOWN (перебазований пік), `risk_event` переходу з `actor`, два записи
`audit_log` (`killswitch.release`, `risk.release`: стан до/після), подія `risk`. `risk.limits.changed` (або зміна
mtime/розміру файлу, перевіряється щобару) → `TradingLoop.apply_risk_limits` (лише секції `limits` і `state_machine`;
W-18) + запис журналу `control.risk_limits`. Команди застосовуються між барами; після кінця потоку `--linger S` тримає
прогін RUNNING і приймає команди.

## 3. `workers.ingest_worker`

```bash
python -m fuzzhelm.workers.ingest_worker [--symbols BTCUSDT,ETHUSDT] [--minutes N] [--journal-kinds candle,trade,book,mark]
       [--no-notify] [--database-url URL]
```
```python
IngestOptions(symbols=("BTCUSDT","ETHUSDT"), minutes=None, database_url=None,
              journal_kinds=("candle","trade","book","mark"), publish=True)
await run_ingest(opts, *, settings=None, stop=None, feed=None, http_transport=None) -> dict   # feed/transport — для тестів
InstrumentSink(factory, instrument, instrument_id, *, journal, buffer, stats, journal_kinds, publish)
await pump(source, handle, *, stop, deadline_s=None, halted=None) -> "eof" | "stop" | "deadline"
    # зупинка скасовує лише очікування наступного запису, обробку запису — ніколи (W-20)
route(item, by_symbol) -> list[IngestPipeline]; flush_journal(factory, buffer, stats)
```
Один `IngestPipeline` на інструмент (кадр → конвеєр за префіксом імені потоку, записи керування — усім), REST-добір
прогалин (`RestBackfiller`). Стоки: закриті свічки → `candle` (src = WS) + пакет журналу + NOTIFY `candle`/`health`;
кожна зміна прогалини → `ingest_gap`; прийняті події → `event_journal` (власний run_id сесії інжесту без рядка `run`,
W-09; скидання на кожну свічку і кожні 2 000 записів; `ingest.end` містить причину зупинки); Q лише ЗАВЕРШЕНИХ годин → `dq_score` (незавершену не пишемо: її
пізніше повністю порахує `scheduler.hourly_dq`). Вихід — JSON зі статистикою (кадри, свічки, журнал, прогалини, Q годин,
лаг p95, статистика WS).

## 4. Скрипти

| скрипт | що робить |
|---|---|
| `scripts/run_backtest.py [--symbol BTCUSDT] [--profile backtest] [--database-url]` | перший повний прогін (фаза 6): вікно `data/dataset_window.json` з БД (хеш звіряється), фандинг, `BacktestConfig.from_profile`, seed профілю; ідентичний прогін уже є → звіт перебудовується з нього; інакше паспорт RUNNING → `run_backtest(journal_sink=…)` → `persist_backtest` → DONE; заміри часу — у `run_metric` `wall_*`; перевірка з БД `equity_hash` і ланцюга журналу; якщо ідентичний прогін уже є — рушій проганяється ще раз ПОТОЧНИМ кодом і звіт чесно каже, чи відтворюються `equity_hash` і голова журналу (`--no-verify` вимикає; W-24); FAILED з тією самою ідентичністю → зрозуміла відмова; `run.error` — `public_error`; `docs/figures/first_run_metrics.md` і `first_run_equity.png` будуються з рядків БД |
| `scripts/make_demo_scenario.py [--check] [--name] [--out-dir] [--profile ms:f,...]` | стрес-сценарій `flash_crash` (W-04), побайтово відтворюваний (`--check` будує в тимчасовій теці й порівнює сценарій і `.meta.json`, нічого не перезаписуючи; W-25) |
| `scripts/demo_flash_crash.py [--db --linger S --speed 30]` | демо акту 2:30–3:20: офлайн (у пам'яті) або через воркер у PostgreSQL з очікуванням POST /risk/killswitch/release |

## 5. Зміни в інших пакетах (цільові; журнал — `deviations.d/workers.md` W-12, W-13, W-16, W-22, W-23, W-27)

* `backtest/engine.py`: `run_backtest(..., journal_sink=None)`; `run_order_ids(seed, run_id)` (client_order_id збереженого
  прогону — з run_id у просторі імен, W-10); `TradingLoop.release_halt` більше не губить запис переходу між барами
  (`take_pending_risk_events()`; незабрані записи йдуть у StepResult наступного бару); `TradingLoop.apply_risk_limits(tree)`.
* `ingest/replay.py`: `iter_items(path, streams=None)`, `stream_kind(stream)`, `STREAM_KINDS`, `ReplayFeed(..., streams=None)`.
* `storage/repositories/order.py`: `order_values(...)`, `OrderRepo.insert_many(rows)`; `position.py`: `position_values(...)`,
  `PositionRepo.insert_many(rows)`.
* `api/backtest_runner.py`: `DbBacktestRunner` збирає журнал рушія і пише результат через `workers.persist.persist_backtest`
  (журнал прогону + VaR/CVaR; `plan_persistence`/`persist_backtest_result` лишились для сумісності).
* `config/profiles/replay.yaml` (`feed`, `params.u_enter = 0.20`), `config/profiles/paper.yaml` (`feed.streams`).
* рецензування: `api/live.py` (`LiveHub.last_by_source(kind)`), `api/schemas.py` + `api/routers/market.py`
  (`HealthOut.pipelines`), `storage/repositories/run.py` (`RunRepo.finish(..., ts_to_ns=None)`), `api/backtest_runner.py`
  (`run_metric.git_dirty` для POST /backtests).

## 6. Тести

| файл | що перевіряє |
|---|---|
| `tests/e2e/test_replay_e2e.py` (офлайн, за замовчуванням) | **`test_replay_session_end_to_end`** (прогрів зі справжньої історії → 45-хв сесія → ≥1 угода, 0 LookaheadError, MARKET виконано рівно на open_{t+1} кроку наступного бару, тотожність капіталу на кожному барі незалежною реконструкцією з сирих виконань/фандингу, fired_rules у кожної угоди, DONE, цілий журнал); шапка стану; сценарій змінює лише кадри від t₀; flash_crash → HALTED без людини (стоп виконано за open розриву); засувка до admin: «continue», analyst/operator — відмова, admin → COOLDOWN + аудит |
| `tests/unit/test_workers.py` | VaR/CVaR (пакетний = покроковий = risk.var), план запису бектесту + ланцюг журналу, гаряча заміна лімітів (рушій і файл), профілі/сценарії/шапка, dataset_hash сесії, прогрів, різні client_order_id прогонів з тим самим seed; `pump` ingest-воркера: зупинка не рве запис, що обробляється, скасовує лише очікування наступного (stop/deadline/eof/помилка) |
| `tests/integration/test_worker_db.py` (`integration`, БД 5443; очікування NOTIFY — подіями, без sleep) | інтеграційний варіант e2e з повним паспортом (зокрема `ts_to`), NOTIFY, два прогони поспіль; flash_crash → HALTED → підроблена команда аналітика відхилена → POST /risk/killswitch/release (admin, справжній API) → COOLDOWN, аудит воркера з user_id адміністратора і посиланням на запит; пакетний запис бектесту (хеші з БД = паспорт); ingest-воркер на записаній 4-хв сесії; `DbBacktestRunner` пише журнал і VaR |
