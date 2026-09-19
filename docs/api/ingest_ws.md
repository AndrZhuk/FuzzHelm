# API модуля `ingest_ws` (фактичний, хвиля 1)

WebSocket-інжест: запис/відтворення сесій, клієнт Binance на двох з'єднаннях, backoff і сторож тиші,
детектор прогалин, агрегатор свічок, конвеєр з REST-добором. Сигнатури звірені інтроспекцією коду.
Автор: Андрій Жук, 2026.

Загальне:
* `ingest` — пакет-межа (AST-тести детермінізму його не сканують), але час і випадковість тут усе одно
  **ін'єктуються**: `Clock` (`core.ports`), `sleep: async (float) -> None`, `recv(ws, timeout)`, `seed`.
  Тести не ходять у мережу й не сплять: `ManualClock`/`FrameClock` + фіктивні `sleep`/`recv`/`connect`.
* Нормалізація — лише `ingest.normalize.normalize_binance` (REST-агент); тут вона не дублюється.
* Два з'єднання (D-01): `market` = kline/aggTrade/markPrice (`Settings.binance_ws_market`),
  `public` = depth (`Settings.binance_ws_public`).
* Типи записів сесії (`RawFrame`, `ControlRecord`) визначені в `ingest/recorder.py` і спільні для клієнта,
  реплею і конвеєра.

---

## 1. `ingest/recorder.py` — формат файлу сесії (docs/contracts.md §7)

```python
@dataclass(frozen=True, slots=True) class RawFrame(conn: str, ts_ingest_ns: int, stream: str, data: Mapping[str, Any])
    def to_record(self) -> dict       # {"v":1,"kind":"frame","conn","ts_ingest_ns","stream","data"}
@dataclass(frozen=True, slots=True) class ControlRecord(conn: str, ts_ingest_ns: int, event: str, detail: str = "")
    def to_record(self) -> dict       # {"v":1,"kind":"control",...}; event ∈ {"connected","disconnected"}
SessionItem = RawFrame | ControlRecord
def header_record(*, venue, symbol, urls, started_ns, minutes) -> dict
def footer_record(*, ended_ns, frames) -> dict
def dumps_record(rec) -> str          # json.dumps(rec, separators=(",", ":")) — побайтово як scripts/record_ws_session.py

class SessionRecorder(path, *, symbol: str, urls: Mapping[str, str], clock: Clock, venue="BINANCE_USDM",
                      minutes: float | None = None, compresslevel=6, deterministic=False):
    path; part_path (= path + ".part"); frames; records; started_ns
    def open(self, header: Mapping | None = None) -> SessionRecorder   # header=None → started_ns = clock.now_ns()
    def close(self, *, ended_ns: int | None = None) -> Path            # footer + атомарний os.replace(.part → path)
    def abort(self) -> None                                           # без footer, .part видаляється
    __enter__/__exit__                                                # виняток усередині → abort()
    def write(self, item: SessionItem) -> None
    def write_frame(self, conn, stream, data, ts_ingest_ns=None) -> None   # None → clock.now_ns()
    def write_control(self, conn, event, detail="", ts_ingest_ns=None) -> None
    def write_all(self, items) -> None
def write_session(path, header, items, *, ended_ns: int, deterministic=True, compresslevel=6) -> Path
    # готова сесія одним викликом; deterministic → gzip mtime=0, порожнє ім'я → побайтово відтворюваний файл
```

## 2. `ingest/replay.py` — відтворення (порт `MarketFeed`) і офлайн-REST

