# API компонента `exp_search` — пошукові експерименти фази 7 (інструментарій)

Автор: Андрій Жук, 2026. Файли: `src/fuzzhelm/backtest/experiments_search.py`, `scripts/{run_walkforward,run_grid,
bench_amdahl,sensitivity,plot_walkforward,plot_pareto,plot_amdahl,plot_sensitivity}.py`,
`tests/unit/test_experiments_search.py`. Вимоги — `docs/BRIEF.md` §5.14–§5.16, §12 фаза 7, §14 п. 2.6–2.8, 2.11, 2.12,
§15 сцена 3:20–4:10, §16 ФК7/ФК9/ФК12. Розходження — `docs/deviations.d/exp_search.md` (XS-01…XS-13), журнал —
`docs/journal.d/exp_search.md`.

**Статус:** інструментарій готовий і перевірений швидкими прогонами (`--smoke`, вивід у `artifacts/tmp/`, gitignored).
Повні 45-денні прогони виконано наступною хвилею після коміту цього коду: `b933802`, `git_dirty = False` у кожному паспорті.
Виводи — `docs/report_tables/raw/exp_search/{amdahl,grid,walkforward,sensitivity}/`, зведення з числами — `docs/results.md`
§3–§5, §10. Цей документ описує API і чисел не дублює.

---

## 1. `backtest.experiments_search` — чисті функції

```python
CRITERIA = ("sharpe", "max_drawdown", "turnover")        # (SR↑, MaxDD↓, Turnover↓)
SELECTION_RULE = "eps_constraint_max_sr"; SELECTION_RULE_UK: str   # текст правила — у кожен вивід

# вибір робочої точки з фронту Парето
front_of(metrics, keys=CRITERIA) -> list[int]                       # backtest.pareto.pareto_front; NaN — найгірше
choose_from_front(metrics, *, dd_cap, keys=CRITERIA) -> FrontChoice(index, front, feasible, dd_cap, rule)
default_dd_cap(cfg) -> float                                        # = risk_limits.state_machine.cool_enter (0.08)

# фолди і сегменти
subset_indices(n_total, n) -> list[int]                              # n рівномірно розкиданих клітинок (smoke)
smoke_folds(n_bars, *, embargo_bars, warmup, k=2) -> list[Fold]      # справжнє embargo, OOS = крок = n/8
oos_segment(fold, warmup) -> (start, eval_start)                     # прогрів W перед OOS усередині embargo

# конкатенація OOS
chain_equity(curves, e0=None) -> (equity, starts)                    # ланцюг ДОХОДНОСТЕЙ сегментів
concat_oos_metrics(segments, periods_per_year) -> dict               # CONCAT_KEYS (див. §3)

# PSR / DSR (Шарпи ЗА ПЕРІОД)
sr0_expected_max(trial_srs, n_trials=None) -> float
deflated_sharpe_report(sr, n_obs, skew, kurt, trial_srs, *, threshold=0.95) -> dict
significance_statement_uk(report, *, what) -> str; psr_statement_uk(psr, *, what) -> str

# Амдал
amdahl_fit(times: {p: T(p)}) -> {workers, times, speedup, efficiency, serial_fraction, bound, karp_flatt, sse, limit_speedup}
overhead_breakdown(*, p, wall_s, startup_s, task_walls, task_pids, pickle_s_per_task, compute_s_p1=None) -> dict

# чутливість
SENS_SPECS / SENS_NAMES = (u_enter, u_exit, chi, rho_base, lam, sigma_target, kappa_min, n_atr); DEFAULT_LEVELS = (−1, −½, ½, 1)
base_param_values(cfg) -> dict; sensitivity_space(cfg, *, levels, names=None, spans=None) -> list[SensParam]
apply_param(cfg, name, value) -> BacktestConfig                      # поле або дерево; змінює config_hash
elasticity_coord(name, v) -> float                                   # λ → пам'ять 1/(1−λ), решта — θ
tornado_rows(base_metrics, runs, space, keys=CRITERIA) -> list[dict] # упорядковано за розмахом SR

# воркери ProcessPoolExecutor (функції верхнього рівня, pickle, "spawn"; БД не торкаються)
segment_task(task) -> dict       # 17 метрик + extras + wall_s, pid, config_hash, final_state, halted_at
                                 # (+ equity float64 і equity_ts int64 з keep_equity; + equity_hash з equity_hash=True)
startup_probe(task) -> {task, pid}
make_task(payload, cfg_dict, seed, *, params=None, eval_start=0, keep_equity=False, equity_hash=False) -> dict

# оркестрація (через backtest.parallel.run_parallel)
walkforward_search(dataset, cfg, *, folds, cells, seed, workers, dd_cap, cell_ids=None) -> WalkForwardSearch
oos_tasks_all_cells(dataset, cfg, *, folds, cells, seed) -> list[task]   # фолд-major
concat_by_cell(oos_results, n_cells, n_folds, periods_per_year) -> list[dict]

# спільне для скриптів
GIT_DIRTY_IGNORED = ("artifacts", "docs")
git_state(repo=None) -> {sha, dirty, dirty_any, dirty_paths, dirty_ignored}   # dirty — без artifacts/, docs/ (XS-11)
commit_refusal(persist, git_state, *, allow_dirty) -> str | None     # причина відмови --persist commit
add_common_args(ap); resolve_dataset(args) -> Dataset; resolve_out(args, name) -> Path; profile_seed(profile)
load_dataset("db" | "fixture", symbol, *, fixture_path=None); smoke_slice(ds, days); candles_hash(ds)
provenance(argv, ds, cfg, *, seed, workers, created_utc, extra=None, git=None) -> dict; provenance_md(prov) -> str
md_table(headers, rows); fmt(x); json_safe(obj)                      # NaN → "NaN", ±inf → "Infinity"/"-Infinity"
```

