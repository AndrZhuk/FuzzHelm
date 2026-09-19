"""e2e: записана WS-сесія → IngestPipeline → TradingLoop (профіль replay, офлайн, без БД) і стрес-сценарій
flash_crash → HALTED без участі людини, засувка до зняття адміністратором.

Найменування: tests/e2e/test_replay_e2e.py
Автор: Андрій Жук, 2026.

Прогрів — справжня історія fixtures/rest/binance_klines.json.gz строго ДО першої свічки потоку; потік —
fixtures/ws/btcusdt_2026-09-18.jsonl.gz (45 хв, 92 101 кадр; торговий воркер споживає kline + markPrice).
Записи сесії йдуть у MemorySink (той самий протокол, що й DbSink воркера). Інтеграційний варіант на
PostgreSQL — tests/integration/test_worker_db.py.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from decimal import Decimal

import pytest
from tests.helpers.workers_replay import FLASH_CRASH, SESSION, replay, replayed_clean

from fuzzhelm.core.enums import OrderType, RiskState, RunStatus
from fuzzhelm.core.errors import LookaheadError
from fuzzhelm.core.journal import verify_chain
from fuzzhelm.ingest.recorder import RawFrame
from fuzzhelm.ingest.replay import iter_items
from fuzzhelm.workers.trading_worker import MemorySink, TradingSession, header_line

TF_NS = 60_000_000_000


def test_replay_session_end_to_end() -> None:
    """≥1 угода, 0 LookaheadError, тотожність капіталу на кожному барі, кожна угода має fired_rules,
    прогін DONE (брифінг §10 N)."""
    lookahead_errors = 0
    try:
        r = replayed_clean()
    except LookaheadError:  # pragma: no cover — саме це й перевіряється
        lookahead_errors += 1
        raise
    assert lookahead_errors == 0
    s, sink, summary = r.session, r.sink, r.summary
    steps = [o.step for o in sink.steps]

    # прогін завершено, 45 живих свічок після 523 барів прогріву, прогрів строго до потоку
    assert summary.status is RunStatus.DONE and summary.error is None
    assert summary.warmup_bars == 523 and summary.bars == len(steps) == 45
    assert int(r.warmup.t_ns[-1]) == r.first_open_ns - TF_NS < steps[0].open_time_ns
    assert [x.open_time_ns for x in steps] == [steps[0].open_time_ns + i * TF_NS for i in range(45)]

    # ≥ 1 угода: вхід і вихід виконані, позиція закрита
    fills = [f for x in steps for f in x.fills]
    closed = [p for x in steps for p in x.closed_positions]
    assert summary.closed_trades >= 1 and len(closed) >= 1 and len(fills) >= 2

    # без зазирання вперед: рішення бару t — на його ж закритті, виконання — не раніше open_{t+1}
    orders = {o.request.client_order_id: o for x in steps for o in x.orders}
    for x in steps:
        if x.decision is not None:
            assert x.decision.open_time_ns == x.open_time_ns and x.decision.decided_at_ns == x.close_time_ns
    for out in sink.steps:
        for f in out.step.fills:
            o = orders[f.client_order_id]
            assert f.ts_fill_ns > o.ts_created_ns
            if o.request.otype is OrderType.MARKET:
                # рішення на закритті t → виконання рівно на open_{t+1} (у кроці НАСТУПНОГО бару, за його open
                # ± модель витрат), жодного виконання в тому ж барі
                assert f.ts_fill_ns == out.step.open_time_ns == o.ts_created_ns + 1_000_000
                assert abs(f.price - out.candle.o) / out.candle.o < Decimal("0.001")

    # тотожність обліку на КОЖНОМУ барі: незалежна реконструкція з сирих потоків виконань і фандингу
    w0 = s.cfg.initial_equity
    assert w0 is not None
    cash, fees, pos = w0, Decimal(0), Decimal(0)
    for out in sink.steps:
        x = out.step
        for f in x.fills:
            signed = f.qty * int(f.side)
            cash -= signed * f.price
            fees += f.fee
            pos += signed
        for ch in x.funding:
            cash -= ch.amount
        recon = cash + pos * out.candle.c - fees
        assert abs(recon - x.equity) <= Decimal("1e-9"), (x.index, recon, x.equity)
        assert x.position_qty == pos
        assert x.equity_point is not None and x.equity_point.cash == cash

    # кожна угода має формальне виведення: рішення-відкриття і кожне рішення із заявками — з fired_rules
    by_open = {x.decision.open_time_ns: x.decision for x in steps if x.decision is not None}
    for p in [*closed, *(p for x in steps for p in x.opened_positions)]:
        d = by_open[p.opening_decision_ns]
        assert d.trace is not None and d.trace.fuzzy.fired, "trade without fired rules"
        assert d.trace.to_dict()["fired_rules"]
    for o in orders.values():
        d = by_open[o.decision_ns]
        assert d.order_ids and d.trace is not None and d.trace.fuzzy.fired

    # ризик-ланцюг оцінено на кожен намір (7 записів), Q живого конвеєра ≥ 0.90 (StaleDataGuard пропускав)
    intents = [x for x in steps if x.decision is not None and x.decision.order_ids]
    assert intents and all(len(x.risk_events) == 7 for x in intents)
    assert all(out.q is not None and out.q >= 0.9 for out in sink.steps)

    # журнал прогону — цілий ланцюг: session.start, 45 свічок, заявки/виконання/рішення/ризик, session.end
    kinds = [e.kind for e in sink.journal]
    assert verify_chain(sink.journal) is None and sink.journal[-1].hash.hex() == summary.journal_head
    assert kinds[0] == "session.start" and kinds[-1] == "session.end" and kinds.count("candle") == 45
    assert {"order", "fill", "decision", "risk_event"} <= set(kinds)
    assert summary.equity_hash is not None and summary.final_state is RiskState.NORMAL


def test_replay_header_says_paper_replay_no_mainnet() -> None:
    r = replayed_clean()
    h = r.session.health()
    assert h["mode"] == "PAPER" and h["feed"] == "REPLAY" and h["mainnet_keys"] is False
    assert h["source"] == "trading_worker"          # /market/health тримає його окремо від ingest-воркера
    assert h["header"] == header_line(feed="REPLAY", seed=20260918, q=r.summary.q_last)
    assert h["header"].startswith("MODE: PAPER · FEED: REPLAY · NO MAINNET KEYS · SEED 20260918 · Q=0.9")
    assert len(json.dumps(h)) < 7900                       # влазить у NOTIFY (api.live.NOTIFY_MAX_BYTES)


def test_flash_crash_scenario_frames_before_injection_are_the_recording() -> None:
    """Стрес-сценарій змінює лише кадри від t₀ (межа хвилини, на яку стратегія входить у лонгу)."""
    meta = json.loads((FLASH_CRASH.parent / "flash_crash.meta.json").read_text(encoding="utf-8"))
    t0 = int(meta["injection"]["t0_ns"])
    assert t0 % TF_NS == 0
    src = [i for i in iter_items(SESSION, ("kline",)) if isinstance(i, RawFrame)]
    dst = [i for i in iter_items(FLASH_CRASH, ("kline",)) if isinstance(i, RawFrame)]
    assert len(src) == len(dst)
    before = [(a, b) for a, b in zip(src, dst, strict=True) if int(a.data["k"]["t"]) * 1_000_000 < t0]
    after = [(a, b) for a, b in zip(src, dst, strict=True) if int(a.data["k"]["t"]) * 1_000_000 >= t0]
    assert before and after and all(a == b for a, b in before)
    gap_open = next(b for _, b in after)
    assert Decimal(gap_open.data["k"]["o"]) < Decimal("0.94") * Decimal(before[-1][0].data["k"]["c"])


@dataclass(frozen=True)
class Crashed:
    """Прогін flash_crash (спільний для модуля) + знімок стану в кінці потоку: тест зняття засувки змінює
    сесію, тож перевірки «до людини» читають знімок і не залежать від порядку тестів."""

    session: TradingSession
    sink: MemorySink
    state_at_end: RiskState
    tripped_at_end: bool
    human_records_at_end: int          # out_of_band + audits (команди людини)


@pytest.fixture(scope="module")
def crashed() -> Crashed:
    r = asyncio.run(replay(FLASH_CRASH, run_id=2, finish=False))
    return Crashed(r.session, r.sink, r.session.loop.fsm.state, r.session.loop.fsm.killswitch.is_tripped,
                   len(r.sink.out_of_band) + len(r.sink.audits))


def test_flash_crash_reaches_halted_without_human_input(crashed: Crashed) -> None:
    """§17: сценарій flash_crash доводить систему до HALTED без участі людини."""
    s, sink = crashed.session, crashed.sink
    meta = json.loads((FLASH_CRASH.parent / "flash_crash.meta.json").read_text(encoding="utf-8"))
    t0 = int(meta["injection"]["t0_ns"])
    assert crashed.human_records_at_end == 0                      # жодної команди людини до кінця потоку
    assert s.halted_at_ns is not None and crashed.state_at_end is RiskState.HALTED
    assert crashed.tripped_at_end
    halt = [t for t in s.transitions if t[2] == "HALTED"]
    assert len(halt) == 1 and halt[0][0] == t0 + TF_NS - 1_000_000          # на закритті бару розриву
    assert halt[0][3] == "HALT_BREACH"
    # довгу позицію закрив стоп за ціною ВІДКРИТТЯ бару розриву (gap-through), а не за рівнем стопа
    crash_step = next(o for o in sink.steps if o.step.open_time_ns == t0)
    [stop_fill] = crash_step.step.fills
    [closed] = crash_step.step.closed_positions
    assert closed.exit_reason is not None and closed.exit_reason.value == "STOP"
    # ціна виконання = open бару розриву ± модель витрат (ковзання + seeded-шум, EXE-02), далеко під стопом
    o = crash_step.candle.o
    assert abs(stop_fill.price - o) / o < Decimal("0.001")
    assert stop_fill.price < Decimal("0.94") * closed.stop_price
    # денний збиток ≤ −3 % від E_open (поріг halt_daily_loss), журнал переходу має observed = просадку
    tr_rec = next(r for r in sink.risk_events if r.rule == "risk_state" and r.state_to is RiskState.HALTED)
    assert Decimal(tr_rec.payload["day_return"]) <= Decimal("-0.03")
    assert tr_rec.observed is not None and tr_rec.observed > 0


def test_halted_latches_until_admin_release(crashed: Crashed) -> None:
    """«Continue» (наступні бари) засувку не знімає; зняти може лише admin; зняття → COOLDOWN + аудит."""
    s, sink = crashed.session, crashed.sink
    assert s.halted_at_ns is not None
    after = [o.step for o in sink.steps if o.step.close_time_ns > s.halted_at_ns]
    assert len(after) >= 10                                            # потік ішов далі
    assert all(x.risk_state is RiskState.HALTED for x in after)        # засувка трималась без людини
    assert all(x.position_qty == 0 and not x.fills and not x.orders for x in after)   # нуль нової експозиції

    async def release(role: str) -> tuple[str, RiskState]:
        await s.apply_release(actor=f"{role}@demo", role=role, audit_id=None)
        return sink.out_of_band[-1]["outcome"], s.loop.fsm.state

    assert asyncio.run(release("analyst")) == ("denied", RiskState.HALTED)
    assert asyncio.run(release("operator")) == ("denied", RiskState.HALTED)
    assert sink.audits == []
    assert asyncio.run(release("admin")) == ("released", RiskState.COOLDOWN)
    assert not s.loop.fsm.killswitch.is_tripped
    tr = sink.risk_events[-1]
    assert tr.rule == "risk_state" and tr.state_from is RiskState.HALTED and tr.state_to is RiskState.COOLDOWN
    assert tr.actor == "admin@demo"
    assert {a.action for a in sink.audits} == {"killswitch.release", "risk.release"}
    kinds = [e.kind for e in sink.journal]
    assert kinds.count("control.killswitch_release") == 3 and verify_chain(sink.journal) is None
