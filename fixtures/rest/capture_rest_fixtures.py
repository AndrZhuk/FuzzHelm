"""Одноразовий запис REST-фікстур з публічних read-only ендпоінтів Binance USDⓈ-M і Kraken.

Найменування: fixtures/rest/capture_rest_fixtures.py
Призначення: офлайн-фікстури для тестів ingest (respx-моки будуються з цих байтів; тести мережу не чіпають).
Автор: Андрій Жук, 2026.

Навмисно НЕ використовує fuzzhelm.ingest.*: фікстура — це сирі байти біржі, незалежні від коду,
який ними перевіряється (інакше помилка клієнта «переїхала» б у фікстуру й тест став би тавтологією).
Хости — лише з fuzzhelm.config.ALLOWED_READONLY_HOSTS.

Запуск (одноразово, з мережею):  uv run python fixtures/rest/capture_rest_fixtures.py
"""

from __future__ import annotations

import gzip
import json
import time
from pathlib import Path
from typing import Any

import httpx

from fuzzhelm.config import assert_readonly_url

OUT = Path(__file__).resolve().parent
BINANCE = assert_readonly_url("https://fapi.binance.com")
KRAKEN = assert_readonly_url("https://api.kraken.com")
MIN_MS = 60_000
N_KLINES = 3000
KEEP_SYMBOLS = ("BTCUSDT", "ETHUSDT")
KEEP_ASSETS = ("USDT", "BTC", "ETH")


def _used(r: httpx.Response) -> int:
    return int(r.headers["x-mbx-used-weight-1m"])


def _get(c: httpx.Client, url: str, params: dict[str, Any] | None = None) -> httpx.Response:
    r = c.get(url, params=params)
    if r.status_code != 200:
        raise RuntimeError(f"{url} {params} -> {r.status_code} {r.text[:200]}")
    return r


def measure_kline_weights(c: httpx.Client) -> list[dict[str, int]]:
    """Емпірична перевірка таблиці ваг klines: приріст X-MBX-USED-WEIGHT-1M між двома запитами /time
    (вага /time = 1). Якщо між замірами змінилась хвилина-вікно Binance, замір повторюється."""
    out: list[dict[str, int]] = []
    for limit in (1, 99, 100, 101, 499, 500, 501, 1000, 1001, 1500):
        for _ in range(3):
            before = _get(c, f"{BINANCE}/fapi/v1/time")
            r = _get(c, f"{BINANCE}/fapi/v1/klines", {"symbol": "BTCUSDT", "interval": "1m", "limit": limit})
            after = _get(c, f"{BINANCE}/fapi/v1/time")
            w_kl = _used(r) - _used(before)
            if _used(after) - _used(r) == 1 and w_kl > 0:
                out.append({"limit": limit, "rows": len(r.json()), "weight": w_kl})
                break
        else:
            raise RuntimeError(f"could not measure weight for limit={limit}")
    return out


def main() -> None:
    meta: dict[str, Any] = {"captured_at_local_ns": time.time_ns(), "files": {}}
    with httpx.Client(timeout=30.0, headers={"User-Agent": "fuzzhelm-fixture-capture/0.1"}) as c:
        # --- серверний час (оцінка зсуву годинника в тестах)
        t0 = time.time_ns()
        r = _get(c, f"{BINANCE}/fapi/v1/time")
        t1 = time.time_ns()
        (OUT / "server_time.json").write_bytes(r.content)
        meta["files"]["server_time.json"] = {"local_send_ns": t0, "local_recv_ns": t1}
        server_ms = r.json()["serverTime"]

        # --- exchangeInfo, обрізаний до двох символів (+ rateLimits, які лишаються повністю)
        r = _get(c, f"{BINANCE}/fapi/v1/exchangeInfo")
        info = r.json()
        info["symbols"] = [s for s in info["symbols"] if s["symbol"] in KEEP_SYMBOLS]
        info["assets"] = [a for a in info.get("assets", []) if a["asset"] in KEEP_ASSETS]
        (OUT / "exchange_info.json").write_text(json.dumps(info, indent=1) + "\n", encoding="utf-8")
        meta["files"]["exchange_info.json"] = {"trimmed_to": list(KEEP_SYMBOLS),
                                               "assets_trimmed_to": list(KEEP_ASSETS)}

        # --- premiumIndex (mark/index/funding)
        r = _get(c, f"{BINANCE}/fapi/v1/premiumIndex", {"symbol": "BTCUSDT"})
        (OUT / "premium_index.json").write_bytes(r.content)

        # --- реальна відповідь-помилка Binance (для тесту розбору помилок)
        r = c.get(f"{BINANCE}/fapi/v1/premiumIndex", params={"symbol": "NOSUCHSYMBOL"})
        (OUT / "binance_error_invalid_symbol.json").write_bytes(r.content)
        meta["files"]["binance_error_invalid_symbol.json"] = {"http_status": r.status_code}

        # --- 3000 закритих 1m-свічок BTCUSDT, сторінками по 1500 з перекриттям в один бар
        last_open = (server_ms // MIN_MS) * MIN_MS - MIN_MS          # останній ЗАКРИТИЙ бар
        start = last_open - (N_KLINES - 1) * MIN_MS
        rows: list[list[Any]] = []
        pages: list[dict[str, Any]] = []
        cursor = start
        while True:
            limit = min(1500, (last_open - cursor) // MIN_MS + 1)
            r = _get(c, f"{BINANCE}/fapi/v1/klines",
                     {"symbol": "BTCUSDT", "interval": "1m", "startTime": cursor, "endTime": last_open,
                      "limit": limit})
            page = r.json()
            overlap_ok = None
            if rows:
                overlap_ok = page[0] == rows[-1]
                page = page[1:] if page and page[0][0] == rows[-1][0] else page
            pages.append({"startTime": cursor, "limit": limit, "rows": len(r.json()),
                          "overlap_identical": overlap_ok, "used_weight_1m": _used(r)})
            rows.extend(page)
            if rows[-1][0] >= last_open:
                break
            cursor = rows[-1][0]                                          # перекриття: 1 бар
        with gzip.open(OUT / "binance_klines.json.gz", "wt", encoding="utf-8") as f:
            json.dump(rows, f, separators=(",", ":"))
        meta["files"]["binance_klines.json.gz"] = {
            "symbol": "BTCUSDT", "interval": "1m", "rows": len(rows),
            "first_open_ms": rows[0][0], "last_open_ms": rows[-1][0], "pages": pages,
        }

        # --- ваги klines, виміряні за заголовком X-MBX-USED-WEIGHT-1M
        meta["kline_weight_measurements"] = measure_kline_weights(c)

        # --- Kraken: OHLC XBTUSD 1m (останні ~720 хв) і довідник пари
        r = _get(c, f"{KRAKEN}/0/public/OHLC", {"pair": "XBTUSD", "interval": 1})
        (OUT / "kraken_ohlc.json").write_bytes(r.content)
        r = _get(c, f"{KRAKEN}/0/public/AssetPairs", {"pair": "XBTUSD"})
        (OUT / "kraken_asset_pairs.json").write_bytes(r.content)

    (OUT / "capture_meta.json").write_text(json.dumps(meta, indent=1) + "\n", encoding="utf-8")
    print(json.dumps(meta, indent=1))


if __name__ == "__main__":
    main()
