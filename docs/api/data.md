# API компонента `data` (фактичний, хвиля 2)

Реальні дані для роботи: 45-денний REST-добір у PostgreSQL, історія ставок фінансування, крос-звірка
Binance ↔ Kraken, реальні рядки `ingest_gap` зі статусом FILLED і калібрування МФ на реальних даних.
Користуватися компонентом можна через CLI `fuzzhelm` (див. `docs/api/cli.md`) або через функції нижче.
Сигнатури звірено інтроспекцією коду 2026-09-19. Автор: Андрій Жук, 2026.

Розходження зі спекою описано в `docs/deviations.d/data.md` (DATA-01…12), журнал робіт — у `docs/journal.d/data.md`.

---

## 1. Артефакти даних (закомічені, невеликі)

| Файл | Що всередині | Хто пише / читає |
|---|---|---|
| `data/dataset_window.json` | Зафіксоване вікно `[2026-08-04T00:00Z, 2026-09-18T00:00Z)` (45 днів, 1m, BTCUSDT+ETHUSDT). Там само перше IS-вікно (дні 1–15) і для кожного символу кількість рядків, перший/останній бар і `dataset_hash` | пише `fuzzhelm backfill`; читають `calibrate`, `fetch-funding`, бектест |
| `data/funding_<SYMBOL>.json` | Історія ставок фінансування за вікном: 135 записів на символ, рядки біржі дослівно, `rows_digest` | пише `fuzzhelm fetch-funding`; читає `ingest.funding.load_funding_json` (у т.ч. `api/backtest_runner.funding_arrays`) |
| `data/calibration_manifest.json` | Маніфест калібрування: дані (вікно, хеш), параметри методу, seed, хеш `detectors.yaml`, повний результат. `id` = `source_run_id` у `membership.yaml` | пише `fuzzhelm calibrate` |
| `data/crosscheck_input.json.gz` | Сирі відповіді Kraken (XBTUSD, USDTUSD) і Binance (klines, indexPriceKlines, premiumIndex) одного вікна | пише `fuzzhelm crosscheck`; `--from-input` перераховує звіт офлайн |
| `config/membership.yaml` | Калібровані T (перцентилі) і V (KMeans); `provisional: false`, `source_run_id: cal-727167b30ad655a5` | пише `fuzzhelm calibrate` (`regimes.calibrate_mf.write_membership_yaml`) |

Формат `data/funding_<SYMBOL>.json` (v1):
```json
{"v": 1, "source": "GET https://fapi.binance.com/fapi/v1/fundingRate", "venue": "BINANCE_USDM",
 "symbol": "BTCUSDT", "symbol_canon": "BTC-USDT-PERP", "window": {...}, "fetched_at_utc": "...",
 "requests": 1, "count": 135, "columns": ["funding_time_ms", "funding_rate", "mark_price", "rate_type"],
 "rows_digest": "<BLAKE2b-256 canonical_json(rows)>", "rows": [[1785801600004, "0.00003081", "…", "Regular"], …]}
```

**Фінансування і `dataset_hash`:** `ingest.funding.funding_columns(rates)` повертає колонки `funding_t_ns`
(int64, нс) і `funding_rate` (float64 через `features.convert.to_float`). `backtest.dataset.Dataset`, створений
із `funding_t_ns/funding_rate`, додає обидві колонки в `Dataset.columns()`, а отже і в
`backtest.manifest.dataset_hash`. Інша ставка дає інший датасет, а отже й інший паспорт прогону (тест
`test_funding_columns_enter_the_backtest_dataset_hash`). Файли з `data/` у `Dataset` передає
`api/backtest_runner.funding_arrays`.

## 2. `ingest/rest_client.py` — нові ендпоінти `BinanceRestClient`

