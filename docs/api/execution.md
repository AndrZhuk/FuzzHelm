# API модуля `execution` (фактично реалізоване)

Пакет: `src/fuzzhelm/execution/`. Домен — **Decimal** (ціни, кількості, гроші); `float(<expr>)` і
`Decimal(<expr>)` не використовуються (межа типів). Модуль поза списком детермінізму, але вся
випадковість — лише seeded `numpy.random.Generator(PCG64(seed))` усередині `CostModel`.

---

## 1. `execution.cost_model`

```python
class CostMode(StrEnum): ZERO = "zero"; SQRT_IMPACT = "sqrt_impact"; FULL = "full"

class CostModelConfig(BaseModel)          # схема config/cost_model.yaml (extra="forbid")
    mode: CostMode = FULL; k_s: Decimal = 0.5; taker_fee: Decimal = 0.0004; maker_fee: Decimal = 0.0002
    default_spread_ticks: int = 1; funding_hours_utc: tuple[int, ...] = (0, 8, 16); noise_bps: Decimal = 0

@dataclass(frozen=True, slots=True)
class FillQuote:  price, raw_price, half_spread, impact, noise, slippage_bps   # усе Decimal

@dataclass(frozen=True, slots=True)
class FundingCharge: instrument: str; ts_ns: int; position_qty: Decimal; mark_price: Decimal;
                     rate: Decimal; amount: Decimal      # amount > 0 — рахунок СПЛАТИВ

class CostModel:
    def __init__(self, mode: CostMode | str = FULL, k_s=Decimal("0.5"), taker=Decimal("0.0004"),
                 maker=Decimal("0.0002"), noise_bps=Decimal(0), seed: int = 0, *, spread_ticks: int = 1,
                 funding_hours_utc: Sequence[int] = (0, 8, 16)) -> None
        # позиційний порядок = contracts.md §12: CostModel(mode, k_s, taker, maker, noise_bps, seed)
    @classmethod
    def from_config(cls, cfg: Mapping | None = None, *, seed: int, mode: CostMode | str | None = None) -> CostModel
    charges_fees: bool; charges_funding: bool; has_price_impact: bool      # властивості режиму
    def impact(self, p_ref, qty, sigma, v_bar) -> Decimal                   # k_s·σ·P_ref·√(Q/V_bar)
    def quote(self, side: Side, p_ref, qty, *, sigma, v_bar, tick) -> FillQuote
    def fee_rate(self, liquidity: Liquidity) -> Decimal
    def fee(self, notional, liquidity) -> Decimal                           # |N|·τ
    def funding_payment(self, position_qty, mark_price, rate) -> Decimal    # q·P_mark·rate
    def is_funding_time(self, t_ns) -> bool
    def next_funding_time(self, t_ns) -> int                                # строго > t_ns
    def funding_times(self, t_from_ns, t_to_ns) -> list[int]                # у (t_from, t_to]
```

Формула: `P_fill = P_ref + side·(δ_spread/2 + k_s·σ·P_ref·√(Q/V_bar)) + P_ref·noise_bps·1e−4·z`,
`δ_spread = default_spread_ticks·tick`, `z ~ N(0,1)` з PCG64(seed); ціна → `quantize_price` (HALF_EVEN),
мінімум — один tick. `slippage_bps = side·(price − P_ref)/P_ref·1e4` (квантовано до 0.0001).

| режим | спред + імпакт + шум | комісії | фандинг |
|---|---|---|---|
| `zero` | ні (`P_fill = P_ref`) | ні | ні |
| `sqrt_impact` | так | ні | ні |
| `full` | так | так | так |

Інваріанти: `impact(k²·Q) == k·impact(Q)` точно; `fee(N, TAKER) > fee(N, MAKER)`; кожен `quote` у режимах
`sqrt_impact/full` при `noise_bps > 0` споживає рівно одне `z` (послідовність = порядок виконань);
`V_bar ≤ 0` → участь `Q/V_bar := 1` (консервативно, не нуль). **Шум — єдине джерело залежності кривої
капіталу від seed** (потрібно для негативного контролю `test_different_seed_changes_equity`); у `zero`
і при `noise_bps = 0` результат від seed не залежить.

---

## 2. `execution.paper_broker`

