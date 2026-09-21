"""API на справжньому PostgreSQL: вхід, аудит before/after, CRUD стратегій, /explain, LISTEN/NOTIFY, бектест,
планувальник.

Найменування: tests/integration/test_api_db.py
Автор: Андрій Жук, 2026.

Потребує `docker compose -f docker-compose.test.yml up -d --wait` (порт 5443); без БД — skip (conftest).
Застосунок працює роллю fuzzhelm_app (як у робочій збірці build_db_services), тестові дані пише власник.
"""

from __future__ import annotations

import asyncio
import contextlib
import gzip
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

import asyncpg
import httpx
import numpy as np
import orjson
import pytest
import yaml
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from tests.helpers.api_fakes import FakeBacktests
from tests.helpers.api_traces import make_trace
from tests.integration._data import BTC, IDS, T0_NS, add_instrument

from fuzzhelm.api.backtest_runner import DbBacktestRunner, prepare_backtest
from fuzzhelm.api.backtests import EngineBacktestService, load_engine_module
from fuzzhelm.api.deps import get_services
from fuzzhelm.api.live import CONTROL_CHANNEL, LIVE_CHANNEL, asyncpg_dsn, encode_notify_payload
from fuzzhelm.api.main import create_app
from fuzzhelm.api.services import ApiServices, build_db_services
from fuzzhelm.backtest.dataset import Dataset, load_exchange_instrument
from fuzzhelm.backtest.manifest import equity_hash
from fuzzhelm.backtest.metrics import METRIC_NAMES
from fuzzhelm.config import Settings
from fuzzhelm.core.clock import NS_PER_MIN, ManualClock
from fuzzhelm.core.enums import EngineKind, Role, RunKind, VerdictKind
from fuzzhelm.fuzzy.defuzz import centroid
from fuzzhelm.ingest.funding import COLUMNS as FUNDING_COLUMNS
from fuzzhelm.ingest.funding import load_funding_json, rows_digest, write_funding_json
from fuzzhelm.ingest.normalize import normalize_rest_klines
from fuzzhelm.risk.journal import RiskEventRecord
from fuzzhelm.storage.models import APP_ROLE
from fuzzhelm.storage.repositories import (
    AuditRepo,
    CandleRepo,
    DecisionRecord,
    DecisionRepo,
    EquityRepo,
    InstrumentRepo,
    OrderRepo,
    RiskEventRepo,
    RunRepo,
    StrategyRepo,
    UserRepo,
)
from fuzzhelm.storage.repositories.common import SQLSTATE_INSUFFICIENT_PRIVILEGE, sqlstate
from fuzzhelm.storage.session import make_engine, session_factory, session_scope

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "config"
SECRET = "integration-secret-" + "z" * 32
CLIENT_IP = "198.51.100.4"
PASSWORDS = {Role.ADMIN: "admin-pass-1", Role.ANALYST: "analyst-pass-1", Role.OPERATOR: "operator-pass-1"}


@dataclass
class Api:
    client: httpx.AsyncClient
    services: ApiServices
    owner: async_sessionmaker[AsyncSession]
    app_engine: AsyncEngine
    limits_path: Path
    uids: dict[Role, int]

    async def login(self, role: Role) -> dict[str, str]:
        r = await self.client.post("/auth/login", data={"username": role.value, "password": PASSWORDS[role]})
        assert r.status_code == 200, r.text
        return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.fixture
