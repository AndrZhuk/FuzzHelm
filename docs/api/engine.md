# API модуля `engine` — торговий цикл, рушій бектесту, воркери сітки (фактичний, хвиля 2)

Автор: Андрій Жук, 2026. Файли: `src/fuzzhelm/backtest/{dataset,engine,runner}.py`, `config/engine.yaml`,
ключі рушія в `config/profiles/*.yaml`. Вимоги — `docs/BRIEF.md` §4.1, §4.2, §5.8–5.16, §6, §10 K;
розходження — `docs/deviations.d/engine.md` (ENG-01…ENG-21).

**Головне.** Один покроковий код рішень `TradingLoop.step()` працює і в бектесті (`run_backtest`), і в
live/replay (`TradingLoop.on_candle`), і в клітинках сітки (`runner.run_cell`). Рушій **не пише в БД**:
кожен крок повертає `StepResult` із записами у формі рядків DDL (§6), а воркер/API пише їх 1:1.
Домени типів: ціни/кількості/гроші — `Decimal` (конвертація лише `sizing.convert.*`, `core.money.dec`,
`.item()`), ознаки/ядро/σ — `float`.

---

## 1. `backtest.dataset`

```python
TF_NS = {"1m": 60e9, "5m": …, "15m": …, "1h": …}      # ns на бар
BARS_PER_YEAR = {"1m": 525_600, …}                       # 24/7
FLOAT_COLUMNS = ("o","h","l","c","v","qv"); INT_COLUMNS = ("t_ns","n")
HASH_COLUMNS = ("t_ns","o","h","l","c","v")              # = storage CandleArrays.columns()

@dataclass(frozen=True, eq=False)
class Dataset:
    instrument: Instrument; tf: str
    t_ns, n: int64[N]; o, h, l, c, v, qv: float64[N]
    funding_t_ns: int64[M] | None = None; funding_rate: float64[M] | None = None; source: str = ""
    # валідація: однакові довжини, t_ns строго зростає, ціни > 0, l ≤ min(o,c) ≤ max(o,c) ≤ h, обсяги ≥ 0

    from_arrays(instrument, *, t_ns, o, h, l, c, v, qv=None, n=None, tf="1m",
                funding_t_ns=None, funding_rate=None, source="arrays") -> Dataset
    from_candles(candles: Sequence[Candle], instrument, *, funding_t_ns=None, funding_rate=None) -> Dataset
    from_candle_arrays(arrays, instrument, *, tf="1m", funding: Sequence[FundingRate] | None = None,
                       source="db") -> Dataset          # arrays = storage.CandleArrays (або з тими ж атрибутами)
    with_funding(rates: Sequence[FundingRate]) -> Dataset   # колонки ingest.funding.funding_columns,
                                                            # вікно [t₀ − 1 доба, t_last + 1 хв] (як у slice)
    __len__; tf_ns; bars_per_year; close_time_ns(i)     # close = open + tf − 1 мс (конвенція Binance)
    columns() -> dict                                   # HASH_COLUMNS (+ funding_t_ns, funding_rate)
    dataset_hash: str                                   # backtest.manifest.dataset_hash(columns())
    bar(i) -> features.convert.Bar;  dec_bar(i) -> execution.paper_broker.DecBar;  dec_bars: list[DecBar]
    slice(start, stop) -> Dataset                       # копії масивів + фандинг за вікном часу
    feed() -> BarFeed                                   # послідовна подача через LookaheadGuard
    to_payload() -> dict;  from_payload(payload) -> Dataset   # pickle-вантаж для ProcessPoolExecutor

class BarFeed:  # for bar, dec_bar, close_ns in ds.feed(): …
    cursor: int; history(column) -> float64[cursor+1]; peek(i)   # i > cursor → LookaheadError

dec_bar_of(bar: Bar, instrument) -> DecBar             # price_to_decimal(x, tick): точно для tick-кратних цін
load_klines_json(path, instrument, *, tf="1m", funding_t_ns=None, funding_rate=None) -> Dataset
load_exchange_instrument(symbol="BTCUSDT", path=fixtures/rest/exchange_info.json) -> Instrument
load_fixture_dataset(symbol="BTCUSDT") -> Dataset     # 3000 реальних 1m-барів (fixtures/rest)
decimal_ohlc(ds, i) -> (o, h, l, c)
```

