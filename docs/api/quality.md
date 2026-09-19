# API модуля `quality` (фактичний, хвиля 1)

Інваріанти подій, AHP-ваги, погодинний скор якості Q, MLP-автокодувальник аномалій, стан здоров'я
конвеєра. Сигнатури звірені інтроспекцією коду. Автор: Андрій Жук, 2026.

`quality` — пакет-межа (AST-тести детермінізму/типів його не сканують): тут дозволені і `Decimal`
(інваріанти подій), і `float`/numpy (Q, MLP). Випадковість — лише `random_state=seed`.

---

## 1. `quality/invariants.py`

```python
class CandleLike(Protocol): tf, open_time_ns, close_time_ns, o, h, l, c, volume, quote_volume, trades_count, vwap
@dataclass(frozen=True) class InstrumentSpec(tick_size: Decimal, step_size: Decimal | None = None)
def check_candle(c: CandleLike, instrument: Instrument | InstrumentSpec | None = None, *,
                 check_volume_step: bool = True) -> list[str]
def check_trade(t: Trade, instrument=None) -> list[str]
def check_book(b: BookSnapshot, instrument=None) -> list[str]
def is_valid_candle(c, instrument=None) -> bool
def violation_code(v: str) -> str          # "PRICE_OFF_TICK:h" → "PRICE_OFF_TICK"
```
Порожній список = подія валідна. Коди (константи модуля): `NONPOSITIVE_PRICE`, `HIGH_BELOW_LOW`,
`HIGH_BELOW_MAX_OC` (h < max(o,c)), `LOW_ABOVE_MIN_OC` (l > min(o,c)), `NEGATIVE_VOLUME`,
`NEGATIVE_QUOTE_VOLUME`, `QUOTE_VOLUME_OUT_OF_RANGE` (qv ∉ [v·l, v·h]; qv = 0 при v > 0 — «не надано»,
Kraken), `NEGATIVE_TRADES_COUNT`, `VWAP_OUT_OF_RANGE`, `OPEN_TIME_UNALIGNED`, `CLOSE_TIME_MISMATCH`
(close ≠ open + tf − 1 мс), `PRICE_OFF_TICK:<o|h|l|c|price|book>`, `QTY_OFF_STEP:<volume|qty>`,
`NONPOSITIVE_QTY`, `BOOK_UNSORTED`, `BOOK_CROSSED`. Кратність перевіряється точно (`x % step == 0` у Decimal).
Без `instrument` — лише структурні інваріанти. Негативний контроль: 3000 справжніх барів REST — 0 порушень.

Примітка: канонічний `Candle` уже відкидає неузгоджений OHLC у конструкторі; `check_candle` приймає і
об'єкти в обхід валідації (`Candle.model_construct`, рядки БД) — тому працює як друга лінія.

## 2. `quality/ahp.py`

```python
SAATY_RI = {1: 0, 2: 0, 3: 0.58, 4: 0.90, 5: 1.12, 6: 1.24, 7: 1.32, 8: 1.41, 9: 1.45, 10: 1.49}
CR_THRESHOLD = 0.1;  CRITERIA = ("completeness", "validity", "timeliness", "continuity")
@dataclass(frozen=True) class AhpResult(weights: tuple[float, ...], lambda_max, ci, cr, n, ri, method); consistent: bool
def ahp_weights(matrix, method: "eig" | "power" = "eig") -> AhpResult
    # валідація: квадратна, > 0, діагональ 1, a_ij·a_ji = 1 з rtol 5e-3 (0.333 замість 1/3), n ∈ SAATY_RI
    # CI = (λ_max − n)/(n − 1), CR = CI/RI(n); ваги — головний власний вектор, Σ = 1
def load_matrix(config_dir=None) -> list[list[float]]           # config/dq_weights.yaml: ahp_matrix
def write_weights_yaml(result, path=None, *, decimals=6) -> str  # переписує лише рядки weights: і consistency_ratio:
python -m fuzzhelm.quality.ahp                                  # перерахувати й вписати в config/dq_weights.yaml
```
Фактичний результат на матриці конфігурації (вписаний у `config/dq_weights.yaml`):
`w = (0.455446, 0.262850, 0.140852, 0.140852)`, `λ_max = 4.009825`, `CI = 0.003275`, `CR = 0.003639`;
степенева ітерація збігається з `numpy.linalg.eig` до 2.2·10⁻¹⁶.

## 3. `quality/dq_score.py`

