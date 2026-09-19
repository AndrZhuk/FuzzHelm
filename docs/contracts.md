# FuzzHelm — контракти між модулями (хвиля 0)

Цей документ фіксує **публічні інтерфейси**, на які спираються модулі, що пишуться паралельно.
Первинне джерело вимог — `docs/BRIEF.md` (копія брифінгу). Якщо контракт тут суперечить брифінгу —
брифінг головніший, а розходження фіксується у `docs/deviations.d/<модуль>.md`.

Кожен модуль після реалізації описує свій **фактичний** публічний API у `docs/api/<модуль>.md`
(сигнатури, інваріанти, приклад виклику). Хвиля 2 (інтеграція) читає саме ці файли.

---

## 0. Загальні правила для всіх модулів

1. Python 3.12, `from __future__ import annotations`, типи скрізь. Стиль — як у `src/fuzzhelm/core/*`
   (заголовний докстрінг файлу: найменування, призначення, автор «Андрій Жук, 2026»; коментарі —
   українською, лише там, де вони пояснюють *чому* / формулу; ідентифікатори — англійською).
2. **Межа детермінізму** (`tests/arch/test_determinism_boundary.py`): у `core, features, detectors,
   fuzzy, decision, risk, sizing` заборонені `datetime.now/utcnow`, `time.time*`, `time.monotonic`,
   модуль `random`, будь-який атрибут `.random` (включно з `np.random`), `uuid4`, імпорт `fuzzhelm.infra`.
   Час — лише через порт `Clock`, ідентифікатори — через `IdGenerator` (`core/clock.py`).
3. **Межа типів** (`tests/arch/test_decimal_float_boundary.py`):
   * float-домен `features, detectors, fuzzy, decision, regimes` не імпортує `decimal` взагалі
     (виняток — `features/convert.py`);
   * Decimal-домен `core, sizing, risk, execution, backtest` не викликає `float(<вираз>)` і
     `Decimal(<вираз>)` з нелітеральним аргументом. Замість цього:
     `features.convert.to_float(dec)` (Decimal→float), `sizing.convert.to_decimal(x, step)` /
     `price_to_decimal(x, tick)` / `float_to_decimal_exact(x)` (float→Decimal),
     `core.money.dec(int|str|Decimal)` (рантайм-конструктор, що відкидає float),
     для numpy-скалярів — `.item()`.
   * ingest/quality/storage/api/notify — пакети-межі (парсять рядки біржі/БД/HTTP), не скануються.
4. Гроші/ціни/кількості — `Decimal`; квантування: ціна → `core.money.quantize_price(x, tick)`
   (HALF_EVEN), кількість → `core.money.floor_qty(x, step)` (DOWN). Серіалізація Decimal — лише
   `core.money.dec_str` / `core.digest.canonical_json` (без експоненти).
5. Жодних мережевих викликів у тестах (`respx` / фікстури). Жодних `sleep` у тестах. Тести
   unit+property мусять іти швидко (весь набір < 25 с). Інтеграційні — маркер `integration`.
6. Конфігурація — лише з `config/*.yaml` через `fuzzhelm.config.load_yaml(name)` + власна
   pydantic-схема модуля; секрети — лише `fuzzhelm.config.Settings` (env / `.env`).
7. Назви тест-функцій із брифінгу (§10) — **дослівно** (грепаються при підрахунку покриття брифінгу).
   Додаткові тести дозволені.
8. НЕ редагувати чужі пакети, `tests/conftest.py`, `pyproject.toml`, `uv.lock`, `Makefile`.
   Потрібна нова залежність — не ставити, а записати в `docs/deviations.d/<модуль>.md` як запит.
   Спільні тестові утиліти — `tests/helpers/<модуль>_*.py`.
9. Журнал робіт — `docs/journal.d/<модуль>.md` (абзац: що зроблено, яке рішення, чому).
   Розходження зі спекою — `docs/deviations.d/<модуль>.md` у форматі
   `що в спеці / що насправді / що зробив / чим обґрунтовано`. Жодного вигаданого числа:
   якщо числа ще нема — маркер TBD з назвою експерименту в подвійних кутових дужках (брифінг §0.1 п. 3).

---

## 1. `core` (готово, хвиля 0)

