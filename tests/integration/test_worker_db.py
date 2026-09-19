"""Воркери на справжньому PostgreSQL: реплей сесії з повним паспортом, flash_crash → HALTED → зняття
через API, пакетний запис бектесту (журнал, VaR/CVaR, хеші з БД = паспорт).

Найменування: tests/integration/test_worker_db.py
Автор: Андрій Жук, 2026.

Потребує `docker compose -f docker-compose.test.yml up -d --wait` (порт 5443); без БД — skip (conftest).
Воркер пише роллю fuzzhelm_app (як у робочій збірці), тестові дані і перевірки — власник.
"""

from __future__ import annotations

import asyncio
import contextlib
import math
from collections.abc import AsyncIterator
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

import asyncpg
import httpx
import orjson
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from tests.helpers.api_fakes import FakeBacktests

from fuzzhelm.api.deps import get_services
from fuzzhelm.api.live import LIVE_CHANNEL, asyncpg_dsn
from fuzzhelm.api.main import create_app
from fuzzhelm.api.services import build_db_services
from fuzzhelm.backtest.dataset import load_exchange_instrument, load_fixture_dataset
from fuzzhelm.backtest.engine import BacktestConfig, run_backtest
from fuzzhelm.backtest.manifest import equity_hash
from fuzzhelm.config import Settings
from fuzzhelm.core.enums import EngineKind, Role, RunKind, RunStatus
from fuzzhelm.core.journal import JournalEntry
from fuzzhelm.ingest.recorder import RawFrame
from fuzzhelm.ingest.replay import iter_items
from fuzzhelm.storage.models import APP_ROLE
from fuzzhelm.storage.repositories import (
    AuditRepo,
    CandleRepo,
    DecisionRepo,
    EquityRepo,
    InstrumentRepo,
    JournalRepo,
    OrderRepo,
    PositionRepo,
    RiskEventRepo,
    RunRepo,
    UserRepo,
)
from fuzzhelm.storage.session import make_engine, session_factory, session_scope
from fuzzhelm.workers.ingest_worker import IngestOptions, run_ingest
from fuzzhelm.workers.persist import GIT_DIRTY_METRIC, Passport, create_run, finish_run, persist_backtest
from fuzzhelm.workers.trading_worker import WorkerOptions, run_worker

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]
SECRET = "worker-integration-" + "k" * 32


async def _seed_instrument(factory: async_sessionmaker[AsyncSession]) -> int:
    async with session_scope(factory) as s:
        return await InstrumentRepo(s).upsert(load_exchange_instrument("BTCUSDT"))


class _Events(list[tuple[str, dict[str, Any]]]):
    """Отримані NOTIFY (kind, data) + очікування події за предикатом БЕЗ sleep: пробудження — на кожен NOTIFY
    (або завершення таска воркера, чия помилка тоді піднімається)."""

    def __init__(self) -> None:
        super().__init__()
        self.changed = asyncio.Event()

    def on_notify(self, _c: Any, _pid: int, _ch: str, payload: str) -> None:
        msg = orjson.loads(payload)
        self.append((msg["kind"], msg["data"]))
        self.changed.set()

    async def wait_for(self, pred: Any, *, timeout: float = 30.0,
                       task: asyncio.Task[Any] | None = None) -> dict[str, Any]:
        loop = asyncio.get_running_loop()
        end = loop.time() + timeout
        while True:
            self.changed.clear()
            for k, d in self:
                if pred(k, d):
                    return d
            if task is not None and task.done():
                task.result()
                raise AssertionError("worker finished before the event arrived")
            left = end - loop.time()
            if left <= 0:
                raise AssertionError("event did not arrive")
            waiter = asyncio.ensure_future(self.changed.wait())
            await asyncio.wait({waiter, *(() if task is None else (task,))}, timeout=left,
                               return_when=asyncio.FIRST_COMPLETED)
            waiter.cancel()


@contextlib.asynccontextmanager
async def _listen(db_url: str, channel: str) -> AsyncIterator[_Events]:
    got = _Events()
    conn = await asyncpg.connect(asyncpg_dsn(db_url))
    await conn.add_listener(channel, got.on_notify)
    try:
        yield got
    finally:
        with contextlib.suppress(Exception):
            await conn.close()


