# Таблиця мапінгу полів двох бірж на канонічні DTO

Артефакт звіту (фаза 2, підрозділ 2.4 «Підсистема агрегації, нормалізації та збереження»).
Реалізація — `src/fuzzhelm/ingest/normalize.py`; перевірка — `tests/unit/test_normalize.py` на сирих
відповідях бірж, записаних 2026-09-18 (`fixtures/rest/*`, `fixtures/ws/sample_btcusdt_4m.jsonl.gz`).
Автор: Андрій Жук, 2026.

Умовні позначення: «—» — біржа поля не надає; *курсив* — значення обчислене, а не надане біржею.
Усі ціни/обсяги — `Decimal` з рядка біржі без проміжного float (рядок має вигляд `-?\d+(\.\d+)?`);
час — ціле число наносекунд UTC (мс·10⁶ або с·10⁹, лише цілочисельне множення).

## 1. Свічка → `Candle`

| Поле `Candle` | Binance REST `/fapi/v1/klines` (масив з 12) | Binance WS `<s>@kline_1m` | Kraken REST `/0/public/OHLC` (масив з 8) |
|---|---|---|---|
| `instrument` | з `symbols.py`: `BTCUSDT` → `BTC-USDT-PERP` | те саме (`s`, `k.s`) | `XBTUSD` / ключ `XXBTZUSD` → `BTC-USD-SPOT` |
| `venue` | `BINANCE_USDM` | `BINANCE_USDM` | `KRAKEN` |
| `tf` | параметр запиту `interval` | `k.i` (звіряється з іменем потоку) | параметр `interval` (хв): 1 → `1m` |
| `open_time_ns` | `[0]` openTime, мс · 10⁶ | `k.t`, мс · 10⁶ | `[0]` time, **с** · 10⁹ |
| `close_time_ns` | `[6]` closeTime, мс · 10⁶ (перевірка: `= open + tf − 1 мс`) | `k.T`, мс · 10⁶ (та сама перевірка) | *open + tf − 1 мс* (конвенція Binance) |
| `o, h, l, c` | `[1] [2] [3] [4]` | `k.o k.h k.l k.c` | `[1] [2] [3] [4]` |
| `volume` (база) | `[5]` | `k.v` | `[6]` |
| `quote_volume` | `[7]` | `k.q` | — → `0` (значення DTO «не надано») |
| `trades_count` | `[8]` | `k.n` | `[7]` count |
| `vwap` | — → `None` (виводиться як `[7]/[5]`, але не зберігається) | — → `None` | `[5]` vwap; `None`, якщо `volume = 0` |
| `is_closed` | *`closeTime < час біржі`* (`server_time_ms`) | `k.x` | *`time ≤ last`* (`last` — курсор останнього зафіксованого бару) |
| `src` | `REST (2)` | `WS (1)` або `REPLAY (3)` | `REST (2)` |
| `ts_event_ns` | closeTime · 10⁶; для незакритого бару з відомим часом біржі — *server_time_ms · 10⁶* | `E` (час події) · 10⁶ | *close_time_ns* |
| `ts_ingest_ns` | момент отримання відповіді (Clock) | `ts_ingest_ns` кадру | момент отримання відповіді (Clock) |
| `event_uid` | `BLAKE2b-128([venue, "klines", symbol_canon, tf, open_time_ns])` | те саме | те саме |
| не мапиться | `[9]` takerBuyBase, `[10]` takerBuyQuote, `[11]` ignore | `k.f`, `k.L` (id угод), `k.V`, `k.Q` (taker buy), `k.B` (ignore) | — |

## 2. Угода → `Trade` (лише Binance WS `<s>@aggTrade`)

| Поле `Trade` | Поле payload | Примітка |
|---|---|---|
| `agg_id` | `a` | ключ послідовності для детекції прогалин |
| `first_trade_id`, `last_trade_id` | `f`, `l` | |
| `price`, `qty` | `p`, `q` | ціна > 0, кількість > 0 |
| `is_buyer_maker` | `m` | |
| `ts_event_ns` | `T` (час угоди) · 10⁶ | `E` (час розсилки) перевіряється, не мапиться |
| `event_uid` | `[venue, "trades", symbol_canon, a]` | |
| не мапиться | `E`, `nq`, `st` | у 4-хв зразку `nq` = `q` в усіх 3887 кадрах; `st` = `1` в усіх кадрах обох сесій (у брифінгу не описані) |