```python
DEFAULT_VOL_LAMBDA = Decimal("0.94"); DEFAULT_FUNDING_RATE = Decimal("0.0001")

class BarLike(Protocol): instrument; open_time_ns; o; h; l; c; volume      # Candle або DecBar
@dataclass(frozen=True, slots=True)
class DecBar: instrument: str; open_time_ns: int; o; h; l; c; volume: Decimal   # легкий бар для бектесту
def dec_bar_from_candle(c: Candle) -> DecBar

class PaperBroker:                                # реалізує core.ports.ExecutionVenue, name = "paper"
    def __init__(self, cost_model: CostModel, instruments: Mapping[str, Instrument] | Iterable[Instrument],
                 ids: IdGenerator, clock: Clock, *, vol_lambda=Decimal("0.94"),
                 default_funding_rate=Decimal("0.0001")) -> None
    # порт
    def submit(self, req: OrderRequest) -> OrderAck          # лише черга; ack.ts_ns = clock.now_ns()
    def on_bar(self, bar: BarLike) -> list[Fill]
    def cancel_all(self, instrument: str) -> None            # скасовує заявки в черзі і TP (liq лишається)
    def cancel(self, client_order_id: UUID) -> bool          # одна заявка NEW → CANCELED; False — немає/фінальна
                                                             # (засувка рушія знімає вхід, стоп лишає: ENG-20)
    # рівні позиції
    def set_take_profit(self, instrument, price: Decimal | None, *, side: Side | None = None) -> None
        # квантується до tick; side=None — на поточну/наступну позицію (скидається її закриттям/розворотом);
        # side=LONG|SHORT — діє лише на позицію цього боку і переживає закриття протилежної
    def set_liquidation_price(self, instrument, price: Decimal | None) -> None
    def set_funding_rate(self, instrument, rate: Decimal) -> None            # з MarkPrice/premiumIndex
    # стан
    def position(self, instrument) -> Decimal                # підписана, погляд біржі
    def sigma(self, instrument) -> Decimal; def v_bar(self, instrument) -> Decimal
    def order_ack(self, client_order_id) -> OrderAck | None  # поточний статус заявки
    def exit_reason(self, client_order_id) -> ExitReason | None   # STOP | TP | LIQUIDATION | None
    def open_orders(self, instrument) -> list[OrderRequest]
    def drain_funding(self) -> list[FundingCharge]           # забрати нарахування (рівно один раз)
```

Порядок усередині `on_bar(bar)` (бари одного інструмента — строго зростаючі за `open_time_ns`,
інакше `ValueError`):

1. **Фандинг** за кожен момент 00/08/16 UTC у `(попередній open, bar.open]` на позицію *до* виконань бару;
   mark = `bar.o` (для моменту, що збігається з open бару), ставка — остання з `set_funding_rate`,
   інакше `DEFAULT_FUNDING_RATE`. Нараховується лише в режимі `full`. Забирати через `drain_funding()`.
2. **MARKET** з черги (FIFO) → за `bar.o` + модель витрат; виконується лише на барі з
   `open_time_ns ≥ req.ts_created_ns` (рішення на закритті t ⇒ open t+1).