```python
TAU0_MS = 1000.0; HOUR_S = 3600.0
@dataclass(frozen=True) class DqInputs(expected_buckets, observed_buckets, total_count, invalid_count=0,
                                        anomaly_count=0, gap_seconds=0.0, lag_p95_ms=0.0, window_s=3600.0)
@dataclass(frozen=True) class DqScore(completeness, validity, timeliness, continuity, score, weights, inputs)
    def as_row(self) -> dict      # колонки таблиці dq_score (округлення NUMERIC(6,4)/(10,2)/(12,2))
def completeness(observed, expected) -> float      # N_obs/N_exp, clip [0,1]; N_exp = 0 → 1
def validity(invalid, anomalies, total) -> float   # 1 − (N_invalid + N_anomaly)/N_total, clip; N_total = 0 → 1
def timeliness(lag_p95_ms, tau0_ms=1000.0) -> float    # exp(−max(0, lag)/τ₀); NaN → ValueError
def continuity(gap_seconds, window_s=3600.0) -> float  # 1 − gap/3600, clip; NaN → ValueError
def check_weights(weights) -> tuple[4 × float]     # 4 шт., скінченні, ≥ 0, |Σ − 1| ≤ 1e−6
def dq_score(inputs, weights, tau0_ms=1000.0) -> DqScore     # Q = Σ wᵢ·компонентаᵢ ∈ [0, 1]
def load_dq_weights(config_dir=None) -> tuple[4 × float]    # weights з YAML (ренормовані) або AHP з ahp_matrix
def load_tau0_ms(config_dir=None) -> float
def p95(values) -> float                           # numpy percentile 95 (лінійна інтерполяція); порожньо → 0
def merged_length_ns(intervals) -> int             # довжина об'єднання [lo, hi) — перекриття не двічі
@dataclass class DqAccumulator(window_start_ns, window_ns=3600·10⁹):
    add_bucket(open_ns); add_checked(*, invalid=False, anomaly=False); add_lag_ms(x); add_gap(lo_ns, hi_ns)
    inputs(expected_buckets) -> DqInputs
```
Інваріант (property-тест): для будь-яких невід'ємних лічильників, лагу, прогалин і будь-яких ваг з симплекса
`0 ≤ Q ≤ 1` і `min(компонент) ≤ Q ≤ max(компонент)`. «Ідеальна година»: 60/60 кошиків, 0 невалідних,
0 с прогалин, лаг p95 = 0 мс → Q = 1 (будь-який лаг > 0 дає Q < 1).

## 4. `quality/anomaly_mlp.py`

```python
FEATURE_NAMES = ("dlogp", "log_rg_atr", "log_v_vbar", "bw_pct", "abs_z", "log_n_nbar", "body", "open_gap")
@dataclass(frozen=True) class ExtractorParams(n_atr=14, n_bb=20, bw_rank_window=200, volume_window=60)
class AnomalyFeatureExtractor(params=None):
    warmup: int                      # 219 при дефолтах (перший бар з вектором, рахуючи з 1)
    def update(self, bar: Bar) -> np.ndarray | None     # 8 ознак; нормування — за ПОПЕРЕДНІМИ барами
def feature_matrix(bars, params=None) -> tuple[np.ndarray (n×8), np.ndarray idx]
class AnomalyAutoencoder(seed=0, *, hidden=3, max_iter=500, quantile=0.99, activation="tanh"):
    threshold: float (q99 навчальних скорів); train_scores; n_iter; converged; fitted
    def fit(self, X) -> AnomalyAutoencoder       # StandardScaler → MLPRegressor((3,), tanh, 500, random_state=seed), ціль = вхід
    def score(self, X) -> np.ndarray             # ‖z − ẑ‖² у стандартизованому просторі
    def is_anomaly(self, X) -> np.ndarray        # score > threshold
@dataclass(frozen=True) class AnomalyVerdict(t_ns, score, threshold, anomaly, features)
class AnomalyScorer(model, params=None): def update(self, bar: Bar) -> AnomalyVerdict | None   # потоковий скоринг
ANOMALY_KINDS = ("price_spike", "wick", "volume_burst", "frozen")
def inject_anomaly(bars, i, kind, magnitude) -> Bar   # спотворена копія bars[i], OHLC узгоджений
```
Ознаки 1–5 — вектор брифінгу §5.17, 6–8 — розширення до 8-3-8 (§3); див. deviations.d/ingest_ws.md.

### 4a. `quality/anomaly_eval.py` — оцінювання на розмічених ін'єкціях (хвиля 3, PLAT-05)