Межа типів: модуль у Decimal-домені `backtest` не викликає `float(...)`/`Decimal(...)` з виразами (крива капіталу →
`features.convert.to_float`, numpy → `.item()`); тест `tests/arch/test_decimal_float_boundary.py` зелений.

## 2. Правило вибору робочої точки (не argmax SR)

Метод ε-обмеження (Haimes) на фронті Парето (SR↑, MaxDD↓, Turnover↓):

1. фронт — недоміновані клітинки (`backtest.pareto.pareto_front`, NaN — найгірше значення);
2. допустимі — клітинки фронту з `MaxDD ≤ dd_cap` (дефолт `dd_cap = state_machine.cool_enter = 0.08`: робоча точка,
   що вже на IS доходить до просадки COOLDOWN, неприйнятна; `--dd-cap` перекриває);
3. з допустимих — найбільший SR; рівність → менший оборот → менший індекс сітки (`rule = "eps_constraint"`);
4. якщо допустимих немає — клітинка фронту з найменшою MaxDD (рівність → більший SR → менший оборот → індекс;
   `rule = "fallback_min_dd"`).

Розв'язок `max SR s.t. MaxDD ≤ dd_cap` завжди недомінований, тож пошук серед фронту еквівалентний пошуку серед
усіх клітинок; правило механічне (застосовне в кожному фолді) і відрізняється від argmax SR щоразу, коли argmax SR
порушує обмеження просадки. Правило (`SELECTION_RULE_UK`) і стан (`rule`, розмір фронту/допустимої частини) пишуться
в кожен вивід. `runner.select_cell` (argmax SR, ENG-21) не змінено — скрипти використовують це правило (XS-01).

## 3. Формули

* Ланцюг OOS: `r_t` кожного сегмента (від бару першого рішення, `E_k(0)` — стартовий капітал фолду),
  `E = E₀·∏(1 + r)`; `C_k` — капітал ланцюга на початку сегмента k; оборот ланцюга
  `Σ_k N_k·C_k/E_k(0) / mean(E) · P/n` (номінал перемасштабовано на капітал ланцюга); метрики `CONCAT_KEYS` =
  `total_return, cagr, ann_vol, sharpe, sortino, max_drawdown, calmar, ulcer_index, turnover, n_trades, sr_period,
  skew, kurt, psr, n_obs, n_segments` (формули 17 метрик — `backtest.metrics`, `P = 525 600`).