3. **Тригери** в межах `[l, h]`: STOP_MARKET-заявки (sell-стоп ≤ рівня, buy-стоп ≥ рівня; лише на барах з
   `open_time_ns ≥ req.ts_created_ns`, як і MARKET), рівень TP (якщо він не прив'язаний до іншого боку),
   ціна ліквідації. Розрив на відкритті → виконання за `open`. Шлях усередині бару — песимістичний:
   лонг `O→L→H→C`, шорт `O→H→L→C`, без позиції — спершу ближчий екстремум. Рівні на одному відрізку —
   у порядку досягнення; при однаковому рівні пріоритет liq < stop < tp. Опорна ціна виконання — рівень
   (або open при розриві) + модель витрат.
4. **Оцінки** `σ_t` (EWMA λ квадратів простих доходностей close-to-close) і `V_bar` (EWMA обсягу) —
   *після* виконань: ціна на барі t+1 бачить лише бари ≤ t. До двох закриттів `σ = 0` (імпакту немає).

Правила заявок: дубль `client_order_id` → `REJECTED/DUPLICATE_CLIENT_ID` (оригінал не змінюється, другого
виконання немає); невідомий інструмент → `VENUE_ERROR`; `qty` → `floor_qty(step)`, 0 → `ZERO_QTY`;
не-reduce-only з `qty·last_close < min_notional` → `BELOW_MIN_NOTIONAL`; усі виконання — `TAKER`.
Reduce-only ніколи не збільшує позицію (обрізається до |позиції|; 0 → `CANCELED/REDUCE_ONLY`).
Закриття або розворот позиції скасовує reduce-only стопи, що її захищали, і скидає liq та TP цієї позиції
(OCO); TP з `side`, протилежним до закритої позиції, лишається. **Розворот у рушії:** тейк нової позиції
задавати як `set_take_profit(inst, tp, side=<новий бік>)` разом із розворотною заявкою — TP без `side`
було б скинуто тим самим виконанням. Ціну ліквідації (залежить від фактичної ціни входу) задавати після
виконання;
наприкінці бару reduce-only стопи-«сироти» (не захищають ні поточну позицію, ні ту, яку відкриє заявка з
черги) скасовуються. TP і ліквідація виконуються синтетичною reduce-only заявкою (id з `ids`,
`decision_ref="TP"|"LIQUIDATION"`). `ts_fill_ns = bar.open_time_ns` (час усередині бару невідомий).

### Рекомендований цикл рушія (хвиля 2)

```python
fills = broker.on_bar(bar)                     # t+1: MARKET за open, потім стопи/TP/liq
for ch in broker.drain_funding():
    portfolio.apply_funding(ch)
for f in fills:
    portfolio.apply_fill(f, broker.exit_reason(f.client_order_id) or <SIGNAL для власних виходів>)
equity = portfolio.mark({bar.instrument: bar.c})
# ... рішення на закритті бару → broker.submit(OrderRequest(..., ts_created_ns=<час закриття бару>))
```

Швидкодія (виміряно на синтетичних 64 800 хвилинних барах, 271 виконання, режим `full`):
≈ 2.2 мкс на бар для `on_bar + Portfolio.mark` на машині розробника. Незалежний повторний замір
рецензента після виправлень (64 800 барів, 376 виконань, `full` + шум, стопи через кожні 240 барів,
3 прогони): 2.76–2.90 мкс на бар — той самий порядок.

---

## 3. `execution.portfolio`

```python
@dataclass(slots=True)
class Position: instrument; qty (зі знаком); cost_basis (= qty·avg_entry); realized_pnl; funding_paid;
                fees_paid; opened_at_ns; max_adverse_excursion   # + avg_entry, side, unrealized(mark)

@dataclass(frozen=True, slots=True)
class ClosedTrade: instrument; side; qty (max |q|); entry_price; exit_price (VWAP); gross_pnl; fees;
                   funding; pnl (= gross − fees − funding); entry_notional; exit_notional;
                   opened_at_ns; closed_at_ns; exit_reason: ExitReason | None; max_adverse_excursion

@dataclass(frozen=True, slots=True)
class PortfolioSnapshot: ts_ns; equity; cash; wallet_balance; unrealized; gross_exposure; net_exposure;
                         leverage (None при equity ≤ 0); fees_paid; funding_paid; realized_pnl

class Portfolio:
    def __init__(self, cash: Decimal) -> None                 # W₀ — початковий депозит
    def apply_fill(self, fill: Fill, exit_reason: ExitReason | None = None) -> list[ClosedTrade]
    def apply_funding(self, charge: FundingCharge) -> None
    def mark(self, prices: Mapping[str, Decimal]) -> Decimal   # оновлює mark і MAE, повертає equity
    initial; cash; wallet_balance; unrealized_pnl; equity; equity_cash_form; gross_exposure;
    net_exposure; leverage; fees_paid; funding_paid; realized_pnl; traded_notional; n_fills;
    positions: dict[str, Position]; marks: dict[str, Decimal]; closed_trades: list[ClosedTrade]
    def identity_residual(self) -> Decimal                    # (cash + Σq·p − Σfees) − equity
    def position_qty(self, instrument) -> Decimal
    def snapshot(self, ts_ns: int) -> PortfolioSnapshot
```

**Тотожність обліку (перп USDT-M)**, `dq` — підписана кількість виконання:

```
equity         = wallet_balance + unrealized
wallet_balance = W₀ + realized − fees − funding
unrealized     = Σ qᵢ·markᵢ − cost_basisᵢ
cash           = W₀ − Σ_fills dq·price − Σ funding          («спот-еквівалентні» гроші, без комісій)
cash + Σ qᵢ·markᵢ − Σ fees == equity                          (формула брифінгу)
```

Перевіряється після **кожного** кроку з допуском `1e−18` (`test_equity_accounting_identity`, hypothesis, і
наскрізно брокер→портфель). Для `equity_point.cash` рекомендовано писати `Portfolio.cash` — тоді рядки
таблиці самі задовольняють тотожність. Реалізований PnL — метод середньої ціни; розворот через нуль
закриває стару угоду і відкриває нову, комісія розворотного виконання ділиться пропорційно кількості.
До першого `mark` ціною інструмента вважається ціна першого виконання. `ClosedTrade` — вхід для
`backtest.metrics.compute_metrics(trades=...)`.

---

## 4. `execution.testnet_venue`

```python
class VenueUnavailableError(FuzzHelmError)     # транспорт/5xx: стан заявки НЕВІДОМИЙ

class BinanceTestnetVenue:                      # ExecutionVenue, name = "binance_testnet"
    def __init__(self, settings: Settings, http: httpx.Client, *, clock: Clock | None = None,
                 instruments: Mapping[str, Instrument] | Iterable[Instrument] | None = None,
                 recv_window_ms: int = 5000, time_offset_ms: int = 0) -> None
    def sign(self, query: str) -> str                         # HEX(HMAC-SHA256(secret, query))
    def submit(self, req: OrderRequest) -> OrderAck           # POST /fapi/v1/order (лише MARKET)
    def on_bar(self, bar) -> list[Fill]                       # = reconcile()
    def cancel_all(self, instrument: str) -> None             # DELETE /fapi/v1/allOpenOrders
    def query_order(self, client_order_id: UUID, instrument: str) -> OrderAck | None   # GET /fapi/v1/order
    def fetch_fill(self, client_order_id: UUID) -> Fill | None                        # GET /fapi/v1/userTrades
    def reconcile(self) -> list[Fill]                         # по одному Fill на FILLED-заявку
```

* Базова адреса проходить `assert_testnet_url` у конструкторі **і** перед кожним запитом
  (`MainnetHostRejected`); відсутні ключі → `ConfigValidationError(path="BINANCE_TESTNET_KEY")`.
* Параметри POST: `symbol, side (BUY|SELL), type=MARKET, quantity (floor до step, якщо instrument відомий),
  newClientOrderId=str(uuid), newOrderRespType=RESULT, [reduceOnly=true], recvWindow, timestamp (мс)`,
  потім `&signature=…`; заголовок `X-MBX-APIKEY`. Секрет ніколи не йде в мережу/лог/`repr`.
* Коди помилок: −2019 → `INSUFFICIENT_MARGIN`, −2022 → `REDUCE_ONLY`, −4164 → `BELOW_MIN_NOTIONAL`,
  −4116 → `DUPLICATE_CLIENT_ID`, інші 4xx → `VENUE_ERROR`; 5xx/транспорт → `VenueUnavailableError`.
  Номери й назви (−2013 NO_SUCH_ORDER, −2019 MARGIN_NOT_SUFFICIEN, −2022 REDUCE_ONLY_REJECT,
  −4116 DUPLICATED_CLIENT_ORDER_ID, −4164 MIN_NOTIONAL) звірено з публічною сторінкою кодів помилок
  Binance USDⓈ-M Futures 2026-09-18; наживо (ордером) не перевірялися.
* Статуси: NEW, PARTIALLY_FILLED→PARTIAL, FILLED, CANCELED/EXPIRED/EXPIRED_IN_MATCH→CANCELED, REJECTED.
* Символ: `Instrument.symbol_venue`, інакше `"BTC-USDT-PERP" → "BTCUSDT"`, інакше рядок як є.
* `STOP_MARKET` на testnet не надсилається (`REJECTED/VENUE_ERROR`, див. deviations).
* Заявка береться під нагляд **до** POST: якщо відповідь загублено (`VenueUnavailableError`), звіряння
  (`OrderRouter` → `query_order`, далі `reconcile()`) знаходить її і віддає рівно один `Fill`; якщо біржа
  відповідає −2013 (заявки немає), заявка позначається `REJECTED` і більше не опитується, повтор з тим
  самим id дозволено. Повторний `submit` id, який біржа вже прийняла, → `REJECTED/DUPLICATE_CLIENT_ID`
  без мережевого виклику.
* Скрипт `scripts/testnet_one_order.py --confirm [--symbol BTCUSDT --side BUY --qty 0.002]` — ОДИН
  MARKET-ордер (лише людина, лише з ключами в env); друкує `client_order_id`, `venue_order_id`, `status`.

## 5. `execution.router`

```python
class ReconcilableVenue(Protocol): def query_order(self, client_order_id, instrument) -> OrderAck | None
class OrderRouter:                               # ExecutionVenue, name = f"router:{venue.name}"
    def __init__(self, venue: ExecutionVenue, clock: Clock | None = None) -> None
    def submit(self, req) -> OrderAck; def on_bar(self, bar) -> list[Fill]; def cancel_all(self, instrument)
    def ack_of(self, client_order_id) -> OrderAck | None
    venue_calls: int
```

Ідемпотентність: той самий `client_order_id` + той самий вміст → збережена квитанція без повторного
виклику біржі; той самий id з іншим вмістом → `REJECTED/DUPLICATE_CLIENT_ID`; виняток біржі → звіряння
`query_order` (якщо біржа вміє), інакше `REJECTED/VENUE_ERROR` без запам'ятовування ключа (повтор можливий).
