# API: `features` + `detectors` + `regimes` (фактично реалізоване)

Автор: Андрій Жук, 2026. Пакети float-домену: без `decimal`, без настінного часу/випадковості
(`regimes` — поза межею детермінізму, але використовує лише `random_state=seed`).
Вхід — `fuzzhelm.features.convert.Bar` (float-двійник закритої свічки, `bar_from_candle(Candle)`).

Типовий цикл (live, реплей, бектест — однаковий код):

```python
from fuzzhelm.config import load_yaml
from fuzzhelm.features.pipeline import FeatureParams, FeaturePipeline
from fuzzhelm.features.window import BarWindow
from fuzzhelm.detectors.registry import build_detectors, MIN_WINDOW_CAPACITY

cfg = load_yaml("detectors")
pipe = FeaturePipeline(FeatureParams.from_config(cfg))
window = BarWindow(capacity=64)            # ≥ MIN_WINDOW_CAPACITY (= 2)
detectors = build_detectors(cfg)           # 6 шт., порядок DETECTOR_NAMES

for bar in bars:                           # закриті бари за зростанням часу
    window.append(bar, pipe.update(bar))
    outputs = [d.compute(window) for d in detectors]   # list[DetectorOutput]
    V = outputs[-1].features["V"]                      # VolRegime завжди має ключ "V"
```

Виміряно (Python 3.12.12, Apple arm64, 100 000 барів реальних BTCUSDT 1m, медіана 5 прогонів):
`pipeline.update` — **4.36 мкс/бар**; `pipeline.update + window.append + 6×compute` — **12.5 мкс/бар**
(3 прогони). Відтворити можна скриптом-заміром з `perf_counter` поза пакетом `features` (у самому
пакеті `perf_counter` забороняє арх-тест). Для 7·10⁶ бар-кроків це ≈ 87 с на ядро (екстраполяція
заміру, не окремий прогін).

---

## 1. `fuzzhelm.features.indicators`

Спільний контракт: `update(...) -> float | None` (None до прогріву), властивість `value`
(останнє значення або None). Ковзні класи мають `resync_every` (дефолт `DEFAULT_RESYNC = 1024`):
раз на стільки оновлень стан точно перераховується з кільцевого буфера (`math.fsum`, двопрохідно),
що обмежує дрейф округлення; амортизовано O(n/1024).

| Клас | Сигнатура `update` | Перше значення | Формула / примітки | Складність |
|---|---|---|---|---|
| `EMA(n)` | `update(x)` | на n-му значенні | α = 2/(n+1); ініціалізація = SMA перших n | O(1) |
| `WilderATR(n)` | `update(h, l, c)` | на барі з індексом n (n+1-й бар) | TR з c_{t−1}; ATR₀ = середнє перших n TR; далі α = 1/n. `alpha == 1/n` | O(1) |
| `WilderRSI(n)` | `update(c)` | на барі з індексом n | Ḡ, L̄ ініціалізуються середнім перших n змін; α = 1/n; RSI = 100·Ḡ/(Ḡ+L̄); Ḡ=L̄=0 → 50 | O(1) |
| `SMA(n)` | `update(x)` | n-те значення | ковзна сума + ресинхронізація | O(1) |
| `RollingWelford(n)` | `update(x) -> mean` | n-те значення | властивості `mean`, `var` (популяційна S/n), `sample_var`, `std`, `ready` | O(1) |
| `MonotonicDequeMax(n)` / `MonotonicDequeMin(n)` | `update(x)` | n-те значення | max/min останніх n (включно з x); `value` — поточний екстремум | амортизовано O(1) |
| `Parkinson(w)` | `update(h, l)` | w-те значення | σ_P = √(Σ ln²(h/l) / (4 ln2 · w)); h ≤ 0 або l ≤ 0 → внесок 0 | O(1) |
| `PercentileRank(n)` | `update(x)` | n-те значення | (1/n)·#{x_i ≤ x} у вікні з самим x → ∈ [1/n, 1]; NaN → `ValueError` | O(log n) пошук + **O(n) memmove** вставки/видалення |
| `RollingOLS(n)` | `update(y) -> slope` | n-те значення | регресія y на x = 0..n−1; `slope`, `r2`, `mean`, `ready`; R² := 0, якщо std(y) ≤ 1e−9·|ȳ| | O(1) |

Допоміжне: `log_range_sq(h, l) -> float`.

## 2. `fuzzhelm.features.pipeline`

