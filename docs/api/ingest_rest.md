# API модуля `ingest_rest` (фактичний, хвиля 1)

Пакет `fuzzhelm.ingest`: REST-клієнти Binance USDⓈ-M і Kraken, token bucket, retry, backfill,
нормалізація (REST **і всі чотири WS-потоки**), символи, квантування, дедуплікація, крос-звірка.
Усі сигнатури нижче звірені з кодом інтроспекцією. Автор: Андрій Жук, 2026.

Загальне:
* ingest — пакет-межа (не сканується AST-тестами детермінізму/типів), але час і випадковість тут
  все одно **ін'єктуються**: `clock: Clock` (`core.ports`), `sleep: async (float) -> None`, `rng_seed`.
* Гроші/ціни/кількості — лише `Decimal`, розібраний із рядка біржі без float.
* Помилки схеми — `core.errors.NormalizationError(msg, field=<шлях>, venue=<"BINANCE_USDM"|"KRAKEN">)`.
* Фікстури (сирі байти бірж, 2026-09-18): `fixtures/rest/` — див. `fixtures/rest/capture_meta.json`
  і скрипт запису `fixtures/rest/capture_rest_fixtures.py`.

---

## 1. `ingest/symbols.py`

```python
@dataclass(frozen=True, slots=True)
class SymbolRef:                      # мінімальна ідентичність інструмента (без tick/step)
    venue: Venue; symbol_venue: str; symbol_canon: str
    base_asset: str; quote_asset: str; contract_type: ContractType

BTC_USDT_PERP: SymbolRef   # (BINANCE_USDM, "BTCUSDT", "BTC-USDT-PERP", "BTC", "USDT", PERP)
ETH_USDT_PERP: SymbolRef   # (BINANCE_USDM, "ETHUSDT", "ETH-USDT-PERP", "ETH", "USDT", PERP)
BTC_USD_SPOT:  SymbolRef   # (KRAKEN, "XBTUSD", "BTC-USD-SPOT", "BTC", "USD", SPOT)
REGISTRY: tuple[SymbolRef, ...]
CROSSCHECK_PAIRS = {"BTC-USDT-PERP": "BTC-USD-SPOT"}
KRAKEN_RESULT_KEYS = {"XBTUSD": "XXBTZUSD"}          # ключ, під яким Kraken повертає OHLC/AssetPairs

def symbol_ref(venue: Venue, symbol_venue: str) -> SymbolRef        # регістр неважливий; Kraken: і XXBTZUSD
def canonical_symbol(venue: Venue, symbol_venue: str) -> str
def venue_symbol(venue: Venue, symbol_canon: str) -> str
def make_canonical(base: str, quote: str, contract_type: ContractType) -> str   # "BTC-USDT-PERP"
def parse_canonical(symbol_canon: str) -> tuple[str, str, ContractType]
def kraken_pair_from_result_key(key: str) -> str   # "XXBTZUSD" → "XBTUSD"
def kraken_result_key(pair: str) -> str            # "XBTUSD" → "XXBTZUSD"
def kraken_asset(asset: str) -> str                # "XXBT"/"XBT" → "BTC", "ZUSD" → "USD"
```
Невідомий символ → `NormalizationError(field="symbol" | "symbol_canon")`.

## 2. `ingest/normalize.py` — межа нормалізації

```python
InstrumentLike = Instrument | SymbolRef        # потрібні лише .venue, .symbol_venue, .symbol_canon
NS_PER_MS = 1_000_000; NS_PER_S = 1_000_000_000; DEFAULT_MMR = Decimal("0.005")
TF_MS: dict[str, int]                          # "1m"→60000 … "1d"→86400000
KRAKEN_INTERVAL_TF: dict[int, str]             # 1→"1m", 5→"5m", …, 1440→"1d"

def ms_to_ns(ms: int) -> int        # лише int (float → TypeError); ціле множення
def s_to_ns(s: int) -> int
def tf_ms(tf: str) -> int           # невідомий tf → NormalizationError(field="tf")

# природні ключі event_uid (docs/contracts.md §1) — ті самі функції для REST, WS і реплею
def kline_uid(venue: Venue, symbol_canon: str, tf: str, open_time_ns: int) -> str
def trade_uid(venue: Venue, symbol_canon: str, agg_id: int) -> str
def depth_uid(venue: Venue, symbol_canon: str, last_update_id: int) -> str
def mark_uid(venue: Venue, symbol_canon: str, ts_event_ns: int) -> str
```