Інваріанти (тести): `Dataset.from_candles(candles)`, `load_klines_json(...)`, `from_payload(to_payload())` і
`from_candle_arrays(CandleRepo.load_arrays(...))` дають **той самий** `dataset_hash`, що й
`manifest.dataset_hash(CandleArrays.columns())` (= `data/dataset_window.json` для свічок без фандингу);
ряд фандингу входить у хеш (інша ставка — інший датасет). Кеш Decimal-барів процесу ключується
`(dataset_hash, symbol_canon, str(tick_size))` (ENG-15).

**Реальне вікно з БД** (приклад, лише читання):
```python
async with get_default_factory()() as s:
    row = await InstrumentRepo(s).get_by_canon("BTC-USDT-PERP")
    arr = await CandleRepo(s).load_arrays(row.id, "1m", lo_ns, hi_ns)
inst = row.to_dto()
ds = Dataset.from_candle_arrays(arr, inst, funding=load_funding_json("data/funding_BTCUSDT.json", inst).rates)
```
Те саме робить `backtest.runner.load_db_window(symbol)` (звіряє хеш свічок з `data/dataset_window.json`).

---

## 2. `backtest.engine`

### 2.1 Конфігурація — `BacktestConfig` (frozen dataclass)

```python
BacktestConfig(engine=None, cost_mode=None, initial_equity: Decimal | None = None,
               n_atr: int | None = None, chi=None, u_enter=None, u_exit=None, rho_base=None, lam=None,
               tp_multiple=None, detectors: tuple[str, ...] | None = None,
               detector_weights: tuple[tuple[str, float], ...] = (),
               linear_weights: tuple[tuple[str, float], ...] | None = None, warmup_bars: int | None = None,
               funding_fallback_rate: Decimal | None = None, funding_match_ns: int | None = None,
               record_traces: "all" | "trades" | "none" | None = None, check_invariants: bool | None = None,
               invariant_tolerance: Decimal | None = None, trees: Mapping[str, Any] = {})
```
`None` → значення з конфігураційних дерев `trees` (`engine, risk_limits, detectors, cost_model, membership,
rules`; відсутні читаються з `config/*.yaml` у `__post_init__`, далі конфіг герметичний — воркер файлів не читає).

| поле | джерело за замовчуванням | що перекриває |
|---|---|---|
| `engine` | `engine.yaml: engine` (`mamdani`) | `mamdani` \| `linear` (ablation) |
| `cost_mode` | `engine.yaml: cost_mode` (`full`) | `cost_model.mode`: `zero` \| `sqrt_impact` \| `full` |
| `n_atr` | `detectors.yaml: features.n_atr` (14) | період ATR Уайлдера (сітка) |
| `chi`, `rho_base`, `lam` | `risk_limits.yaml: sizing.chi_atr/rho_base/ewma_lambda` | сітка |
| `u_enter`, `u_exit` | `risk_limits.yaml: hysteresis.enter/exit` | тригер Шмітта (сітка: `u_enter`) |
| `tp_multiple` | `engine.yaml: take_profit.multiple_of_stop` (2.0) | TP = P_ref ± m·Δ_stop (ENG-01) |
| `detectors`, `detector_weights` | усі 6; ваги з `detectors.yaml` | підмножина / ω_k (ablation) |
| `warmup_bars` | `engine.yaml: warmup.bars` (null → 523) | барів до першого рішення |
| `funding_*` | `engine.yaml: funding` (0.0001, ±60 с) | ENG-07 |
| `record_traces` | `engine.yaml` (`trades`); профілі перекривають | обсяг записів (не входить у хеш) |
| `check_invariants` | `false` (тести: `true`) | тотожність обліку щобару (не входить у хеш) |