* PSR: `Φ((ŜR − 0)·√(n−1)/√(1 − γ₃ŜR + (γ₄−1)/4·ŜR²))`, `ŜR` за період (1 бар), γ₃/γ₄ популяційні.
* DSR: `SR₀ = √Var(SR_i)·[(1−γ_E)Φ⁻¹(1−1/N) + γ_E Φ⁻¹(1−1/(Ne))]`, `γ_E = 0.5772`; `N` = фактичне число ОЦІНЕНИХ
  клітинок (усі 108, зокрема з NaN), `Var` — вибіркова (ddof = 1) по скінченних `SR_i` за період; `DSR = PSR(SR* = SR₀)`.
  Якщо `PSR < 0.95` — вивід містить пряме твердження «перевага статистично НЕ встановлена» (§5.15).
* Амдал: `S(p) = T(1)/T(p)`, `f = argmin Σ(S_p − 1/(f + (1−f)/p))²` на `[0, 1]` (`parallel.fit_serial_fraction`),
  Карп–Флатт `e(p) = (1/S − 1/p)/(1 − 1/p)`; «структурна» частка `f_struct = (старт пулу + n·pickle.dumps задачі)/T(1)`.
  Розклад: `compute = Σ` часу задач у воркерах, `ideal = compute/p`, `makespan = max_pid Σ`, `imbalance = makespan −
  ideal`, `residual = T(p) − старт − makespan`, `cpu_inflation = compute(p)/compute(1)`.
* Чутливість: рівні `θ₀·(1 + ℓ·span)`, `ℓ ∈ {−1, −½, ½, 1}`; для λ — пам'ять `N = 1/(1−λ)` множиться на
  `(1 + ℓ·span)`, `λ = 1 − 1/N` (λ ∈ (0, 1)); `n_ATR` округлюється. Розмах `k_swing = max − min` метрики по рівнях
  разом із базою; дугова еластичність `(Δm/|m₀|)/(Δθ/θ₀)` між крайніми рівнями (NaN, якщо `|m₀| ≈ 0`); для λ
  θ — пам'ять `1/(1−λ)`, та сама координата, в якій задано рівні (XS-06).

| параметр | де | база | span | рівні при базі | чому |
|---|---|---|---|---|---|
| `u_enter` | `risk_limits.hysteresis.enter` | 0.25 | ±20 % | 0.20, 0.225, 0.275, 0.30 | частота входів; тримає `u_exit < u_enter`; = вісь сітки |
| `u_exit` | `risk_limits.hysteresis.exit` | 0.12 | ±25 % | 0.09, 0.105, 0.135, 0.15 | ширина петлі гістерезису (§5.9) |
| `χ_ATR` | `risk_limits.sizing.chi_atr` | 2.0 | ±25 % | 1.5, 1.75, 2.25, 2.5 | відстань стопу і `q_atr ∝ 1/χ`; = вісь сітки |
| `ρ_base` | `risk_limits.sizing.rho_base` | 0.005 | ±50 % | 0.0025 … 0.0075 | масштаб позиції `q_atr ∝ ρ` |
| `λ_EWMA` | `risk_limits.sizing.ewma_lambda` | 0.94 | пам'ять ±50 % | 0.88, 0.92, 0.952, 0.96 | λ — не масштабний параметр |
| `σ_target` | `risk_limits.sizing.sigma_target` | 0.20 | ±50 % | 0.10 … 0.30 | рівень таргетування волатильності (§5.8) |
| `κ_min` | `detectors.agreement.kappa_min` | 0.35 | ±40 % | 0.21, 0.28, 0.42, 0.49 | підлога κ (§5.4), лишає κ_min ∈ (0, 1) |
| `n_ATR` | `detectors.features.n_atr` | 14 | ±50 % | 7, 10, 18, 21 | період ATR; = вісь сітки |

