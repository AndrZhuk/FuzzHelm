"""Демо «ризик-контур стримує сам себе» (§15, акт 2:30–3:20; чек-лист §17): flash_crash → HALTED без людини.

Найменування: demo_flash_crash.py
Призначення: відтворити стрес-сценарій fixtures/ws/scenarios/flash_crash.jsonl.gz (make_demo_scenario.py)
    тим самим торговим шляхом, що й воркер (профіль replay: прогрів з fixtures/rest → IngestPipeline →
    TradingLoop), і показати ФАКТИЧНУ послідовність станів автомата, записи ризик-ланцюга (VETO з observed і
    limit), засувку HALTED, «Continue» без зняття, відмову не-admin і зняття адміністратором → COOLDOWN.
Автор: Андрій Жук, 2026.

Запуск:
    uv run python scripts/demo_flash_crash.py        # офлайн, у пам'яті, без БД (Wi-Fi можна вимкнути)
    uv run python scripts/demo_flash_crash.py --db --linger 600 --speed 30
        # той самий сценарій через воркер у PostgreSQL + NOTIFY для LiveView; після кінця потоку прогін чекає
        # POST /risk/killswitch/release від admin (curl нижче) і пише зняття в audit_log / risk_event
Числа в документації — лише з виводу цього скрипта (див. docs/journal.d/workers.md).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID

ROOT = Path(__file__).resolve().parents[1]
SCENARIO = ROOT / "fixtures" / "ws" / "scenarios" / "flash_crash.jsonl.gz"


def _t(ns: int) -> str:
    return datetime.fromtimestamp(ns / 1e9, tz=UTC).strftime("%H:%M:%S")


def _pct(x: Decimal | float | None) -> str:
    return "—" if x is None else f"{float(x) * 100:+.2f}%"


async def offline(speed: float) -> int:
    from fuzzhelm.backtest.dataset import load_exchange_instrument  # noqa: PLC0415
    from fuzzhelm.core.enums import RiskState, RunKind, RunStatus  # noqa: PLC0415
    from fuzzhelm.infra.wallclock import SystemClock  # noqa: PLC0415
    from fuzzhelm.ingest.replay import ReplayFeed  # noqa: PLC0415
    from fuzzhelm.workers.trading_worker import (  # noqa: PLC0415
        MemorySink,
        TradingSession,
        WorkerProfile,
        first_kline_open_ns,
        header_line,
        offline_warmup_history,
    )

    meta = json.loads((SCENARIO.parent / "flash_crash.meta.json").read_text(encoding="utf-8"))
    t0 = int(meta["injection"]["t0_ns"])
    prof = WorkerProfile.load("replay")
    cfg = prof.backtest_config(check_invariants=True)
    inst = load_exchange_instrument("BTCUSDT")
    n = cfg.resolved_warmup()
    assert prof.history is not None
    warm = await offline_warmup_history(prof.history, inst, first_kline_open_ns(SCENARIO), n)
    sink = MemorySink()
    s = TradingSession(
        inst,
        cfg,
        seed=prof.seed,
        run_id=UUID(int=0xF1A5),
        kind=RunKind.REPLAY,
        feed="REPLAY",
        warmup_bars=n,
        sink=sink,
        streams=prof.streams,
        heartbeat_timeout_s=prof.heartbeat_timeout_s,
    )
    await s.begin({"demo": "flash_crash", "scenario_sha256": meta["sha256"]})
    s.warm_up(warm)
    print(
        header_line(feed="REPLAY", seed=prof.seed, q=None).replace("Q=—", "Q=…") + "   scenario: flash_crash"
    )
    print(
        f"ін'єкція: {_t(t0)} UTC — межа хвилини, на яку стратегія входить у лонгу "
        f"{meta['injection']['long_qty']} BTC (вхід {meta['injection']['entry_price']}, "
        f"стоп {meta['injection']['stop_price']}); профіль цін від t₀: {meta['profile_ms_from_t0']}"
    )
    feed = ReplayFeed(SCENARIO, speed=speed, clock=SystemClock(), instrument=inst, streams=prof.streams)
    await s.run(feed.items())

    print("\nхвилина  open        close       позиція  капітал      DD      денний PnL  стан")
    peak = Decimal(cfg.initial_equity or 10000)
    for out in sink.steps:
        x = out.step
        if x.open_time_ns < t0 - 3 * 60_000_000_000:
            continue
        peak = max(peak, x.equity)
        dd = 1 - x.equity / peak
        day = x.equity / Decimal(cfg.initial_equity or 10000) - 1
        mark = "  ← розрив, стоп виконано за open" if x.open_time_ns == t0 else ""
        print(
            f"{_t(x.open_time_ns)[:5]}    {out.candle.o!s:<11} {out.candle.c!s:<11} {x.position_qty!s:<8} "
            f"{x.equity:<12.2f} {_pct(dd):<7} {_pct(day):<11} {x.risk_state.value}{mark}"
        )
        if x.close_time_ns >= t0 + 8 * 60_000_000_000:
            print("…  (далі до кінця сесії — без змін стану)")
            break

    print("\nпереходи автомата (без жодної дії людини):")
    for ts, a, b, ev in s.transitions:
        print(f"  {_t(ts)}  {a} → {b}  ({ev})")
    trs = [r for r in sink.risk_events if r.rule == "risk_state"]
    for r in trs:
        print(
            f"  risk_event: observed(DD)={_pct(r.observed)}  "
            f"day_return={_pct(Decimal(r.payload['day_return']))}"
        )
    vetoes = [r for r in sink.risk_events if r.verdict is not None and r.verdict.value == "VETO"]
    print("\nзаписи VETO ризик-ланцюга:", "немає" if not vetoes else "")
    for r in vetoes:
        print(f"  {_t(r.ts_ns)}  {r.rule} · VETO · observed={r.observed} · limit={r.limit_value}")
    daily = [r for r in sink.risk_events if r.rule == "max_daily_loss"]
    if not any(r.verdict is not None and r.verdict.value == "VETO" for r in daily):
        print(
            "  MaxDailyLoss · VETO не зафіксовано: після бару розриву автомат уже HALTED, бажаний бік = 0, "
            "намірів ЗБІЛЬШИТИ експозицію немає — ланцюг (де живе MaxDailyLoss) не питали про приріст."
        )

    after = [
        o.step for o in sink.steps if s.halted_at_ns is not None and o.step.close_time_ns > s.halted_at_ns
    ]
    print(
        f"\n«Continue»: ще {len(after)} барів потоку після HALTED — стан "
        f"{sorted({x.risk_state.value for x in after})}, виконань {sum(len(x.fills) for x in after)}, "
        f"позиція {after[-1].position_qty if after else '—'}"
    )
    for role in ("analyst", "operator", "admin"):
        await s.apply_release(actor=f"{role}@demo", role=role)
        ev = sink.out_of_band[-1]
        print(f"зняття від {role:<8}: {ev['outcome']:<8} {ev['state_before']} → {ev['state_after']}")
    for a in sink.audits:
        print(
            f"  audit_log: {a.action}  before={dict(a.before).get('state', dict(a.before).get('tripped'))}  "
            f"after={dict(a.after).get('state', dict(a.after).get('tripped'))}  actor={a.actor}"
        )
    summary = await s.finish(RunStatus.DONE)
    print(
        f"\nпідсумок: капітал {summary.equity_first} → {summary.equity_last}, фінальний стан "
        f"{summary.final_state.value}, журнал {summary.journal_entries} записів, "
        f"голова {summary.journal_head[:16]}…"
    )
    ok = s.halted_at_ns is not None and all(x.risk_state is RiskState.HALTED for x in after)
    return 0 if ok else 1


async def with_db(speed: float, linger: float) -> int:
    from fuzzhelm.workers.trading_worker import WorkerOptions, run_worker  # noqa: PLC0415

    stop = asyncio.Event()
    print(
        "воркер: replay + scenario flash_crash → PostgreSQL + NOTIFY fuzzhelm_live; після кінця потоку "
        f"прогін чекає команду адміністратора до {linger:.0f} с:"
    )
    print(
        '  TOKEN=$(curl -s -X POST localhost:8000/auth/login -d "username=admin&password=…" '
        "| jq -r .access_token)"
    )
    print(
        '  curl -X POST localhost:8000/risk/killswitch/release -H "Authorization: Bearer $TOKEN" '
        '-H "Content-Type: application/json" -d \'{"reason": "demo review"}\''
    )
    summary = await run_worker(
        WorkerOptions(profile="replay", speed=speed, scenario="flash_crash", linger_s=linger), stop=stop
    )
    print(json.dumps(summary.as_dict(), ensure_ascii=False, indent=2))
    return 0 if summary.halted_at_ns is not None else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="flash_crash → HALTED demo")
    ap.add_argument("--db", action="store_true", help="run through the worker into PostgreSQL (needs the DB)")
    ap.add_argument("--speed", default="inf", help="replay pace (30 = ×30 as in the demo; inf = no pauses)")
    ap.add_argument("--linger", type=float, default=300.0, help="--db: seconds to wait for the admin release")
    args = ap.parse_args(argv)
    speed = math.inf if args.speed == "inf" else float(args.speed)
    if args.db:
        return asyncio.run(with_db(speed, args.linger))
    return asyncio.run(offline(speed))


if __name__ == "__main__":
    sys.exit(main())
