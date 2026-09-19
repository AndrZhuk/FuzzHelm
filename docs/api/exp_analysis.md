# API компонента `exp_analysis` — аналітичні обчислювальні експерименти (фаза 7), хвиля 3b

Автор: Андрій Жук, 2026. Файли: `src/fuzzhelm/backtest/experiments_analysis.py` (бібліотека: чисті помічники,
воркер, будівники таблиць, рисунки), `scripts/{cost_models,ablation,var_backtest,hysteresis_cost,plot_equity,
plot_var,export_report_tables}.py`, тести `tests/unit/test_experiments_analysis.py`. Вимоги — `docs/BRIEF.md`
§2, §5.9, §5.13–§5.16, §10, §12 (фаза 7, фаза 11), §14 (2.6–2.8, 2.11, 2.12), §15 (сцена 3:20–4:10), §16 (ФК7, ФК9,
ФК12). Розходження — `docs/deviations.d/exp_analysis.md` (XA-01…XA-22), журнал — `docs/journal.d/exp_analysis.md`.

**Головне.** Хвиля будує лише ІНСТРУМЕНТ: скрипти, перевірені димовими прогонами на малих входах
(`artifacts/tmp/`, у git не потрапляє). Жодного числа результатів тут немає — повні прогони робить наступна хвиля
закомітченим кодом (паспорт кожного виводу несе `git_sha` + `git_dirty`). Відсутній вхід у зведенні таблиць —
маркер TBD з назвою входу, а не підставлене число. Повні прогони виконано на `b933802`; виводи — `docs/report_tables/raw/`,
зведені таблиці — `docs/report_tables/`, результати — `docs/results.md`.

---

## 1. Спільне для всіх скриптів

```bash
uv run python scripts/<скрипт>.py [--db | --fixture [PATH]] [--database-url URL] [--symbol BTCUSDT|ETHUSDT]
    [--seed N] [--workers N] [--out DIR] [--smoke] [--smoke-days 3] [--params JSON|@file.json]
    [--scope oos|full|both] [--selection fixed|is_grid] [--cells N] [--rule front|sharpe] [--dd-cap X]
    [--risk-loop default|relaxed]
```

| прапорець | значення |
|---|---|
| `--db` | 45-денне вікно `data/dataset_window.json` з PostgreSQL (лише читання, роль `fuzzhelm_app`; хеш свічок звіряється з файлом) + ставки фандингу `data/funding_<SYMBOL>.json` — той самий `dataset_hash`, що в API/сітці (`Dataset.from_candle_arrays`) |
| `--fixture [PATH]` | офлайн: `fixtures/rest/binance_klines.json.gz` (3 000 барів BTCUSDT); типове джерело, якщо `--db` не задано |
| `--smoke` | малі входи: з `--db` — перші `--smoke-days` (3) доби вікна; розбиття фолдів — зменшене (`smoke_folds`, 2 фолди, ті самі правила embargo); `is_grid` — 2 клітинки. Без `--smoke` потрібне повне 45-денне вікно (профіль `backtest`: 15/5/5 днів × 6 фолдів) |
| `--seed` | за замовчуванням — `seed` профілю `config/profiles/backtest.yaml` (20260918); усі прогони одного виклику мають той самий seed (спільні випадкові числа шуму ковзання) |
| `--workers N` | `backtest.parallel.run_parallel` (ProcessPoolExecutor, spawn, initializer з Decimal-контекстом і seed); 0 — у поточному процесі. Результат від `N` не залежить |
| `--params` | перекриття конфігурації профілю `backtest`: ключі `n_atr, chi, u_enter, rho_base, lam, u_exit, tp_multiple, cooldown_policy`; `@файл.json` — об'єкт у корені або під `params` / `chosen` / `selected_params` / `choice.params` (тобто **вихід сітки exp_search з робочою точкою фронту Парето підходить напряму**) |
| `--scope` | `oos` — OOS-фолди walk-forward; `full` — повне вікно; `both` |
| `--selection` | `fixed` — ті самі параметри на всіх фолдах; `is_grid` — на IS кожного фолду (лише бари `[is_start, is_end)`) сітка `--cells` клітинок (`backtest.grid.make_grid`, рівномірна підмножина), вибір за `--rule`, на OOS — вибрана клітинка; окремо для кожного варіанта |
| `--rule` | лише `is_grid`: `front` (типово) — фронт Парето (SR↑, MaxDD↓, оборот↓, `backtest.pareto`) і ε-обмеження: max SR серед клітинок фронту з MaxDD ≤ `dd_cap` (рівність → менший оборот → менший індекс; жодної — мінімальна MaxDD фронту) — **те саме правило, що в walk-forward exp_search** (§5.16: робоча точка — з фронту, а не argmax SR); `sharpe` — `runner.select_cell` (argmax SR) |
| `--dd-cap` | лише `is_grid --rule front`: типово `state_machine.cool_enter` СИСТЕМНОЇ конфігурації (0.08; для `--risk-loop relaxed` — теж системної, не послабленої) |
| `--risk-loop relaxed` | ЛИШЕ для ізоляції ефекту: пороги автомата (`warn/cool/halt_*`), `max_daily_loss`, `max_drawdown_halt` → 0.93–0.97, `warn_vol_ratio` → 1e9 (XA-08). Системну конфігурацію не змінює |
| `--out` | каталог виводу; типово `artifacts/tmp/<експеримент>` |

