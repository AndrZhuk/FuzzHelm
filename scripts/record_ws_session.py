"""Запис живої WS-сесії Binance USDⓈ-M Futures у fixtures/ws/<symbol>_<date>.jsonl.gz.

Найменування: record_ws_session.py
Призначення: фаза 0 — офлайн-фікстура для ReplayFeed (демо без мережі).
Автор: Андрій Жук, 2026.

Формат файлу (контракт, див. docs/contracts.md §7):
  рядок 1  — {"v":1,"kind":"header", ...}
  далі     — {"v":1,"kind":"frame","conn":"market|public","ts_ingest_ns":int,"stream":str,"data":{...}}
             {"v":1,"kind":"control","conn":...,"ts_ingest_ns":int,"event":"connected|disconnected","detail":str}
  останній — {"v":1,"kind":"footer","ended_ns":int,"frames":int}

Binance з 2026 р. розділив ринкові WS-потоки на два шляхи (див. docs/deviations.md, D-01):
  /market/stream — kline, aggTrade, markPrice;   /public/stream — depth.
Тому рекордер тримає два з'єднання і пише їх в один файл, упорядкований за ts_ingest_ns.
"""

from __future__ import annotations

import argparse
import asyncio
import gzip
import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path

import websockets

BASE = "wss://fstream.binance.com"


def urls(symbol: str) -> dict[str, str]:
    s = symbol.lower()
    return {
        "market": f"{BASE}/market/stream?streams={s}@kline_1m/{s}@aggTrade/{s}@markPrice@1s",
        "public": f"{BASE}/public/stream?streams={s}@depth20@100ms",
    }


async def pump(conn: str, url: str, q: asyncio.Queue, stop: asyncio.Event) -> None:
    backoff = 1.0
    while not stop.is_set():
        try:
            async with websockets.connect(url, max_size=2**22, open_timeout=15, ping_interval=20) as ws:
                await q.put({"v": 1, "kind": "control", "conn": conn, "ts_ingest_ns": time.time_ns(),
                             "event": "connected", "detail": url})
                backoff = 1.0
                while not stop.is_set():
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
                    except TimeoutError:
                        continue
                    ts = time.time_ns()
                    msg = json.loads(raw)
                    await q.put({"v": 1, "kind": "frame", "conn": conn, "ts_ingest_ns": ts,
                                 "stream": msg.get("stream"), "data": msg.get("data")})
        except Exception as e:  # рекордер мусить пережити будь-який розрив
            await q.put({"v": 1, "kind": "control", "conn": conn, "ts_ingest_ns": time.time_ns(),
                         "event": "disconnected", "detail": f"{type(e).__name__}: {e}"})
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--minutes", type=float, default=45.0)
    ap.add_argument("--out", default=None)
    a = ap.parse_args()

    started = time.time_ns()
    date = datetime.fromtimestamp(started / 1e9, tz=UTC).strftime("%Y-%m-%d")
    out = Path(a.out or f"fixtures/ws/{a.symbol.lower()}_{date}.jsonl.gz")
    out.parent.mkdir(parents=True, exist_ok=True)
    part = out.with_suffix(out.suffix + ".part")

    u = urls(a.symbol)
    q: asyncio.Queue = asyncio.Queue()
    stop = asyncio.Event()
    tasks = [asyncio.create_task(pump(c, url, q, stop)) for c, url in u.items()]

    frames = 0
    deadline = time.monotonic() + a.minutes * 60
    with gzip.open(part, "wt", encoding="utf-8", compresslevel=6) as f:
        f.write(json.dumps({"v": 1, "kind": "header", "venue": "BINANCE_USDM", "symbol": a.symbol,
                            "urls": u, "started_ns": started, "minutes": a.minutes},
                           separators=(",", ":")) + "\n")
        last_report = time.monotonic()
        while time.monotonic() < deadline:
            try:
                item = await asyncio.wait_for(q.get(), timeout=1.0)
            except TimeoutError:
                continue
            f.write(json.dumps(item, separators=(",", ":")) + "\n")
            if item["kind"] == "frame":
                frames += 1
            if time.monotonic() - last_report > 60:
                f.flush()
                print(f"[record] {frames} frames, {int(deadline - time.monotonic())}s left", flush=True)
                last_report = time.monotonic()
        stop.set()
        for t in tasks:
            t.cancel()
        while not q.empty():
            item = q.get_nowait()
            f.write(json.dumps(item, separators=(",", ":")) + "\n")
            frames += item["kind"] == "frame"
        f.write(json.dumps({"v": 1, "kind": "footer", "ended_ns": time.time_ns(), "frames": frames},
                           separators=(",", ":")) + "\n")
    os.replace(part, out)
    print(f"[record] done: {out} ({frames} frames, {out.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    asyncio.run(main())