```python
async def agg_trades(self, symbol: str, *, from_id: int | None = None, start_ms: int | None = None,
                     end_ms: int | None = None, limit: int = 500) -> list[dict[str, Any]]   # limit ≤ 1000
async def index_price_klines(self, pair: str, interval: str = "1m", start_ms: int | None = None,
                             end_ms: int | None = None, limit: int = 1500) -> list[list[Any]]
async def funding_rate_history(self, symbol: str, start_ms: int | None = None, end_ms: int | None = None,
                               limit: int = 1000) -> list[dict[str, Any]]                  # limit ≤ 1000
AGG_TRADES = "/fapi/v1/aggTrades"; INDEX_PRICE_KLINES = "/fapi/v1/indexPriceKlines"; FUNDING_RATE = "/fapi/v1/fundingRate"
MAX_AGG_TRADES_LIMIT = 1000; MAX_FUNDING_LIMIT = 1000
```
Запит іде тим самим транспортом `_get`: відро (вага) → GET → ресинхронізація за заголовком → повтор за
`RetryPolicy`. `limit` поза межами дає `ValueError`, відповідь не того JSON-типу —
`NormalizationError(field="$")`. Ваги (`ratelimit`, виміряні — DATA-02): `aggTrades` = 20,
`indexPriceKlines` рахується як klines за `limit`, `fundingRate` = 1. Останнє — лише облік у власному відрі:
біржа REQUEST_WEIGHT на ньому не списує.

## 3. `ingest/normalize.py` — нові нормалізатори

```python
def normalize_rest_agg_trade(row: Mapping[str, Any], instrument: InstrumentLike, ts_ingest_ns: int) -> Trade
    # строга схема {a, p, q, f, l, T, m[, nq]}; event_uid = trade_uid(...) — той самий, що у WS (WS-04, DATA-03)
@dataclass(frozen=True, slots=True)
class FundingRate: instrument: str; venue: Venue; funding_time_ns: int; funding_rate: Decimal;
                   mark_price: Decimal | None; rate_type: str | None = None      # .funding_time_ms
def normalize_funding_rate(row: Mapping[str, Any], instrument: InstrumentLike) -> FundingRate
    # поля {symbol, fundingTime, fundingRate[, markPrice, rateType]}; невідоме поле / не той тип / чужий символ /
    # markPrice ≤ 0 → NormalizationError(field=…); markPrice "" (старі записи) → None; масштаб рядка зберігається
def normalize_funding_rates(rows, instrument) -> list[FundingRate]
    # за зростанням часу, дублікати того самого запису зливаються, дві РІЗНІ ставки на один момент →
    # NormalizationError(field="[i].fundingTime"); помилка рядка i → field="[i].<поле>"
```
`pipeline.normalize_rest_agg_trade` лишено як реекспорт (історичне місце імпорту).

## 4. `ingest/funding.py` — історія ставок фінансування

```python
async def fetch_funding_history(client: BinanceRestClient, instrument: InstrumentLike, start_ms: int, end_ms: int,
                                *, limit: int = 1000) -> tuple[list[FundingRate], int]
    # усі ставки з fundingTime ∈ [start_ms, end_ms]; наступна сторінка — з (останній час + 1 мс); (ставки, запитів)
def rows_digest(rows) -> str                         # BLAKE2b-256(canonical_json(rows)), hex
def funding_document(rates, instrument, *, base_url, window, fetched_at_utc, requests) -> dict[str, Any]
def write_funding_json(path, doc) -> Path            # атомарно (tmp + replace)
def load_funding_json(path, instrument) -> FundingSeries(meta, rates)
    # перевіряє v/columns, rows_digest (файл не змінено після добору), символ; кожен рядок — тим самим нормалізатором
def funding_columns(rates, prefix="funding_") -> {"funding_t_ns": int64[], "funding_rate": float64[]}
```

## 5. `ingest/pipeline.py`

`fetch_agg_trades(client, symbol, from_id, to_id, instrument, *, limit=1000, sleep=None) -> (угоди, запитів)`
тепер ходить через `client.agg_trades`. Параметр `sleep` лишено для сумісності, і він більше не діє: паузи
повторів задає `sleep` клієнта (DATA-03).

## 6. `regimes/calibrate_mf.py` — розширення хвилі 2