**Кожен вивід** (`write_outputs`): `<stem>.json` (строгий JSON: NaN → null, ±inf → "inf"), `<stem>.csv` (рядки),
`<stem>.md` (таблиця), рисунки `*.png` (matplotlib Agg, лише 2D, 150 dpi, українські підписи осей),
`<stem>.timing.json` (час стіни — єдине недетерміноване число, винесене окремо). JSON містить `passport`:

```json
{"experiment", "command": "uv run python scripts/…", "git_sha", "git_dirty", "git_dirty_any", "git_dirty_paths",
 "seed", "dataset_hash", "source", "symbol", "tf", "n_bars", "t_first_ns", "t_last_ns",
 "config_hashes": {variant: hash}, "fold_layout": "profile|smoke",
 "folds": [{index, is_start, is_end, embargo_bars, oos_start, oos_end}], "scopes", "selection", "cells",
 "is_rule", "dd_cap", "risk_loop", "params", "params_source", "smoke", "workers", "engine", "warmup_bars"}
```
Та сама команда, `dataset_hash` і `config_hash` друкуються в stdout на старті.

* `git_dirty` — незакомічені зміни **коду** (`git status --porcelain` поза `artifacts/` і `docs/`, як у паспортах
  exp_search); `git_dirty_any` — сирий прапорець (`manifest.read_git_sha`), `git_dirty_paths` — перші 20 рядків змін
  коду. Виводи попередніх кроків хвилі (`docs/report_tables/raw`, `docs/figures`, `artifacts/exp_search`) не роблять
  наступні прогони «брудними» (XA-19).
* `params_source` — походження `--params`: `{kind: profile|inline|file, path, front_space, selection_rule,
  choice_rule, choice_index, dd_cap, source_git_sha, source_command, selected_on_oos}`. `selected_on_oos = True`
  (файл сітки exp_search з `choice` і `front_space = "oos"`) ⇒ у markdown кожного експерименту зі `--selection fixed`
  — застереження: OOS-зведення цієї конфігурації **не чиста поза-вибіркова оцінка** (робочу точку обрано за OOS тих
  самих фолдів; зміщення в ablation — на користь базової лінії); чиста — `--selection is_grid` (XA-17).

### 1.1 Протокол оцінки (однаковий для cost_models / ablation / var_backtest / hysteresis_cost)

* **Фолди** — `backtest.walkforward.folds_from_profile(len(ds), max_lookback)` (ті самі, що в сітці exp_search);
  OOS-сегмент фолду = `[oos_start − W, oos_end)` з вікном оцінки від `W = resolved_warmup() = 523` (прогрів
  усередині embargo `E = 2·max_lookback = 1046`; `E < W` → `ValueError` до прогону) — так само, як
  `runner.run_walkforward`. IS-сегмент (для `is_grid`) = `[is_start, is_end)`, прогрів усередині IS.
* **Воркер** `segment_task(task)` — чиста функція верхнього рівня над `Dataset.to_payload()` (без БД і файлів):
  `run_backtest(record_traces="none")` + `on_step` (стан автомата і `u_final` кожного бару) → 17 метрик рушія,
  доповнення (`sr_period, skew, kurt, psr, n_obs, …`), витрати вікна оцінки (`fees = Σ fill.fee`,
  `funding = Σ charge.amount`, `slippage_cost ≈ Σ qty·price·slippage_bps/10⁴`, `traded_notional`, `n_fills`,
  `gross_pnl` закритих угод), частки барів у станах, `equity_hash`, `config_hash`, `dataset_hash`, ряди (`series`).
* **Зведення OOS** `summarize(results)`: кожен фолд — окремий прогін з E₀ і NORMAL; дохідності фолдів
  конкатенуються: `curve = Π(1 + r_t)`; `sharpe = √P·mean(r)/std(r, ddof=1)` (P = 525 600), `total_return =
  curve_T − 1`, `max_drawdown = max(1 − curve/cummax)`, `turnover = Σ turnover_k·n_k / Σ n_k` (та сама формула, що
  `compute_metrics`), `fees_per_day_pct = 100·Σ fees / (E₀ · Σ днів)`, `psr = psr_from_returns(r)`, `exposure`,
  `share_halted/share_cooldown`, `halted_segments`; також `sharpe_fold_mean` — середнє Шарпів фолдів. Для одного
  сегмента (повне вікно) зведення дорівнює метрикам рушія (тест: Шарп, дохідність, MaxDD, оборот, експозиція).