async def test_replay_worker_persists_run_with_full_passport(
        db_url: str, factory: async_sessionmaker[AsyncSession]) -> None:
    """Інтеграційний варіант test_replay_session_end_to_end: той самий воркер пише прогін у PostgreSQL."""
    iid = await _seed_instrument(factory)
    settings = Settings(database_url=db_url, _env_file=None)  # type: ignore[call-arg]
    async with _listen(db_url, LIVE_CHANNEL) as events:
        summary = await run_worker(WorkerOptions(profile="replay", speed=math.inf), settings=settings)
        # останній NOTIFY прогону (після COMMIT фінального запису) — усі попередні вже доставлені
        await events.wait_for(lambda k, d: k == "run" and d.get("run_id") == str(summary.run_id)
                              and d.get("status") == "DONE")
    # другий прогін тієї самої сесії: окрема ідентичність сесії (W-02), жодної колізії client_order_id (W-10),
    # прогрів уже з БД (свічки першого прогону)
    again = await run_worker(WorkerOptions(profile="replay", speed=math.inf), settings=settings)
    assert again.status is RunStatus.DONE and again.run_id != summary.run_id
    assert again.equity_hash == summary.equity_hash       # той самий шлях рішень → та сама крива
    rid = summary.run_id
    assert summary.status is RunStatus.DONE and summary.bars == 45 and summary.closed_trades >= 1
    async with session_scope(factory) as s:
        run = await RunRepo(s).get(rid)
        metrics = await RunRepo(s).get_metrics(rid)
        bad = await JournalRepo(s).verify(rid)
        head = await JournalRepo(s).head(rid)
        kinds = await JournalRepo(s).kinds(rid)
        decisions = await DecisionRepo(s).list_for_run(rid, limit=1000)
        orders = await OrderRepo(s).list_for_run(rid)
        positions = await PositionRepo(s).list_for_run(rid)
        curve = await EquityRepo(s).curve(rid)
        ts, eq = await EquityRepo(s).equity_series(rid)
        risk = await RiskEventRepo(s).list_for_run(rid, limit=1000)
        warm_count = (await s.execute(text(
            "SELECT count(*) FROM candle WHERE instrument_id = :i AND src = 2"), {"i": iid})).scalar_one()
        replay_count = (await s.execute(text(
            "SELECT count(*) FROM candle WHERE instrument_id = :i AND src = 3"), {"i": iid})).scalar_one()
    # паспорт: усі поля ідентичності + результати
    assert run is not None and run.status == "DONE" and run.kind == "replay" and run.engine == "mamdani"
    cfg = BacktestConfig.from_profile("replay", u_enter=0.2)
    assert run.config_hash.hex() == cfg.config_hash and run.seed == 20260918 and len(run.dataset_hash) == 32
    assert run.git_sha is not None and len(run.git_sha) == 40 and GIT_DIRTY_METRIC in metrics
    assert run.journal_head_hash is not None and run.journal_head_hash.hex() == summary.journal_head
    assert run.equity_hash is not None and run.equity_hash.hex() == summary.equity_hash
    assert run.instrument_id == iid and run.tf == "1m" and run.finished_at_ns is not None
    # вікно даних прогону: [open першої живої свічки, open бару після останньої) — 45 хвилин сесії
    assert run.ts_from_ns is not None and run.ts_to_ns == run.ts_from_ns + 45 * 60_000_000_000
    # журнал: ланцюг цілий, голова = паспорт; вхідні свічки теж у ланцюгу
    assert bad is None and head.head == run.journal_head_hash and head.next_seq == summary.journal_entries
    assert kinds["candle"] == 45 and kinds["session.start"] == 1 and kinds["session.end"] == 1
    # прогрів записано в БД з офлайн-фікстури REST (src=2), свічки потоку — src=3 (REPLAY)
    assert warm_count == 523 and replay_count == 45
    # рішення кожного бару (record_traces=all), ордери з FK на рішення, позиції з причиною виходу
    assert len(decisions) == 45 and all(d.fired_rules is not None for d in decisions)
    dec_ids = {d.id: d for d in decisions}
    assert orders and all(o.decision_id in dec_ids for o in orders)
    assert all(dec_ids[o.decision_id].fired_rules for o in orders)            # угоди мають формальний вивід
    assert any(o.status == "FILLED" for o in orders)
    assert positions and all(p.exit_reason is not None for p in positions)
    # крива: 45 точок, хеш з БД = паспорт, VaR/CVaR з 21-ї точки
    assert len(curve) == 45 and equity_hash(eq, ts) == summary.equity_hash
    assert all(p.var95 is None for p in curve[:20]) and all(p.var95 is not None for p in curve[20:])
    intents = {o.decision_id for o in orders if o.otype == "MARKET"}          # вхід і вихід — наміри
    assert len([r for r in risk if r.rule != "risk_state"]) == 7 * len(intents)
    # SSE: воркер публікував події транзакційно
    kinds_seen = {k for k, _ in events}
    assert {"run", "candle", "decision", "equity", "health", "fill"} <= kinds_seen
    health = next(d for k, d in reversed(events) if k == "health")
    assert health["header"].startswith("MODE: PAPER · FEED: REPLAY · NO MAINNET KEYS · SEED 20260918 · Q=")