### 2.1 WebSocket Binance (для агента WS/replay)

```python
def normalize_binance(stream: str, data: Mapping[str, Any], ts_ingest_ns: int,
                      instrument: InstrumentLike, *, src: Src = Src.WS) -> MarketEvent
def parse_stream(stream: str) -> StreamName   # StreamName(symbol, kind, interval, levels, speed)
```
* `stream` — поле `"stream"` кадру комбінованого потоку (напр. `"btcusdt@kline_1m"`), `data` — поле
  `"data"` без змін (як у `fixtures/ws/*.jsonl.gz`). Для реплею передавайте `src=Src.REPLAY`.
* Символ у `stream` і поле `s` мусять збігатися з `instrument.symbol_venue`, `e` — з типом потоку.
* Перевірено на всіх кадрах обох записаних сесій: 4-хв зразок `sample_btcusdt_4m.jsonl.gz` (7 013 кадрів)
  і завершена 45-хв сесія `btcusdt_2026-09-18.jsonl.gz` (92 101 кадр: 57 078 aggTrade, 25 912 depth20,
  6 412 kline, 2 699 markPrice) — 0 помилок (повторна перевірка під час ревізії модуля);
  ≈ 27–35 мкс/кадр на ноутбуці розробника (без розпакування gzip і розбору JSON).

| Потік | DTO | Поля DTO ← поля payload | `ts_event_ns` | `event_uid` |
|---|---|---|---|---|
| `<s>@kline_<tf>` | `Candle` (`src` з аргументу) | `open_time_ns←k.t`, `close_time_ns←k.T`, `o,h,l,c←k.o,h,l,c`, `volume←k.v`, `quote_volume←k.q`, `trades_count←k.n`, `is_closed←k.x`, `tf←k.i`, `vwap=None` | `E` (час події) | `kline_uid(venue, canon, tf, open_time_ns)` — **однаковий для всіх оновлень хвилини** |
| `<s>@aggTrade` | `Trade` | `agg_id←a`, `first_trade_id←f`, `last_trade_id←l`, `price←p`, `qty←q`, `is_buyer_maker←m` | `T` (час угоди) | `trade_uid(venue, canon, a)` |
| `<s>@markPrice[@1s]` | `MarkPrice` | `mark_price←p`, `index_price←i`, `funding_rate←r`, `next_funding_time_ns←T` | `E` | `mark_uid(venue, canon, ts_event_ns)` |
| `<s>@depth<5\|10\|20>[@100ms\|250ms\|500ms]` | `BookSnapshot` | `bids←b`, `asks←a` (`BookLevel(price, qty)`), `last_update_id←u` | `T` (час транзакції) | `depth_uid(venue, canon, u)` |

Строга схема (білі списки `WS_*_FIELDS` у модулі). Поля, які біржа реально надсилає, але DTO не має,
дозволені й перевіряються за типом, але не мапляться: kline `k.f, k.L, k.V, k.Q, k.B`; aggTrade
`E`, `nq` (опц.), `st` (опц.); markPrice `P`, `ap` (опц.), `st` (опц.); depth `E, U, pu`, `ps` (опц.),
`st` (опц.). Будь-яке інше поле → `NormalizationError(field="<ім'я>")` (у свічці — `"k.<ім'я>"`).
Інші перевірки: `k.T == k.t + tf − 1 мс`; рівні книги — `[str, str]`, ціна > 0, qty ≥ 0, `b` строго
спадає, `a` строго зростає (порушення → `field="b[i]"`/`"a[i]"`), рівнів ≤ N з імені потоку;
непідтримуваний потік (напр. diff `depth@100ms`, `trade`) → `field="stream"`.