* **Оборот ланцюга** — формула `compute_metrics` на зчепленій кривій: `turnover = Σ_k N_k·(C_k/E_k(0)) / mean(E) ·
  P / n`, `N_k` — номінал виконань фолду k, `C_k` — капітал ланцюга на старті фолду, `E_k(0)` — стартовий капітал
  фолду (номінал перемасштабовано на ланцюг — так само зводить OOS exp_search, `concat_oos_metrics`; XA-20).
* Повне вікно (`full`) для BTCUSDT містить дні 1–15 — вікно калібрування МФ (`data/dataset_window.json`
  `first_is_window`), тож це не поза-вибіркова оцінка; заголовок області це каже.

---

## 2. Бібліотека `backtest.experiments_analysis`

```python
VAR_WINDOW = 500; VAR_ALPHA = 0.05; V_NEUTRAL = decision.aggregator.V_DEFAULT (0.5); BRIEF_HYSTERESIS_CLAIM_PCT = 1.15
PARAM_KEYS = (*GRID_KEYS, "u_exit", "tp_multiple", "cooldown_policy"); STATE_ORDER / STATE_CODE (NORMAL…HALTED → 0…3)

parse_params(text) -> dict                  # JSON | @file (params / chosen / selected_params / choice.params)
base_config(params=None, *, profile="backtest") -> BacktestConfig      # валідує ризик-дерево одразу
Variant(key, label, config, tie_exit=False, note="")
kappa_off(cfg)        # agreement.kappa_min = 1 ⇒ κ ≡ 1
hysteresis_off(cfg)   # u_exit = u_enter
relaxed_risk(cfg); apply_risk_loop(cfg, "default" | "relaxed")
ablation_variants(cfg) -> 10 варіантів: baseline, no_<детектор> × 6, linear, kappa_off, hysteresis_off
cost_variants(cfg) -> zero | sqrt_impact | full;   hysteresis_variants(cfg) -> with | without
cell_config(variant, cell)                  # клітинка поверх варіанта (tie_exit: u_exit := u_enter клітинки)
select_cells(n, cells=None)                 # n рівномірно розставлених клітинок 108-сітки
FRONT_KEYS = ("sharpe", "max_drawdown", "turnover")
select_on_front(metrics, dd_cap) -> {index, rule: eps_constraint|fallback_min_dd, front, feasible, dd_cap}
select_is_cell(metrics, rule="front"|"sharpe", dd_cap); default_dd_cap(cfg) = cool_enter
params_source(text) -> dict; selection_caveat(passport) -> [md]; is_rule_text(passport) -> str

Segment(label, kind, fold, start, stop, eval_start)
oos_segments(folds, warmup); is_segments(folds, warmup); full_segment(n); smoke_folds(n, max_lookback, warmup, k=2)
eval_folds(n_bars, cfg, *, smoke) -> (folds, "profile" | "smoke")

segment_task(task) -> dict                  # воркер (pickle, spawn)
TaskPlan().add(dataset, segment, cfg, seed, *, series, meta, payloads); .run(workers, seed)
evaluate_variants(ds, variants, *, folds, seed, workers=0, scopes=("oos",), selection="fixed", cells=None,
                  series=True, rule="front", dd_cap=None, log=None)
    -> {"results": {variant: {scope: [...]}}, "chosen", "choice_info", "is_metrics"}
summarize(results, periods_per_year=525600) -> dict (SUMMARY_KEYS)
compare(ref, other, key) -> {reference, other, diff, ratio (лише коли обидва > 0), sign_flip}
run_variant_experiment(args, experiment, argv, make_variants) -> VariantRun   # спільний хід скриптів
summary_rows / summary_table / folds_table / chosen_table / fold_rows

# VaR
active_mask(returns, position) -> bool[n]  # (pos[t−1] ≠ 0) ∨ (pos[t] ≠ 0) ∨ (r_t ≠ 0)
rolling_parametric_var(r, window=500, alpha=0.05) -> [n − W]   # z·σ̂(r[t−W:t], ddof=1), середнє 0
count_breaches(realized, var) -> int        # r_t < −VaR_t (строго)
var_block(r, *, window, alpha, equity) -> {n, zero_share, moments, static, rolling | None, reason}
fat_tail_conclusion(block, name) -> list[str]      # українські речення лише з чисел блоку

# гістерезис
band_stats(u_final, enter, exit) -> {n_decisions, share_in_band, share_above_enter, enter_crossings, crossings_per_day}
churn_cost_pct_per_day(fee_rate, fills_per_bar, notional_over_equity) = 100·1440·fills_per_bar·τ·N/E

# таблиці, вивід, паспорт
fmt(x), pct(x), md_table(headers, rows), sanitize(obj), passport(...), passport_md(p), write_outputs(dir, stem, …)
tbd(name) -> маркер TBD:name у подвійних кутових дужках (TBD_FMT); METRIC_LABELS_UK
GIT_DIRTY_IGNORED = ("artifacts", "docs"); git_state(repo) -> {sha, dirty, dirty_any, dirty_paths}
sr0_expected_max(trial_srs)                 # N — усі прогони (і з невизначеним SR), Var — по скінченних (ddof=1)
dsr_for(metrics, trial_srs) -> {dsr, n_trials, n_finite, sr0, reason}   # PSR з SR* = SR₀
pick_trial_group(rows, dataset_hash, *, git_sha, engine) -> (Шарпи, {git_sha, seed, engine, n, groups} | None)

# брифінг / тести / трасування
brief_section(text, n); parse_brief_test_groups(text) -> [BriefGroup(letter, title, declared, names)]
parse_collected(stdout) -> [nodeid]; test_function_name(nodeid); test_group_rows(groups, nodeids)
extract_md_tables(text, heading=None); parse_deviation_titles(text, source); TRACE_ROWS (11); TRACE_BRIEF_CLAIMS

# рисунки (matplotlib Agg, імпорт лише всередині функцій — воркери matplotlib не тягнуть)
plot_equity_figure(path, ts_ns, equity, *, states=None, title="", boundaries=())
plot_var_histogram(path, panels, *, title="")      # panel = {title, returns, lines: {підпис: частка}}

# джерела
add_common_args(ap, experiment); add_variant_args(ap); load_source(args) -> Dataset
async load_db_dataset(symbol, database_url=None); async load_db_run(run_id, database_url=None)
async read_run(session, run_id)             # те саме в уже відкритій сесії (перечитування в rollback-транзакції)
first_decision_index(n_points, run_metrics, default) -> (decide_from, звідки)   # n_points − 1 − n_obs
series_from_curve(curve) ; state_names(codes) ; state_codes(names) ; neutral_v_memberships(cfg)
```
Межі типів дотримано (`tests/arch`): Decimal → float лише `features.convert.to_float`, без `float(<вираз>)` /
`Decimal(<вираз>)`; БД і matplotlib імпортуються ліниво.