async def test_flash_crash_halts_on_db_and_only_admin_release_via_api_unlatches(
        db_url: str, factory: async_sessionmaker[AsyncSession], tmp_path: Path) -> None:
    """flash_crash → HALTED без людини; «підроблена» команда не-admin (запис в audit_log в обхід API) не діє;
    POST /risk/killswitch/release від admin → воркер знімає засувку → COOLDOWN, аудит, risk_event."""
    await _seed_instrument(factory)
    async with session_scope(factory) as s:
        await UserRepo(s).create("admin", "admin-pass-1", Role.ADMIN)
        analyst = await UserRepo(s).create("analyst", "analyst-pass-1", Role.ANALYST)
    settings = Settings(database_url=db_url, jwt_secret=SECRET, _env_file=None)  # type: ignore[call-arg]
    stop = asyncio.Event()
    async with _listen(db_url, LIVE_CHANNEL) as events:
        task = asyncio.create_task(run_worker(
            WorkerOptions(profile="replay", speed=math.inf, scenario="flash_crash", linger_s=60.0),
            settings=settings, stop=stop))

        def wait_for(pred: Any) -> Any:
            return events.wait_for(pred, task=task)

        halted = await wait_for(lambda k, d: k == "risk" and d.get("state_to") == "HALTED")
        rid = UUID(halted["run_id"])
        await wait_for(lambda k, d: k == "health" and d.get("candles_closed") == 45)   # потік дочитано
        # 1) команда від аналітика (в обхід API — API таку відхилив би 403): воркер перевіряє роль сам
        async with session_scope(factory) as s:
            await AuditRepo(s).append("risk.killswitch.release", f"risk/killswitch/{rid}",
                                      after={"command": "release", "run_id": str(rid)}, user_id=analyst.id)
        denied = await wait_for(lambda k, d: k == "risk" and d.get("outcome") == "denied")
        assert denied["state_after"] == "HALTED"
        # 2) справжній шлях: POST /risk/killswitch/release від admin
        app_engine = make_engine(db_url, null_pool=True, role=APP_ROLE)
        services = build_db_services(settings, engine=app_engine, limits_path=tmp_path / "limits.yaml",
                                     backtests=FakeBacktests(), listen=False)
        (tmp_path / "limits.yaml").write_text((ROOT / "config" / "risk_limits.yaml").read_text())
        app = create_app(build_default=False)
        app.dependency_overrides[get_services] = lambda: services
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
            tok = (await c.post("/auth/login", data={"username": "admin", "password": "admin-pass-1"})).json()
            hdr = {"Authorization": f"Bearer {tok['access_token']}"}
            r = await c.post("/risk/killswitch/release", json={"run_id": str(rid), "reason": "demo review"},
                             headers=hdr)
            assert r.status_code == 202, r.text
            released = await wait_for(lambda k, d: k == "risk" and d.get("outcome") == "released")
            state = (await c.get("/risk/state", params={"run_id": str(rid)}, headers=hdr)).json()
        await services.aclose()
        await app_engine.dispose()
        stop.set()
        summary = await asyncio.wait_for(task, timeout=30)
    assert released["state_before"] == "HALTED" and released["state_after"] == "COOLDOWN"
    assert released["actor"] == "admin" and released["role"] == "admin"
    assert state["state"] == "COOLDOWN"
    assert summary.status is RunStatus.DONE and summary.halted_at_ns is not None
    async with session_scope(factory) as s:
        transitions = await RiskEventRepo(s).transitions(rid)
        audit = await AuditRepo(s).list(limit=50)
        curve = await EquityRepo(s).curve(rid)
        bad = await JournalRepo(s).verify(rid)
        kinds = await JournalRepo(s).kinds(rid)
        replay_candles = (await s.execute(text("SELECT count(*) FROM candle WHERE src = 3"))).scalar_one()
    path = [(t.state_from, t.state_to, t.actor) for t in transitions]
    assert path == [("NORMAL", "HALTED", None), ("HALTED", "COOLDOWN", "admin")]
    assert transitions[0].payload is not None
    assert Decimal(transitions[0].payload["day_return"]) <= Decimal("-0.03")
    actions = [a.action for a in audit]
    assert actions.count("risk.killswitch.release") == 2          # аналітик (підробка) + admin (API)
    assert "risk.release" in actions and "killswitch.release" in actions    # запис воркера: стан до/після
    request = next(a for a in audit if a.action == "risk.killswitch.release" and a.user_id != analyst.id)
    worker_rows = [a for a in audit if a.action in ("risk.release", "killswitch.release")]
    assert request.user_id is not None and request.ts_ns is not None and len(worker_rows) == 2
    for a in worker_rows:
        # аудит воркера: автор — admin, що подав запит; час — настінний момент застосування (не час бару
        # реплею 2026-09-18, яким живе автомат); посилання на запис запиту
        assert a.user_id == request.user_id and a.after_json is not None
        assert a.after_json["request_audit_id"] == request.id
        assert a.ts_ns is not None and a.ts_ns >= request.ts_ns
    assert summary.halted_at_ns is not None
    after_halt = [p for p in curve if p.ts_ns >= summary.halted_at_ns]
    assert after_halt and all(p.risk_state == "HALTED" for p in after_halt)    # засувка трималась без людини
    assert bad is None and kinds["control.killswitch_release"] == 2
    assert replay_candles == 0                  # синтетичні ціни сценарію в candle не пишуться