* `REL_FLOOR = 1e-9` — масштабна «нуль-підлога» (розкид/ATR, менші за 1e−9·|ціна|, вважаються шумом округлення).
* `@dataclass(frozen=True, slots=True) FeatureParams`:
  `n_ema=21, ema_horizon=5, ema_r2_points=5, n_atr=14, n_rsi=14, rsi_div_lookback=14, bb_n=20, bb_k=2.0,
  bb_rank_window=200, donchian_n=20, park_window=24, vol_rank_window=500, volume_window=60, resync_every=1024`.
  * `FeatureParams.from_config(cfg: Mapping | None = None)` — з `config/detectors.yaml`:
    `ema_slope.{n_ema,horizon,r2_points}`, `rsi_exhaustion.{n,div_lookback}`, `bollinger_z.{n,k,bw_rank_window}`,
    `donchian.n`, `vol_regime.{window,rank_window}`, `features.{n_atr,volume_window}`; відсутні ключі → дефолти.
  * `warmup() -> dict[str, int]` — для кожного поля `Features`: номер бару (рахуючи з 1), на якому поле
    вперше не-None. Дефолти: `ema 21, ema_lag 26, ema_r2 25, atr 15, rsi 15, rsi_delta 29, close_delta 15,
    sma/sigma/bb_upper/bb_lower/bb_bw 20, bb_bw_rank 219, donch_hi/donch_lo 21, park_sigma 24, vol_rank 523,
    vol_mean/vol_z 60`. Перевірено тестом на реальних даних (рівно на цьому барі).
  * `max_lookback -> int` = max(warmup) = **523** при дефолтах.
* `@dataclass(frozen=True, slots=True) Features` — усі поля `float | None`, дефолт None (можна будувати частково в тестах):

| Поле | Значення |
|---|---|
| `ema`, `ema_lag` | E_t (α=2/22), E_{t−h} |
| `ema_r2` | R² OLS по останніх 5 значеннях EMA |
| `atr`, `rsi` | Уайлдер, n = 14 |
| `rsi_delta`, `close_delta` | RSI_t − RSI_{t−k}, c_t − c_{t−k}, k = `rsi_div_lookback` |
| `sma`, `sigma` | середнє і **популяційне** σ за 20 барів (Велфорд) |
| `bb_upper`, `bb_lower` | sma ± bb_k·σ |
| `bb_bw` | 2σ/|μ| (як у брифінгу §5.1) |
| `bb_bw_rank` | ρ_t — перцентильний ранг bb_bw за 200 барів |
| `donch_hi`, `donch_lo` | U_t = max(h_{t−20..t−1}), L_t = min(l_{t−20..t−1}) — канал **попередніх** 20 барів (без поточного) |
| `donch_ref`, `donch_dir`, `bars_since_breakout` | рівень останнього пробою (U при закритті `c_t > U_t`, L при `c_t < L_t`), напрям ±1, вік у барах (0 на барі пробою). None до першого пробою |
| `park_sigma` | σ_P Паркінсона, W = 24 |
| `vol_rank` | V_t — перцентильний ранг σ_P за 500 барів |
| `vol_mean`, `vol_z` | v̄ і (v − v̄)/σ_v за 60 барів (σ_v ≤ 1e−9·v̄ → 0) |

  `Features.as_dict() -> dict[str, float | None]`; `FEATURE_NAMES: tuple[str, ...]`.
* `FeaturePipeline(params: FeatureParams | None = None)`:
  `update(bar: Bar) -> Features` (O(1) амортизовано, крім O(L) memmove у рангах), `run(bars) -> list[Features]`,
  `max_lookback`, `bars_seen`, `params`. Стан лише в екземплярі; той самий потік барів → ті самі Features.
* `is_finite(x: float | None) -> bool`.

**Embargo walk-forward:** `max_lookback = 523` покриває всі скінченні вікна точно; рекурентні EMA/ATR/RSI
мають геометрично згасаючу пам'ять ((13/14)^523 ≈ 1.6e−17), тож залишковий вплив IS-барів після 523 барів — нижче точності float.

## 3. `fuzzhelm.features.window`

* `BarWindow(capacity: int)` — кільцевий буфер пар (Bar, Features).
  * `append(bar: Bar, feats: Features) -> None`
  * `bar(lag=0) -> Bar`, `feats(lag=0) -> Features`, `feat(name, lag=0) -> float | None`
  * `series(name, n) -> np.ndarray` — останні n значень від старого до нового; `name` — поле Bar
    (`o,h,l,c,v,qv,n,t_ns`) або поле Features; None → NaN. dtype — `int64` для `t_ns`/`n` (нс-мітки > 2^53
    у float64 втратили б точність), інакше `float64`.
  * `len(window)` — кількість збережених барів (≤ capacity); `t` — абсолютний індекс поточного бару (−1 до першого).
  * `lag < 0` або `n < 0` → **`LookaheadError`**; lag ≥ len / n > len → `IndexError` (нестача історії, не майбутнє).
