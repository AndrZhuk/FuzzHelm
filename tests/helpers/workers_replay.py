"""Спільний офлайн-прогін торгової сесії для тестів воркерів (без БД, без мережі, без sleep).

Найменування: tests/helpers/workers_replay.py
Автор: Андрій Жук, 2026.
"""

from __future__ import annotations

import asyncio
import functools
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from fuzzhelm.backtest.dataset import Dataset, load_exchange_instrument
from fuzzhelm.config import FIXTURES_DIR
from fuzzhelm.core.enums import RunKind, RunStatus
from fuzzhelm.ingest.replay import iter_items
from fuzzhelm.workers.trading_worker import (
    MemorySink,
    SessionSummary,
    TradingSession,
    WorkerProfile,
    first_kline_open_ns,
    offline_warmup_history,
)

SESSION = FIXTURES_DIR / "ws" / "btcusdt_2026-09-18.jsonl.gz"
FLASH_CRASH = FIXTURES_DIR / "ws" / "scenarios" / "flash_crash.jsonl.gz"


@dataclass
class Replayed:
    session: TradingSession
    sink: MemorySink
    summary: SessionSummary
    warmup: Dataset
    first_open_ns: int


async def replay(path: Path, *, run_id: int = 1, finish: bool = True) -> Replayed:
    """Профіль replay: прогрів з fixtures/rest (строго до першої свічки потоку) → потік → кроки."""
    prof = WorkerProfile.load("replay")
    cfg = prof.backtest_config(check_invariants=True)
    inst = load_exchange_instrument("BTCUSDT")
    n = cfg.resolved_warmup()
    first_open = first_kline_open_ns(path)
    assert prof.history is not None
    warm = await offline_warmup_history(prof.history, inst, first_open, n)
    sink = MemorySink()
    sess = TradingSession(inst, cfg, seed=prof.seed, run_id=UUID(int=run_id), kind=RunKind.REPLAY,
                          feed="REPLAY", warmup_bars=n, sink=sink, streams=prof.streams,
                          heartbeat_timeout_s=prof.heartbeat_timeout_s)
    await sess.begin({"test": path.name})
    sess.warm_up(warm)
    await sess.run(iter_items(path, prof.streams))
    summary = await sess.finish(RunStatus.DONE) if finish else sess.summary(RunStatus.RUNNING)
    return Replayed(sess, sink, summary, warm, first_open)


@functools.cache
def replayed_clean() -> Replayed:
    """Кешований прогін чистої 45-хв сесії (кілька тестів перевіряють різні інваріанти одного прогону)."""
    return asyncio.run(replay(SESSION, run_id=1))
