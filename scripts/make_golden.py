"""Еталонні (golden) ряди RSI14 і ATR14 за Уайлдером для перевірки інкрементальних індикаторів.

Найменування: scripts/make_golden.py
Призначення: 1) один раз завантажити ~2000 реальних 1m-свічок BTCUSDT з публічного read-only
             ендпоінта Binance (fixtures/golden/source_klines_btcusdt_1m.json);
             2) порахувати RSI14/ATR14 НЕЗАЛЕЖНОЮ пакетною реалізацією (прості цикли, інша форма
             рекурентності, інший код, ніж features/indicators.py) і записати
             fixtures/golden/{rsi14.csv,atr14.csv}.
Автор: Андрій Жук, 2026.

Мережа — лише тут і лише за прапорцем --fetch; тести читають готові файли.

Визначення (Уайлдер, 1978; ініціалізація як у TA-Lib — середнє перших n значень):
  d_i  = c_i − c_{i−1},  G_i = max(d_i, 0),  L_i = max(−d_i, 0),  i ≥ 1
  Ḡ_n  = (1/n)·Σ_{i=1..n} G_i,   Ḡ_i = (Ḡ_{i−1}·(n−1) + G_i)/n,  i > n   (так само L̄)
  RSI_i = 100 − 100/(1 + Ḡ_i/L̄_i)  (L̄ = 0: 100, якщо Ḡ > 0; 50, якщо обидва нулі)
  TR_i = max(h_i − l_i, |h_i − c_{i−1}|, |l_i − c_{i−1}|),  i ≥ 1
  ATR_n = (1/n)·Σ_{i=1..n} TR_i,  ATR_i = (ATR_{i−1}·(n−1) + TR_i)/n,  i > n
Перше значення — на індексі n (0-based), до того порожньо.

Запуск:
  uv run python scripts/make_golden.py --fetch   # завантажити сирі свічки + порахувати
  uv run python scripts/make_golden.py           # лише перерахувати CSV з наявного JSON
"""

from __future__ import annotations

import argparse
import csv
import json
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = ROOT / "fixtures" / "golden"
SOURCE = GOLDEN / "source_klines_btcusdt_1m.json"
BASE_URL = "https://fapi.binance.com/fapi/v1/klines"
SYMBOL = "BTCUSDT"
INTERVAL_MS = 60_000
# Фіксоване вікно (відтворюваність): 2026-09-15 00:00 UTC, 1500 + 500 = 2000 закритих хвилинних свічок.
START = datetime(2026, 9, 15, tzinfo=UTC)
PAGES = (1500, 500)
N = 14


def fetch() -> None:
    import httpx  # noqa: PLC0415 — мережевий клієнт потрібен лише в режимі --fetch

    from fuzzhelm.config import assert_readonly_url  # noqa: PLC0415

    assert_readonly_url(BASE_URL)
    start_ms = int(START.timestamp() * 1000)
    rows: list[list[object]] = []
    with httpx.Client(timeout=20.0) as client:
        cursor = start_ms
        for limit in PAGES:
            params: dict[str, str | int] = {"symbol": SYMBOL, "interval": "1m",
                                            "startTime": cursor, "limit": limit}
            resp = client.get(BASE_URL, params=params)
            resp.raise_for_status()
            page = resp.json()
            if len(page) != limit:
                raise RuntimeError(f"expected {limit} klines, got {len(page)}")
            rows.extend(page)
            cursor = int(page[-1][0]) + INTERVAL_MS
    opens = [int(r[0]) for r in rows]  # type: ignore[call-overload]
    gaps = [i for i in range(1, len(opens)) if opens[i] - opens[i - 1] != INTERVAL_MS]
    if gaps:
        raise RuntimeError(f"non-contiguous klines at rows {gaps[:5]}")
    doc = {
        "source": BASE_URL,
        "symbol": SYMBOL,
        "interval": "1m",
        "start_utc": START.isoformat(),
        "pages": list(PAGES),
        "columns": ["open_time_ms", "open", "high", "low", "close", "volume", "close_time_ms",
                    "quote_volume", "trades", "taker_buy_base", "taker_buy_quote", "ignore"],
        "klines": rows,
    }
    SOURCE.write_text(json.dumps(doc, separators=(",", ":")) + "\n", encoding="utf-8")
    print(f"wrote {SOURCE.relative_to(ROOT)}: {len(rows)} klines")


def load_source() -> list[tuple[int, float, float, float]]:
    doc = json.loads(SOURCE.read_text(encoding="utf-8"))
    return [(int(r[0]), float(r[2]), float(r[3]), float(r[4])) for r in doc["klines"]]


def wilder_rsi_batch(closes: list[float], n: int) -> list[float | None]:
    out: list[float | None] = [None] * len(closes)
    if len(closes) <= n:
        return out
    gains = [0.0] * len(closes)
    losses = [0.0] * len(closes)
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains[i] = diff if diff > 0 else 0.0
        losses[i] = -diff if diff < 0 else 0.0
    avg_g = sum(gains[1:n + 1]) / n
    avg_l = sum(losses[1:n + 1]) / n
    for i in range(n, len(closes)):
        if i > n:
            avg_g = (avg_g * (n - 1) + gains[i]) / n
            avg_l = (avg_l * (n - 1) + losses[i]) / n
        if avg_l == 0.0:
            out[i] = 100.0 if avg_g > 0.0 else 50.0
        else:
            out[i] = 100.0 - 100.0 / (1.0 + avg_g / avg_l)
    return out


def wilder_atr_batch(highs: list[float], lows: list[float], closes: list[float],
                     n: int) -> list[float | None]:
    out: list[float | None] = [None] * len(closes)
    if len(closes) <= n:
        return out
    tr = [0.0] * len(closes)
    for i in range(1, len(closes)):
        tr[i] = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
    atr = sum(tr[1:n + 1]) / n
    out[n] = atr
    for i in range(n + 1, len(closes)):
        atr = (atr * (n - 1) + tr[i]) / n
        out[i] = atr
    return out


def write_csv(path: Path, header: list[str], rows: list[list[object]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(header)
        w.writerows(rows)


def fmt(x: float | None) -> str:
    # repr — найкоротший рядок, що точно відновлює float (round-trip), без втрати бітів
    return "" if x is None else repr(x)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--fetch", action="store_true", help="download source klines from Binance first")
    args = ap.parse_args()
    if args.fetch or not SOURCE.exists():
        fetch()
    data = load_source()
    t = [r[0] for r in data]
    h = [r[1] for r in data]
    lo = [r[2] for r in data]
    c = [r[3] for r in data]
    rsi = wilder_rsi_batch(c, N)
    atr = wilder_atr_batch(h, lo, c, N)
    write_csv(GOLDEN / "rsi14.csv", ["open_time_ms", "close", "rsi14"],
              [[t[i], repr(c[i]), fmt(rsi[i])] for i in range(len(c))])
    write_csv(GOLDEN / "atr14.csv", ["open_time_ms", "high", "low", "close", "atr14"],
              [[t[i], repr(h[i]), repr(lo[i]), repr(c[i]), fmt(atr[i])] for i in range(len(c))])
    print(f"wrote rsi14.csv / atr14.csv: {len(c)} rows, first value at index {N}")


if __name__ == "__main__":
    main()
