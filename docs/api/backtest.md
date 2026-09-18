# API модуля `backtest` — аналітика (фактично реалізоване)

Пакет: `src/fuzzhelm/backtest/`. Тут — `metrics, walkforward, pareto, manifest, parallel, grid`.
`engine.py` (подієвий рушій) — хвиля 2. Статистика рахується у float/numpy; Decimal-капітал
перетворюється лише через `features.convert.to_float`, numpy-скаляри — через `.item()`.

---

## 1. `backtest.metrics`

```python
NumSeq = Sequence[Decimal] | Sequence[float] | Sequence[int] | NDArray
METRIC_NAMES: tuple[str, ...]            # рівно 17, порядок нижче
EULER_GAMMA = 0.5772

def compute_metrics(equity: NumSeq, trades: Sequence[Any], periods_per_year: float, *, rf: float = 0.0,
                    positions: NumSeq | None = None,
                    traded_notional: Decimal | float | None = None) -> dict[str, float]
def returns_from_equity(equity) -> NDArray      # r_t = E_t/E_{t−1} − 1 (E ≤ 0 → ValueError)
def drawdown_series(equity) -> NDArray          # DD_t = 1 − E_t/max_{τ≤t} E_τ (E_0 ≤ 0 → ValueError)
# NaN/inf у кривій капіталу → ValueError у compute_metrics / returns_from_equity / drawdown_series /
# max_drawdown / ulcer_index (а не тихий NaN у метриках)
def sharpe_ratio(r, periods_per_year, rf=0.0) -> float
def sortino_ratio(r, periods_per_year, rf=0.0) -> float
def max_drawdown(equity) -> float; def ulcer_index(equity) -> float
def moments(r) -> tuple[float, float]           # (γ₃, γ₄) популяційні; стала серія → (0, 3)
def psr(sr: float, n: int, skew: float = 0.0, kurt: float = 3.0, sr_star: float = 0.0) -> float
def psr_from_returns(r, sr_star: float = 0.0) -> float
def expected_max_sr(trial_srs) -> float         # SR₀; N < 2 → 0
def dsr(sr: float, n: int, skew: float, kurt: float, trial_srs) -> float
```

**17 метрик** (`compute_metrics` повертає рівно ці ключі в цьому порядку; `r` — прості доходності
кривої, `n = len(r)`, `P = periods_per_year`, для 1m 24/7 `P = 525600`):

| # | ключ | визначення |
|---|---|---|
| 1 | `total_return` | `E_T/E_0 − 1` |
| 2 | `cagr` | `exp(ln(E_T/E_0)·P/n) − 1` (переповнення → `inf`; `E_T ≤ 0` → −1) |
| 3 | `ann_vol` | `std(r, ddof=1)·√P` |
| 4 | `sharpe` | `√P·(mean r − rf)/std(r, ddof=1)` |
| 5 | `sortino` | `√P·(mean r − rf)/√(mean min(r,0)²)` (нижнє відхилення відносно 0, як у брифінгу) |
| 6 | `max_drawdown` | `max_t DD_t` (частка) |
| 7 | `calmar` | `cagr/max_drawdown` |
| 8 | `ulcer_index` | `√(mean DD_t²)` (частки, не відсотки) |
| 9 | `profit_factor` | `Σ pnl>0 / |Σ pnl<0|` за угодами |
| 10 | `expectancy` | `p·avg_win − (1−p)·avg_loss` (валюта рахунку на угоду) |
| 11 | `win_rate` | `p = #(pnl>0)/#угод` |
| 12 | `avg_win` | середній pnl прибуткових угод |
| 13 | `avg_loss` | модуль середнього pnl збиткових угод |
| 14 | `n_trades` | кількість угод |
| 15 | `turnover` | `Σ|номінал| / mean(E) · P/n` — річний оборот у капіталах |
| 16 | `exposure` | частка точок кривої з ненульовою позицією (`positions`), без них — `NaN` |
| 17 | `tail_ratio` | `|q95(r)| / |q05(r)|` |