Методи: `from_profile(profile="backtest" | dict, **overrides)` (ключі рушія профілю: `engine, cost_mode,
record_traces, check_invariants, initial_equity`), `from_dict(d)` / `to_dict()` (pickle/JSON; `from_dict(to_dict())
== cfg`), `with_params(**fields)` (клітинка сітки), `identity_dict()` (вхід `config_hash`: без перемикачів запису,
дерева перекриті параметрами), `config_hash: str`, `resolved_trees()`, `risk_config()`, `feature_params()`,
`build_engine() -> InferenceEngine` (Мамдані кешується в процесі за вмістом дерев, ≤ 16), `resolved_warmup()`.
Помилки: невідомий `engine`/`cost_mode`/`record_traces`/детектор, `tp_multiple ≤ 0`, `initial_equity ≤ 0` → `ValueError`.
Дерево `engine` (config/engine.yaml) валідується схемою `EngineTreeCfg` (pydantic, `extra="forbid"`):
`validate_engine_tree(tree) -> EngineTreeCfg`, помилка → `ConfigValidationError(path="engine.take_profit.multiple_of_stop")`
(невідомий ключ, `multiple_of_stop ≤ 0`, `sigma_base.method ≠ warmup_median`, `warmup.bars < 1`, …).

### 2.2 `TradingLoop` — один крок рішення/ризику/виконання

```python
TradingLoop(instrument: Instrument, cfg: BacktestConfig | None = None, *, seed: int, tf="1m",
            clock: ManualClock | None = None, ids: IdGenerator | None = None,   # дефолт SeededIdGenerator(seed)
            venue: PaperBroker | None = None, keep_records=True, trade_start=0, run_id: UUID | None = None,
            event_journal: EventJournal | None = None, audit_sink: AuditSink | None = None)

loop.step(bar: Bar, close_ns: int, *, dbar: DecBar | None = None, dq_score: Decimal = 1,
          last_data_ns: int | None = None) -> StepResult          # бектест/сітка (бари з Dataset.feed())
loop.on_candle(candle: Candle, *, dq_score=1, last_data_ns=None) -> StepResult
          # live/replay: лише закриті свічки свого інструмента (інакше ValueError); той самий step()
loop.set_funding_series(t_ns, rates)       # історичні ставки (fundingTime, rate); None → fallback
loop.release_halt(actor_role, *, actor=None) -> Transition | None   # лише ADMIN (PermissionDeniedError)
loop.refresh_order_statuses()             # фінальні статуси sim_order з брокера (кінець прогону)
# стан: index, decide_from, warmup_bars, sigma_base, open_position, halted_at, window (BarWindow),
#       core (DecisionCore), fsm (RiskStateMachine), guard (RiskGuard), broker (PaperBroker), router,
#       portfolio, cost_model, risk_cfg, journal (RiskJournal)
# накопичувачі (keep_records=True): equity, equity_ts, position_series, equity_points, decisions, orders,
#       fills, funding, positions (закриті), risk_events, transitions
```

**Порядок усередині `step` для закритого бару t** (§4.1; час — `ManualClock`, що йде за барами):
0. засувка: якщо автомат у HALTED **або** kill-switch спрацював (зокрема між барами — оператором/API), заявки на
   вхід/розворот, що чекають `open_t`, і стопи, подані разом із ними, скасовуються (`PaperBroker.cancel`);
   стоп відкритої позиції лишається, flatten-all подається на закритті t (ENG-20);
1. ставка фандингу для моментів 00/08/16 у `(open_{t−1}, open_t]` (ряд або fallback; запис ставки — не пізніше
   `close_t`) → `router.on_bar(bar_t)`: фандинг на позицію до виконань, MARKET-заявки закриття t−1 **за `open_t`**,
   потім STOP / TP / ліквідація в межах `[l_t, h_t]` (песимістичний шлях брокера);
2. облік: фандинг і виконання → `Portfolio`; записи позицій (відкриття/закриття, `exit_reason`, MAE);
   `order_updates`; ціна ліквідації фактичної позиції → брокеру (після кожного виконання/фандингу);
3. оцінка за `c_t` → `equity`; `check_invariants`: тотожність `cash + Σq·p − Σfees == equity` (≤ 1e−9),
   позиція брокера == позиція обліку, за засувки на початку бару (HALTED або kill-switch) |позиція| після
   виконань не більша, ніж до них — інакше `EngineInvariantError`;