| Файл | Що є |
|---|---|
| `core/enums.py` | `Venue, ContractType, Src(WS=1,REST=2,REPLAY=3), Stream, Side(SHORT=-1,FLAT=0,LONG=1), OrderType(MARKET, STOP_MARKET), OrderStatus, Liquidity, RejectCode, RiskState, VerdictKind, ExitReason, RunKind, RunStatus, EngineKind, Role, GapStatus, GapDetectorKind` |
| `core/dto.py` | pydantic frozen/forbid: `Instrument, Candle, Trade, BookLevel, BookSnapshot, MarkPrice, MarketEvent (union), OrderRequest, OrderAck, Fill` |
| `core/ports.py` | `Clock.now_ns()`, `IdGenerator.next_uuid()`, `MarketFeed.__aiter__()`, `ExecutionVenue.submit/on_bar/cancel_all` (синхронний) |
| `core/clock.py` | `FixedClock, ManualClock(set/advance, не назад), SeededIdGenerator(seed, namespace)`, `NS_PER_SEC/MIN/DAY`, `utc_day_start_ns` |
| `core/money.py` | `DECIMAL_CONTEXT (prec 38, HALF_EVEN, traps)`, `setup_decimal_context()`, `quantize_step/quantize_price/floor_qty/quantize_money/quantize_internal`, `dec_str`, `dec` |
| `core/digest.py` | `assert_no_float(obj)`, `to_canonical(obj)`, `canonical_json(obj)->bytes` (orjson, OPT_SORT_KEYS), `digest(obj,size=32)`, `hex_digest`, `event_uid(*natural_key)->hex32` |
| `core/journal.py` | `JournalEntry`, `GENESIS`, `entry_hash(...)`, `EventJournal(run_id, sink).append(kind,payload,ts_event_ns,ts_ingest_ns)`, `verify_chain(entries)->bad_seq|None`, `assert_chain` |
| `core/errors.py` | `NormalizationError(field,venue), LookaheadError, MainnetHostRejected, JournalIntegrityError(seq), ConfigValidationError(path), RiskHaltedError, PermissionDeniedError, OrderRejectedError(code)` |
| `infra/wallclock.py` | `SystemClock`, `RandomIdGenerator`, `new_run_id()` — ПОЗА межею детермінізму |
| `config.py` | `Settings` (allowlist хостів), `ALLOWED_TESTNET_HOSTS`, `ALLOWED_READONLY_HOSTS`, `assert_testnet_url`, `load_yaml(name)`, `parse_yaml_text(text)` |
| `features/convert.py` | `to_float(dec, scale=None)`, `Bar(t_ns,o,h,l,c,v,qv,n)` (frozen slots float), `bar_from_candle(Candle)->Bar` |
| `sizing/convert.py` | `to_decimal(x, step, rounding=ROUND_DOWN)`, `price_to_decimal(x, tick)`, `float_to_decimal_exact(x)` |
| `detectors/base.py` | `DetectorGroup(TREND/REVERSION/CONTEXT)`, `DetectorOutput(name,s,c,features,group,weight)` (валідує межі), `Detector` Protocol: `name, group, weight, warmup, compute(window)->DetectorOutput` |
| `fuzzy/base.py` | `FiredRule(rule_id, alpha, consequent, antecedent)`, `FuzzyResult(u_raw, inputs, memberships, fired, grid, mu_agg, engine, extras)`, `InferenceEngine` Protocol: `name, infer(T,R,V)->FuzzyResult` |

`event_uid` — природні ключі (фіксовано, щоб дедуплікація збігалась між модулями):
* свічка: `event_uid(venue, "klines", symbol_canon, tf, open_time_ns)`
* угода: `event_uid(venue, "trades", symbol_canon, agg_id)`
* книга: `event_uid(venue, "depth", symbol_canon, last_update_id)`
* mark: `event_uid(venue, "mark", symbol_canon, ts_event_ns)`

Канонічні символи: `BTCUSDT@BINANCE_USDM → "BTC-USDT-PERP"`, `ETHUSDT → "ETH-USDT-PERP"`,
Kraken `XBTUSD → "BTC-USD-SPOT"` (крос-звірка BTC-USDT-PERP ↔ BTC-USD-SPOT, див. `ingest/symbols.py`).

---

## 2. `features` (float-домен)