* `GuardedArray(arr, cursor=0)` ≡ **`LookaheadGuard`** — повний масив бектесту з курсором:
  * `g[i]`, `g[a:b]`, `g[i, j]` — numpy-семантика; будь-який індекс > cursor (від'ємні рахуються від кінця
    ПОВНОГО масиву) → `LookaheadError`; зріз перевіряється за найбільшим індексом, який зачепить.
  * `cursor` (get/set, −1..len−1), `advance(steps=1) -> int`, `len(g) == cursor + 1`,
    `visible() -> np.ndarray` (read-only view на `arr[:cursor+1]`); повернені масиви — read-only views.

## 4. `fuzzhelm.detectors`

Усі класи реалізують Protocol `detectors.base.Detector` (атрибути `name, group, weight, warmup`;
`compute(window: BarWindow) -> DetectorOutput`). Чисті: читають лише `window.feats(0)`, `window.bar(0)`
і (CandleGeometry) `window.bar(1)`; не змінюють вікно; однаковий вміст вікна → однаковий вихід.
До прогріву вхідних ознак — `s = 0, c = 0` (для VolRegime ще й `features={"V": 0.5, "warm": 0.0}`).
Для будь-яких скінченних додатних OHLC і v ≥ 0: `s ∈ [−1,1]`, `c ∈ [0,1]`, усі `features` скінченні (property-тест).

| Клас (name) | Група | warmup (дефолти) | s | c | features |
|---|---|---|---|---|---|
| `EmaSlope(weight=1.0, *, horizon=5, scale=0.15, params=None)` (`ema_slope`) | TREND | 26 | tanh(g/0.15), g = (e_t − e_{t−5})/(5·max(ATR, 1e−9·e)) | R² OLS e_{t−4..t} | `g`, `r2` |
| `Donchian(weight=1.0, *, decay=0.3, d_scale=0.5, width_atr=6.0, params=None)` (`donchian`) | TREND | 21 | tanh(d/0.5), d = (p_t − U*)/ATR, U* — пробитий рівень | min(1, (U−L)/(6·ATR))·exp(−0.3·bars_since_breakout) | `d`, `width_atr`, `bars_since_breakout`, `direction` |
| `RsiExhaustion(weight=0.8, *, power=1.6, conf_power=0.8, base_w=0.6, div_w=0.4, rsi_scale=10.0, z0=0.1, params=None)` (`rsi_exhaustion`) | REVERSION | 29 | sign(z)·|z|^1.6, z = (50 − RSI)/50 | min(1, 0.6·|z|^0.8 + 0.4·div_score) | `rsi`, `z`, `div_score` |
| `BollingerZ(weight=0.8, *, z_scale=2.0, params=None)` (`bollinger_z`) | REVERSION | 219 | −tanh(z/2), z = (p − SMA)/max(σ, 1e−9·SMA) | 1 − ρ_t | `z`, `bw_rank` |
| `CandleGeometry(weight=0.6, *, w_pin=1.0, w_engulf=1.0, params=None)` (`candle_geometry`) | REVERSION | 21 | Σ sign_j v_j score_j / Σ v_j, v_j = w_j·score_j | max_j score_j·exp(−dist_j/(2·ATR)) | `pin_bull`, `pin_bear`, `engulf_bull`, `engulf_bear`, `prox_support`, `prox_resistance` |
| `VolRegime(weight=1.0, *, params=None)` (`vol_regime`) | CONTEXT | 523 | 0 | 1.0 (0 до прогріву) | `V` (= Features.vol_rank), `park_sigma`, `warm` |

Уточнення недовизначених місць брифінгу (усі без порогових `if` у скорах; обґрунтування — `docs/deviations.d/features.md`):

* **Donchian.** Пробій — подія «закриття поза каналом попередніх 20 барів»; U* = U при пробої вгору, L при пробої вниз.
  d > 0, поки ціна над пробитим рівнем (після пробою опору) і d < 0 після пробою підтримки/хибного пробою.
  Закриття рівно на пробитому рівні дає d = 0 (перший пробій стартує з s ≈ 0); за наявності давнішого пробою
  новий пробій переносить U* і c·s змінюється стрибком (неперервність за ціною загалом не гарантується).
  До першого пробою s = c = 0.
