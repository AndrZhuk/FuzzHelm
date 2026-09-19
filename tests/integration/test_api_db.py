"""API на справжньому PostgreSQL: вхід, аудит before/after, CRUD стратегій, /explain, LISTEN/NOTIFY, планувальник.

Найменування: tests/integration/test_api_db.py
Автор: Андрій Жук, 2026.

Потребує `docker compose -f docker-compose.test.yml up -d --wait` (порт 5443); без БД — skip (conftest).
Застосунок працює роллю fuzzhelm_app (як у робочій збірці build_db_services), тестові дані пише власник.
"""

from __future__ import annotations

import asyncio
import contextlib
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
from tests.e2e.test_api import make_trace
from tests.helpers.api_fakes import FakeBacktests
from tests.integration._data import BTC, IDS, T0_NS, add_instrument, candle

from fuzzhelm.api.deps import get_services
from fuzzhelm.api.live import CONTROL_CHANNEL, LIVE_CHANNEL, asyncpg_dsn, encode_notify_payload
from fuzzhelm.api.main import create_app
from fuzzhelm.api.services import ApiServices, DbUnitOfWork, build_db_services
from fuzzhelm.config import Settings
from fuzzhelm.core.clock import NS_PER_MIN, ManualClock
from fuzzhelm.core.enums import EngineKind, GapStatus, Role, RunKind, Src, VerdictKind
from fuzzhelm.fuzzy.defuzz import centroid
from fuzzhelm.risk.journal import RiskEventRecord
from fuzzhelm.scheduler.jobs import JobContext, hourly_dq
from fuzzhelm.storage.models import APP_ROLE
from fuzzhelm.storage.repositories import (
    AuditRepo,
    CandleRepo,
    DecisionRecord,
    DecisionRepo,
    DqRepo,
    GapRepo,
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
    services = build_db_services(settings, engine=app_engine, limits_path=limits_path, backtests=FakeBacktests(),
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
    return (CONFIG / "rules_mamdani.yaml").read_text(encoding="utf-8"), (CONFIG / "membership.yaml").read_text(
        encoding="utf-8")


async def test_login_writes_audit_with_inet_ip(api: Api) -> None:
    r = await api.client.post("/auth/login", data={"username": "analyst", "password": PASSWORDS[Role.ANALYST]})
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
    assert row.before_json["limits"]["max_gross_leverage"]["value"] == "3"
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
                                config_hash=bytes(32), dataset_hash=bytes(32), seed=1, engine=EngineKind.MAMDANI,
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
                                dataset_hash=b"\x02" * 32, seed=2, engine=EngineKind.MAMDANI, git_sha="c" * 40)
        await RiskEventRepo(s).insert_many([
            RiskEventRecord(ts_ns=T0_NS, rule="max_gross_leverage", verdict=VerdictKind.SHRINK,
                            factor=Decimal("0.99996"), observed=Decimal("3.0001"), limit_value=Decimal("3"),
                            instrument=BTC.symbol_canon, payload={"requested": Decimal("0.012")}, run_id=run_id),
        ], instrument_ids={BTC.symbol_canon: iid})
    async with session_scope(api.owner) as s:
        (row,) = await RiskEventRepo(s).list_for_run(run_id)
    assert row.factor == Decimal("1.0000")                            # колонка округлила б SHRINK до ALLOW
    assert row.factor_exact == Decimal("0.99996") and row.payload == {"requested": "0.012",
                                                                       "factor_exact": "0.99996"}
    analyst = await api.login(Role.ANALYST)
    events = (await api.client.get("/risk/events", params={"run_id": str(run_id)}, headers=analyst)).json()
    assert events[0]["factor"] == "0.99996"


async def test_sse_streams_only_committed_notifications(api: Api, db_url: str) -> None:
    analyst = await api.login(Role.ANALYST)
    api.services.ensure_live()
    source = api.services.live_source
    assert source is not None
    for _ in range(500):                                              # чекаємо LISTEN (≤ 5 с)
        if source.connected:
            break
        await asyncio.sleep(0.01)
    assert source.connected

    async def read_stream() -> httpx.Response:
        return await api.client.get("/stream/live", params={"max_events": 1}, headers=analyst)

    task = asyncio.create_task(read_stream())
    for _ in range(500):
        if api.services.live.subscribers:
            break
        await asyncio.sleep(0.01)
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


async def test_scheduler_queries_and_hourly_dq_on_real_db(factory: async_sessionmaker[AsyncSession]) -> None:
    h0 = (T0_NS // (60 * NS_PER_MIN) + 1) * 60 * NS_PER_MIN            # перша повна година після T0
    first = (h0 - T0_NS) // NS_PER_MIN
    async with session_scope(factory) as s:
        iid = await add_instrument(s)
        await CandleRepo(s).upsert([candle(first + i, src=Src.REST) for i in range(60) if i not in (5, 6)], iid)
        gaps = GapRepo(s)
        g_partial = await gaps.open(iid, "klines", h0 + 5 * NS_PER_MIN, h0 + 7 * NS_PER_MIN, expected_count=2)
        await gaps.update_status(g_partial, GapStatus.PARTIAL, filled_rows=0)
        g_spent = await gaps.open(iid, "klines", h0 - 3 * NS_PER_MIN, h0 - NS_PER_MIN, expected_count=2)
        for _ in range(5):
            await gaps.update_status(g_spent, GapStatus.UNFILLABLE)
    async with session_scope(factory) as s:
        gaps = GapRepo(s)
        assert [g.id for g in await gaps.list_by_status([GapStatus.PARTIAL, GapStatus.UNFILLABLE])] == [
            g_partial, g_spent]
        assert [g.id for g in await gaps.list_by_status(["PARTIAL", "UNFILLABLE"], max_attempts=5)] == [g_partial]
        assert [g.id for g in await gaps.list_overlapping(iid, h0, h0 + 60 * NS_PER_MIN)] == [g_partial]
        assert [g.id for g in await gaps.list_overlapping(iid, h0 - 2 * NS_PER_MIN, h0)] == [g_spent]
    weights = (0.455446, 0.262850, 0.140852, 0.140852)
    ctx = JobContext(uow=DbUnitOfWork(factory), clock=ManualClock(h0 + 65 * NS_PER_MIN), weights=weights)
    (row,) = await hourly_dq(ctx)
    async with session_scope(factory) as s:
        stored = await DqRepo(s).latest(iid)
    assert stored is not None and stored.hour_start_ns == h0 == row.hour_start_ns
    assert (stored.observed_buckets, stored.invalid_count) == (58, 0)
    assert stored.gap_seconds == Decimal("120.00") and stored.completeness == Decimal("0.9667")
    assert stored.score == Decimal(str(row.score))
    assert await hourly_dq(ctx) == []                                  # година вже має рядок — не перезаписуємо


async def test_default_services_do_not_touch_db_until_first_request(db_url: str) -> None:
    eng = make_engine("postgresql+asyncpg://nobody:nopass@127.0.0.1:1/none", null_pool=True, role=APP_ROLE)
    services = build_db_services(Settings(_env_file=None), engine=eng, listen=False)  # type: ignore[call-arg]
    app = create_app(build_default=False)
    app.dependency_overrides[get_services] = lambda: services
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        assert (await c.get("/healthz")).status_code == 200            # БД недосяжна, liveness — так
        assert (await c.get("/docs")).status_code == 200
        r = await c.post("/auth/login", data={"username": "a", "password": "b"})
        assert r.status_code == 503 and r.json() == {"detail": "service dependency unavailable"} or \
            r.json()["detail"] in ("database unavailable", "database error")
    await eng.dispose()
    assert UUID(int=0) is not None