4. `FeaturePipeline.update(bar_t)` → `BarWindow`; `VolTarget.update(r_t)`; σ_base (медіана σ_ann прогріву,
   ENG-02) → `vol_ratio`; `RiskStateMachine.update(E_t)` (переходи → `risk_event` з `rule="risk_state"`);
5. якщо `index ≥ decide_from` — рішення на закритті t (§2.3), заявки з `ts_created = close_t`.

Бар t+1 на кроці t недосяжний: цикл отримує бари по одному (у бектесті — через `LookaheadGuard`).

### 2.3 Рішення (`_decide`) — «м'яке пропонує, жорстке вирішує»

`DecisionCore.intent(window)` (без трасування, `infer_u`) або `decide(window)` (повний `DecisionTrace`) →
`u_final = clip(κ·u_raw)` → `HysteresisGate` (стан перед кожним рішенням = фактичний бік позиції, ENG-05) →
бажаний бік (0 у HALTED/kill-switch). Намір = бажаний бік ≠ поточний. Лише на намір:
`PositionSizer.size(u_final, E_t, c_t, ATR_t, s_t, κ_mode = fsm.kappa_mode_float, step, minNotional)` →
ціль `±qty` → `RiskGuard.evaluate(RiskContext.from_snapshot(fsm.snapshot, …, dq_score, last_data_ns,
risk_state))` (6 правил + `risk_mode`, кожна перевірка → `risk_event`) → дозволена ціль → заявки:

| дія `action` | коли | заявки |
|---|---|---|
| `enter` | пласка → новий бік | MARKET (не reduce-only) + reduce-only STOP_MARKET на `c_t ∓ Δ_stop` + рівень TP `c_t ± m·Δ_stop` (ENG-03) |
| `flip` | бік → протилежний | одна MARKET на `|cur| + |new|` + новий стоп/TP (TP з `side` нового боку, EXE-03) |
| `exit` | гістерезис → FLAT (`SIGNAL`) або розворот, новий бік якого ланцюг відхилив цілком (`RISK_VETO`) | reduce-only MARKET |
| `flatten` | `GuardResult.flatten_all` (HALTED / halt-правило / kill-switch) | reduce-only MARKET, `exit_reason = HALT` |
| `hold` / `none` | немає наміру, або сайзер сам відмовив у вході (`BELOW_MIN_NOTIONAL`: ризик не оцінюється, ENG-12) | — |

Розмір фіксується на вході (без доторговування, ENG-04). `Δ_stop = χ·ATR_t` (`SizingResult.stop_distance`).
Стиснута ланцюгом ціль, нижча за minNotional, не відкривається (`trace.risk.note = BELOW_MIN_NOTIONAL`).

### 2.4 Записи (форма рядків DDL §6; рушій їх не пише в БД)

```python
StepResult(index, open_time_ns, close_time_ns, equity, position_qty, risk_state, kappa_mode, decided,
           fills: tuple[Fill], funding: tuple[FundingCharge], decision: DecisionEntry | None,
           orders: tuple[OrderRecord],          # заявки, створені на цьому кроці (зокрема синтетичні TP/liq)
           opened_positions, closed_positions: tuple[PositionRecord], risk_events: tuple[RiskEventRecord],
           transition: Transition | None, equity_point: EquityPointRecord | None,
           order_updates: tuple[OrderRecord] = ())   # заявки попередніх кроків, стан яких змінився
```