---

## 3. Скрипти

### 3.1 `scripts/cost_models.py` — три моделі витрат (§5.14)
Та сама конфігурація (профіль + `--params`) і ті самі фолди під `cost_mode` = `zero` (P_fill = P_ref, без комісій і
фандингу) | `sqrt_impact` (спред + k_s·σ·P·√(Q/V) + seeded шум, без комісій/фандингу) | `full` (+ τ_taker, фандинг).
Таблиця по областях: Шарп (зчеплений і середній по фолдах), дохідність, MaxDD, оборот, комісії, фандинг, ковзання,
угоди, PSR, сегменти з HALTED; «завищення» наївної моделі — `compare(full, zero|sqrt_impact)`: різниця, відношення
(лише для двох додатних Шарпів), зміна знака; текст висновку будується з чисел (твердження «у кілька разів»
ПІДТВЕРДЖЕНО лише при відношенні ≥ 2; інакше — різниця / зміна знака / «не підтверджено»). `--selection is_grid` —
наївний дослідник і оптимізує на наївній моделі (сітка окремо для кожного режиму).
Вивід: `cost_models.{json,csv,md}`, `cost_models_folds.{json,csv,md}`, `cost_models.timing.json`.
JSON: `{experiment, passport, summary: [рядок на (варіант, область)], overstatement: {scope: {zero_vs_full,
sqrt_impact_vs_full: {sharpe, total_return: compare}}}, chosen}`.

### 3.2 `scripts/ablation.py` — ablation ядра
Базова лінія + 9 варіантів з рівно однією зміною: без кожного з 6 детекторів (без VolRegime — V ≡ 0.5, у виводі — μ
термів V у цій точці з `config/membership.yaml`), `LinearVoteEngine` (u = 0.5·T + 0.5·R), κ ≡ 1, без гістерезису.
Таблиця + різниці до базової (ΔШарп, Δдохідність, ΔMaxDD, Δоборот, Δугод, Δкомісії) + рядок «Мамдані проти лінійного».
Вивід: `ablation.{json,csv,md}`, `ablation_folds.*`, `ablation.timing.json`; JSON — `{…, summary, deltas, neutral_v,
chosen}`.