async def test_persist_backtest_batch_writes_passport_journal_and_var(
        db_url: str, factory: async_sessionmaker[AsyncSession]) -> None:
    iid = await _seed_instrument(factory)
    ds = load_fixture_dataset().slice(0, 1500)
    cfg = BacktestConfig.from_profile("backtest")
    rid = UUID(int=77)
    entries: list[JournalEntry] = []
    res = run_backtest(ds, cfg, seed=5, run_id=rid, journal_sink=entries.append, git=True)
    m = res.manifest
    app_engine = make_engine(db_url, null_pool=True, role=APP_ROLE)
    app = session_factory(app_engine)
    try:
        async with session_scope(app) as s:
            await create_run(s, Passport(run_id=rid, kind=RunKind.BACKTEST, config=cfg.identity_dict(),
                                         config_hash=m.config_hash, dataset_hash=m.dataset_hash, seed=5,
                                         engine=EngineKind.MAMDANI.value, git_sha=m.git_sha,
                                         git_dirty=m.git_dirty,
                                         instrument_id=iid, tf="1m", ts_from_ns=int(ds.t_ns[0]),
                                         ts_to_ns=int(ds.t_ns[-1]) + 60_000_000_000))
        async with session_scope(app) as s:
            counts = await persist_backtest(s, rid, res, instrument_id=iid, symbol="BTC-USDT-PERP",
                                            journal=entries)
            await finish_run(s, rid, RunStatus.DONE, journal_head_hash=m.journal_head_hash,
                             equity_hash=m.equity_hash)
    finally:
        await app_engine.dispose()
    async with session_scope(factory) as s:
        run = await RunRepo(s).get(rid)
        ts, eq = await EquityRepo(s).equity_series(rid)
        curve = await EquityRepo(s).curve(rid)
        bad = await JournalRepo(s).verify(rid)
        head = await JournalRepo(s).head(rid)
        orders = await OrderRepo(s).list_for_run(rid)
        decisions = await DecisionRepo(s).list_for_run(rid, limit=5000)
        n_candles = await CandleRepo(s).count()
    assert run is not None and run.status == "DONE"
    assert counts.equity_points == len(ds) and counts.journal_entries == len(entries) > 0
    assert equity_hash(eq, ts) == m.equity_hash == run.equity_hash.hex()  # type: ignore[union-attr]
    assert bad is None and head.head.hex() == m.journal_head_hash
    assert len(orders) == counts.orders and {o.decision_id for o in orders} <= {d.id for d in decisions}
    filled = [o for o in orders if o.status == "FILLED"]
    assert filled and all(o.filled_qty == o.qty and o.avg_fill_price is not None for o in filled)
    assert all(p.var95 is None for p in curve[:20])
    assert all(p.cvar95 >= p.var95 for p in curve[20:])  # type: ignore[operator]
    assert n_candles == 0