| запис | таблиця | відображення |
|---|---|---|
| `DecisionEntry(open_time_ns, decided_at_ns, u_raw, kappa, u_final, gate_side, current_qty, requested_qty, target_side, target_qty, binding_constraint, stop_price, tp_price, liq_price, action, verdict, order_ids, trace)` | `decision` | `t_in/r_in/v_in, agreement, detector_outputs, memberships, fired_rules` — з `trace` (`DecisionTrace.to_dict()`); `trace.sizing` = `SizingResult.to_dict()` (`q_atr, q_vt, q_lev, s_t, sigma_ann, binding_constraint, kappa_mode, qty, …`); `trace.risk` = `{state, kappa_mode, evaluated, verdict, records[7], approved_qty, vetoes, note?}`; `liq_price` — оцінка для дозволеної цілі (крос-маржа, `W = E_t`, `P = c_t`) |
| `OrderRecord(request, decision_ns, role, status, reject_code, venue_order_id, ts_created_ns, filled_qty, avg_fill_price, fee, slippage_bps, liquidity, ts_filled_ns, intent_reason)` | `sim_order` | `decision_id` ← рішення з `open_time_ns = decision_ns` (синтетичні TP/ліквідація — рішення, що відкрило позицію); `role ∈ {enter, exit, flip, flatten, stop, tp, liquidation}` |
| `Fill` (core.dto) | `sim_order` (агрегати) | уже враховано в `OrderRecord` |
| `PositionRecord(instrument, side, opened_at_ns, opening_decision_ns, avg_entry, qty, leverage, allocated_margin, stop_price, tp_price, liq_price, closed_at_ns, exit_reason, realized_pnl, funding_paid, fees, max_adverse_excursion, exit_price)` | `position` | `qty` — максимальний `|q|` угоди; `realized_pnl` = валовий − комісії (нетто = `realized_pnl − funding_paid`); `liq_price` — від фактичної ціни входу |
| `RiskEventRecord` (risk.journal) | `risk_event` | `.to_row()`; `ts_ns → ts`, `instrument → instrument_id` відображає storage |
| `EquityPointRecord(ts_ns, equity, cash, unrealized, gross_exposure, leverage, drawdown, risk_state, kappa, position_qty, var95=None, cvar95=None)` | `equity_point` | `cash = Portfolio.cash` ⇒ рядки самі задовольняють тотожність; `kappa` = κ_mode автомата; VaR/CVaR — звітна метрика, рушій не рахує |

`record_traces`: `all` — кожне рішення з повним `DecisionTrace` + `equity_point` щобару (paper/replay, /explain);
`trades` — `DecisionTrace` лише в рішень із заявками (бектест; відхилені наміри лишаються в `risk_event`);
`none` — жодних трасувань і `equity_point`, лише легкі `DecisionEntry` рішень із заявками (клітинки сітки).
Режим на криву капіталу не впливає (тест), і в `config_hash` не входить.

### 2.5 `run_backtest`

```python
run_backtest(dataset: Dataset, cfg: BacktestConfig | None = None, seed: int = 0, *, eval_start: int = 0,
             git: bool = False, kind: RunKind | str = RunKind.BACKTEST, run_id: UUID | None = None,
             hash_equity: bool = True, on_step: Callable[[StepResult], None] | None = None) -> BacktestResult

BacktestResult(config, seed, manifest: RunManifest, metrics: dict[str, float],   # 17 метрик METRIC_NAMES
               extras: dict[str, float],   # sr_period, skew, kurt, psr, n_obs, n_fills, traded_notional, halted
               equity: list[Decimal], equity_ts, position_series, equity_points, trades: list[ClosedTrade],
               positions: list[PositionRecord],  # закриті + остання відкрита
               orders, fills, funding, decisions, risk_events, transitions, warmup_bars,
               eval_start,  # перший бар вікна оцінки = decide_from
               halted_at: int | None, final_state: RiskState, killswitch_tripped: bool)
   .equity_hash -> str        # = manifest.equity_hash (ValueError, якщо hash_equity=False)
deterministic_run_id(cfg_hash, ds_hash, seed) -> UUID     # дефолтний run_id (той самий прогін → той самий id)
EngineInvariantError(AssertionError)
```

`manifest`: `config_hash = cfg.config_hash`, `dataset_hash = dataset.dataset_hash`, `seed`, `engine`,
`journal_head_hash` (хеш-ланцюг `EventJournal`: заявки, виконання, рішення із заявками, кожен `risk_event`;
`None` при `record_traces="none"`), `equity_hash` (SHA-256 над `(close_ts, E)` кожного бару), `git_sha` при
`git=True`. Метрики рахуються з бару першого можливого рішення (`decide_from`), `periods_per_year` —
`BARS_PER_YEAR[tf]`; `trades` — лише закриті угоди (відкрита на кінець позиція — у кривій, не в угодах).