* **RsiExhaustion.div_score** ∈ [0,1]: `a = tanh(Δc_k/(ATR·√k))`, `b = tanh(ΔRSI_k/10)`, `ζ = tanh(z/0.1)`,
  `div = max(0, −ζ·a)·max(0, ζ·b)` — бичача дивергенція при перепроданості (ціна ↓, RSI ↑), ведмежа — дзеркально;
  «неправильна» дивергенція дає 0. Сила s від div не залежить.
* **CandleGeometry.** Патерни: `pin_bull` (формула брифінгу), `pin_bear` (дзеркало), `engulf_bull`, `engulf_bear`:
  `E± = σ_s(B_t/B_{t−1} − 1)·σ_s(±4δ_t − 1)·σ_s(∓4δ_{t−1} − 1)·min(1, Rg/ATR)`, δ = (c−o)/Rg. Ефективні
  екстремуми — max/min(o,h,l,c). `dist_j`: для бичачих — |low − L|, для ведмежих — |high − U| (канал Дончіана).
  Допоміжні чисті функції: `sigma_s(x)`, `pin_bar_scores(bar, atr) -> (π⁺, π⁻)`, `engulfing_scores(bar, prev, atr) -> (E⁺, E⁻)`.
  Властивість: при симетричних тінях π⁺ == π⁻ побітово ⇒ s = 0 точно; кожен π ≤ σ_s(−0.5)² ≈ 0.0333 (не 0).
* `rsi_exhaustion.rsi_strength(rsi, power=1.6) -> float` — відображення RSI → s окремо.
* `vol_regime.V_UNKNOWN = 0.5`.

**Реєстр** `fuzzhelm.detectors.registry`:
* `DETECTOR_NAMES = ("ema_slope", "donchian", "rsi_exhaustion", "bollinger_z", "candle_geometry", "vol_regime")`
* `MIN_WINDOW_CAPACITY = 2`
* `build_detectors(cfg: Mapping | None = None) -> list[Detector]` — cfg = увесь вміст `detectors.yaml`
  (None → `load_yaml("detectors")`); ваги й параметри скорів з YAML, структурні довжини вікон → `FeatureParams.from_config(cfg)`
  (прогріви детекторів і конвеєра з одного джерела). Порушення схеми → `ConfigValidationError(path="detectors.ema_slope.weight")`.
* `load_detectors_config(cfg) -> DetectorsConfig` (pydantic; секції детекторів `extra="forbid"`, верхній рівень `extra="allow"` —
  секція `agreement` належить decision), `max_warmup(detectors) -> int`.
* Необов'язкові ключі, яких немає в поточному YAML (працюють дефолти): `ema_slope.r2_points`, `rsi_exhaustion.div_lookback`,
  `candle_geometry.{w_pin,w_engulf}`, `features.volume_window`.

## 5. `fuzzhelm.regimes`

* `cluster.fit_regimes(X, k_range=range(2, 7), seed=0, *, n_init=10, standardize=True, silhouette_sample=10_000) -> ClusterResult`
  * `KMeans(n_clusters=k, n_init=n_init, random_state=seed)` на стандартизованих колонках X (n, d);
    силует — `silhouette_score(..., sample_size=silhouette_sample if n > silhouette_sample, random_state=seed)`.
  * `ClusterResult` (frozen): `k` (argmax силуету; при рівності — менше k), `centroids` (k, d) в **оригінальних** одиницях,
    `silhouette_by_k: dict[int, float]`, `labels`, `centroids_by_k`, `labels_by_k`, `inertia_by_k`, `n_samples`, `seed`.
  * Нескінченні значення в X → `ValueError` (відкиньте рядки прогріву заздалегідь).
* `calibrate_mf.calibrate(vol_pct, ema_slope_norm, volume_z, t_series, *, seed, k_range=range(2, 7), n_init=10,
  run_id=None, silhouette_sample=10_000, min_gap=1e-3, min_sigma=1e-3) -> dict` — **приймає масиви**
  (рядки з NaN у будь-якій з трьох ознак відкидаються; NaN у T — теж). Повертає JSON-сумісний dict:

```text
{"T": {"percentiles": [8,25,50,75,92], "raw_breakpoints": [...], "breakpoints": [...], "n": int,
       "terms": {STRONG_DOWN: trap(-1,-1,p8,p25), WEAK_DOWN: tri(p8,p25,p50), NEUTRAL: tri(p25,p50,p75),
                 WEAK_UP: tri(p50,p75,p92), STRONG_UP: trap(p75,p92,1,1)}},
 "V": {"k_used": 3, "centres": [m_LO, m_MID, m_HI], "sigmas": [...], "silhouette": s(k=3),
       "terms": {"LO": {"type": "gauss", "m": .., "sigma": ..}, "MID": ..., "HI": ...}},
 "centroids_k3": [[vol_pct, slope, vol_z] × 3, відсортовані за vol_pct], "feature_columns": [...],
 "silhouette_by_k": {2: .., ..., 6: ..}, "inertia_by_k": {...}, "k_best": int, "k_best_is_3": bool,
 "n_samples": int, "seed": int, "source_run_id": str | None, "warnings": [str, ...]}
```

  * V-центри = відсортовані координати vol_pct центроїдів KMeans(k=3); σ_i = 0.5·відстань до найближчого сусіда
    (`v_sigmas(centres)`; приклад брифінгу (0.17, 0.51, 0.88) → (0.17, 0.17, 0.185)). σ < `min_sigma` → підлога + warning.
  * T-точки = `np.percentile(T, [8,25,50,75,92])`; нестрого зростаючі (напр. T ≡ 0) → примусово строго зростаючі
    з кроком `min_gap` у (−1, 1) + warning (інакше вироджені трикутники). Руспіні Σμ = 1 виконується (тест, 2001 точка, 1e−12).
  * Якщо силует обирає k ≠ 3 — `k_best` це показує + warning; V все одно має 3 терми.
* `calibrate_mf.features_to_arrays(feats: Sequence[Features], horizon=5) -> (vol_pct, ema_slope_norm, volume_z)` —
  з виходу конвеєра; непрогріте → NaN; нахил g_t ідентичний `EmaSlope.features["g"]` (та сама підлога
  знаменника `max(ATR, 1e−9·|e|)`); `horizon` має дорівнювати `FeatureParams.ema_horizon`.
* `calibrate_mf.write_membership_yaml(result, path, *, source_run_id=None) -> Path` — переписує файл за шаблоном
  з **тими самими ключами**, що й `config/membership.yaml` (T, R, V, U, defuzz); T/V — з `result`, R/U/defuzz — з наявного
  файлу; `V.provisional: false`, `T/V.source_run_id`, `V.silhouette`; силуети за k, k_best, сирі перцентилі й warnings —
  у YAML-коментарях (щоб не ламати строгу схему fuzzy). Самоперевірка парсингом + атомарний запис (tmp + `os.replace`).
  Переноси рядків у run_id/warnings не «виходять» з коментаря; рядкові значення, які YAML прочитав би як не-рядок
  (`yes`, `null`, `123`), пишуться в лапках; NaN-силует виродженого k у коментарі — `n/a`.
* Константи: `T_PERCENTILES`, `T_TERMS`, `V_TERMS`, `V_K = 3`, `FEATURE_COLUMNS`; допоміжні `t_terms(bp)`, `v_sigmas(m)`.

**Хвиля 2 (`scripts/calibrate_mf.py`):** БД → `FeaturePipeline.run` → `features_to_arrays` → детектори + `decision.consensus`
→ T-ряд → `calibrate(...)` → `write_membership_yaml(result, CONFIG_DIR / "membership.yaml", source_run_id=run_id)`.

## 6. Фікстури і скрипт

* `fixtures/golden/source_klines_btcusdt_1m.json` — 2000 сирих 1m-свічок BTCUSDT (Binance USDⓈ-M, публічний
  `/fapi/v1/klines`, фіксоване вікно від 2026-09-15 00:00 UTC, 1500 + 500), об'єкт `{source, symbol, interval,
  start_utc, pages, columns, klines}`.
* `fixtures/golden/rsi14.csv` (`open_time_ms, close, rsi14`), `atr14.csv` (`open_time_ms, high, low, close, atr14`);
  значення — `repr(float)` (точний round-trip), порожньо до прогріву (перші 14 рядків).
* `scripts/make_golden.py [--fetch]` — незалежна пакетна реалізація Уайлдера (цикли, форма `(prev·(n−1)+x)/n`,
  RSI через `100 − 100/(1+RS)`); мережа лише з `--fetch`, хост перевіряється `assert_readonly_url`.
* Зовнішній еталон RSI: тест `test_rsi14_matches_stockcharts_published_example` (приклад StockCharts ChartSchool,
  33 ціни з 4 знаками → 19 опублікованих значень RSI(14), збіг до 0.01) підтверджує конвенцію ініціалізації (середнє
  перших 14 змін), на якій побудовано golden-CSV; `test_wilder_golden_csv_matches_exact_rational_recurrence` звіряє
  golden-CSV з точною раціональною арифметикою (`fractions.Fraction`).