Чому саме ці 8: неперервні параметри ланцюга «рішення → розмір», задані експертно. МФ відкалібровано з даних (§5.5),
ліміти ризику — обмеження безпеки, детектори — окремий ablation, модель витрат — окремий експеримент (§5.14).

---

## 4. Скрипти

Спільні прапорці (`add_common_args`): `--db` (типово: вікно `data/dataset_window.json` з PostgreSQL через
`backtest.runner.load_db_window`, хеш свічок звіряється) | `--fixture [PATH]` (типово `fixtures/rest/binance_klines.json.gz`,
3000 барів BTCUSDT), `--symbol` (BTCUSDT), `--seed` (типово seed профілю, 20260918), `--workers` (типово min(8, CPU);
0 — послідовно в процесі), `--out DIR` (типово `artifacts/exp_search/<script>`, зі `--smoke` —
`artifacts/tmp/exp_search/<script>`), `--smoke`, `--smoke-days` (4: перші N діб вікна БД), `--database-url`
(перекриває `FUZZHELM_DATABASE_URL`), `--cooldown-policy scaled_entries|reduce_only` (типово `config/risk_limits.yaml`).
`bench_amdahl.py` приймає `--workers`, але ігнорує його: розміри пулу — `--ps`. `run_grid.py` і `run_walkforward.py`
мають ще `--allow-dirty` (див. §5).

Кожен вивід містить: точну команду (`uv run python …`), `dataset_hash` (свічки + специфікація інструмента + фандинг),
хеш лише свічок (= `data/dataset_window.json`), `config_hash` бази, `git_sha` і `git_dirty` (зміни поза `artifacts/`,
`docs/`, знято ДО обчислень), `git_dirty_any` (сирий `git status --porcelain`) і `git_dirty_paths` (перші 20 змін),
seed, кількість воркерів, прогрів; машиночитний JSON (NaN/inf рядками) і CSV + markdown-таблиці. Єдині
недетерміновані поля — час (`created_utc`, `wall_s`, стовпець `wall_s` клітинок), `run_id` і `persisted_runs`; решта
збігається байт у байт між повторами і між різною кількістю воркерів (перевірено рецензуванням, §6).