```python
DEFAULT_MAGNITUDES = {"price_spike": (0.003, 0.02), "wick": (0.003, 0.02), "volume_burst": (5.0, 30.0), "frozen": (0, 0)}
ARCHITECTURES = {"8-3-8": 8, "5-3-5": 5}          # кількість перших ознак FEATURE_NAMES на вході
def structurally_valid(bar) -> bool                # ціни > 0, h ≥ max(o,c), l ≤ min(o,c), v ≥ 0, n ≥ 0
def plan_injections(lo, hi, *, per_kind, rng, magnitudes=DEFAULT_MAGNITUDES) -> InjectionPlan(positions, magnitudes)
def clean_pass(bars, snap_at) -> (X, idx, snaps)   # ознаки чистого ряду + знімки екстрактора ДО барів snap_at
def positive_vectors(bars, plan, snaps) -> {kind: ndarray}   # ін'єкції «по одній» на стані чистого ряду
def evaluate_injections(bars, *, train=(lo, hi), holdout=(lo, hi), seed, per_kind, normal=None,
                        magnitudes=..., architectures=...) -> EvalReport
    # EvalReport(seed, per_kind, n_train_bars, n_holdout_bars, train_vectors, excluded_train_vectors, neg_vectors,
    #            sigma_dlogp_train, archs={label: ArchResult(auc_all, per_kind{auc, recall_at_q99}, fpr_at_q99,
    #            threshold, n_iter, converged)}, sigma_dlogp_holdout, feature_drift{name: {sd_ratio, tail_share}})
def feature_drift(train_X, test_X) -> {name: {"sd_ratio", "tail_share"}}   # tail_share — частка поза q0.5…q99.5 train
```
Інваріанти: held-out строго після навчання (`ValueError` інакше); `normal[i] = False` вилучає бар з навчання;
позиції ін'єкцій — лише в held-out; усе випадкове — з `numpy.random.default_rng(seed)`.

**Виміряно на справжньому IS-вікні** (`uv run python scripts/train_anomaly_mlp.py`, робоча БД, BTCUSDT; навчання —
дні 1–15, ті самі 21 600 барів, що й калібрування МФ, `dataset_hash 9156616d…`; held-out — дні 16–20, 7 200 барів;
144 ін'єкції кожного з 4 типів; seed 20260918): ROC-AUC по всіх типах **0.9531 (8-3-8) проти 0.9729 (5-3-5)**,
FPR при q₉₉ на чистих held-out барах 0.0375 / 0.0233; по 6 seed — 0.9551 ± 0.0021 проти 0.9718 ± 0.0021 (8-3-8
програє в 6 з 6). Таблиця, типи й частоти ін'єкцій, дрейф ознак — `docs/figures/quality_mlp_rocauc.md`; висновки —
`docs/deviations.d/platform.md` PLAT-05. Попередній замір на 3000-барній фікстурі (0.9873 / 0.9796) відтворюється
`--from-fixture --rate 0.125`.

## 5. `quality/health.py`

```python
@dataclass class PipelineHealth(lag_window=4096):
    on_frame(conn, ts_ingest_ns); on_event(kind, ts_event_ns, ts_ingest_ns) -> lag_ms; on_invalid(code)
    on_duplicate(); on_stale(); on_disconnect(cls, *, watchdog=False); on_gap(gap_id, status)
    on_candle_closed(*, anomaly=False)
    lag_p95_ms: float | None (ковзні останні lag_window подій; від'ємний лаг → 0); open_gaps: int; q: float | None
    def snapshot(self) -> HealthSnapshot
@dataclass(frozen=True) class HealthSnapshot(frames, frames_by_conn, events_by_kind, invalid, invalid_by_code,
    duplicates, stale, reconnects, watchdog_fires, disconnects_by_class, gaps_opened, gaps_open, gaps_by_status,
    candles_closed, anomalies, lag_p95_ms, lag_max_ms, last_ingest_ns, last_event_ns, q); def as_dict(self) -> dict
```
Для `risk.StaleDataGuard`: лаг — `now − health.last_event_ns` або `health.lag_p95_ms`, Q — `health.q`
(оновлюється `IngestPipeline.dq_scores()` / `finish()`).

## Приклад

```python
from fuzzhelm.quality.dq_score import DqInputs, dq_score, load_dq_weights
s = dq_score(DqInputs(expected_buckets=60, observed_buckets=58, total_count=3600, invalid_count=2,
                      gap_seconds=120.0, lag_p95_ms=150.0), load_dq_weights())
row = s.as_row()      # → storage.repositories.dq.DqRepo.upsert(DqRow(instrument_id, hour_start_ns, **row))
```