async def api(db_url: str, factory: async_sessionmaker[AsyncSession], tmp_path: Path) -> AsyncIterator[Api]:
    limits_path = tmp_path / "risk_limits.yaml"
    limits_path.write_text((CONFIG / "risk_limits.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    app_engine = make_engine(db_url, null_pool=True, role=APP_ROLE)
    settings = Settings(jwt_secret=SECRET, _env_file=None)  # type: ignore[call-arg]
    services = build_db_services(settings, engine=app_engine, limits_path=limits_path,
                                 backtests=FakeBacktests(),
                                 clock=ManualClock(T0_NS), listen=True)
    uids: dict[Role, int] = {}
    async with session_scope(factory) as s:
        for role, pw in PASSWORDS.items():
            uids[role] = (await UserRepo(s).create(role.value, pw, role)).id
    app = create_app(build_default=False)
    app.dependency_overrides[get_services] = lambda: services
    transport = httpx.ASGITransport(app=app, client=(CLIENT_IP, 40000))
    async with httpx.AsyncClient(transport=transport, base_url="http://api.test") as client:
        yield Api(client, services, factory, app_engine, limits_path, uids)
    await services.aclose()
    await app_engine.dispose()


def strategy_texts() -> tuple[str, str]:
    rules = (CONFIG / "rules_mamdani.yaml").read_text(encoding="utf-8")
    return rules, (CONFIG / "membership.yaml").read_text(encoding="utf-8")


async def test_login_writes_audit_with_inet_ip(api: Api) -> None:
    creds = {"username": "analyst", "password": PASSWORDS[Role.ANALYST]}
    r = await api.client.post("/auth/login", data=creds)
    assert r.status_code == 200 and r.json()["role"] == "analyst"
    bad = await api.client.post("/auth/login", json={"username": "analyst", "password": "nope"})
    assert bad.status_code == 401
    async with session_scope(api.owner) as s:
        rows = await AuditRepo(s).list(limit=10)
    assert [r.action for r in rows] == ["auth.login_failed", "auth.login"]
    assert rows[1].user_id == api.uids[Role.ANALYST] and rows[1].ip == CLIENT_IP


async def test_limit_change_audit_before_after_is_append_only(api: Api) -> None:
    admin = await api.login(Role.ADMIN)
    current = (await api.client.get("/risk/limits", headers=admin)).json()
    body = {**current["config"], "expected_sha256": current["sha256"]}
    body["limits"]["max_gross_leverage"]["value"] = "2.5"
    r = await api.client.put("/risk/limits", json=body, headers=admin)
    assert r.status_code == 200, r.text
    async with session_scope(api.owner) as s:
        rows = await AuditRepo(s).list(action="risk.limits.update")
    assert len(rows) == 1
    row = rows[0]
    assert row.id == r.json()["audit_id"] and row.user_id == api.uids[Role.ADMIN] and row.ip == CLIENT_IP
    assert row.before_json is not None and row.after_json is not None
    # Decimal із файлу дослівно: `3.0` у YAML → "3.0" (масштаб не губиться і не додається)
    assert row.before_json["limits"]["max_gross_leverage"]["value"] == "3.0"
    assert row.after_json["limits"]["max_gross_leverage"]["value"] == "2.5"
    assert row.after_json["_changed"] == ["limits.max_gross_leverage.value"]
    assert "max_gross_leverage:\n    value: 2.5" in api.limits_path.read_text(encoding="utf-8")
    # роль застосунку не може переписати чи стерти слід зміни ліміту (REVOKE, ревізія 0003)
    for stmt in ("UPDATE audit_log SET after_json = '{}'::jsonb", "DELETE FROM audit_log"):
        with pytest.raises(DBAPIError) as exc:
            async with api.app_engine.begin() as conn:
                await conn.execute(text(stmt))
        assert sqlstate(exc.value) == SQLSTATE_INSUFFICIENT_PRIVILEGE
    # аналітик отримує 403, а файл і журнал не змінюються
    analyst = await api.login(Role.ANALYST)
    assert (await api.client.put("/risk/limits", json=body, headers=analyst)).status_code == 403


async def test_strategy_crud_versions_on_real_db(api: Api) -> None:
    op = await api.login(Role.OPERATOR)
    rules, membership = strategy_texts()
    r1 = await api.client.post("/strategies", headers=op, json={
        "name": "demo", "rules_yaml": rules, "membership_yaml": membership, "activate": True})
    assert r1.status_code == 201, r1.text
    tree = yaml.safe_load(rules)
    tree["rules"][3]["if"]["T"] = "SIDEWAYS"
    bad = await api.client.put("/strategies/demo", headers=op, json={
        "rules_yaml": yaml.safe_dump(tree), "membership_yaml": membership})
    assert bad.status_code == 422 and bad.json()["detail"][0]["path"] == "rules[3].if.T"
    m_tree = yaml.safe_load(membership)
    m_tree["variables"]["V"]["terms"]["HI"]["sigma"] = 0.2
    r2 = await api.client.put("/strategies/demo", headers=op, json={
        "rules_yaml": rules, "membership_yaml": yaml.safe_dump(m_tree), "activate": True})
    assert r2.status_code == 200 and r2.json()["version"] == 2
    dup = await api.client.post("/strategies", headers=op, json={
        "name": "copy", "rules_yaml": rules, "membership_yaml": membership})
    assert dup.status_code == 409                                     # UNIQUE (rules_hash) БД
    async with session_scope(api.owner) as s:
        versions = await StrategyRepo(s).list_versions("demo")
        audit = await AuditRepo(s).list(target="strategy/demo")
    assert [(v.version, v.is_active, v.created_by) for v in versions] == [(1, False, "operator"),
                                                                         (2, True, "operator")]
    assert versions[0].rules_yaml == rules                            # тексти збережено дослівно
    assert [a.action for a in audit] == ["strategy.update", "strategy.create"]
    assert audit[0].before_json is not None and audit[0].before_json["active_version"] == 1


async def test_explain_on_real_db_uses_trace_extras_columns(api: Api) -> None:
    rules, membership = strategy_texts()
    trace = make_trace()
    async with session_scope(api.owner) as s:
        iid = await add_instrument(s)
        strategy = await StrategyRepo(s).create_version("demo", rules, membership, activate=True)
        run_id = IDS.next_uuid()
        await RunRepo(s).create(run_id, kind=RunKind.BACKTEST, config={"engine": "mamdani"},
                                config_hash=bytes(32), dataset_hash=bytes(32), seed=1,
                                engine=EngineKind.MAMDANI,
                                git_sha="b" * 40, strategy_id=strategy.id, instrument_id=iid, tf="1m")
        did = await DecisionRepo(s).insert(DecisionRecord.from_trace(
            trace, run_id=run_id, instrument_id=iid, target_side=1, target_qty=Decimal("0.012"),
            stop_price=Decimal("59930.1"), liq_price=Decimal("40180.5"), narrative=None))
    async with session_scope(api.owner) as s:
        row = await DecisionRepo(s).get(did)
    assert row is not None and row.sizing == trace.to_dict()["sizing"] and row.risk == trace.to_dict()["risk"]
    assert row.binding_constraint == row.sizing["binding_constraint"]           # узято з sizing
    analyst = await api.login(Role.ANALYST)
    ex = (await api.client.get(f"/decisions/{did}/explain", headers=analyst)).json()
    assert ex["strategy"]["source"] == "strategy" and ex["strategy"]["id"] == strategy.id
    assert [f["rule_id"] for f in ex["fired_rules"]] == [r.rule_id for r in trace.fired_rules]
    assert ex["consistency"]["ok"] is True                                      # NUMERIC(8,5) у межах допуску
    assert ex["inputs_source"] == "detector_outputs"                            # точні float з JSONB
    grid, mu = np.array(ex["aggregate"]["grid"]), np.array(ex["aggregate"]["mu"])
    assert centroid(grid, mu, "trapezoid") == pytest.approx(ex["u_raw"], abs=1e-12)
    assert ex["narrative_uk"].startswith(f"Спрацювало правило {trace.fired_rules[0].rule_id}")
    assert ex["sizing"]["binding_constraint"] and ex["prices"]["liq"] == "40180.500000000000000000"


async def test_risk_event_exact_factor_survives_numeric_6_4(api: Api) -> None:
    async with session_scope(api.owner) as s:
        iid = await add_instrument(s)
        run_id = IDS.next_uuid()
        await RunRepo(s).create(run_id, kind=RunKind.PAPER, config={}, config_hash=bytes(32),
                                dataset_hash=b"\x02" * 32, seed=2, engine=EngineKind.MAMDANI,
                                git_sha="c" * 40)
        await RiskEventRepo(s).insert_many([
            RiskEventRecord(ts_ns=T0_NS, rule="max_gross_leverage", verdict=VerdictKind.SHRINK,
                            factor=Decimal("0.99996"), observed=Decimal("3.0001"), limit_value=Decimal("3"),
                            instrument=BTC.symbol_canon, payload={"requested": Decimal("0.012")},
                            run_id=run_id),
        ], instrument_ids={BTC.symbol_canon: iid})
    async with session_scope(api.owner) as s:
        (row,) = await RiskEventRepo(s).list_for_run(run_id)
    assert row.factor == Decimal("1.0000")                            # колонка округлила б SHRINK до ALLOW
    assert row.factor_exact == Decimal("0.99996") and row.payload == {"requested": "0.012",
                                                                       "factor_exact": "0.99996"}
    analyst = await api.login(Role.ANALYST)
    events = (await api.client.get("/risk/events", params={"run_id": str(run_id)}, headers=analyst)).json()
    assert events["items"][0]["factor"] == "0.99996" and events["next_cursor"] is None


async def test_risk_events_keyset_pages_on_real_db(api: Api) -> None:
    """Keyset (ts, id) на PostgreSQL: рівні мітки часу (мкс) упорядковує id, кожен рядок досяжний рівно раз,
    межі since_ns/until_ns і курсор у наносекундах, не кратних мікросекунді, округлюються вгору, а не вниз."""
    us = 1_000
    async with session_scope(api.owner) as s:
        iid = await add_instrument(s)
        run_id = IDS.next_uuid()
        await RunRepo(s).create(run_id, kind=RunKind.PAPER, config={}, config_hash=bytes(32),
                                dataset_hash=b"\x03" * 32, seed=3, engine=EngineKind.MAMDANI,
                                git_sha="d" * 40)
        await RiskEventRepo(s).insert_many([
            RiskEventRecord(ts_ns=T0_NS + (i // 4) * us, rule="stale_data" if i % 3 else "max_daily_loss",
                            verdict=VerdictKind.VETO if i % 3 == 0 else VerdictKind.ALLOW,
                            factor=Decimal(0) if i % 3 == 0 else Decimal(1), observed=None, limit_value=None,
                            instrument=BTC.symbol_canon, run_id=run_id)
            for i in range(23)
        ], instrument_ids={BTC.symbol_canon: iid})
    async with session_scope(api.owner) as s:
        all_rows = await RiskEventRepo(s).list_for_run(run_id, limit=1000)
    newest_first = [r.id for r in all_rows]          # list_for_run: ORDER BY ts DESC, id DESC
    by_id = {r.id: r for r in all_rows}
    analyst = await api.login(Role.ANALYST)

    async def walk(params: dict[str, Any]) -> tuple[list[int], int]:
        ids: list[int] = []
        pages = 0
        cursor = None
        while True:
            q = {"run_id": str(run_id), **params, **({"cursor": cursor} if cursor else {})}
            r = await api.client.get("/risk/events", params=q, headers=analyst)
            assert r.status_code == 200, r.text
            pages += 1
            ids += [e["id"] for e in r.json()["items"]]
            cursor = r.json()["next_cursor"]
            if cursor is None:
                return ids, pages

    assert await walk({"limit": 5}) == (newest_first, 5)
    assert await walk({"limit": 4}) == (newest_first, 6)
    # ts_ns < T0 + 2 мкс + 1 нс ⇔ ts ≤ T0 + 2 мкс (12 рядків); округлення вниз дало б лише 8
    ids, _ = await walk({"until_ns": T0_NS + 2 * us + 1, "limit": 5})
    upto_2us = sorted(i for i, r in by_id.items() if (r.ts_ns or 0) <= T0_NS + 2 * us)
    assert sorted(ids) == upto_2us and len(ids) == 12
    ids, _ = await walk({"since_ns": T0_NS + us + 1, "until_ns": T0_NS + 2 * us + 1})
    assert len(ids) == 4 and {by_id[i].ts_ns for i in ids} == {T0_NS + 2 * us}
    ids, _ = await walk({"only": "veto", "limit": 3})
    assert ids == [i for i in newest_first if by_id[i].verdict == "VETO"] and len(ids) == 8
    # курсор не з БД (час між мікросекундами): рівних за часом рядків немає, id не має значення
    q = {"run_id": str(run_id), "cursor": f"{T0_NS + 2 * us + 500}:1", "limit": 100}
    r = await api.client.get("/risk/events", headers=analyst, params=q)
    assert sorted(e["id"] for e in r.json()["items"]) == upto_2us


async def test_sse_streams_only_committed_notifications(api: Api, db_url: str) -> None:
    analyst = await api.login(Role.ANALYST)
    api.services.ensure_live()
    source = api.services.live_source
    assert source is not None
    await asyncio.wait_for(source.wait_connected(), timeout=5)        # подія LISTEN, без опитування
    assert source.connected

    async def read_stream() -> httpx.Response:
        return await api.client.get("/stream/live", params={"max_events": 1}, headers=analyst)

    task = asyncio.create_task(read_stream())
    await asyncio.wait_for(api.services.live.wait_subscribers(1), timeout=5)
    conn = await asyncpg.connect(asyncpg_dsn(db_url))
    try:
        tx = conn.transaction()
        await tx.start()
        await conn.execute("SELECT pg_notify($1, $2)", LIVE_CHANNEL,
                           encode_notify_payload("decision", {"id": -1, "note": "rolled back"}))
        await tx.rollback()                                           # не доставляється
        await conn.execute("SELECT pg_notify($1, $2)", LIVE_CHANNEL,
                           encode_notify_payload("decision", {"id": 42, "u_final": 0.31}))
    finally:
        await conn.close()
    resp = await asyncio.wait_for(task, timeout=10)
    assert resp.status_code == 200
    data = [line[6:] for line in resp.text.splitlines() if line.startswith("data: ")]
    assert [orjson.loads(d) for d in data] == [{"id": 42, "u_final": 0.31}]
    assert "event: decision" in resp.text


async def test_killswitch_release_notifies_control_channel(api: Api, db_url: str) -> None:
    received: list[str] = []
    got = asyncio.Event()
    listener = await asyncpg.connect(asyncpg_dsn(db_url))

    def on_notify(_c: Any, _pid: int, _ch: str, payload: str) -> None:
        received.append(payload)
        got.set()

    await listener.add_listener(CONTROL_CHANNEL, on_notify)
    try:
        admin = await api.login(Role.ADMIN)
        r = await api.client.post("/risk/killswitch/release", json={"reason": "manual restart after review"},
                                  headers=admin)
        assert r.status_code == 202, r.text
        await asyncio.wait_for(got.wait(), timeout=5)
    finally:
        with contextlib.suppress(Exception):
            await listener.close()
    msg = orjson.loads(received[0])
    assert msg["kind"] == "killswitch.release" and msg["data"]["audit_id"] == r.json()["audit_id"]
    assert msg["data"]["actor"] == "admin"


async def test_default_services_do_not_touch_db_until_first_request(db_url: str) -> None:
    eng = make_engine("postgresql+asyncpg://nobody:nopass@127.0.0.1:1/none", null_pool=True, role=APP_ROLE)
    services = build_db_services(Settings(_env_file=None), engine=eng, listen=False)  # type: ignore[call-arg]
    app = create_app(build_default=False)
    app.dependency_overrides[get_services] = lambda: services
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        assert (await c.get("/healthz")).status_code == 200            # БД недосяжна, liveness — так
        assert (await c.get("/docs")).status_code == 200
        r = await c.post("/auth/login", data={"username": "a", "password": "b"})
        # недосяжна БД — 503 без деталей драйвера (OSError чи OperationalError — залежно від asyncpg)
        assert r.status_code == 503, r.text
        assert r.json()["detail"] in (
            "service dependency unavailable", "database unavailable", "database error")
    await eng.dispose()


async def test_backtest_runner_persists_run_and_explain_is_consistent(api: Api, tmp_path: Path) -> None:
    """POST /backtests → справжній рушій над свічками з БД → паспорт, рішення, ордери, капітал, метрики;
    /explain збереженого рішення відтворюється з run.config; повтор ідентичного прогону — FAILED (дубль)."""
    inst = load_exchange_instrument("BTCUSDT")
    raw = json.loads(gzip.decompress((ROOT / "fixtures" / "rest" / "binance_klines.json.gz").read_bytes()))
    rows = raw[:1200]
    candles = normalize_rest_klines(rows, inst, ts_ingest_ns=rows[-1][6] * 1_000_000 + 10**12)
    async with session_scope(api.owner) as s:
        iid = await InstrumentRepo(s).upsert(inst)
        await CandleRepo(s).upsert(candles, iid)
    # застосунок пише результати роллю fuzzhelm_app (як build_db_services) — прав достатньо
    runner = DbBacktestRunner(session_factory(api.app_engine), data_dir=tmp_path, clock=ManualClock(T0_NS),
                              git=False)
    service = EngineBacktestService(runner)
    api.services.backtests = service
    analyst = await api.login(Role.ANALYST)
    body = {"symbol": inst.symbol_canon, "ts_from_ns": candles[0].open_time_ns,
            "ts_to_ns": candles[-1].open_time_ns + NS_PER_MIN, "seed": 7}
    r = await api.client.post("/backtests", json=body, headers=analyst)
    assert r.status_code == 202, r.text
    run_id = UUID(r.json()["run_id"])
    job = await service.wait(run_id)
    assert job is not None and job.status.value == "DONE", job and job.error
    counts = job.result
    assert counts["bars"] == 1200 and counts["decisions"] > 0 and counts["equity_points"] == 1200

    run = (await api.client.get(f"/runs/{run_id}", headers=analyst)).json()
    assert run["status"] == "DONE" and run["kind"] == "backtest" and run["engine"] == "mamdani"
    assert run["equity_hash"] == counts["equity_hash"] and run["dataset_hash"] == counts["dataset_hash"]
    assert run["config"]["trees"]["rules"]["rules"] and run["seed"] == 7
    metrics = (await api.client.get(f"/runs/{run_id}/metrics", headers=analyst)).json()["metrics"]
    assert set(METRIC_NAMES) <= set(metrics)
    eq = (await api.client.get(f"/runs/{run_id}/equity", headers=analyst)).json()
    assert eq["n_total"] == 1200
    async with session_scope(api.owner) as s:
        ts, equity = await EquityRepo(s).equity_series(run_id)
        decisions = await DecisionRepo(s).list_for_run(run_id)
        orders = await OrderRepo(s).list_for_run(run_id)
    assert equity_hash(equity, ts) == counts["equity_hash"]          # крива з БД = паспорт прогону
    assert len(decisions) == counts["decisions"] and len(orders) == counts["orders"]
    assert all(o.decision_id in {d.id for d in decisions} for o in orders)
    assert all(d.fired_rules and d.narrative for d in decisions)

    ex = (await api.client.get(f"/decisions/{decisions[0].id}/explain", headers=analyst)).json()
    assert ex["strategy"]["source"] == "run_config" and ex["consistency"]["ok"] is True, ex["consistency"]
    assert ex["narrative_source"] == "stored" and ex["sizing"]["binding_constraint"]
    audit = [a for a in await _audit(api) if a.action == "backtest.submit"]
    assert audit and audit[0].target == f"run/{run_id}"

    again = await api.client.post("/backtests", json=body, headers=analyst)
    dup = await service.wait(UUID(again.json()["run_id"]))
    assert dup is not None and dup.status.value == "FAILED" and str(run_id) in (dup.error or "")
    await service.aclose()


async def test_api_dataset_hash_equals_engine_canonical_path_for_subwindow(api: Api, tmp_path: Path) -> None:
    """Ті самі свічки й той самий файл фандингу → той самий dataset_hash через API і через шлях рушія
    (`Dataset.from_candle_arrays`, як `backtest.runner.load_db_window`), навіть для під-вікна: ставки з
    [t₀ − 1 доба, t₀) входять у набір за правилом рушія (власне обрізання [ts_from, ts_to) губило їх,
    deviations API-15)."""
    inst = load_exchange_instrument("BTCUSDT")
    raw = json.loads(gzip.decompress((ROOT / "fixtures" / "rest" / "binance_klines.json.gz").read_bytes()))
    rows = raw[:1200]
    candles = normalize_rest_klines(rows, inst, ts_ingest_ns=rows[-1][6] * 1_000_000 + 10**12)
    async with session_scope(api.owner) as s:
        iid = await InstrumentRepo(s).upsert(inst)
        await CandleRepo(s).upsert(candles, iid)
    lo, hi = candles[200].open_time_ns, candles[1000].open_time_ns
    step_ms = 8 * 3600 * 1000  # фіксації фандингу 00/08/16 UTC
    first_ms = (lo // 1_000_000 // step_ms - 4) * step_ms  # 4 фіксації (32 год) до початку вікна
    fund_rows = [[t, "0.00010000", "60000.00000000", "Regular"]
                 for t in range(first_ms, hi // 1_000_000 + 2 * step_ms, step_ms)]
    fpath = tmp_path / "funding_BTCUSDT.json"
    write_funding_json(fpath, {
        "v": 1, "source": "synthetic", "venue": "BINANCE_USDM", "symbol": "BTCUSDT",
        "symbol_canon": inst.symbol_canon, "window": {}, "fetched_at_utc": "2026-09-19T00:00:00Z",
        "requests": 0,
        "count": len(fund_rows), "columns": list(FUNDING_COLUMNS), "rows_digest": rows_digest(fund_rows),
        "rows": fund_rows,
    })
    spec = {"symbol": inst.symbol_canon, "tf": "1m", "ts_from_ns": lo, "ts_to_ns": hi, "engine": "mamdani",
            "seed": 7, "params": {}}
    async with session_scope(session_factory(api.app_engine)) as s:
        prep = await prepare_backtest(s, spec, load_engine_module(), data_dir=tmp_path)
    async with session_scope(api.owner) as s:
        arrays = await CandleRepo(s).load_arrays(iid, "1m", lo, hi, closed_only=True)
    canonical = Dataset.from_candle_arrays(arrays, inst, funding=load_funding_json(fpath, inst).rates)
    assert len(prep.dataset) == 800 and prep.dataset.funding_t_ns is not None
    assert (prep.dataset.funding_t_ns < lo).any()  # ставки до ts_from — частина набору рушія
    assert prep.dataset.dataset_hash == canonical.dataset_hash


async def _audit(api: Api) -> list[Any]:
    async with session_scope(api.owner) as s:
        return await AuditRepo(s).list(limit=50)