### 2.2 REST Binance

```python
def normalize_rest_kline(row: Sequence[Any], instrument: InstrumentLike, ts_ingest_ns: int, *,
                         tf: str = "1m", server_time_ms: int | None = None, src: Src = Src.REST) -> Candle
def normalize_rest_klines(rows, instrument, ts_ingest_ns, *, tf="1m", server_time_ms=None,
                          src=Src.REST) -> list[Candle]          # field помилки: "[i].row[k]"
def normalize_premium_index(data: Mapping[str, Any], instrument: InstrumentLike,
                            ts_ingest_ns: int) -> MarkPrice      # ts_event ← time
def normalize_exchange_info(data: Mapping[str, Any], symbols: Iterable[str] | None = None, *,
                            mmr: Decimal = DEFAULT_MMR) -> dict[str, Instrument]   # ключ — symbol_venue
```
* Рядок kline — рівно 12 елементів; 13-й → `field="row[12]"`. `is_closed = closeTime < server_time_ms`
  (без `server_time_ms` — порівняння з `ts_ingest_ns // 10⁶`; це лише класифікація).
  `ts_event_ns`: закритий бар → `closeTime·10⁶`; **незакритий бар із переданим `server_time_ms` →
  `server_time_ms·10⁶`** (момент зрізу стану біржею; тоді новіший REST-зріз того самого бару витісняє
  старіший у `Deduplicator`); незакритий без `server_time_ms` → `closeTime·10⁶` (плановий, у майбутньому —
  для незакритих барів передавайте `server_time_ms`). `ts_ingest_ns` у `ts_event_ns` не потрапляє ніколи.
  `vwap = None`; `takerBuy*`/`ignore` перевіряються за типом і відкидаються.
* `normalize_exchange_info`: лише PERPETUAL; `tick_size←PRICE_FILTER.tickSize`,
  `step_size←LOT_SIZE.stepSize`, `min_notional←MIN_NOTIONAL.notional`, `mmr=0.005` (D-04);
  перевіряються лише потрібні поля (зайві ігноруються — див. deviations.d/ingest_rest.md).
  Запитаний, але відсутній символ → `field="symbols[<SYM>]"`.

### 2.3 REST Kraken

```python
def normalize_kraken_ohlc(result: Mapping[str, Any], instrument: InstrumentLike, ts_ingest_ns: int, *,
                          tf: str = "1m", src: Src = Src.REST) -> list[Candle]
def normalize_kraken_asset_pair(result: Mapping[str, Any], pair: str = "XBTUSD") -> Instrument
```
* `result` — поле `result` відповіді (`KrakenRestClient.ohlc()` повертає саме його). Ключі — рівно
  один ключ пари (`XXBTZUSD`) і `last`; інший ключ → `field="result.<ключ>"`.
* Рядок `[time_s, o, h, l, c, vwap, volume, count]` (рівно 8). `open_time_ns = time·10⁹`,
  `close_time_ns = open + tf − 1 мс`, `is_closed = time ≤ last`, `ts_event_ns = close_time_ns`,
  `vwap = None` якщо `volume == 0`, `quote_volume = 0` (Kraken не надає). Kraken не дає часу сервера,
  тож для поточного незакритого бару `ts_event_ns = close_time_ns` (плановий); крос-звірка такі бари пропускає.

## 3. `ingest/quantize.py`

```python
def quantize_to_tick(price: Decimal, tick: Decimal) -> Decimal   # HALF_EVEN (core.money.quantize_price)
def floor_to_step(qty: Decimal, step: Decimal) -> Decimal        # DOWN (core.money.floor_qty)
def check_tick(price: Decimal, tick: Decimal) -> bool            # точно на сітці
def check_step(qty: Decimal, step: Decimal) -> bool
def notional(qty: Decimal, price: Decimal) -> Decimal
def reject_below_min_notional(qty, price, instrument: Instrument) -> RejectCode | None
    # BELOW_MIN_NOTIONAL, якщо |qty|·price < min_notional; межа включна (== дозволено)
def prepare_order_qty(qty_raw, price, instrument) -> tuple[Decimal, RejectCode | None]
    # floor до step → ZERO_QTY, якщо == 0 → BELOW_MIN_NOTIONAL; qty_raw < 0 → ValueError
    # (кількість — модуль, напрям задає Side ордера)
```

