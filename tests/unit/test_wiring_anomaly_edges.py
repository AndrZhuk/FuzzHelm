"""Межові випадки проводки MLP-скорера (рецензія WIRE-01): бари, що повторюють прогрів або старші за нього, не
псують стан екстрактора; скор живої свічки після дірки між прогрівом і потоком не губиться.

Найменування: tests/unit/test_wiring_anomaly_edges.py
Автор: Андрій Жук, 2026.

Офлайн: бари — записані REST klines (fixtures/rest/binance_klines.json.gz), сесія —
fixtures/ws/btcusdt_2026-09-18.jsonl.gz, модель — закомічений артефакт data/anomaly_mlp_BTCUSDT.json.
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path
from typing import Any
from uuid import UUID

import orjson

from fuzzhelm.core.enums import RunKind, RunStatus, Src
from fuzzhelm.features.convert import bar_from_candle
from fuzzhelm.ingest.normalize import normalize_exchange_info, normalize_rest_klines
from fuzzhelm.ingest.recorder import RawFrame
from fuzzhelm.ingest.replay import iter_items
from fuzzhelm.quality.anomaly_mlp import load_model_artifact

ROOT = Path(__file__).resolve().parents[2]
REST = ROOT / "fixtures" / "rest"
ARTIFACT = ROOT / "data" / "anomaly_mlp_BTCUSDT.json"
INSTR = normalize_exchange_info(json.loads((REST / "exchange_info.json").read_text()), ["BTCUSDT"])["BTCUSDT"]
MIN_NS = 60_000_000_000


def _bars() -> list[Any]:
    rows = orjson.loads(gzip.decompress((REST / "binance_klines.json.gz").read_bytes()))
    now_ms = rows[-1][6] + 1_000
    return [bar_from_candle(c) for c in normalize_rest_klines(rows, INSTR, now_ms * 1_000_000,
                                                              server_time_ms=now_ms)]


BARS = _bars()


def test_scorer_skips_bars_that_repeat_or_precede_what_it_has_seen() -> None:
    """Свічка, що перекривається з прогрівом (торговий воркер: open_time < next_open), або старший бар не
    подаються в екстрактор вдруге: інакше ATR/σ/ранг двічі враховують один бар, а скор і прапорець такого
    «повтору» потрапили б у health і в N_invalid години. Послідовність скорів після повтору — як без нього."""
    loaded = load_model_artifact(ARTIFACT)
    ref, dup = loaded.scorer(), loaded.scorer()
    assert ref.warm_up(BARS[:300]) == dup.warm_up(BARS[:300]) == 300
    assert dup.update(BARS[299]) is None                           # той самий бар, що й останній прогріву
    assert dup.update(BARS[150]) is None                           # старший бар
    assert dup.scored == 0 and dup.stale_skipped == 2
    va = [ref.update(b) for b in BARS[300:420]]
    vb = [dup.update(b) for b in BARS[300:420]]
    assert all(v is not None for v in va) and va == vb             # стан екстрактора не зіпсовано
    assert (dup.scored, dup.flagged) == (ref.scored, ref.flagged) == (120, ref.flagged)
    # прогрів із повтором усередині подає лише строго нові бари
    w = loaded.scorer()
    assert w.warm_up([*BARS[:10], BARS[5], *BARS[10:20]]) == 20 and w.warmup_bars_fed == 20
    assert w.stale_skipped == 1 and w.bars_seen == 20


async def test_trading_session_keeps_the_stream_candle_score_after_a_filled_discontinuity() -> None:
    """Між прогрівом і першою свічкою потоку 3 хвилини дірки: торговий цикл спершу проходить добрані (REST)
    бари, потім свічку потоку. Скор свічки потоку (on_anomaly приходить ДО on_candle) мусить дійти до її
    StepOutput, а не загубитись на кроці добраного бару; добрані бари скору не мають (конвеєр їх не бачив)."""
    from fuzzhelm.backtest.dataset import load_exchange_instrument  # noqa: PLC0415
    from fuzzhelm.workers.trading_worker import (  # noqa: PLC0415
        MemorySink,
        TradingSession,
        WorkerProfile,
        first_kline_open_ns,
        fixture_history_filler,
        offline_warmup_history,
    )

    session_path = ROOT / "fixtures" / "ws" / "btcusdt_2026-09-18.jsonl.gz"
    prof = WorkerProfile.load("replay")
    cfg = prof.backtest_config(check_invariants=True)
    inst = load_exchange_instrument("BTCUSDT")
    n = cfg.resolved_warmup()
    first_open = first_kline_open_ns(session_path)
    assert prof.history is not None
    hole = 3
    warm = await offline_warmup_history(prof.history, inst, first_open - hole * MIN_NS, n)
    scorer = load_model_artifact(ARTIFACT).scorer()
    sink = MemorySink()
    sess = TradingSession(inst, cfg, seed=prof.seed, run_id=UUID(int=8), kind=RunKind.REPLAY, feed="REPLAY",
                          warmup_bars=n, sink=sink, streams=prof.streams,
                          heartbeat_timeout_s=prof.heartbeat_timeout_s, anomaly=scorer,
                          gap_filler=fixture_history_filler(prof.history, inst))
    await sess.begin({"test": "wiring-edges"})
    sess.warm_up(warm)
    stop_ns = first_open + 3 * MIN_NS + 30 * 1_000_000_000

    def head() -> Any:
        for it in iter_items(session_path, prof.streams):
            if isinstance(it, RawFrame) and it.ts_ingest_ns > stop_ns:
                return
            yield it

    await sess.run(head())
    summary = await sess.finish(RunStatus.DONE)
    assert summary.status is RunStatus.DONE
    filled = [o for o in sink.steps if o.candle.src is Src.REST]
    stream = [o for o in sink.steps if o.candle.src is not Src.REST]
    assert [o.candle.open_time_ns for o in filled] == [first_open - k * MIN_NS for k in range(hole, 0, -1)]
    assert len(stream) >= 2 and stream[0].candle.open_time_ns == first_open
    assert all(o.anomaly is None for o in filled)
    for o in stream:
        assert o.anomaly is not None and o.anomaly.t_ns == o.candle.open_time_ns
    assert scorer.scored == len(stream)