* `features/indicators.py` — інкрементальні O(1)-класи, кожен з `update(...) -> float | None`
  (None до прогріву) і `value`: `EMA(n)` (α=2/(n+1)), `WilderATR(n)` (α=1/n), `WilderRSI(n)`,
  `RollingWelford(n)` (ковзні середнє/дисперсія за Велфордом з add/remove), `MonotonicDequeMax/Min(n)`
  (Donchian), `Parkinson(W)`, `PercentileRank(L)`, `RollingOLS(n)` (нахил + R²), `SMA(n)`.
* `features/pipeline.py` — `FeatureParams` (дефолти з брифінгу), `Features` (frozen slots,
  поля `float | None`: `ema, atr, rsi, sma, sigma, bb_bw, donch_hi, donch_lo, park_sigma, vol_rank, ...`),
  `FeaturePipeline(params).update(bar: Bar) -> Features`, `max_lookback` (для embargo).
* `features/window.py` — `BarWindow(capacity)`: `append(bar, feats)`, `bar(lag=0)`, `feat(name, lag=0)`,
  `series(name, n) -> np.ndarray`, `__len__`, `t` (індекс поточного бару). `lag < 0` → `LookaheadError`.
  `LookaheadGuard` / `GuardedArray(arr, cursor)` для бектесту над повним масивом:
  доступ до індексу > cursor → `LookaheadError`.

## 3. `detectors`

6 класів за §5.2 брифінгу, кожен реалізує `Detector` Protocol:
`EmaSlope, Donchian, RsiExhaustion, BollingerZ, CandleGeometry, VolRegime`.
`VolRegime.compute` повертає `s=0, c=1.0, features={"V": V_t}` (V_t — перцентильний ранг σ_P).
`detectors/registry.py`: `DETECTOR_NAMES` (6 імен як у `config/detectors.yaml`),
`build_detectors(cfg: dict | None = None) -> list[Detector]` (з `config/detectors.yaml`).

## 4. `regimes`

`regimes/cluster.py`: `fit_regimes(X: np.ndarray, k_range=range(2, 7), seed) -> ClusterResult(k, centroids,
silhouette_by_k, labels)`; `regimes/calibrate_mf.py`: `calibrate(bars: Sequence[Bar], seed) -> dict`
(нові точки T-перцентилів, V-центри/σ, силует) + `write_membership_yaml(result, path)`.

## 5. `fuzzy`

* `membership.py`: `tri(a,b,c)`, `trap(a,b,c,d)`, `gauss(m,sigma)` — векторизовані callables
  (`np.ndarray -> np.ndarray` і `float -> float`); `LinguisticVariable(name, range, terms)` з
  `.fuzzify(x) -> dict[str, float]`; `MembershipConfig` + `load_membership(path | dict)`.
* `rules.py`: `Rule(id, antecedent: dict[var, term | "any"], consequent, w)`, `RuleBase`,
  `load_rulebase(src, membership, *, production=True) -> RuleBase` — валідація: рівно 45 правил
  (5×3×3, кожна комбінація рівно раз), без дублікатів антецедентів, усі терми існують,
  `w == 1.0` при `production=True`. Помилки → `ConfigValidationError(path="rules[3].if.T")`.
* `mamdani.py`: `MamdaniEngine(membership, rulebase, scheme="trapezoid", nodes=201)` реалізує
  `InferenceEngine`: `infer(T,R,V) -> FuzzyResult` (fired відсортовані за спаданням α).
* `defuzz.py`: `centroid(grid, mu, scheme: "rect"|"trapezoid"|"simpson") -> float` (порожня
  активація → 0.0), `convergence_study(engine, inputs, deltas=(0.02,0.01,0.002,0.001)) -> dict`.
* `linear.py`: `LinearVoteEngine(weights)` — базова лінія (`name="linear"`, `fired=()`).
* `surface.py`: `control_surface(engine, V, n=41) -> (T_grid, R_grid, U)`.
* `config/rules_mamdani.yaml` — 45 правил за трьома політиками (§5.6), усі `w: 1.0`.

## 6. `decision`

* `aggregator.py`: `Consensus(T, R, V)`, `consensus(outputs: Sequence[DetectorOutput], eps=1e-9) -> Consensus`
  (групи й ваги беруться з самих `DetectorOutput.group/.weight`).
* `agreement.py`: `Agreement(p_plus, p_minus, p_zero, H, A_g, kappa)`,
  `agreement(outputs, kappa_min=0.35, nu=1.0) -> Agreement` (лише групи TREND+REVERSION).