## 4. `ingest/dedup.py`

```python
class DedupOutcome(StrEnum): NEW, DUPLICATE, REPLACED
def precedence(ev: MarketEvent) -> tuple[int, int, int, int]
    # Candle: (is_closed, −src, ts_event_ns, −ts_ingest_ns); інші: (1, 0, ts_event_ns, −ts_ingest_ns)
class Deduplicator(max_size: int | None = None):
    def offer(self, ev) -> DedupOutcome      # NEW | REPLACED (витіснив нижчий пріоритет) | DUPLICATE
    def offer_all(self, events) -> list[MarketEvent]   # ті, що NEW/REPLACED, у порядку входу
    def seen(self, uid) -> bool; def get(self, uid) -> MarketEvent | None; __len__; __contains__
    def values(self) -> list[MarketEvent]    # канонічний порядок (ts_event_ns, event_uid)
    stats: dict[DedupOutcome, int]
def dedup(events) -> list[MarketEvent]       # пакетно; результат не залежить від перестановки входу
```
Інваріант (property-тест): `dedup(perm(x)) == dedup(x)`, `dedup(dedup(x)) == dedup(x)`. Повні часові
«нічиї» розв'язуються BLAKE2b канонічних байтів (а не `==`: `Decimal("81015.4") == Decimal("81015.40")`,
але їхній канонічний JSON різний — переможець не залежить від порядку навіть тоді). **Для WS-агента:** оновлення
однієї свічки мають один uid — `offer` повертає `REPLACED` для новішої версії (пізніший `E`) і
для закритої після незакритої; дублікат кадру з пізнішим `ts_ingest_ns` → `DUPLICATE`, з ранішим →
`REPLACED` (зберігається перше надходження). З `max_size` незалежність від перестановки — лише у вікні.

## 5. `ingest/ratelimit.py`

```python
BINANCE_WEIGHT_LIMIT_1M = 2400; USED_WEIGHT_HEADER = "X-MBX-USED-WEIGHT-1M"
def klines_weight(limit: int) -> int      # ВИМІРЯНО: ≤100→1, 101..500→2, 501..1000→5, 1001..1500→10
def request_weight(path: str, params: Mapping | None = None) -> int
    # /fapi/v1/klines — за limit (дефолт 500); /time, /ping, /exchangeInfo — 1;
    # /premiumIndex — 1 з symbol, 10 без; інший шлях → ValueError
class TokenBucket(capacity: float, refill_per_s: float, clock: Clock, *,
                  sleep: SleepFn = asyncio.sleep, initial: float | None = None):
    tokens: float (property, з поповненням)      total_wait_s: float     acquired_weight: int
    def max_admitted(self, window_s: float) -> float     # C + r·W — межа за будь-яке вікно W
    def wait_time(self, weight: int) -> float
    def try_acquire(self, weight: int) -> bool           # неблокуюче
    async def acquire(self, weight: int) -> float        # блокує через ін'єктований sleep; повертає очікування, с
    def observe_used_weight(self, used: int, limit: int = 2400) -> None   # лише ЗМЕНШУЄ токени
    def drain(self) -> None                              # після 429/418
    # weight ≤ 0 або > capacity → ValueError; capacity/refill_per_s не скінченні або ≤ 0 → ValueError
def binance_request_bucket(clock, *, limit_per_min=2400, sleep=asyncio.sleep) -> TokenBucket
    # C = L/2, r = L/120 за с ⇒ C + 60·r = L: доказово ≤ 2400 за будь-які 60 с
def kraken_public_bucket(clock, *, sleep=asyncio.sleep) -> TokenBucket   # 1 запит/с, власний консервативний вибір
```