async def test_ingest_worker_writes_candles_gaps_journal_and_health(
        db_url: str, factory: async_sessionmaker[AsyncSession]) -> None:
    """Ingest-воркер (той самий код, що й для живого WS) на записаній 4-хв сесії: свічки src=WS, журнал сесії
    цілий, health публікується; REST-транспорт — «бомба» (у сесії без дір мережа не потрібна)."""
    iid = await _seed_instrument(factory)
    sample = ROOT / "fixtures" / "ws" / "sample_btcusdt_4m.jsonl.gz"
    closed = {int(i.data["k"]["t"]) for i in iter_items(sample, ("kline",))
              if isinstance(i, RawFrame) and i.data["k"]["x"]}

    def no_network(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected REST call {request.url}")

    async def feed() -> AsyncIterator[Any]:
        for it in iter_items(sample):
            yield it

    settings = Settings(database_url=db_url, _env_file=None)  # type: ignore[call-arg]
    async with _listen(db_url, LIVE_CHANNEL) as events:
        out = await run_ingest(IngestOptions(symbols=("BTCUSDT",)), settings=settings, feed=feed(),
                               http_transport=httpx.MockTransport(no_network))
        await events.wait_for(lambda k, d: k == "health" and d.get("candles_closed") == len(closed))
    jid = UUID(out["journal_run_id"])
    async with session_scope(factory) as s:
        n_ws = (await s.execute(text("SELECT count(*) FROM candle WHERE instrument_id = :i AND src = 1"),
                                {"i": iid})).scalar_one()
        bad = await JournalRepo(s).verify(jid)
        head = await JournalRepo(s).head(jid)
        kinds = await JournalRepo(s).kinds(jid)
        gaps = (await s.execute(text("SELECT count(*) FROM ingest_gap"))).scalar_one()
    assert n_ws == out["candles_written"] == len(closed) > 0
    assert bad is None and head.head.hex() == out["journal_head"] and head.next_seq == out["journal_entries"]
    assert kinds["ingest.start"] == 1 and kinds["ingest.end"] == 1 and kinds["candle"] >= len(closed)
    assert {"trade", "book", "mark"} <= set(kinds) and gaps == out["gap_rows"] == 0
    health = [d for k, d in events if k == "health"]
    assert health and health[-1]["source"] == "ingest_worker" and health[-1]["candles_closed"] == len(closed)


async def test_api_backtest_runner_persists_journal_chain_and_var(
        db_url: str, factory: async_sessionmaker[AsyncSession], tmp_path: Path) -> None:
    """POST /backtests (DbBacktestRunner) пише через workers.persist: event_journal прогону з головою =
    run.journal_head_hash (раніше API зберігав лише хеш голови — API-08 п. 2) і VaR/CVaR у equity_point."""
    import gzip  # noqa: PLC0415
    import json  # noqa: PLC0415

    from fuzzhelm.api.backtest_runner import DbBacktestRunner  # noqa: PLC0415
    from fuzzhelm.core.clock import ManualClock  # noqa: PLC0415
    from fuzzhelm.ingest.normalize import normalize_rest_klines  # noqa: PLC0415

    inst = load_exchange_instrument("BTCUSDT")
    raw = json.loads(gzip.decompress((ROOT / "fixtures" / "rest" / "binance_klines.json.gz").read_bytes()))
    rows = raw[:900]
    candles = normalize_rest_klines(rows, inst, ts_ingest_ns=rows[-1][6] * 1_000_000 + 10**12)
    async with session_scope(factory) as s:
        await InstrumentRepo(s).upsert(inst)
        await CandleRepo(s).upsert(candles)
    app_engine = make_engine(db_url, null_pool=True, role=APP_ROLE)
    try:
        runner = DbBacktestRunner(session_factory(app_engine), data_dir=tmp_path, clock=ManualClock(1),
                                  git=False)
        rid = UUID(int=99)
        out = await runner(rid, {"symbol": inst.symbol_canon, "ts_from_ns": candles[0].open_time_ns,
                                 "ts_to_ns": candles[-1].open_time_ns + 60_000_000_000, "seed": 3})
    finally:
        await app_engine.dispose()
    async with session_scope(factory) as s:
        run = await RunRepo(s).get(rid)
        bad = await JournalRepo(s).verify(rid)
        head = await JournalRepo(s).head(rid)
        curve = await EquityRepo(s).curve(rid)
    assert run is not None and run.status == "DONE" and out["journal_entries"] == head.next_seq > 0
    assert bad is None and run.journal_head_hash == head.head
    assert curve[25].var95 is not None and curve[25].cvar95 is not None