* `trace.py`: `DecisionTrace` (frozen): `open_time_ns, detector_outputs, consensus, fuzzy: FuzzyResult,
  agreement, u_raw, kappa, u_final, engine` + `to_dict()` (JSON-сумісний; float допустимі — це НЕ
  канонічні дані для хешу) + поля, які заповнює рушій пізніше: `sizing: dict | None`, `risk: dict | None`.
* `narrative_uk.py`: `narrate(trace) -> str` — українське речення(-я) з головним правилом і α.
* `core.py`: `DecisionCore(detectors: Sequence[Detector], engine: InferenceEngine, kappa_min, nu)`:
  `decide(window: BarWindow) -> DecisionTrace` (u_final = clip(κ·u_raw, −1, 1)).

## 7. Формат записаної WS-сесії (`fixtures/ws/*.jsonl.gz`) — ЗАФІКСОВАНО

gzip JSON Lines, рядки:
```
{"v":1,"kind":"header","venue":"BINANCE_USDM","symbol":"BTCUSDT","urls":{...},"started_ns":int,"minutes":float}
{"v":1,"kind":"frame","conn":"market"|"public","ts_ingest_ns":int,"stream":"btcusdt@kline_1m","data":{...сирий payload Binance...}}
{"v":1,"kind":"control","conn":..., "ts_ingest_ns":int,"event":"connected"|"disconnected","detail":str}
{"v":1,"kind":"footer","ended_ns":int,"frames":int}
```
Потоки: `<s>@kline_1m`, `<s>@aggTrade`, `<s>@markPrice@1s` (conn=market), `<s>@depth20@100ms` (conn=public).
Файли: `fixtures/ws/btcusdt_<YYYY-MM-DD>.jsonl.gz` (45 хв, демо/e2e), `fixtures/ws/sample_btcusdt_4m.jsonl.gz`
(4 хв, швидкі тести), `fixtures/ws/pathological/{gap,dup,reorder,clock_jump,stall,flash_crash}.jsonl.gz`.

## 8. `ingest`

* REST: `rest_client.BinanceRestClient(base_url, http: httpx.AsyncClient, bucket, retry)`:
  `klines(symbol, interval, start_ms, end_ms, limit=1500) -> list[list]`, `exchange_info() -> dict`,
  `premium_index(symbol) -> dict`, `server_time_offset_ms()`; `ratelimit.TokenBucket(capacity, refill_per_s, clock)`
  з вагами Binance; `retry.RetryPolicy(base, cap, max_attempts, rng_seed)` — full jitter + `Retry-After`;
  `backfill.backfill_klines(client, symbol, start_ms, end_ms) -> BackfillResult(candles, gaps)` з перекриттям
  1 бар і детекцією прогалин у перекритті; `kraken_client.KrakenRestClient.ohlc(pair, interval, since)`.
* Нормалізація: `normalize.normalize_binance(stream, data, ts_ingest_ns, instrument) -> MarketEvent`
  (невідоме поле → `NormalizationError`), `normalize_rest_kline(row, instrument, ts_ingest_ns) -> Candle`,
  `normalize_kraken_ohlc(...)`; `symbols.py` (мапінг); `quantize.py` (`check_tick`, `check_step`,
  `reject_below_min_notional -> RejectCode.BELOW_MIN_NOTIONAL`); `dedup.Deduplicator` (за `event_uid`,
  ідемпотентний під перестановками); `crosscheck.crosscheck(binance: Seq[Candle], kraken: Seq[Candle],
  threshold_bps=50) -> CrosscheckReport(rows, flagged)`.