## 6. `ingest/retry.py`

```python
class IngestError(FuzzHelmError)
class RetryableError(IngestError)(message, *, retry_after_s: float | None = None, status: int | None = None)
class NonRetryableHttpError(IngestError)(status, body, message="")
class RetryExhaustedError(IngestError)(attempts, last_error)
class RateLimitBannedError(IngestError)(retry_after_s, status)
def parse_retry_after(value: str | None, now_ns: int | None = None) -> float | None  # секунди або HTTP-дата
    # лише ASCII-цифри (заголовок «²» не валить класифікацію, а дає None → звичайний backoff);
    # дата без зони («-0000») трактується як UTC
def header_int(headers: Mapping[str, str], name: str) -> int | None   # ASCII-ціле або None
def error_for_response(resp: httpx.Response, now_ns: int | None = None) -> RetryableError | None
    # 418/429/500/502/503/504 → RetryableError(retry_after_s з заголовка); інакше None
class RetryPolicy(base=0.5, cap=30.0, max_attempts=5, rng_seed=0, *, max_retry_after_s=300.0,
                  rng: np.random.Generator | None = None):
    def ceiling(self, attempt: int) -> float     # min(cap, base·2^attempt), attempt з 0 (точно: math.ldexp;
                                                 # переповнення → cap); base/cap не скінченні або ≤ 0 → ValueError
    def backoff(self, attempt: int) -> float     # U(0, ceiling) — numpy Generator(rng_seed)
    def delay_for(self, error, attempt) -> float # Retry-After, якщо є (> max → RateLimitBannedError)
    async def execute(self, fn: Callable[[], Awaitable[T]], *, sleep=asyncio.sleep, on_retry=None) -> T
    sleeps: list[float]                          # фактичні паузи
    # повторює RetryableError, httpx.TimeoutException/NetworkError/RemoteProtocolError;
    # після max_attempts → RetryExhaustedError(last_error)
```

## 7. `ingest/rest_client.py`

```python
class BinanceApiError(NonRetryableHttpError)(status, code: int | None, msg: str, body: str)
@dataclass ClockSkewSample(server_time_ms, local_send_ns, local_recv_ns): rtt_ms, offset_ms (properties)
class BinanceRestClient(base_url: str, http: httpx.AsyncClient, bucket: TokenBucket, retry: RetryPolicy, *,
                        clock: Clock | None = None,        # None → infra.wallclock.SystemClock()
                        sleep: SleepFn = asyncio.sleep,    # для пауз retry
                        weight_limit_per_min: int = 2400):
    base_url  # перевірено fuzzhelm.config.assert_readonly_url (інакше MainnetHostRejected)
    last_used_weight: int | None; requests_sent: int; clock; bucket; retry
    async def klines(self, symbol, interval="1m", start_ms=None, end_ms=None, limit=1500) -> list[list[Any]]
    async def exchange_info(self) -> dict[str, Any]
    async def premium_index(self, symbol: str) -> dict[str, Any]
    async def server_time_ms(self) -> int
    async def server_time_sample(self) -> ClockSkewSample
    async def server_time_offset_ms(self, samples: int = 3) -> int   # сервер − локальний, мс; замір з мін. RTT
```
Кожен запит: `bucket.acquire(weight)` → GET → `observe_used_weight(X-MBX-USED-WEIGHT-1M)` →
418/429: `bucket.drain()` → 418/429/5xx: `RetryableError` (повтор за політикою) → інші ≥400:
`BinanceApiError` (без повтору) → `orjson.loads` (невалідний JSON → `NormalizationError(field="$body")`).
Повертає **сирі** JSON-структури — у DTO їх перетворює `normalize`.

## 8. `ingest/kraken_client.py`