```python
def iter_records(path) -> Iterator[dict]       # усі записи (header/frame/control/footer), .jsonl.gz або .jsonl
def iter_frames(path) -> Iterator[dict]        # лише kind="frame" (сирий доступ)
def to_item(rec: Mapping) -> SessionItem | None
@dataclass(frozen=True) class Session(header: dict, items: tuple[SessionItem, ...], footer: dict | None); .frames
def read_session(path) -> Session
def read_header(path) -> dict

class FrameClock(t_ns: int = 0):               # Clock реплею: now_ns() = ts_ingest поточного кадру
    def now_ns(self) -> int
    def observe(self, t_ns: int) -> None        # МОЖЕ йти назад (модель стрибка годинника); рахує regressions

@dataclass class ReplayStats(frames, controls, events, errors, clock_regressions, waited_s, error_fields)
class ReplayFeed(path, speed: float = inf, clock: Clock | None = None, *, sleep=None,
                 instrument: InstrumentLike | None = None, src: Src = Src.REPLAY,
                 on_error: "raise" | "skip" = "raise", order: "arrival" | "ingest_ts" = "arrival"):
    header: dict; instrument; stats: ReplayStats
    async def items(self) -> AsyncIterator[SessionItem]     # кадри + керування, з темпом speed
    async def frames(self) -> AsyncIterator[RawFrame]
    def normalize(self, frame: RawFrame) -> MarketEvent | None
    async def events(self) -> AsyncIterator[MarketEvent]
    def __aiter__(self) -> AsyncIterator[MarketEvent]        # core.ports.MarketFeed
```
* `speed=inf` — жодного очікування; скінченна `speed` — очікування `Δts_ingest/speed` через ін'єктовані
  `clock` (за замовчуванням `infra.wallclock.SystemClock`) і `sleep` (за замовчуванням `asyncio.sleep`).
  Для темпу береться монотонна обвідна ts_ingest (стрибок назад не дає від'ємних пауз).
* Порядок: `"arrival"` (за замовчуванням) — порядок файлу = порядок надходження; у справних записах він
  збігається з порядком ts_ingest_ns (4-хв зразок: 0 інверсій). `"ingest_ts"` — стабільне сортування.
* `instrument=None` → `symbols.symbol_ref(Venue(header.venue), header.symbol)` (без tick/step).

```python
def agg_trade_rest_row(data: Mapping) -> dict            # WS aggTrade → поля REST {a,p,q,f,l,T,m}
@dataclass(frozen=True) class RestFixture(symbol, interval, klines: tuple[list, ...], agg_trades: tuple[dict, ...], meta)
    @classmethod def load(cls, path) -> RestFixture      # fixtures/ws/pathological/<назва>.rest.json
class FixtureRestHandler(fixture: RestFixture, clock: Clock):   # httpx-обробник (respx side_effect або MockTransport)
    used_weight: int; calls: list[(path, params)]
    def __call__(self, request: httpx.Request) -> httpx.Response
```
`FixtureRestHandler`: `GET /fapi/v1/klines` — рядки з `openTime ∈ [startTime, endTime]`, лише ЗАКРИТІ на
`clock.now_ns()`, перші `limit`; `GET /fapi/v1/aggTrades?fromId&limit(≤1000)` — угоди `a ≥ fromId`, `T ≤ now`.
Інший символ → 400 `{"code":-1121}`; невідомий шлях → 404. Заголовок `X-MBX-USED-WEIGHT-1M` накопичується.

## 3. `ingest/reconnect.py` — backoff, сторож тиші, класи розривів

```python
class Backoff(base_s=0.5, cap_s=30.0, factor=2.0, *, jitter: "equal"|"full"|"none" = "equal", seed=0, rng=None):
    attempt: int; history: list[float]
    def ceiling(self, attempt: int) -> float      # min(cap, base·factor^attempt)
    def next_delay(self) -> float                 # equal: c/2 + U(0, c/2); full: U(0, c); none: c; attempt += 1
    def reset(self) -> None
class HeartbeatWatchdog(timeout_s: float = 10.0, start_ns: int | None = None):
    timeout_ns; last_ns; fires; timeout_s (property)
    def beat(self, t_ns) -> None
    def deadline_ns(self) -> int | None
    def remaining_s(self, now_ns) -> float
    def expired(self, now_ns) -> bool             # now ≥ last + timeout
    def poll(self, now_ns) -> int | None          # віртуальний час: now ≥ last + timeout і сторож «заведений» →
                                                  # fires += 1, повертає last + timeout; одна тиша — одне спрацювання
    def disarm(self) -> None                      # розрив уже зафіксовано (запис `disconnected`): мовчати до beat()
    def beat(self, t_ns) -> None                  # кадр/`connected`: last = t, сторож знову «заведений»
    def check_gap(self, t_ns) -> int | None       # ретроспективно: тиша > timeout перед кадром ЦЬОГО з'єднання →
                                                  # last + timeout; t назад → None (конвеєр його не використовує)
class CloseClass(StrEnum): NORMAL, GOING_AWAY, TRANSIENT, SERVER_ERROR, RATE_LIMITED, HEARTBEAT_TIMEOUT,
                           PROTOCOL_ERROR, HANDSHAKE_REJECTED
FATAL = {PROTOCOL_ERROR, HANDSHAKE_REJECTED};  IMMEDIATE = {NORMAL, GOING_AWAY}
def classify_close_code(code: int | None) -> CloseClass
    # 1000→NORMAL; 1001,1012→GOING_AWAY; None,1005,1006→TRANSIENT; 1011,1013,1014,4xxx→SERVER_ERROR;
    # 1008→RATE_LIMITED; 1002,1003,1007,1009,1010→PROTOCOL_ERROR; інше→TRANSIENT
def classify_http_status(status: int) -> CloseClass   # 418/429→RATE_LIMITED; 5xx→SERVER_ERROR; інші 4xx→HANDSHAKE_REJECTED
@dataclass(frozen=True) class Disconnect(conn, ts_ns, cls: CloseClass, code: int | None = None, reason: str = "")
    fatal: bool (property); detail: str (property) = "КЛАС:код:причина"
DEFAULT_HEARTBEAT_TIMEOUT_S = 10.0
```

## 4. `ingest/ws_client.py` — `BinanceWsClient` (реалізує `MarketFeed`)

```python
class WsConnection(Protocol): async def recv(self) -> str | bytes
ConnectFactory = Callable[[str], AbstractAsyncContextManager[WsConnection]]
RecvFn = Callable[[WsConnection, float], Awaitable[str | bytes]]
def websockets_connect(url) -> AbstractAsyncContextManager    # websockets.asyncio.client.connect(max_size=4 MiB, ping 20 с)
async def recv_with_timeout(ws, timeout_s) -> str | bytes     # asyncio.wait_for(ws.recv(), timeout_s)
class HeartbeatTimeoutError(FuzzHelmError)
class WsFatalError(FuzzHelmError): disconnect: Disconnect
def classify_exception(e) -> tuple[CloseClass, int | None, str]
    # ConnectionClosed → за кодом close-кадру; InvalidStatus → за HTTP-статусом; HeartbeatTimeoutError →
    # HEARTBEAT_TIMEOUT; InvalidHandshake/TimeoutError/OSError/EOFError → TRANSIENT; інше → ValueError (баг)

class BinanceWsClient(settings: Settings, clock: Clock, *, instruments: Sequence[InstrumentLike] | None = None,
                      connect: ConnectFactory = websockets_connect, recv: RecvFn = recv_with_timeout,
                      sleep=asyncio.sleep, backoff: Callable[[str], Backoff] | None = None,
                      heartbeat_timeout_s: float = 10.0, recorder: SessionRecorder | None = None,
                      src: Src = Src.WS, kline_interval="1m", depth_levels=20, depth_speed="100ms",
                      mark_speed="1s", max_connects: int | None = None, queue_size=10_000,
                      monotonic: Clock | None = None):   # None → сторож іде за clock (реплей/тести)
    def streams(self) -> dict[str, list[str]]     # {"market": [s@kline_1m, s@aggTrade, s@markPrice@1s], "public": [s@depth20@100ms]}
    def urls(self) -> dict[str, str]              # <base>?streams=a/b/c; кожен URL — assert_readonly_url
    async def items(self) -> AsyncIterator[SessionItem]   # кадри + ControlRecord connected/disconnected обох з'єднань
    async def frames(self) -> AsyncIterator[RawFrame]
    def normalize(self, fr: RawFrame) -> MarketEvent | None      # помилка → None, лічильник normalization_errors
    async def events(self) -> AsyncIterator[MarketEvent]; __aiter__ = events
    async def aclose(self) -> None
    # статистика: frames_received, bad_frames, connects{conn}, disconnects[list[Disconnect]],
    #             watchdog_fires{conn}, backoffs{conn: Backoff}, stats (dict)
```
Поведінка (перевірено тестами на фіктивних з'єднаннях):
* `ts_ingest_ns` кожного кадру = `clock.now_ns()` у момент отримання; кадр без `stream`/`data` (напр.
  відповідь на SUBSCRIBE) відкидається (`bad_frames`).
* Сторож тиші: `recv(ws, remaining)`; `TimeoutError` і `now ≥ last + timeout` → `HeartbeatTimeoutError` →
  розрив класу `HEARTBEAT_TIMEOUT` → перепідключення. `now`/`last`/`remaining` — з `monotonic` (воркери передають
  `infra.wallclock.MonotonicClock`, WS-07 → WIRE-02): крок NTP уперед не дає хибного розриву, крок назад не ховає
  справжню тишу (`tests/unit/test_wiring_ws_monotonic.py`). Мітки кадрів і записів керування — і далі `clock`.
* Після розриву: `ControlRecord("disconnected", detail=Disconnect.detail)`; фатальний клас → `WsFatalError`
  у споживача; `NORMAL/GOING_AWAY` після робочої сесії — без паузи; інакше `sleep(backoff.next_delay())`.
  Backoff скидається лише після ПЕРШОГО справжнього кадру нової сесії.
* `max_connects` — межа спроб на з'єднання (для тестів/скриптів); коли всі задачі завершились, `items()` закінчується.
* `recorder` отримує кожен кадр і запис керування (дзеркало live-потоку в `.jsonl.gz`).
* Втрачені під час розриву події клієнт не відновлює — це `IngestPipeline` + REST-добір.

## 5. `ingest/gap_detector.py` — прогалини (`seq` для угод, `time` для свічок)

```python
@dataclass(frozen=True, slots=True)
class GapRecord(gap_id, instrument, stream: Stream, detector: GapDetectorKind, ts_lo_ns, ts_hi_ns, expected_count,
                status=GapStatus.OPEN, filled_rows=0, attempts=0, seq_lo=None, seq_hi=None, tf=None,
                detected_at_ns=0, closed_at_ns=None, exchange_ns=None):
    def to_row(self) -> dict        # колонки ingest_gap: stream, ts_lo_ns, ts_hi_ns, expected_count, filled_rows,
                                    # detector, status, attempts, detected_at_ns, closed_at_ns (instrument_id — репозиторій)
    def to_backfill_gap(self) -> backfill.Gap      # лише для KLINES
TRANSITIONS: OPEN→{FILLING, FILLED, UNFILLABLE}; FILLING→{FILLED, PARTIAL, UNFILLABLE};
             PARTIAL→{FILLING, FILLED, UNFILLABLE}; UNFILLABLE→{FILLING, FILLED}; FILLED→∅
def transition(g, status, *, at_ns, filled_rows=None, count_attempt=False) -> GapRecord   # недозволене → ValueError
@dataclass class GapStats(healed_holes, late_fills, duplicate_trades, stale_klines, kline_minutes_settled)
@dataclass class GapDetector(instrument: str, clock: Clock, tf="1m", grace_ms=2000, max_reorder_ids=50):
    gaps: dict[int, GapRecord]; stats: GapStats; latest_event_ns (біржовий час); open_gaps; pending_holes
    def on_kline(self, c: Candle) -> list[GapRecord]     # нові OPEN і ті, що «заросли» пізнім надходженням (FILLED)
    def on_trade(self, t: Trade) -> list[GapRecord]
    def on_time(self, ts_event_ns: int) -> list[GapRecord]   # біржовий час з markPrice/depth
    def flush(self) -> list[GapRecord]                   # підтвердити всіх кандидатів у дірки (реконект/кінець)
    def begin_fill(self, gap_id) -> GapRecord            # → FILLING, attempts + 1
    def resolve(self, gap_id) -> GapRecord               # → FILLED | PARTIAL | UNFILLABLE (за поштучним обліком), closed_at
    def missing(self, gap_id) -> frozenset[int]; def missing_keys(self, gap_id) -> Iterator[int]
```
Семантика:
* **KLINES / time**: `ts_lo_ns..ts_hi_ns` — open_time першої/останньої відсутньої свічки (включно, як
  `backfill.Gap`); хвилина `m` втрачена, якщо біржовий час (максимум `ts_event` будь-яких подій) ≥
  `m + tf + grace` і закритої свічки `m` немає. Хвилини до першого побаченого оновлення не очікуються.
* **TRADES / seq**: `seq_lo..seq_hi` — відсутні aggTrade id (включно), `ts_lo/ts_hi` — час угод по краях дірки.
  Кандидат `[max+1, a−1]` підтверджується, якщо ширший за `max_reorder_ids`, або біржовий час пішов на
  `grace_ms`, або `flush()`; пізні id «заростають» кандидата (перестановка кадрів — не прогалина).
* `exchange_ns` — біржовий час у момент виявлення: `RestBackfiller` використовує його як `now_ms` добору.
* Детектор `bucket_count` не реалізовано (див. deviations.d/ingest_ws.md, D-WS-05).

## 6. `ingest/candles.py` — `CandleAggregator`

```python
def same_values(a: Candle, b: Candle) -> bool   # o,h,l,c,volume,quote_volume,trades_count
@dataclass class AggregatorStats(released, duplicates, conflicts, skipped, late_after_skip, stale_updates, max_held)
    # late_after_skip ⊆ duplicates: закриття хвилини, вже оголошеної недобірною (skip) і не випущеної
class CandleAggregator(tf="1m", *, instrument: str | None = None, ordered=True, start_open_ns=None, retention=100_000):
    current: Candle | None          # останній стан поточного (незакритого) бару
    next_open_ns; held; stats
    def offer(self, c: Candle) -> list[Candle]      # випущені закриті свічки (строго за зростанням open_time)
    def skip(self, lo_open_ns, hi_open_ns) -> list[Candle]   # оголосити хвилини недобірними → розблокувати наступні
    def flush(self) -> list[Candle]                 # кінець потоку: випустити утримане (з дірками)
    def released_opens(self) -> list[int]
    def is_skipped(self, open_ns) -> bool           # хвилину пропущено (skip) і не випущено — її закриття вже не вийде
```
Інваріанти: кожен `open_time` випускається ≤ 1 разу (повтор → `duplicates`, інші значення → `conflicts`,
перша версія лишається); `ordered=True`: свічка `m+1` чекає, доки не випущено або не пропущено `m`;
перша очікувана хвилина — хвилина першого побаченого оновлення (навіть незакритого).

## 7. `ingest/pipeline.py` — `IngestPipeline` і REST-добір

```python
type Sink[T] = Callable[[T], Any]           # синхронний або async колбек (await, якщо повернув awaitable)
def conn_of(ev) -> "market" | "public";  def event_kind(ev) -> "candle" | "trade" | "book" | "mark"
def journal_sink(journal: EventJournal) -> Callable[[MarketEvent], None]   # append(kind, to_canonical(ev), ts_event, ts_ingest)
@dataclass(frozen=True) class InvalidEvent(ts_ingest_ns, stream, code, detail="")   # code: "NORMALIZATION:<поле>" | код інваріанта
@dataclass(frozen=True) class FillResult(events: tuple[MarketEvent, ...], requests=0, error: str | None = None)
BackfillHook = Callable[[GapRecord], Awaitable[FillResult]]
@dataclass class PipelineSinks(on_event, on_candle, on_trade, on_gap, on_invalid, on_disconnect,
                               on_anomaly)   # усі Optional; on_anomaly(AnomalyVerdict) — скор MLP кожної
                               # випущеної свічки після прогріву (t_ns = open_time) → candle.anomaly_score;
                               # викликається ДО on_candle тієї самої свічки (стік пише обидва однією транзакцією)
AnomalyWarmupHook = Callable[[int, int], Awaitable[Sequence[Bar]]]   # закриті бари open_time ∈ [lo, hi)
def contiguous_tail(bars, lo_ns, hi_ns, tf_ns) -> list[Bar]   # найдовший безперервний хвіст, що закінчується на hi − tf
@dataclass(frozen=True) class HourDq(hour_start_ns: int, score: DqScore)
@dataclass(frozen=True) class PipelineReport(health: HealthSnapshot, gaps: tuple[GapRecord, ...], candles_released,
    trades_emitted, dedup: dict[str, int], aggregator: AggregatorStats, gap_stats: GapStats, dq: tuple[HourDq, ...],
    disconnects: tuple[Disconnect, ...], backfill_requests: int, backfill_errors: tuple[str, ...])

class IngestPipeline(instrument: InstrumentLike, *, sinks=None, backfill: BackfillHook | None = None,
                     clock: Clock | None = None,           # None → FrameClock (реплей, час = ts_ingest кадру)
                     src: Src = Src.REPLAY, tf="1m", heartbeat_timeout_s: float | None = None,
                     grace_ms=2000, max_reorder_ids=50, dedup_window: int | None = 500_000,
                     anomaly: AnomalyScorer | None = None, anomaly_warmup: AnomalyWarmupHook | None = None,
                     dq_weights=None, tau0_ms=None):
    dedup: Deduplicator; detector: GapDetector; aggregator: CandleAggregator; health: PipelineHealth
    spec: InstrumentSpec | None           # tick/step, якщо instrument — повний Instrument (SymbolRef → None)
    trades_emitted; disconnects; backfill_requests; backfill_errors
    async def process(self, item: SessionItem) -> None      # кадр або ControlRecord, у порядку надходження
    async def process_event(self, ev: MarketEvent) -> None  # уже нормалізована подія (інший MarketFeed)
    async def run(self, items: AsyncIterable | Iterable) -> PipelineReport   # process(...)* + finish()
    async def finish(self) -> PipelineReport                # flush дірок, добір, випуск утриманих свічок, Q
    def dq_scores(self) -> list[HourDq]
    def prime_anomaly(self, bars: Iterable[Bar]) -> int   # історія до потоку → екстрактор скорера (торговий воркер)
    anomaly_warmup_error: str | None                      # помилка хука прогріву (холодний старт, не падіння)
```
QualityGate (§4.1, WIRE-01): перед ПЕРШОЮ випущеною свічкою конвеєр один раз кличе `anomaly_warmup(first − 218·tf,
first)` і подає в екстрактор `contiguous_tail(...)` (дірка чи помилка хука → лише частковий/холодний старт: перші
свічки без скору). Далі кожна випущена свічка: `anomaly.update(bar)` → `on_anomaly(verdict)` → `on_candle(c)`;
аномалія → `health.anomalies` і `DqAccumulator.anomalies` години open_time (N_invalid §5.17: validity =
1 − (invalid + anomalies)/total).
Порядок обробки події (однаковий для потоку і REST-добору): `normalize_binance` → `quality.invariants`
(`Instrument` → tick/step) → `Deduplicator.offer` (DUPLICATE відкидається) → stale-фільтр
(BookSnapshot з меншим `last_update_id`, MarkPrice з меншим `ts_event`) → `on_event` → Candle:
`detector.on_kline` + `aggregator.offer` → `on_candle`; Trade: `on_trade` (лише NEW) + `detector.on_trade`;
інші: `detector.on_time` → нові прогалини: `on_gap(OPEN)` → `begin_fill` → `on_gap(FILLING)` → `backfill(gap)` →
події добору тим самим шляхом → `resolve` → `on_gap(FILLED|PARTIAL|UNFILLABLE)`; недобрані хвилини → `aggregator.skip`.
Без `backfill` прогалина лишається `OPEN`, а недобрані хвилини одразу пропускаються (агрегатор не блокується).
`heartbeat_timeout_s` — сторож тиші на віртуальному часі (для реплею; у live — `None`, сторожить клієнт):
перед кожним записом сесії `poll(ts_ingest запису)` для сторожів УСІХ з'єднань, тож розрив класу
`HEARTBEAT_TIMEOUT` фіксується в момент `last + timeout` (стік `on_disconnect` отримує його, щойно віртуальний
час минув дедлайн) — і для з'єднання, що вже ніколи не озветься. Запис керування `disconnected` «роззаводить»
сторож цього з'єднання до `connected`/наступного кадру (той самий розрив не рахується двічі). Розрив
підтверджує кандидатів у дірки aggTrade id (`detector.flush()`) лише для з'єднання `market` (угоди йдуть ним).
Пізнє закриття хвилини, яку вже пропущено (`aggregator.is_skipped`: добору нема або він не вдався, а пізніші
свічки вже випущено), у `on_candle` не потрапляє (порядок) і тому прогалину НЕ закриває: вона лишається
`OPEN`/`PARTIAL`/`UNFILLABLE` для нічного добору (лічильник `aggregator.late_after_skip`).
Лаг (`ts_ingest − ts_event`) рахується лише для подій потоку. Q рахується погодинно (`HourDq`), `N_exp` —
хвилини від першої побаченої свічки до останньої закритої за біржовим часом.

```python
def normalize_rest_agg_trade(row: Mapping, instrument, ts_ingest_ns) -> Trade   # {a,p,q,f,l,T,m[,nq]}; той самий event_uid, що WS
async def fetch_agg_trades(client: BinanceRestClient, symbol, from_id, to_id, instrument, *, limit=1000,
                           sleep=asyncio.sleep) -> tuple[list[Trade], int]      # GET /fapi/v1/aggTrades?fromId, вага 20
class RestBackfiller(client: BinanceRestClient, instrument, *, interval="1m", sleep=asyncio.sleep):   # BackfillHook
    async def __call__(self, gap: GapRecord) -> FillResult
    # KLINES → backfill.backfill_klines(..., now_ms=gap.exchange_ns // 10⁶), лише свічки з діапазону прогалини
    # TRADES → fetch_agg_trades(seq_lo, seq_hi)
@asynccontextmanager async def offline_rest_client(fixture: RestFixture, clock: Clock, *, base_url=...) -> BinanceRestClient
    # BinanceRestClient поверх httpx.MockTransport(FixtureRestHandler) — офлайн-демо без мережі
@dataclass class SessionCapture(candles, trades, gaps, invalid, disconnects, events); def sinks(self) -> PipelineSinks
async def replay_session(path, instrument, *, backfill=None, sinks=None, clock: FrameClock | None = None,
                         heartbeat_timeout_s: float | None = 10.0, **kw) -> PipelineReport
@dataclass(frozen=True) class SessionReference(closed: dict[open_ns, k-payload], agg_ids: frozenset[int])
def session_reference(path) -> SessionReference           # еталон із СИРИХ кадрів (незалежно від конвеєра)
@dataclass(frozen=True) class RecoveryStats(expected_candles, lost_candles, duplicated_candles, extra_candles,
    mismatched_candles, candles_in_order, expected_trades, lost_trades, duplicated_trades, extra_trades, gaps,
    gap_status, watchdog_fires, first_detect_ns, last_closed_ns); zero_loss: bool (property)
def recovery_stats(ref, cap, report) -> RecoveryStats
```

## 8. Фікстури і скрипт

* `fixtures/ws/pathological/{gap,dup,reorder,clock_jump,stall,flash_crash}.jsonl.gz` — формат §7, похідні
  від `sample_btcusdt_4m.jsonl.gz`; поруч `<назва>.rest.json`: `{"scenario","symbol","interval","fault":{kind,
  start_ns,end_ns,...},"klines":[[12 полів]],"aggTrades":[{a,p,q,f,l,T,m}],...}`.
* `scripts/make_pathological.py [--report] [--no-generate]` — генерує (побайтово відтворювано) і пише
  `docs/figures/ingest_pathological.md`.
* `flash_crash` — синтетичний обвал −45 % (M1+5 с … M1+50 с), утримання до M2+15 с, відновлення до M3+15 с;
  OHLC узгоджений, ціни на сітці tick 0.10 — для демо ризик-контуру.

## Приклад: live-інжест у БД (хвиля 2)

```python
from fuzzhelm.config import get_settings
from fuzzhelm.core.enums import Src
from fuzzhelm.infra.wallclock import SystemClock
from fuzzhelm.ingest.pipeline import IngestPipeline, PipelineSinks, RestBackfiller, journal_sink
from fuzzhelm.ingest.ws_client import BinanceWsClient

s, clock = get_settings(), SystemClock()
ws = BinanceWsClient(s, clock, instruments=[inst])                 # inst: Instrument з exchangeInfo
pipe = IngestPipeline(inst, clock=clock, src=Src.WS, backfill=RestBackfiller(rest_client, inst),
                      sinks=PipelineSinks(on_event=journal_sink(journal), on_candle=candle_repo_upsert,
                                          on_gap=gap_repo_save))
async for item in ws.items():
    await pipe.process(item)
```

## Приклад: офлайн-реплей патологічної сесії з добором

```python
from fuzzhelm.ingest.pipeline import RestBackfiller, SessionCapture, offline_rest_client, replay_session
from fuzzhelm.ingest.replay import FrameClock, RestFixture

fx, clock, cap = RestFixture.load("fixtures/ws/pathological/stall.rest.json"), FrameClock(), SessionCapture()
async with offline_rest_client(fx, clock) as client:
    report = await replay_session("fixtures/ws/pathological/stall.jsonl.gz", inst,
                                  backfill=RestBackfiller(client, inst), sinks=cap.sinks(), clock=clock)
assert all(g.status == "FILLED" for g in report.gaps)
```
