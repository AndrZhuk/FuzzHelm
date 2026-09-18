"""Група N брифінгу (§10): шість патологічних WS-сесій відновлюються з нульовою втратою подій.

Найменування: tests/e2e/test_pathological_sessions.py
Призначення: gate фази 2. Кожна сесія fixtures/ws/pathological/<назва>.jsonl.gz відтворюється через
IngestPipeline (віртуальний час кадрів, сторож тиші 10 с); прогалини добираються справжнім
BinanceRestClient поверх respx-імітації Binance, що віддає <назва>.rest.json.
Автор: Андрій Жук, 2026.

«Нуль втрачених подій» (точне означення, еталон — СИРІ кадри файлу, незалежно від конвеєра):
  * кожна закрита 1m-свічка чистого зразка (кадри kline x=true) є на виході рівно один раз,
    з тими самими o/h/l/c/volume/quote_volume/trades_count, і свічки йдуть строго за зростанням open_time;
  * кожен aggTrade id чистого зразка (неперервний діапазон, 0 дірок) є на виході рівно один раз;
  * кожна виявлена прогалина закінчилась статусом FILLED.
Для flash_crash еталон — кадри самої сесії (обвал — властивість даних, а не збій транспорту).
Для сценаріїв без втрат додатково: жодної хибної прогалини.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
import respx

from fuzzhelm.core.enums import GapStatus, Stream
from fuzzhelm.ingest.normalize import normalize_exchange_info
from fuzzhelm.ingest.pipeline import (
    RestBackfiller,
    SessionCapture,
    recovery_stats,
    replay_session,
    session_reference,
)
from fuzzhelm.ingest.ratelimit import TokenBucket
from fuzzhelm.ingest.replay import FixtureRestHandler, FrameClock, RestFixture
from fuzzhelm.ingest.rest_client import BinanceRestClient
from fuzzhelm.ingest.retry import RetryPolicy

ROOT = Path(__file__).resolve().parents[2]
PATHO = ROOT / "fixtures" / "ws" / "pathological"
CLEAN = ROOT / "fixtures" / "ws" / "sample_btcusdt_4m.jsonl.gz"
INSTR = normalize_exchange_info(json.loads((ROOT / "fixtures/rest/exchange_info.json").read_text()),
                                ["BTCUSDT"])["BTCUSDT"]
SCENARIOS = ("gap", "dup", "reorder", "clock_jump", "stall", "flash_crash")
LOSSY = {"gap", "stall"}                   # сценарії, де кадри справді зникли з файлу


async def _no_sleep(_: float) -> None:
    return None


@pytest.mark.parametrize("name", SCENARIOS)
async def test_all_pathological_sessions_recover_with_zero_lost_events(name: str) -> None:
    ws = PATHO / f"{name}.jsonl.gz"
    fx = RestFixture.load(PATHO / f"{name}.rest.json")
    clock = FrameClock()
    handler = FixtureRestHandler(fx, clock)
    cap = SessionCapture()
    async with respx.mock(assert_all_called=False) as router:
        router.route(method="GET", host="fapi.binance.com").mock(side_effect=handler)
        async with httpx.AsyncClient() as http:
            client = BinanceRestClient("https://fapi.binance.com", http,
                                       TokenBucket(1200, 40, clock, sleep=_no_sleep),
                                       RetryPolicy(base=0.5, cap=30, max_attempts=3, rng_seed=7),
                                       clock=clock, sleep=_no_sleep)
            report = await replay_session(ws, INSTR, backfill=RestBackfiller(client, INSTR, sleep=_no_sleep),
                                          sinks=cap.sinks(), clock=clock, heartbeat_timeout_s=10.0)

    ref = session_reference(ws if name == "flash_crash" else CLEAN)
    st = recovery_stats(ref, cap, report)
    # еталон непорожній і сам цілісний: 3 закриті хвилини і неперервний ряд aggTrade id
    assert st.expected_candles == 3
    assert max(ref.agg_ids) - min(ref.agg_ids) + 1 == len(ref.agg_ids) == st.expected_trades == 3887
    # нуль втрачених, нуль дублів, ті самі значення, строгий порядок
    assert (st.lost_candles, st.duplicated_candles, st.mismatched_candles) == (0, 0, 0), st
    assert st.candles_in_order
    assert (st.lost_trades, st.duplicated_trades, st.extra_trades) == (0, 0, 0), st
    assert st.extra_candles == 0
    assert st.zero_loss
    assert report.health.invalid == 0 and report.backfill_errors == ()
    # усі прогалини закриті статусом FILLED; журнал станів кожної — OPEN → FILLING → FILLED
    assert all(g.status is GapStatus.FILLED and g.filled_rows == g.expected_count for g in report.gaps)
    for g in report.gaps:
        assert [x.status for x in cap.gaps if x.gap_id == g.gap_id] == [
            GapStatus.OPEN, GapStatus.FILLING, GapStatus.FILLED]
    if name in LOSSY:
        streams = {g.stream for g in report.gaps}
        assert streams == {Stream.KLINES, Stream.TRADES}, report.gaps
        assert handler.calls, "lossy scenario must hit the REST backfill"
        # добрані дані справді прийшли з REST: є події backfill у лічильниках
        assert report.health.events_by_kind.get("candle_backfill", 0) >= 1
        assert report.health.events_by_kind.get("trade_backfill", 0) >= 1
    else:
        assert report.gaps == () and handler.calls == []        # жодної хибної прогалини
    fault = fx.meta["fault"]
    if name == "stall":
        # тиша market 55 с > 10 с: сторож спрацював саме на цьому з'єднанні
        assert report.health.watchdog_fires == 1
        assert [d.conn for d in report.disconnects] == ["market"]
        fired = report.disconnects[0].ts_ns
        assert fired == pytest.approx(int(fault["start_ns"]) + 10 * 10**9, abs=2 * 10**9)
    elif name == "clock_jump":
        # стрибок локального годинника +40 с — хибна «тиша» на обох з'єднаннях (задокументовано у звіті)
        assert report.health.watchdog_fires == 2
    else:
        assert report.health.watchdog_fires == 0
    if name == "dup":
        assert report.health.duplicates >= int(fault["duplicated_frames"])
    if name == "reorder":
        # перестановки справді дійшли до детектора: дірки в aggTrade id виникали й заростали в межах grace
        assert report.gap_stats.healed_holes > 0
    if name == "flash_crash":
        lows = [c.l for c in cap.candles]
        highs = [c.h for c in cap.candles]
        assert min(lows) <= max(highs) * Decimal("0.60")         # обвал ≥ 40 % видно в закритих свічках
    for h in report.dq:
        assert 0.0 <= h.score.score <= 1.0