```python
class KrakenApiError(IngestError)(errors: list[str])
class KrakenRestClient(base_url: str, http: httpx.AsyncClient, retry: RetryPolicy, *,
                       bucket: TokenBucket | None = None, clock: Clock | None = None, sleep=asyncio.sleep):
    async def ohlc(self, pair="XBTUSD", interval=1, since=None) -> dict[str, Any]   # поле result
    async def asset_pairs(self, pair="XBTUSD") -> dict[str, Any]                    # поле result
```
Помилки Kraken приходять із HTTP 200 у `error`: префікси `EAPI:Rate limit exceeded`,
`EGeneral:Too many requests`, `EService:Unavailable|Busy`, `EGeneral:Temporary lockout` → повтор;
інші → `KrakenApiError`. Інтервали: 1, 5, 15, 30, 60, 240, 1440, 10080, 21600 хв.

## 9. `ingest/backfill.py`

```python
class KlineSource(Protocol): clock: Clock; async def klines(symbol, interval, start_ms, end_ms, limit) -> list[list]
class SeamStatus(StrEnum): OK, MISMATCH, MISSING
@dataclass Segment(index, start_ms, limit, rows, first_open_ms, last_open_ms, seam: SeamStatus | None)
@dataclass OverlapMismatch(open_time_ms, segment, previous: tuple, current: tuple)
@dataclass Gap(instrument, tf, ts_lo_ns, ts_hi_ns, expected_count, stream=Stream.KLINES,
               detector=GapDetectorKind.TIME, status=GapStatus.OPEN, filled_rows=0, attempts=0)
    # ts_lo/hi — open_time першого/останнього ВІДСУТНЬОГО бару (включно); поля = таблиця ingest_gap
@dataclass BackfillResult(candles: tuple[Candle, ...], gaps, segments, overlap_mismatches, requests); .ok
@dataclass GapFillReport(candles, gaps, added, requests)

DEFAULT_CLOCK_GUARD_MS = 5_000     # власний консервативний вибір, не замір
async def backfill_klines(client: KlineSource, symbol: str, start_ms: int, end_ms: int, *,
                          interval="1m", limit=1500, instrument: InstrumentLike | None = None,
                          now_ms: int | None = None,
                          on_page: Callable[[list[Candle]], Awaitable[None]] | None = None,
                          clock_guard_ms: int = DEFAULT_CLOCK_GUARD_MS) -> BackfillResult
def detect_gaps(candles, tf, *, instrument=None, expected_from_ns=None, expected_to_ns=None) -> list[Gap]
async def fill_gaps(client, symbol, candles, gaps, *, dedup: Deduplicator | None = None,
                    instrument=None, interval="1m", limit=1500, now_ms=None) -> GapFillReport
```
* Пагінація: наступна сторінка — `startTime = openTime останнього бару попередньої` (перекриття рівно
  1 бар; тому `limit ≥ 2`). Стик: той самий рядок → `OK`; інший вміст → `MISMATCH` + `OverlapMismatch`
  (зберігається новіша версія); бару стику немає → `MISSING`, а пропущені бари — у `gaps`.
* Стоп: сторінка коротша за `limit`, або досягнуто останнього закритого бару, або немає прогресу.
* `now_ms` — час біржі, визначає `is_closed`; повертаються ЛИШЕ закриті бари, упорядковані за `open_time`.
  Без `now_ms` береться `client.clock − clock_guard_ms`: якщо локальний годинник випереджає біржу, ще
  відкритий бар інакше позначився б закритим, а такий бар upsert (`WHERE candle.is_closed = FALSE`) уже
  не виправить. Бар, закритий за останні `clock_guard_ms`, просто потрапить у наступний запуск (прогалиною
  не вважається). Точний шлях — `now_ms = local_ms + await client.server_time_offset_ms()` (як у прикладі). `gaps` рахуються в очікуваному діапазоні
  `[ceil(start_ms), min(floor(end_ms), floor(now_ms) − tf)]`, включно з крайовими.