`trades` — `ClosedTrade` (використовуються `pnl`, для обороту `entry_notional + exit_notional`) або просто
числа PnL; `traded_notional` (напр. `Portfolio.traded_notional`) має пріоритет над сумою з угод. Ділення на
нуль: `0/0 = 0`, `a/0 = ±inf` (напр. PF без збитків). Нульовий сигнал дає нулі в усіх 16 метриках (і
`exposure = 0`, якщо передано позиції). Застереження: у `expectancy` беззбиткові угоди (pnl = 0)
потрапляють у частку `1−p` — так задано формулою брифінгу.

**PSR / DSR** (Bailey & López de Prado): `ŜR` — Шарп **за період** (не річний), `n` — число доходностей,
`γ₃`/`γ₄` — популяційні асиметрія і куртозис (нормальний γ₄ = 3):
`PSR = Φ((ŜR − SR*)·√(n−1)/√(1 − γ₃ŜR + (γ₄−1)/4·ŜR²))`;
`SR₀ = √Var(SR_i)·[(1−γ_E)Φ⁻¹(1−1/N) + γ_E Φ⁻¹(1−1/(Ne))]`, `Var` — вибіркова (ddof=1) по фактичних N
прогонах (Шарпи теж за період); `DSR = PSR(SR* = SR₀)`. Від'ємний підкореневий вираз або нескінченний `ŜR` (нульова дисперсія доходностей) → `ValueError`
(`psr_from_returns` на всіх нулях дає 0.5).
Перевірено на опублікованому прикладі (N=100, V=1/2 річн., SR=2.5 річн., T=1250, γ₃=−3, γ₄=10):
`SR₀ = 0.11317`, `DSR = 0.90040`.

Приклад:
```python
m = compute_metrics(equity_curve, portfolio.closed_trades, periods_per_year=525600,
                    positions=pos_series, traded_notional=portfolio.traded_notional)
r = returns_from_equity(equity_curve); g3, g4 = moments(r)
sr = sharpe_ratio(r, 1.0)                       # за період: √1 = 1
p = psr(sr, len(r), g3, g4); d = dsr(sr, len(r), g3, g4, trial_srs=[... Шарпи 108 клітинок за період ...])
```

---

## 2. `backtest.walkforward`

```python
@dataclass(frozen=True, slots=True)
class Fold: index; is_start; is_end; embargo_start; embargo_end; oos_start; oos_end   # [start, end)
    is_range; embargo_range; oos_range (range); embargo_bars (int)

def make_folds(n_bars, is_bars, oos_bars, step_bars, embargo_bars, k, *,
               anchor: Literal["start", "end"] = "start") -> list[Fold]
def folds_from_profile(n_bars, *, max_lookback: int, bars_per_day: int = 1440,
                       profile: Mapping | None = None) -> list[Fold]    # config/profiles/backtest.yaml
```

`s_i = offset + i·step`; `IS_i = [s_i, s_i + is − E)`, `embargo_i = [s_i + is − E, s_i + is)`,
`OOS_i = [s_i + is, s_i + is + oos)`. Embargo вирізається з кінця IS (OOS-вікна при `step = oos`
суцільні). Інваріант: `(IS ∪ E) ∩ OOS = ∅`, `max(IS.t) + E ≤ min(OOS.t)`. Помилки (`ValueError`):
`k·…` не вміщується в `n_bars`, `E ≥ is_bars`, непозитивні розміри. Профіль за замовчуванням:
15/5/5 днів × 6 фолдів на 45 днях, `embargo_bars: null → 2·max_lookback`.

## 3. `backtest.pareto`

```python
Sense = Literal["max", "min"]; DEFAULT_SENSES = ("max", "min", "min")   # (SR_OOS, MaxDD_OOS, Turnover)
def dominates(a, b, senses=DEFAULT_SENSES) -> bool
def pareto_front(points: Sequence[Sequence[float]], senses=DEFAULT_SENSES) -> list[int]
```
Індекси недомінованих точок у порядку входу; однакові точки обидві на фронті; `NaN` — найгірше значення.

## 4. `backtest.manifest`