* WS: `ws_client.BinanceWsClient(settings, clock)` (2 з'єднання market/public), `reconnect.Backoff`,
  heartbeat-watchdog, `gap_detector.GapDetector` (aggId, kline openTime), `recorder.SessionRecorder`,
  `replay.ReplayFeed(path, speed=inf, clock=None)` реалізує `MarketFeed` (виходить `MarketEvent`
  у порядку ts_ingest_ns), `replay.iter_frames(path)`, `candles.CandleAggregator` (оновлення kline → закриті
  свічки рівно один раз на open_time).

## 9. `quality`

`invariants.check_candle(c, instrument) -> list[str]` (h≥max(o,c), l≤min(o,c), ціни кратні tick, обсяг ≥ 0);
`ahp.ahp_weights(matrix) -> AhpResult(weights, lambda_max, ci, cr)`; `dq_score.DqInputs` →
`dq_score(inputs, weights, tau0_ms=1000) -> DqScore(completeness, validity, timeliness, continuity, score)`;
`anomaly_mlp.AnomalyAutoencoder(seed).fit(X).score(X)`, `.threshold` (q99 train), `.is_anomaly(x)`;
`health.PipelineHealth` (лічильники кадрів, лаг, реконекти, прогалини, Q).

## 10. `sizing`

`vol_target.VolTarget(lam, A, sigma_target, s_min, s_max, gamma)`: `update(r) -> (sigma_ann, s_star, s_t)`;
`atr_risk.q_atr(u_final, equity, atr, rho_base, chi) -> float`; `sizer.PositionSizer(params)`:
`size(inp: SizingInput) -> SizingResult(qty: Decimal, side, binding_constraint ∈ {"ATR_RISK","VOL_TARGET","LEVERAGE"},
q_atr, q_vt, q_lev, s_t, sigma_ann, kappa_mode, reject_code: RejectCode | None)`;
`hysteresis.HysteresisGate(enter=0.25, exit=0.12).update(u_final) -> Side`.

## 11. `risk`

`verdict.Verdict(kind, factor)`, `ALLOW`, `VETO`, `shrink(f)`, `compose(verdicts) -> Verdict`,
`exposure(verdict, requested: Decimal) -> Decimal`; `rules/*`: кожне правило —
`check(ctx: RiskContext) -> RuleVerdict(rule, verdict, observed, limit, payload)`;
`guard.RiskGuard(rules, journal).evaluate(ctx) -> GuardResult(verdict, records, approved_qty)`;
`state.RiskStateMachine(cfg)`: `update(obs) -> Transition | None`, `.state`, `.kappa_mode`,
`release(actor_role)` (лише admin, інакше `PermissionDeniedError`); таблиця переходів 4×6 тотальна;
`margin.liq_price_long/short(q, entry, wallet, mmr, maint_amount)`, `margin_ratio`, `dtl_atr`,
`reduced_max_leverage`; `var.historical_var_cvar(returns, alpha=0.05)`, `parametric_var(...)`;
`kupiec.kupiec_pof(breaches, W, p=0.05) -> KupiecResult(lr, reject)`; `journal.RiskJournal` (sink записів
risk_event); `killswitch.KillSwitch` (засувний).

## 12. `execution`

`cost_model.CostModel(mode, k_s, taker, maker, noise_bps, seed)`; `paper_broker.PaperBroker(cost_model,
instruments, ids: IdGenerator, clock)` реалізує `ExecutionVenue` (MARKET → bar t+1 open; STOP_MARKET
у межах бару; песимізм: стоп раніше за тейк); `portfolio.Portfolio(cash)` — `apply_fill`, `mark(prices)`,
`equity`, тотожність `cash + Σq·p − Σfees == equity`; `testnet_venue.BinanceTestnetVenue(settings, http)`
(HMAC-SHA256, `POST /fapi/v1/order`, allowlist); `router.OrderRouter(venue)` (ідемпотентність за
client_order_id).

## 13. `backtest`

`metrics.compute_metrics(equity: Sequence[Decimal] | np.ndarray, trades, periods_per_year) -> dict[str, float]`
(17 метрик) + `psr(...)`, `dsr(...)`; `walkforward.make_folds(n_bars, is_bars, oos_bars, step_bars, embargo_bars,
k) -> list[Fold]`; `pareto.pareto_front(points, senses) -> list[int]`; `manifest.RunManifest` (config_hash,
dataset_hash, git_sha, seed, engine, journal_head_hash, equity_hash); `parallel.run_parallel(fn, tasks, workers)`
з initializer (decimal context + seed); `grid.make_grid(space) -> list[dict]` (3·3·3·2·2 = 108);
`engine.py` — **хвиля 2** (подієвий рушій bar-close → open(t+1)).

## 14. `storage`

SQLAlchemy 2.0 async (`storage/models.py` — 12 таблиць + `app_user`, `audit_log`, `run_metric` за DDL §6),
`storage/session.py` (`make_engine(url)`, `session_factory`), репозиторії `storage/repositories/*.py`
(ідемпотентний upsert свічок `ON CONFLICT ... WHERE candle.is_closed = FALSE AND EXCLUDED.src <= candle.src`),
Alembic 3 ревізії `0001_core`, `0002_trading`, `0003_auth_audit` (+ `REVOKE UPDATE, DELETE ON event_journal`
для ролі застосунку).
