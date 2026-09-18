# REST-фікстури (сирі байти бірж, записано 2026-09-18)

Записано один раз скриптом `capture_rest_fixtures.py` з публічних read-only хостів
(`fapi.binance.com`, `api.kraken.com`); тести мережу не чіпають — respx-моки віддають ці байти.
Метадані запису (часи, сторінки, виміряні ваги) — `capture_meta.json`. Автор: Андрій Жук, 2026.

| Файл | Джерело | Вміст |
|---|---|---|
| `binance_klines.json.gz` | `GET /fapi/v1/klines?symbol=BTCUSDT&interval=1m` | 3000 закритих 1m-барів (2026-09-16 17:25 — 2026-09-18 19:24 UTC), зшиті з 3 сторінок з перекриттям 1 бар; обидва стики побайтово збіглися |
| `exchange_info.json` | `GET /fapi/v1/exchangeInfo` | обрізано до `BTCUSDT`, `ETHUSDT` і активів USDT/BTC/ETH; `rateLimits` — повністю |
| `premium_index.json` | `GET /fapi/v1/premiumIndex?symbol=BTCUSDT` | без змін |
| `server_time.json` | `GET /fapi/v1/time` | без змін (локальні часи відправлення/отримання — у `capture_meta.json`) |
| `binance_error_invalid_symbol.json` | `GET /fapi/v1/premiumIndex?symbol=NOSUCHSYMBOL` | HTTP 400, `{"code":-1121,"msg":"Invalid symbol."}` |
| `kraken_ohlc.json` | `GET /0/public/OHLC?pair=XBTUSD&interval=1` | без змін: 721 бар (останній — незакритий), `last` |
| `kraken_asset_pairs.json` | `GET /0/public/AssetPairs?pair=XBTUSD` | без змін |
| `capture_meta.json` | — | сторінки klines, `kline_weight_measurements` (вага за приростом `X-MBX-USED-WEIGHT-1M`) |