### 3.3 `scripts/var_backtest.py` — VaR/CVaR і тест Купця (§5.13)
Джерела: обрана конфігурація (OOS і/або повне вікно) або `--run-id UUID` (крива `equity_point` збереженого прогону,
лише читання; відкидаються точки до першого рішення рушія: `decide_from = n_points − 1 − run_metric.n_obs` —
точно, `first_decision_index`; без `n_obs` — `resolved_warmup() − 1`, з позначкою в паспорті `skip_source`). Для
кожної області — дві підмножини: **усі бари**
і **бари в позиції** (`active_mask`, XA-01). Для кожної: опис розподілу (частка нулів, асиметрія, куртозис), оцінки на
всій вибірці (історичні VaR₉₅/CVaR₉₅ — `risk.var.historical_var_cvar`; параметричний z·σ; частки перевищень) і
**бектест прогнозу на крок уперед**: історичний `risk.var.rolling_var_breaches(r, W)`, параметричний
`rolling_parametric_var(r, W)`, пробій `r_t < −VaR_t`, `risk.kupiec.kupiec_pof(x, n, 0.05)` проти χ²₁(0.95) =
3.8415 (у таблиці — 3.841). Якщо в підмножині ≤ W доходностей — бектест не робиться, причина пишеться явно;
`--window-active N` — менше вікно для барів у позиції як ЗАДЕКЛАРОВАНЕ відхилення (XA-16). Висновок про товсті хвости
— `fat_tail_conclusion` (лише з виміряних чисел; напрям відхилення: «надто консервативна» / «ризик занижено»).
Вивід: `var_backtest.{json,csv,md}`, `var_series.npz` (ряди `<scope>__{returns, active, equity, ts_ns, state,
position, boundaries, equity0}` для plot_var/plot_equity), `var_hist_<символ | run_<id8>>_<scope>.png`,
`var_backtest.timing.json`.
JSON: `{…, blocks: [var_block + scope/subset], conclusion, reference_metrics: {oos: зведення, full: зведення,
full_engine_metrics: 17 метрик + доповнення, full_equity_hash, oos_folds}, config_hash, figures}`.

### 3.4 `scripts/hysteresis_cost.py` — ціна відсутності гістерезису (§5.9)
`with` (u_enter / u_exit) проти `without` (u_exit = u_enter) на тих самих сегментах. Таблиця: угоди, виконання
(за добу, на бар), номінал виконання / E₀, комісії (USDT і % E₀ за добу), оборот, дохідність, MaxDD, Шарп, частки
HALTED/COOLDOWN, аналітика §5.9 при ВИМІРЯНИХ частоті й розмірі (`churn_cost_pct_per_day(τ, fills/bar, N/E)`) і
сценарій брифінгу «2 виконання щобару» при виміряному розмірі; смуга петлі (`band_stats`: частка рішень з
u_exit ≤ |u| < u_enter, перетини порогу). Звітується виміряна різниця «без − з» у % E₀ за добу поруч із твердженням
брифінгу 1.15 %/добу і його виправленою арифметикою 115.2 %/добу (R-03). Для ізоляції від засувки ризику — другий
прогін з `--risk-loop relaxed`.
Вивід: `hysteresis_cost.{json,csv,md}`, `hysteresis_cost.timing.json`; JSON — `{…, fee_rate, u_enter, u_exit,
brief_claim_pct_per_day, brief_arithmetic_pct_per_day_at_full_notional, measured: {scope: {with, without}}, band,
summary}`.

### 3.5 `scripts/plot_equity.py` — капітал + просадка + смуги режимів
Джерела: `--run-id` (БД, лише читання: `equity_point.equity/risk_state`), `--series var_series.npz --scope
full|oos|run`, або прогін тут же (`--db/--fixture`, `--params`, `--scope full|oos`; OOS — зчеплена крива з
вертикалями меж фолдів). `--persist` (лише `--db`) пише прогін обраної конфігурації в БД існуючим шляхом API —
`api.backtest_runner.DbBacktestRunner` (паспорт RUNNING → рушій → `workers.persist` → DONE з
`journal_head_hash`/`equity_hash`; лише ключі `PARAM_KEYS` API):

| прапорець | що робить |
|---|---|
| `--persist-mode commit` (типово) | повне вікно `data/dataset_window.json`, рядки лишаються; друкує `run_id` і малює з БД. Код із незакоміченими змінами (поза `artifacts/`, `docs/`) → **відмова** (числа звіту — лише із закоміченого коду), `--allow-dirty` — свідомий обхід |
| `--persist-mode rollback` | той самий шлях у зовнішній транзакції (кожен `session_scope` раннера — SAVEPOINT), рядки прогону перечитуються в ній же, потім **ROLLBACK** — перевірка запису на головній БД без слідів; дозволяє `--smoke` (перші `--smoke-days` доби вікна); звіряє `config_hash` з очікуваним, `equity_hash` рядка з рушієм і `dataset_hash` шляху API з завантажувачем експериментів |
| `--dry-run` | лише специфікація, очікуваний `config_hash` і стан git |