```python
def calibrate(vol_pct, ema_slope_norm, volume_z, t_series, *, seed: int, k_range=range(2, 7), n_init=10,
              run_id=None, silhouette_sample=10_000, min_gap=1e-3, min_sigma=1e-3,
              sigma_rule: str = "nearest",      # "nearest" (буквально §5.5) | "cover" (DATA-05)
              t_symmetric: bool = False) -> dict # True — перцентилі вибірки T ∪ −T (DATA-06)
def v_sigmas(centres) -> list[float]              # 0.5·d до найближчого сусіднього центру
def v_sigmas_cover(centres, lo=0.0, hi=1.0) -> list[float]
    # 0.5·max(d⁻, d⁺) з дзеркальними привидами 2·lo − m₁, 2·hi − m_k; теорема: max_i μ_i ≥ e^−½ на [lo, hi]
def coverage_min(centres, sigmas, lo=0.0, hi=1.0, n=2001) -> tuple[float, float]   # (min_x max_i μ_i, x)
def t_symmetric_breakpoints(t) -> list[float]     # [−a84, −a50, 0, a50, a84] за |T|; точно антисиметричні
def t_terms(breakpoints) -> dict                  # 5 термів T (Руспіні для будь-яких зростаючих точок)
def write_membership_yaml(result, path, *, source_run_id=None) -> Path
```
Нові поля результату: `T.symmetric`, `T.raw_breakpoints`, а також `V.sigma_rule`, `V.sigmas_nearest`,
`V.coverage_min[_at]` і `V.coverage_min_nearest[_at]`. За замовчуванням бібліотечна функція поводиться, як
у хвилі 1 (`nearest`, без симетризації). Робоче калібрування (`cli.run_calibration`) передає
`sigma_rule="cover"` і `t_symmetric=True`.

## 7. `fuzzhelm.cli` — функції, придатні для повторного використання

```python
@dataclass(frozen=True) class DatasetWindow(start_ms, end_ms, tf="1m", symbols=("BTCUSDT", "ETHUSDT"))
    # межі — UTC-півночі, кінець виключно; .ending_at(end_ms, days, symbols), .days, .bars_per_symbol,
    # .last_open_ms, .sub_window(first_day, n_days) -> (lo, hi), .to_dict()/.from_dict()
def load_window(path) -> tuple[DatasetWindow, dict]           # CliError, якщо файлу немає
def pages_needed(n_bars, limit) -> int                        # перекриття 1 бар: 1 + ⌈(n − limit)/(limit − 1)⌉
def plan_backfill(window, limit=1500) -> BackfillPlan         # сторінки, запити, вага (виміряна таблиця klines)
def window_document(window, stats) -> dict                    # вміст data/dataset_window.json
def backfill_report_md(stats) -> str
def analyze_crosscheck(raw, threshold_bps=50) -> CrosscheckAnalysis(report, stats, decomposition, series)
    # чиста функція від збережених сирих відповідей: mean/median/p95 (найближчий ранг)/max б.п., кількість
    # > порогу і точний лог-розклад на премію перпетуала, дисконт USDT і залишок
def crosscheck_report_md(a, raw, *, input_path, command) -> str
def calibration_series(bars, detectors_cfg) -> CalibrationSeries(t_ns, T, R, V, vol_pct, ema_slope_norm, volume_z,
                                                                 warmup_bars)
    # бари → FeaturePipeline → 6 детекторів → decision.aggregator.consensus (той самий код, що в рушії);
    # до прогріву max(warmup детекторів, max_lookback) = 523 барів — NaN; причинна (майбутнє не змінює минуле)
def run_calibration(bars, *, seed, detectors_cfg, dataset) -> CalibrationRun(result, series, manifest, run_id)
def resolve_calibration_outputs(args) -> set[str]       # ⊆ CAL_OUTPUTS = (membership, manifest, report, figure)
    # робочі шляхи за замовчуванням — лише калібрування з БД без --no-write; --from-fixture/--no-write пишуть
    # тільки явно задані шляхи; --no-write ніколи не пише YAML (рецензія, DATA-11)
def manifest_id(manifest) -> str                              # "cal-" + 16 hex BLAKE2b канонічного маніфесту
def cluster_labels(series, centroids) -> (X, labels); def standardized_centroids(series, centroids) -> ndarray
def cluster_interpretation_md(series, centroids) -> list[str] # домінантна ознака кластера (DATA-09)
def calibration_report_md(run, *, window_doc, command, membership_path) -> str
def load_fixture_bars(path) -> (bars, meta)                   # офлайн-бари з fixtures/golden (тести)
def replay_run_id(session_path) -> UUID                       # uuid5(sha256 файлу сесії): повтор — no-op
```

## Приклад: калібрування офлайн на фікстурі

```python
from pathlib import Path
from fuzzhelm import cli
from fuzzhelm.config import load_yaml

bars, meta = cli.load_fixture_bars(Path("fixtures/golden/source_klines_btcusdt_1m.json"))
run = cli.run_calibration(bars, seed=20260918, detectors_cfg=load_yaml("detectors"), dataset=meta)
run.run_id, run.result["V"]["centres"], run.result["T"]["breakpoints"]
```