| скрипт | що робить | виводи (`<S>` = символ, `<E>` = рушій) |
|---|---|---|
| `run_walkforward.py [--engine mamdani\|linear\|both] [--cells N] [--dd-cap X] [--persist auto\|commit\|dry-run\|none]` | 6 фолдів профілю backtest (IS 15 / OOS 5 / крок 5 діб, embargo `2·max_lookback` = 1046, D-03); IS: усі клітинки сітки воркерами; фронт + правило §2; OOS обраної з прогрівом 523 бари без торгівлі; ланцюг OOS; `both` — Мамдані і лінійне на тих самих фолдах; запис фолдів у БД (§5) | `walkforward_<S>_<E>.{json,md}`, `_folds.csv` (IS vs OOS по фолдах), `_is_cells.csv` (усі клітинки IS: метрики, фронт, вибір, config_hash), `_oos_equity.csv` (ланцюг: i, close_ns, фолд, капітал); з `both` — `walkforward_<S>_engines.{json,md}` |
| `run_grid.py [--engine] [--cells N] [--dd-cap X] [--no-oos] [--persist …]` | 108 клітинок на всьому вікні (`equity_hash` кожної) + OOS кожної клітинки на 6 фолдах → ланцюг; фронт (SR_OOS, MaxDD_OOS, Turnover_OOS) і правило §2; DSR з N = число оцінених клітинок (простір вибору — OOS; довідково — усе вікно); PSR обраної + твердження; 108 рядків `run` kind=grid_cell (§5) | `grid_<S>_<E>.{json,csv,md}` |
| `bench_amdahl.py [--ps 1,2,4,8] [--cells 16] [--repeats 3] [--max-load 1.5] [--force] [--no-seq]` | T(p) пулу `run_parallel(segment_task, …, p)` на фіксованій підмножині клітинок усього вікна; повтори чергуються по p; медіана; S(p), f (НК), Карп–Флатт, f_struct, T_seq (без пулу), розклад накладних витрат, розмір pickle задачі/результату, час задач по процесах (гетерогенні ядра); `sysctl` (модель CPU, P/E ядра, пам'ять), load average до/після/на кожному замірі; load > `--max-load` на старті → код 2 (відмова) або з `--force` — гучне попередження і позначка «завантажена машина» у виводі й на рисунку | `amdahl_<S>.{json,csv,md}` |
| `sensitivity.py [--engine] [--target oos\|full] [--params a,b] [--levels -1,-0.5,0.5,1] [--base-cell N]` | OAT-чутливість 8 параметрів (§3), ціль — ланцюг OOS тих самих фолдів з фіксованими параметрами (`oos`) або все вікно (`full`); база — дефолтна конфігурація або клітинка сітки | `sensitivity_<S>_<E>_<target>.{json,csv,md}` |
| `plot_walkforward.py [--symbol] [--engine] [--in JSON] [--out DIR] [--smoke]` | парні стовпчики IS vs OOS (SR, MaxDD) по фолдах; крива ланцюга OOS з межами фолдів; Мамдані vs лінійне (якщо є `_engines.json`) | `walkforward_<S>_<E>_folds.png`, `_oos_equity.png`, `walkforward_<S>_engines.png` |
| `plot_pareto.py [--symbol] [--engine] [--in] [--out] [--smoke]` | три 2D-проєкції (оборот–SR, MaxDD–SR, оборот–MaxDD): доміновані, фронт, обрана, `dd_cap` | `grid_<S>_<E>_pareto.png` |
| `plot_amdahl.py [--symbol] [--in] [--out] [--smoke]` | S(p) (медіана + кожен повтор) проти межі Амдала з f (НК) і f_struct та ідеалу S = p; стовпчики T(p) = старт + makespan + залишок | `amdahl_<S>.png` |
| `plot_sensitivity.py [--symbol] [--engine] [--target] [--in] [--out] [--smoke]` | «торнадо» Δ SR / Δ MaxDD (п.п.) / Δ обороту при найменшому і найбільшому значенні параметра | `sensitivity_<S>_<E>_<target>.png` |

Рисунки: matplotlib `Agg`, лише 2D, українські підписи, 150 dpi, категоріальні кольори з перевіреної палітри
(`#2a78d6`, `#eb6834`, `#1baf7a`), легенда для ≥ 2 рядів, таблиці — у markdown поруч.

## 5. Запис у БД (лише головний процес скрипта; `workers.persist`)

`--persist auto` = `commit` для повного `--db`, `dry-run` для `--db --smoke` (усе в одній транзакції, наприкінці
ROLLBACK — перевіряє шлях запису на головній БД, нічого не лишаючи), `none` для `--fixture` (`commit` з фікстурою
заборонено: це не фіксований набір). Підключення — `make_engine(settings.database_url, role=fuzzhelm_app)`.

**Лише закомічений код (XS-11).** `--persist commit` відмовляє (код 2, до будь-яких обчислень), якщо `git_state().dirty`
(незакомічені зміни поза `artifacts/`, `docs/`) або git_sha недоступний; `--allow-dirty` — свідомий обхід (паспорт тоді
несе `git_dirty = 1`). Перед записом стан перевіряється ще раз: HEAD змінився або з'явились зміни в коді під час прогону
→ `git_dirty = 1` і замість COMMIT — ROLLBACK (статус рядків `rolled_back_tree_changed`, виводи все одно пишуться).
Паспорт `git_sha`/`git_dirty` = стан до обчислень (об'єднаний зі станом перед записом).

* `run_grid.py`: кожна клітинка → `create_run(Passport(kind=grid_cell, config=identity_dict, config_hash,
  dataset_hash = набір усього вікна, seed, engine, git_sha, git_dirty, instrument_id, tf, ts_from/ts_to — межі вікна))`
  → `finish_run(DONE, equity_hash, metrics)`. `run_metric`: 17 метрик + `sr_period, skew, kurt, psr, n_obs, n_fills,
  traded_notional, halted, halted_at, wall_s`, `git_dirty`, `pareto_full`; з OOS — `oos_<CONCAT_KEYS>`, `pareto_oos`,
  `selected_oos`; в обраної — `dsr_oos, dsr_sr0_oos, dsr_n_trials, dsr_var_sr_oos, psr_selected_oos` (без OOS —
  суфікс `_full`). Покрокової кривої (`equity_point`) немає — XS-04. `config_hash` воркера звіряється з паспортом;
  кожен записаний рядок перечитується в тій самій транзакції (статус, `config_hash`, `equity_hash`).
* `run_walkforward.py`: IS і OOS обраної клітинки кожного фолду → `run` kind=backtest з повним записом
  (`persist_backtest`: рішення, ордери, позиції, капітал з VaR/CVaR, ризик-події, журнал), `ts_from/ts_to` — межі
  сегмента прогону (OOS — разом із прогрівом), `run_metric` + теги `wf_fold`, `wf_oos` (0/1), `wf_cell`. Прогін
  повторюється в головному процесі з `record_traces=trades`; `equity_hash` OOS звіряється з воркером (`matches_worker`),
  IS — Шарп і дохідність.
* Ідентичний прогін (`ux_run_identity` = config_hash, dataset_hash, seed, engine, git_sha) уже є → новий рядок не
  пишеться, у виводі — `exists:<kind>:<status>` і його `run_id` (XS-04).

SQL для gate фази 7: `select count(*) from run where kind = 'grid_cell' and status = 'DONE' and git_sha = '<sha>';`
прогони фолдів: `select r.id, m.value from run r join run_metric m on m.run_id = r.id and m.name = 'wf_fold'`.

## 6. Швидка перевірка (виконано в цій хвилі; вивід — `artifacts/tmp/exp_search/`, рецензування — `artifacts/tmp/exp_search_review/`)

```bash
uv run python scripts/run_walkforward.py --fixture --smoke --workers 2 --engine both
uv run python scripts/run_walkforward.py --db --smoke --workers 4 [--engine both]  # persist dry-run (ROLLBACK)
uv run python scripts/run_grid.py --fixture --smoke --workers 2 [--no-oos]
uv run python scripts/run_grid.py --fixture --smoke --cells 24 --workers 4
uv run python scripts/run_grid.py --db --smoke --workers 4 [--symbol ETHUSDT --cells 2 --persist none]  # dry-run
uv run python scripts/run_grid.py --db --persist commit --cells 1           # брудне дерево → REFUSED, код 2
uv run python scripts/bench_amdahl.py --fixture --smoke                       # load > 1.5 → відмова, код 2
uv run python scripts/bench_amdahl.py --fixture --smoke --force               # позначено «завантажена машина»
uv run python scripts/sensitivity.py --fixture --smoke --workers 2 [--base-cell 71]
uv run python scripts/sensitivity.py --fixture --smoke --workers 4 --params u_enter,u_exit,chi,rho_base,lam,sigma_target,kappa_min,n_atr --levels=-1,-0.5,0.5,1
uv run python scripts/sensitivity.py --db --smoke --workers 4 --target full
uv run python scripts/plot_{walkforward,pareto,amdahl,sensitivity}.py --smoke   # або --in <JSON>
```

Детермінізм (рецензування): кожен швидкий прогін повторено з іншою кількістю воркерів (2 ↔ 4, 2 ↔ 0) і порівняно
`diff`: CSV кривих OOS, IS-клітинок і фолдів — байт у байт; JSON/MD відрізняються лише командою, часом і `workers`;
CSV сітки — лише стовпцем `wall_s`. Після швидких прогонів з `--db` кількість рядків `run`/`run_metric`/`equity_point`
у головній БД та сама (запит до `fuzzhelm-db-1`).

## 7. Тести (`tests/unit/test_experiments_search.py`, 17 тестів, ≈ 0.7 с)

Правило фронту: max SR у межі `dd_cap` ≠ глобальний argmax; рівність SR → менший оборот → індекс; фолбек на min MaxDD;
NaN — найгірше; DSR зі списку Шарпів = ручний розрахунок (SR₀ = 0.0229398204, DSR = 0.5875990, PSR = 0.8265592 для
N = 5) і = `metrics.expected_max_sr`; N рахує й NaN-клітинки, Var — лише скінченні; підгонка Амдала відновлює
відоме f = 0.12 (точно і з шумом ±1 %), Карп–Флатт = f; розклад накладних витрат; ланцюг OOS склеює доходності і
перемасштабовує оборот; 8 параметрів чутливості в межах ±50 %, `u_exit < u_enter`, недопустима амплітуда → помилка;
`apply_param` змінює рівно один параметр і `config_hash`; таблиця торнадо (порядок за розмахом, Δ, еластичність,
NaN — у кінці; еластичність λ — у координаті пам'яті); **оркестрація walk-forward без рушія** (підмінений
`run_parallel`): IS-задачі несуть лише бари IS свого фолду (без embargo і OOS), вибір — правилом фронту за IS-метриками
(не argmax SR і не найкраща на OOS клітинка), OOS-задача — обрана клітинка з прогрівом W = 523 бари всередині embargo і
`eval_start = W`; **git**: у тимчасовому репозиторії нові файли під `artifacts/`, `docs/` не роблять `dirty`, зміна
коду — робить, і тоді `commit_refusal` відмовляє (без `--allow-dirty`); підмножина клітинок, геометрія smoke-фолдів,
`json_safe`/`fmt`. `@pytest.mark.slow` (0.35 с): walk-forward на фікстурі (workers = 0) = `runner.run_cell` для тих
самих параметрів і сегмента; довжина ланцюга OOS.

## 8. Повні прогони — наступна хвиля (після коміту; порядок і оцінки часу)

Передумови: код закомічено, `git status --porcelain -- . ':(exclude)artifacts' ':(exclude)docs'` порожній (інакше
`--persist commit` відмовить), головна БД піднята, під час кроків 2–3 ніхто не редагує код (інакше ROLLBACK, XS-11).
Оцінки часу — екстраполяція калібрувальних прогонів (8 клітинок, 4 воркери, load 2.3–3.3); на тихій машині — менше.

1. **Тиха машина (load < 1.5, жодних інших агентів):** `uv run python scripts/bench_amdahl.py --db --symbol BTCUSDT
   --cells 16 --repeats 3` (≈ 9–10 хв), потім `uv run python scripts/plot_amdahl.py --symbol BTCUSDT`.
2. `uv run python scripts/run_grid.py --db --symbol BTCUSDT --engine mamdani --workers 8` (≈ 2.5–3.5 хв; COMMIT 108
   рядків grid_cell — першим з прогонів, що пишуть у БД, і без `scripts/run_backtest.py` на тому самому коміті після
   нього, XS-04), потім `uv run python scripts/plot_pareto.py --symbol BTCUSDT --engine mamdani`.
3. `uv run python scripts/run_walkforward.py --db --symbol BTCUSDT --engine both --workers 8` (≈ 5–7 хв, з них запис
   фолдів ≈ 20 с на рушій; COMMIT 12 фолдових прогонів kind=backtest на рушій), потім `uv run python
   scripts/plot_walkforward.py --symbol BTCUSDT --engine mamdani` і `… --engine linear`.
4. `uv run python scripts/sensitivity.py --db --symbol BTCUSDT --target oos --workers 8` (≈ 0.5–1 хв), потім
   `uv run python scripts/plot_sensitivity.py --symbol BTCUSDT --target oos`; за бажання `--target full` (≈ 0.5–1 хв) і
   `--base-cell <choice.index з grid_BTCUSDT_mamdani.json>`.
5. За бажання — кроки 2–3 з `--symbol ETHUSDT` (вікно і фандинг ETH є; швидка перевірка пройшла).

Gate фази 7: `select count(*) from run where kind = 'grid_cell' and status = 'DONE' and git_sha = '<sha>';` = 108.
Виводи — `artifacts/exp_search/<скрипт>/` (на `git_dirty` не впливають).