`git_dirty` паспорта прогону — зміни КОДУ (підклас раннера з перевизначеним `_git`, як у прогонах exp_search), а не
сирий `git status` (XA-19). Мітка файлів: `run_<id8>` (БД; `…_rolled_back` для rollback), `<каталог npz>_<scope>`
(npz, напр. `var_BTCUSDT_oos`), `<символ>_<scope>` (прогін тут же). Ідентичний прогін уже є (`ux_run_identity` =
config_hash, dataset_hash, seed, engine, git_sha — **без kind**; напр. клітинка сітки exp_search з тими самими
параметрами — це очікуваний випадок наступної хвилі) — нічого не пишеться, дубль читається тією самою фабрикою
сесій (`run_or_duplicate`): дубль `backtest` малюється з БД, дубль `grid_cell` — з прогону тут же зі звіркою
`equity_hash` проти рядка БД (`None`, якщо в рядку хеша немає) (XA-15). Вивід: `equity_<мітка>.png`,
`plot_equity_<мітка>.{json,md}` (бари за станами, дохідність, MaxDD, кількість змін стану; `meta.persist` — режим,
специфікація, `run_id`/дубль, звірки хешів, кількості записаних рядків).

### 3.6 `scripts/plot_var.py` — гістограма з VaR/CVaR
`--results DIR` (вивід var_backtest: `var_series.npz`) або прогін тут же. Дві панелі на область (усі бари / бари в
позиції), логарифмічна вісь частот, вертикалі на −VaR₉₅ (іст.), −CVaR₉₅ (іст.), −VaR₉₅ (парам.). Вивід:
`var_hist_<символ>_<scope>.png`, `plot_var_<символ>.{json,md}` (символ — з паспорта var_backtest).

### 3.7 `scripts/export_report_tables.py` — таблиці звіту
```bash
uv run python scripts/export_report_tables.py [--db | --fixture] [--database-url URL] [--results-dir DIR]...
    [--input KIND=PATH]... [--run-id UUID]... [--no-collect] [--collect-file F] [--cov-file F] [--out DIR] [--smoke]
```
* Пошук результатів: `*.json` у `--results-dir` (рекурсивно; `*.timing.json` і `*_folds.json` пропускаються); вид —
  ключ `experiment` (exp_analysis) / імена `walkforward|wf`, `pareto`, `sensitivity`, `amdahl|speedup|bench`, `grid`
  (exp_search, поле `provenance`); явно — `--input KIND=PATH`. Для результатів exp_search таблиці береться з
  сусіднього `.md` (дослівно), інакше — рядки JSON; паспорт (`passport`/`provenance`) — рядком джерела над таблицею.
* БД (`--db`, лише читання): прогони за видами, DONE-бектести **без прогонів фолдів walk-forward** (тег
  `run_metric.wf_fold` exp_search) і кількість таких фолдів, `run_metric` прогонів `--run-id` (або найновішого DONE-
  бектесту не-фолду), `risk_event` на прогін, прогони `grid_cell` (`dataset_hash, git_sha, seed, engine, started_at,
  sr_period` — для DSR), свічки в межах `data/dataset_window.json`, кількість таблиць схеми.
* DSR стовпця метрик: Шарпи (за період) прогонів сітки на ТОМУ САМОМУ `dataset_hash` і з однієї сітки — група
  `(git_sha, seed, engine)` (`pick_trial_group`: група з тим самим git_sha і рушієм, що в стовпця; інакше найновіша
  того самого рушія): повторний запуск сітки на іншому коміті чи з іншим рушієм не подвоює N (XA-21). N — усі прогони
  групи, Var(SR_i) — по скінченних (`sr0_expected_max`, як у exp_search).
* Таблиці (`--out`, типово `docs/report_tables`, з `--smoke` — `artifacts/tmp/export_report_tables`):