* `on_page` — потоковий колбек для upsert у БД посторінково (без утримання 45 днів у пам'яті).
* `fill_gaps` ідемпотентний: повторний виклик з тими самими прогалинами робить REST-запити, але
  `added == ()` (дедуплікація за event_uid). Статус: `FILLED` (усі бари), `PARTIAL`, `UNFILLABLE` (біржа
  теж не має даних).

## 10. `ingest/crosscheck.py`

```python
BPS = Decimal(10_000); DEFAULT_THRESHOLD_BPS = Decimal(50)
@dataclass CrosscheckRow(open_time_ns, binance_close, kraken_close, diff, diff_bps: Decimal, flagged: bool)
@dataclass CrosscheckReport(binance_instrument, kraken_instrument, tf, threshold_bps, rows, flagged,
                            missing_in_binance, missing_in_kraken)
    n_matched; mean_bps; mean_abs_bps; max_abs_bps   # Decimal | None
    def summary(self) -> dict[str, str | int | None]  # б.п. округлені до 0.0001, рядками
    def table(self, *, only_flagged=False) -> list[dict[str, str]]   # «таблиця розбіжностей»
def divergence_bps(price: Decimal, reference: Decimal) -> Decimal   # 10⁴·(price − ref)/ref
def crosscheck(binance: Sequence[Candle], kraken: Sequence[Candle],
               threshold_bps: Decimal | int | str = 50) -> CrosscheckReport
```
Звіряються лише закриті свічки з однаковим `open_time_ns`; позначка — `|d| > поріг` (строго).
Уся арифметика (`diff`, `diff_bps`, середні) — у `core.money.DECIMAL_CONTEXT` (38 знаків) незалежно від
контексту потоку, що викликає: звіт відтворюваний і без `setup_decimal_context()`.
`missing_*` рахуються лише в спільному часовому вікні. Дублікат `open_time` → `ValueError` (спершу `dedup`);
порожній вхід, змішані інструменти/таймфрейми або різні tf двох сторін → `ValueError`.

---

## Приклад: 45 днів 1m-свічок + крос-звірка

```python
import httpx
from fuzzhelm.config import get_settings
from fuzzhelm.infra.wallclock import SystemClock
from fuzzhelm.ingest.backfill import backfill_klines
from fuzzhelm.ingest.crosscheck import crosscheck
from fuzzhelm.ingest.kraken_client import KrakenRestClient
from fuzzhelm.ingest.normalize import normalize_exchange_info, normalize_kraken_ohlc
from fuzzhelm.ingest.ratelimit import binance_request_bucket, kraken_public_bucket
from fuzzhelm.ingest.rest_client import BinanceRestClient
from fuzzhelm.ingest.retry import RetryPolicy
from fuzzhelm.ingest.symbols import BTC_USD_SPOT

s, clock = get_settings(), SystemClock()
async with httpx.AsyncClient(timeout=10) as http:
    bn = BinanceRestClient(s.binance_rest_base, http, binance_request_bucket(clock),
                           RetryPolicy(rng_seed=s.seed), clock=clock)
    inst = normalize_exchange_info(await bn.exchange_info(), ["BTCUSDT"])["BTCUSDT"]
    offset = await bn.server_time_offset_ms()
    now_ms = clock.now_ns() // 1_000_000 + offset
    res = await backfill_klines(bn, "BTCUSDT", now_ms - 45 * 86_400_000, now_ms,
                                instrument=inst, now_ms=now_ms, on_page=candle_repo_upsert)
    assert res.ok or res.gaps            # прогалини → таблиця ingest_gap → fill_gaps(...)
    kr = KrakenRestClient(s.kraken_rest_base, http, RetryPolicy(rng_seed=s.seed),
                          bucket=kraken_public_bucket(clock), clock=clock)
    k = normalize_kraken_ohlc(await kr.ohlc("XBTUSD", 1), BTC_USD_SPOT, clock.now_ns())
    report = crosscheck(res.candles, k)  # report.table() → «таблиця розбіжностей»
```

## Приклад: реплей WS-кадру

```python
from fuzzhelm.core.enums import Src
from fuzzhelm.ingest.normalize import normalize_binance
from fuzzhelm.ingest.symbols import BTC_USDT_PERP
ev = normalize_binance(frame["stream"], frame["data"], frame["ts_ingest_ns"], BTC_USDT_PERP, src=Src.REPLAY)
```