```python
def canonicalize_config(obj) -> Any          # float → Decimal(repr), кортежі → списки
def config_hash(config: Mapping) -> str       # BLAKE2b-256(canonical_json), hex
def dataset_hash(columns: Mapping[str, NDArray]) -> str      # колонки свічок, канонічна форма, hex
def dataset_hash_candles(candles: Sequence[Candle]) -> str   # альтернатива для DTO (не взаємозамінна)
def equity_hash(equity: Sequence[Decimal], ts_ns: Sequence[int] | None = None) -> str   # SHA-256, hex
def read_git_sha(repo: Path = ROOT) -> tuple[str | None, bool | None]   # (sha, dirty) через git

class RunManifest(BaseModel, frozen):
    kind: RunKind; engine: EngineKind; seed: int; config_hash: str; dataset_hash: str
    git_sha: str | None; git_dirty: bool | None; journal_head_hash: str | None; equity_hash: str | None
    def identity(self) -> tuple   # (config_hash, dataset_hash, seed, engine, git_sha) = ux_run_identity
    def with_results(self, *, journal_head: bytes | None = None, equity=None, equity_ts_ns=None) -> RunManifest

def build_manifest(*, kind, engine, seed, config, dataset, journal_head=None, equity=None,
                   equity_ts_ns=None, git=True, repo=ROOT) -> RunManifest
```
Канонічна форма `dataset_hash`: колонки за іменем; для кожної — `canonical_json([ім'я, dtype, shape])` +
сирі байти (`int*/uint*/bool → '<i8'`, `float → '<f8'`, `−0.0 → 0.0`, `NaN` заборонено).
`equity_hash`: значення квантуються до `1e−18` → однакові числа з різним експонентом дають однаковий хеш.
Хеші — hex-рядки; у БД (`BYTEA`) — `bytes.fromhex(...)`.

## 5. `backtest.parallel`

```python
def task_seed(base_seed: int, index: int) -> int
def run_parallel(fn: Callable[[T], R], tasks: Iterable[T], workers: int, *, seed: int = 0,
                 chunksize: int = 1, mp_context: str = "spawn") -> list[R]
def worker_probe(task) -> dict                 # діагностика: prec/rounding Decimal, pid, random()
def amdahl_bound(p, f) -> float                # 1/(f + (1−f)/p)
def speedups(times: Mapping[int, float]) -> dict[int, float]
def karp_flatt(p, s) -> float
def fit_serial_fraction(ps, ss) -> float       # НК на самих S, f ∈ [0, 1]
@dataclass class AmdahlReport: workers; times; speedup; serial_fraction; bound; karp_flatt
def measure_speedup(fn, tasks, workers=(1, 2, 4, 8), *, seed=0, repeats=1, timer=time.perf_counter) -> AmdahlReport
def amdahl_report(times: Mapping[int, float]) -> AmdahlReport
```
Результати — **у порядку задач**. `workers ≥ 1` — `ProcessPoolExecutor` (initializer: `setup_decimal_context()`
+ seed глобальних ГВЧ), `workers = 0` — послідовно в поточному процесі (стан ГВЧ викликача
відновлюється). Перед кожною задачею — свіжий `DECIMAL_CONTEXT` і глобальні ГВЧ, пересіяні
`task_seed(seed, index)`, тож результат не залежить від кількості воркерів навіть для «брудних» задач.
`fn` — функція верхнього рівня модуля (pickle, старт "spawn"). T(p) включає старт пулу.

## 6. `backtest.grid`

```python
DEFAULT_SPACE = {"n_atr": (7, 14, 21), "chi": (1.5, 2.0, 2.5), "u_enter": (0.20, 0.25, 0.30),
                 "rho_base": (0.0025, 0.005), "lam": (0.94, 0.97)}
def make_grid(space: Mapping[str, Sequence] | None = None) -> list[dict[str, Any]]   # 108 за замовчуванням
def load_grid_space(profile: Mapping | None = None) -> dict   # ключ `space` у config/profiles/grid.yaml, інакше DEFAULT_SPACE
```
Порядок: декартів добуток осей у порядку ключів, остання вісь найшвидша; `make_grid()[0] =
{n_atr: 7, chi: 1.5, u_enter: 0.2, rho_base: 0.0025, lam: 0.94}`; дефолтна конфігурація
`{14, 2.0, 0.25, 0.005, 0.94}` — одна з клітинок. `n_atr` — період ATR Уайлдера; `chi` — множник ATR у
`q_atr`; `u_enter` — поріг входу гістерезису (вихід 0.12); `rho_base` — ризик на угоду; `lam` — λ EWMA
vol-target.