| файл | вміст | джерело |
|---|---|---|
| `metrics.md/csv` | 17 метрик + PSR + DSR по стовпцях (прогони БД; обрана конфігурація, повне вікно — з var_backtest, у заголовку стовпця — символ, джерело, кількість барів) | `run_metric`, `reference_metrics.full_engine_metrics`; DSR — `dsr_for(metrics, trial_srs)` лише за прогонами однієї сітки на ТОМУ САМОМУ `dataset_hash` (БД `grid_cell` або всі клітинки `full_sr_period` сітки exp_search), інакше TBD; плюс таблиця DSR із виводу сітки exp_search |
| `wf_folds.md`, `pareto.md`, `sensitivity.md`, `amdahl.md` | IS/OOS по фолдах, фронт і робоча точка (`choice.params`), чутливість, S(p) | вивід exp_search (дослівно); немає → TBD |
| `cost_models`, `ablation`, `var_kupiec`, `hysteresis` (`.md/.csv`) | зведення експериментів цього компонента по символах; заголовок блоку — символ, вибір (`fixed`/`is_grid` + правило), контур ризику, smoke; застереження `selection_caveat`, якщо параметри обрано за OOS | JSON цього компонента |
| `pathological.md`, `mlp_rocauc.md`, `calibration.md` | таблиці дослівно | `docs/figures/{ingest_pathological,quality_mlp_rocauc,calibration_report}.md` |
| `test_groups.md/csv` | групи A–N: заявлено в заголовку, названо в §10, знайдено дослівно, вузлів (з параметризацією), усіх вузлів у файлах групи (евристика «файл належить групі, чиїх дослівних тестів у ньому найбільше»), відсутні назви; усього вузлів, інтеграційних і **фактичний типовий відбір** `uv run pytest` (addopts pyproject — отже й майбутній фільтр `slow`) | `pytest --collect-only -q` з `-m ""`, `-m integration` і без `-m` (підпроцеси; час збору з рядка підсумку вирізано — вивід детермінований) або `--collect-file` (збережений вивід `-m ""`) |
| `traceability.md/csv` | 11 рядків §2: фрагмент → модулі (перевірено наявність) → що заявлено → чим підтверджено (лише наявні файли, рядки БД, файли результатів; немає → TBD) | `TRACE_ROWS` |
| `deviations.md/csv` | усі розходження з ID (`## ID. назва`) | `docs/deviations.md` + `docs/deviations.d/*.md` |
| `index.md/json` | зміст, кількість TBD у кожній таблиці, знайдені файли, паспорт виводу | — |

---

## 4. Тести (`tests/unit/test_experiments_analysis.py`, 32 тести, ≈ 0.35 с)

Пробої VaR — строгі й перевірені на формі; параметричний прогноз = наївний цикл `z·std(r[t−W:t], ddof=1)` і та сама
вирівнюваність, що в `risk.var.rolling_var_breaches`; `var_block` = наївні цикли (історичний і параметричний) + Купець;
коротка вибірка → причина замість чисел; маска «в позиції» (вхід, утримання, вихід, вхід+стоп в одному барі);
висновок про хвости містить виміряні числа; зведення фолдів (зчеплення, MaxDD через межу фолду, оборот, зважений за
барами); воркер на 900 барах фікстури: зведення одного сегмента = метрики рушія, довжини рядів, хеші; OOS-сегменти
(прогрів усередині embargo, помилка при короткому embargo), розбиття `--smoke`; 10 варіантів ablation — рівно одна
зміна кожен, різні `config_hash`, спільний прогрів; прив'язка u_exit у клітинці без гістерезису; режими витрат,
послаблений контур; вибір клітинок; `--params` з `choice.params` і відмова на невідомий ключ; смуга петлі й перетини;
арифметика R-03 (115.2 %); форматування, строгий JSON, `compare`, `dsr_for`, `write_outputs`; розбір груп A–N
справжнього `docs/BRIEF.md`, зведення вузлів з параметризацією і прив'язкою файлів, таблиці за заголовком, назви
розходжень; 11 рядків трасування з наявними модулями. Після рецензії: зведення одного сегмента = рушій і за оборотом та
експозицією, оборот ланцюга перемасштабовано на капітал ланцюга (ручний розрахунок); вибір на фронті — ε-обмеження ≠
argmax SR, рівність → оборот, фолбек на min MaxDD, NaN-Шарп найгірший, невідоме правило → помилка; DSR: N — усі
випробування, Var — скінченні, збіг з `metrics.dsr`/`expected_max_sr` для скінченних; вибір групи сітки (git_sha,
seed, engine) на тому самому наборі; походження `--params` (OOS-обрана точка → застереження, для `is_grid` — ні);
`decide_from` збереженого прогону з `n_obs`; паспорт відділяє зміни коду від виводів (`artifacts/`, `docs/`).

---

## 5. Швидкодія (димові прогони 2026-09-19, машина розробника, load ≈ 2–3, паралельно працював інший агент)

| команда (димова) | роботи рушія | стіна |
|---|---|---|
| `cost_models.py --db --smoke --workers 2` (3 доби, 2 фолди + повне) | 9 прогонів, ≈ 23 тис. барів | 1.4 с |
| `ablation.py --db --smoke --workers 2` | 30 прогонів, ≈ 77 тис. барів | 2.7 с |
| `ablation.py --db --smoke --workers 2 --selection is_grid --cells 2 --scope oos` | 60 прогонів | 2.4 с |
| `var_backtest.py --db --smoke --workers 2` | 3 прогони | 1.5 с |
| `hysteresis_cost.py --db --smoke --workers 2` (default / relaxed) | 6 + 6 прогонів | 1.2 + 1.2 с |
| `var_backtest.py --run-id 89619416-…` (64 800 точок з БД) | 0 | 1.2 с |
| `plot_equity.py --run-id 89619416-…` | 0 | 1.2 с |
| `export_report_tables.py --smoke --results-dir artifacts/tmp --db` (з `pytest --collect-only` × 2) | — | 4.2–5.4 с |