Приклад:
```python
ds = load_fixture_dataset()
res = run_backtest(ds, BacktestConfig.from_profile("backtest"), seed=20260918)
res.metrics["sharpe"], res.manifest.identity(), res.equity_hash
```

---

## 3. `backtest.runner` — воркери сітки і walk-forward (чисті функції, без БД і event loop)

```python
run_cell(params: {n_atr, chi, u_enter, rho_base, lam}, arrays: Dataset.to_payload(), seed, *,
         config: dict | None = None, eval_start=0, equity_hash=False) -> dict[str, float | str]
         # 17 метрик + extras (+ "equity_hash"); record_traces примусово "none"; невідомий параметр → ValueError
cell_task(task: dict) -> dict                      # точка входу ProcessPoolExecutor (pickle, spawn)
grid_tasks(dataset, cells, seed, *, config=None, eval_start=0, equity_hash=False) -> list[dict]
run_grid(dataset, cells=None (= backtest.grid.make_grid(): 108), seed=0, *, workers=0, config=None,
         eval_start=0, equity_hash=False) -> list[dict]          # порядок = cells, від workers не залежить
select_cell(metrics, rule="sharpe") -> int         # argmax SR (NaN — найгірше) → менший оборот → менший індекс
run_walkforward(dataset, folds=None (= walkforward.folds_from_profile, embargo = 2·max_lookback),
                cells=None, seed=0, *, workers=0, config=None, oos_all_cells=False,
                selection="sharpe") -> WalkForwardResult
FoldReport(fold, is_metrics, selected, params, is_selected, oos_selected, is_pareto,
           oos_metrics=None, oos_pareto=None)
WalkForwardResult(cells, folds, seed, config_hash, warmup_bars, selection).summary() -> dict
bench_loop(dataset, cfg=None, seed=0, *, repeats=5) -> {bars, repeats, median_s, us_per_bar, min_us_per_bar,
                                                        max_us_per_bar}
async load_db_window(symbol) -> Dataset            # лише CLI заміру (читає БД, ліниві імпорти)
```

Walk-forward: IS-прогін — `dataset.slice(is_start, is_end)` з прогрівом усередині IS; OOS-прогін —
`slice(oos_start − W, oos_end)` з `eval_start = W` (W = прогрів = 523 бари), тобто прогрів OOS лежить **усередині
embargo** (якщо `E < W` → `ValueError` до будь-якого прогону). Для 45 днів і профілю 15/5/5 × 6:
`max_lookback = 523`, `E = 1046` бар, IS = 20 554 бари, OOS = 7 200 барів на фолд. Усі клітинки однієї сітки
мають той самий seed (спільні випадкові числа моделі витрат).

---

## 4. Швидкодія (виміряно; машина розробника, 8 ядер, паралельно працювали інші агенти — вказано load average)

| замір | команда | результат |
|---|---|---|
| повний цикл, `record_traces=none`, Мамдані, 3000 барів фікстури | `uv run python -m fuzzhelm.backtest.runner --repeats 7` | **44.5 мкс/бар** (медіана 7 прогонів після розігріву; min 44.4, max 48.1; load ≈ 7). Під load ≈ 15 той самий замір давав 64.4 мкс/бар |
| те саме, `LinearVoteEngine` | `… --repeats 7 --engine linear` | 33.7 мкс/бар (min 32.8; load ≈ 7) |
| одна клітинка на 45-денному вікні BTCUSDT (64 800 барів з БД + фандинг) | `FUZZHELM_DATABASE_URL=postgresql+asyncpg://fuzzhelm:fuzzhelm@localhost:5442/fuzzhelm uv run python -m fuzzhelm.backtest.runner --db BTCUSDT --repeats 3 --grid-workers 8` | 3.70 с = **57.1 мкс/бар** (медіана 3; min 50.0; load ≈ 6.5) → екстраполяція 108 клітинок на 1 ядрі ≈ 400 с |
| уся сітка 108 клітинок × 64 800 барів BTCUSDT, `ProcessPoolExecutor(8)` | та сама команда | **123.4 с** стіни (load зріс до ≈ 21 — інші агенти + 8 воркерів); угод на клітинку min/медіана/max = 108/144/172 |
| повний прогін з трасуванням угод і тотожністю обліку на кожному барі | `… --db BTCUSDT --report` (і `ETHUSDT`) | 4.81 с (BTC) / 10.84 с (ETH) під load ≈ 17; порушень тотожності — 0 на 64 800 барах кожного символу |