## 3. Mark price / funding → `MarkPrice`

| Поле `MarkPrice` | Binance WS `<s>@markPrice@1s` | Binance REST `/fapi/v1/premiumIndex` |
|---|---|---|
| `mark_price` | `p` | `markPrice` |
| `index_price` | `i` | `indexPrice` |
| `funding_rate` | `r` | `lastFundingRate` |
| `next_funding_time_ns` | `T` · 10⁶ | `nextFundingTime` · 10⁶ |
| `ts_event_ns` | `E` · 10⁶ | `time` · 10⁶ |
| `event_uid` | `[venue, "mark", symbol_canon, ts_event_ns]` | те саме |
| не мапиться | `P` (оцінна ціна розрахунку), `ap` (у 4-хв зразку = `p` в усіх 239 кадрах), `st` | `estimatedSettlePrice`, `interestRate` |

## 4. Книга заявок → `BookSnapshot` (Binance WS `<s>@depth20@100ms`)

| Поле `BookSnapshot` | Поле payload | Примітка |
|---|---|---|
| `bids` | `b`: `[[price, qty], …]` | строго за спаданням ціни, ≤ 20 рівнів |
| `asks` | `a`: `[[price, qty], …]` | строго за зростанням ціни |
| `last_update_id` | `u` | |
| `ts_event_ns` | `T` (час транзакції) · 10⁶ | |
| `event_uid` | `[venue, "depth", symbol_canon, u]` | |
| не мапиться | `E`, `U`, `pu`, `ps`, `st` | у 4-хв зразку `pu` = `u` попереднього знімка в усіх 2286 парах сусідніх кадрів |

## 5. Довідник інструмента → `Instrument`

| Поле `Instrument` | Binance `/fapi/v1/exchangeInfo` (`symbols[]`) | Kraken `/0/public/AssetPairs` |
|---|---|---|
| `symbol_venue` | `symbol` (`BTCUSDT`) | `altname` (`XBTUSD`) |
| `symbol_canon` | `BTC-USDT-PERP` | `BTC-USD-SPOT` |
| `base_asset` / `quote_asset` | `baseAsset` / `quoteAsset` | `base` / `quote` з аліасами (`XXBT`→`BTC`, `ZUSD`→`USD`) |
| `contract_type` | `contractType = PERPETUAL` → `PERP` | `SPOT` |
| `tick_size` | `PRICE_FILTER.tickSize` (`0.10`) | `tick_size` (`0.1`) |
| `step_size` | `LOT_SIZE.stepSize` (`0.001`) | *10^−`lot_decimals`* (`0.00000001`) |
| `min_notional` | `MIN_NOTIONAL.notional` (`50`) | `costmin` (`0.5`) |
| `mmr` | — у публічному API немає → `0.005` (deviations D-04) | — → `0.005` (для споту не використовується) |

## 6. Спостережені відмінності двох джерел (для підрозділу 2.4)

* **Одиниці часу:** Binance — мілісекунди, Kraken — секунди; час закриття свічки Kraken не надає.
* **Позначка закритості:** Binance WS — явне `x`; Binance REST — лише за порівнянням з часом біржі;
  Kraken — курсор `last` (у записаній відповіді останній рядок — поточний незакритий бар, `time > last`).
* **Обсяги:** Binance дає обсяг і в базі, і в котирувальній валюті; Kraken — лише в базі плюс VWAP.
* **Точність:** Binance BTCUSDT — 2 знаки ціни (`tickSize = 0.10`, рядки `"81015.40"`), Kraken XBTUSD —
  1 знак (`"81200.0"`); масштаб рядка зберігається в Decimal і в канонічному JSON.
* **Ідентифікатори символів:** Kraken приймає `XBTUSD`, але повертає результат під ключем `XXBTZUSD`.
* **Ціна:** перпетуал USDT (Binance) проти споту USD (Kraken) — різниця містить базис USDT/USD і премію
  перпетуалу. На спільному вікні 720 хв (2026-09-18 07:25–19:24 UTC): середня розбіжність закриттів
  +2.03 б.п., середня за модулем 3.01 б.п., медіана за модулем ≈ 3.32 б.п., p95 ≈ 5.62 б.п. (лінійна інтерполяція; найближчий ранг дає те саме),
  максимум 8.42 б.п.;
  жодна хвилина не перевищила поріг 50 б.п. (розрахунок — `ingest.crosscheck` на `fixtures/rest/*`).