Повторні димові прогони рецензента (2026-09-19, після правок; load ≈ 3–5, паралельно працював інший агент): ті самі
команди — 0.7–3.1 с; `ablation.py --db --smoke --selection is_grid --cells 3 --scope oos` — 80 прогонів, 2.3–2.8 с
(воркери 2 і 3 — ідентичний JSON); `plot_equity.py --db --smoke --persist --persist-mode rollback` — 1.6 с (4 320
точок записано й перечитано, ROLLBACK, кількості рядків БД до/після однакові); той самий rollback на повному вікні —
7.2 с (64 800 точок); `export_report_tables.py --smoke --db` з трьома `pytest --collect-only` — 6.0–6.3 с.

Оцінка повних прогонів — у §6 (планування: ≈ 55 мкс/бар на ядро, ефективно ≈ 14–18 мкс/бар стіни на 8 воркерах — за
виміряною сіткою 108 × 64 800 барів, 96.7–123 с, `docs/api/engine.md` §4).

---

## 6. Повні прогони наступної хвилі (після коміту; порядок важливий)

Бар-кроки на символ: варіант з `--scope both` = 6 OOS-фолдів × (523 + 7 200) + 64 800 = 111 138 барів; IS-фолд —
20 554 бари. Оцінки — з µs/бар вище; «тиха машина» — load < 2 (інакше час множиться на навантаження).

0. exp_search (їхні команди, `docs/api/exp_search.md`): `run_grid.py --db --symbol S --workers 8` (пише 108 `grid_cell`
   у БД і `artifacts/exp_search/grid/grid_S_mamdani.json` = `$GRID_S`) і `run_walkforward.py --db --symbol S`.
1. `uv run python scripts/cost_models.py --db --symbol S --workers 8 --params @$GRID_S --out docs/report_tables/raw/cost_models_S`
   — 333 тис. бар-кроків, 21 задача, ≈ 10–15 с.
2. `uv run python scripts/ablation.py --db --symbol S --workers 8 --params @$GRID_S --out docs/report_tables/raw/ablation_S`
   — 1.11 млн бар-кроків, 70 задач, ≈ 15–30 с.
3. `uv run python scripts/hysteresis_cost.py --db --symbol S --workers 8 --params @$GRID_S --out docs/report_tables/raw/hysteresis_S`
   і те саме з `--risk-loop relaxed --out docs/report_tables/raw/hysteresis_S_relaxed` — 222 тис. бар-кроків, ≈ 6–10 с кожна.
4. `uv run python scripts/var_backtest.py --db --symbol S --workers 8 --params @$GRID_S --out docs/report_tables/raw/var_S`
   — 111 тис. бар-кроків + ковзні VaR на ~110 тис. доходностей, ≈ 6–10 с; якщо в рядку «бари в позиції» бектест
   неможливий — додатково `--window-active 250 --out docs/report_tables/raw/var_S_w250` (задеклароване відхилення XA-16).
5. `uv run python scripts/plot_var.py --results docs/report_tables/raw/var_S --out docs/figures` — ≈ 1–2 с.
6. `uv run python scripts/plot_equity.py --db --symbol S --params @$GRID_S --persist --out docs/figures` — ≈ 5–10 с;
   очікувано — дубль `grid_cell` кроку 0 (та сама ідентичність): друкує його id і звірку `equity_hash`; інакше пише
   новий `backtest` і друкує `run_id`. Далі `uv run python scripts/plot_equity.py --series docs/report_tables/raw/var_S/var_series.npz --scope oos --out docs/figures` — ≈ 1 с.
7. Рекомендовано (тиха машина): чиста OOS-оцінка з вибором на IS (правило фронту) —
   `cost_models.py … --selection is_grid --scope oos --out docs/report_tables/raw/cost_models_S_isgrid` (40 млн
   бар-кроків, ≈ 9–12 хв на 8 воркерах) і `ablation.py … --selection is_grid --scope oos --out
   docs/report_tables/raw/ablation_S_isgrid` (133 млн, ≈ 31–40 хв). Без них OOS-числа кроків 1–2 для точки,
   обраної за OOS, мають лише застереження XA-17.
8. `uv run pytest --cov=fuzzhelm --cov-report=term > docs/report_tables/raw/coverage.txt` — ≈ 40 с.
9. `uv run python scripts/export_report_tables.py --db --results-dir docs/report_tables/raw --results-dir artifacts/exp_search --run-id <id з кроку 6, BTC> --run-id <id, ETH> --cov-file docs/report_tables/raw/coverage.txt --out docs/report_tables`
   — ≈ 6–10 с.