Незалежний повторний замір рецензента (2026-09-19, ті самі команди): фікстура — 47.8 мкс/бар Мамдані (min 46.2) і
35.8 мкс/бар лінійне (min 34.1) при load ≈ 5; вікно з БД — 54.1 мкс/бар BTCUSDT (медіана 3, min 51.0) і 50.6 мкс/бар
ETHUSDT (min 50.5) при load ≈ 4; сітка 108 × 64 800 BTCUSDT на 8 воркерах — 96.7 с стіни (load до ≈ 16; замір до
правки ENG-20, яка прогонів без засувки не змінює), угод на клітинку 108/144/172 (збігається); `--report` після правки:
BTC 113 угод, total_return −0.059926, equity_hash `8a82f814e93855ce…` (той самий, що й до правки); ETH 137 угод,
−0.059751; порушень тотожності — 0 на кожному з 64 800 барів обох символів; фінальний стан обох — COOLDOWN.

Профіль (cProfile, фікстура, частки часу `step`): ядро рішень 48 % (6 детекторів 22 %, `MamdaniEngine.infer_u`
16.5 %, κ 3.8 %, консенсус 2.5 %), `FeaturePipeline.update` 17 %, подача барів через `LookaheadGuard` 7 %,
автомат ризику 4 %, `RiskGuard.evaluate` 3 % (лише на намір: 435 з 9000 барів), брокер 3 %, `Portfolio.mark` 2 %.
Уже зроблені оптимізації: швидкий шлях `DecisionCore.intent` → `infer_u` без `DecisionTrace` (трасування
будується лише для рішень, що зберігаються), `OrderRequest.model_construct` для вже провалідованих заявок,
ризик-ланцюг і сайзер — лише на намір заявки, Decimal-бари і рушій Мамдані кешуються в процесі воркера.
Висновок: 108 клітинок на 45 днях здійсненні (≈ 400 с на одному ядрі за екстраполяцією; 123 с стіни на 8 воркерах
під навантаженням машини). Замір `S(p)` за Амдалом — окремий експеримент (`backtest.parallel`). Кожна задача сітки
несе свою копію масивів (≈ 4 МБ pickle на 64 800 барів) — це і є частка серіалізації, яку має показати аналіз Амдала.

---

## 5. Тести (`tests/unit/test_backtest_engine.py`, `tests/unit/test_trading_loop.py`, `tests/property/test_engine_property.py`)

Брифінг, група K: `test_shuffling_future_bars_does_not_change_past_decisions`,
`test_backtest_deterministic_same_seed_same_equity_sha256`, `test_different_seed_changes_equity` (+ контроль:
без шуму seed на криву не впливає), `test_zero_signal_yields_flat_equity` [mamdani, linear],
`test_grid_results_independent_of_worker_count` (реальні клітинки, воркери 1 vs 2 vs 4 + зворотний порядок).
Додатково: тотожність обліку на кожному барі (незалежна реконструкція з сирих потоків), кожна угода має
непорожні `fired_rules` у рішенні-відкритті, `LookaheadGuard`/префікс-інваріантність, flash-crash → HALTED без
людини + засувка, HALTED ⇒ нуль нової експозиції і flatten-all, kill-switch між барами скасовує вхід/розворот у
черзі (стоп відкритої позиції лишається), зняття HALTED лише admin → COOLDOWN
reduce-only, вето розвороту → `RISK_VETO`, 7 записів ризику на кожен намір, фандинг з ряду ставок, live-шлях ==
бектест-шлях, TP → рядок `sim_order`, `order_updates`, швидкий шлях == трасоване рішення, хеш-контракт
`from_candle_arrays`/фандингу, валідація `Dataset` і схеми `engine.yaml`, ключ кешу Decimal-барів, відмова сайзера не йде в ризик-ланцюг; property: інваріанти
для будь-якої послідовності намірів/збоїв якості/спрацювань засувки і незалежність минулих рішень від будь-яких
майбутніх барів.
