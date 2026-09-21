"""N. API та e2e: FastAPI-застосунок цілком (маршрути, JWT, матриця доступу, аудит, /explain, SSE) офлайн.

Найменування: tests/e2e/test_api.py
Автор: Андрій Жук, 2026.

Сховище — реалізація в пам'яті (tests/helpers/api_fakes.py), підставлена через
app.dependency_overrides[get_services]; маршрути, валідація, нечітке ядро, сайзер і аудит — справжні.
Та сама поведінка на справжньому PostgreSQL — tests/integration/test_api_db.py (маркер integration).
"""

from __future__ import annotations

import base64
import json
import re
from collections.abc import AsyncIterator, Mapping
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import jwt
import numpy as np
import pytest
import yaml
from sqlalchemy.exc import DBAPIError
from tests.helpers.api_fakes import JWT_SECRET, NS_PER_MIN, T0_NS, MemoryDb, memory_services
from tests.helpers.api_traces import make_trace

from fuzzhelm.api import backtests as backtests_mod
from fuzzhelm.api.auth import ACCESS_MATRIX, Permission, issue_token
from fuzzhelm.api.backtests import BacktestQueueFull, EngineBacktestService, JobStatus
from fuzzhelm.api.deps import get_services
from fuzzhelm.api.explain import is_finite_json, parse_detector_outputs
from fuzzhelm.api.limits import (
    FileLimitsStore,
    LimitsConflictError,
    diff_paths,
    leading_comments,
    render_limits_yaml,
    sha256_text,
)
from fuzzhelm.api.live import LiveHub, decode_notify_payload, encode_notify_payload
from fuzzhelm.api.main import PUBLIC_PATHS, create_app, iter_api_routes, route_permission
from fuzzhelm.api.services import ApiServices
from fuzzhelm.core.clock import ManualClock
from fuzzhelm.core.enums import Role, RunKind, RunStatus, VerdictKind
from fuzzhelm.core.errors import ConfigValidationError
from fuzzhelm.decision.trace import DecisionTrace
from fuzzhelm.detectors.base import DetectorGroup
from fuzzhelm.fuzzy.defuzz import centroid
from fuzzhelm.risk.config import load_risk_config
from fuzzhelm.storage.repositories import CandleRow, DecisionRecord, EquityRow, InstrumentRow, RunRow

CONFIG = Path(__file__).resolve().parents[2] / "config"
PASSWORDS = {
    Role.OPERATOR: "op-pass-1",
    Role.ANALYST: "an-pass-1",
    Role.AUDITOR: "au-pass-1",
    Role.ADMIN: "ad-pass-1",
}
CYRILLIC = re.compile(r"[А-ЩЬЮЯҐЄІЇа-щьюяґєії]")


# ------------------------------------------------------------------ фікстури


class Env:
    def __init__(
        self,
        services: ApiServices,
        db: MemoryDb,
        client: httpx.AsyncClient,
        clock: ManualClock,
        limits_path: Path,
    ) -> None:
        self.services, self.db, self.client, self.clock, self.limits_path = (
            services,
            db,
            client,
            clock,
            limits_path,
        )
        self.tokens: dict[Role, str] = {}
        self.uids: dict[Role, int] = {}

    def h(self, role: Role) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.tokens[role]}"}


@pytest.fixture
async def env(tmp_path: Path) -> AsyncIterator[Env]:
    clock = ManualClock(T0_NS)
    services, db = memory_services(tmp_path, config_dir=CONFIG, clock=clock)
    app = create_app(build_default=False)
    app.dependency_overrides[get_services] = lambda: services
    repos = db.repos()
    transport = httpx.ASGITransport(app=app, client=("203.0.113.7", 50000))
    async with httpx.AsyncClient(transport=transport, base_url="http://api.test") as client:
        e = Env(services, db, client, clock, tmp_path / "risk_limits.yaml")
        for role, pw in PASSWORDS.items():
            user = await repos.users.create(role.value, pw, role)
            e.uids[role] = user.id
            e.tokens[role] = issue_token(
                uid=user.id, login=role.value, role=role, secret=JWT_SECRET, ttl_hours=8, clock=clock
            ).token
        yield e


def add_run(
    db: MemoryDb,
    *,
    strategy_id: int | None = None,
    kind: RunKind = RunKind.BACKTEST,
    status: RunStatus = RunStatus.DONE,
    started: int = T0_NS,
    rid: int = 1,
) -> UUID:
    run_id = UUID(int=rid)
    db.state.runs[run_id] = RunRow(
        id=run_id,
        kind=kind.value,
        strategy_id=strategy_id,
        instrument_id=1,
        tf="1m",
        ts_from_ns=T0_NS,
        ts_to_ns=T0_NS + 60 * NS_PER_MIN,
        config={"engine": "mamdani"},
        config_hash=bytes(32),
        dataset_hash=bytes(range(32)),
        git_sha="a" * 40,
        seed=20260918,
        engine="mamdani",
        journal_head_hash=None,
        equity_hash=b"\x01" * 32,
        status=status.value,
        error=None,
        started_at_ns=started,
        finished_at_ns=None,
    )
    return run_id


async def add_decision(
    db: MemoryDb, run_id: UUID, *, narrative: str | None = None
) -> tuple[int, DecisionTrace]:
    trace = make_trace()
    sizing = trace.sizing or {}
    rec = DecisionRecord.from_trace(
        trace,
        run_id=run_id,
        instrument_id=1,
        target_side=int(sizing["side"]),
        target_qty=Decimal(sizing["qty"]),
        stop_price=Decimal("59930.1"),
        tp_price=Decimal("60140.1"),
        liq_price=Decimal("40180.5"),
        narrative=narrative,
    )
    return await db.repos().decisions.insert(rec), trace


def add_instrument(db: MemoryDb) -> None:
    db.state.instruments[1] = InstrumentRow(
        id=1,
        venue="BINANCE_USDM",
        symbol_venue="BTCUSDT",
        symbol_canon="BTC-USDT-PERP",
        base_asset="BTC",
        quote_asset="USDT",
        contract_type="PERP",
        tick_size=Decimal("0.1"),
        step_size=Decimal("0.001"),
        min_notional=Decimal("5"),
        mmr=Decimal("0.005"),
        maint_amount=Decimal(0),
        max_leverage=3,
        active=True,
        spec_fetched_at_ns=None,
    )


def strategy_texts() -> tuple[str, str]:
    return (CONFIG / "rules_mamdani.yaml").read_text(encoding="utf-8"), (
        CONFIG / "membership.yaml"
    ).read_text(encoding="utf-8")


# ------------------------------------------------------------------ брифінг §10 N (дослівні назви)


async def test_login_returns_jwt_and_role(env: Env) -> None:
    r = await env.client.post(
        "/auth/login", data={"username": "analyst", "password": PASSWORDS[Role.ANALYST]}
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["token_type"] == "bearer" and body["role"] == "analyst" and body["expires_in"] == 8 * 3600
    # підпис HS256 перевіряється секретом сервера; claims — sub, uid, role, exp = iat + 8 год
    claims = jwt.decode(
        body["access_token"],
        JWT_SECRET,
        algorithms=["HS256"],
        issuer="fuzzhelm",
        options={"verify_exp": False, "verify_iat": False, "verify_nbf": False},
    )
    assert (
        claims["sub"] == "analyst" and claims["role"] == "analyst" and claims["uid"] == env.uids[Role.ANALYST]
    )
    assert claims["exp"] - claims["iat"] == 8 * 3600 and claims["iat"] == T0_NS // 1_000_000_000
    assert jwt.get_unverified_header(body["access_token"])["alg"] == "HS256"
    # токен справді відкриває захищений маршрут і несе ту саму роль
    me = await env.client.get("/auth/me", headers={"Authorization": f"Bearer {body['access_token']}"})
    assert me.status_code == 200 and me.json()["role"] == "analyst"
    assert "risk:limits:write" not in me.json()["permissions"]
    # JSON-форма входу теж працює; хибний пароль і невідомий логін дають однаковий 401
    rj = await env.client.post("/auth/login", json={"username": "admin", "password": PASSWORDS[Role.ADMIN]})
    assert rj.status_code == 200 and rj.json()["role"] == "admin"
    bad = await env.client.post("/auth/login", data={"username": "analyst", "password": "wrong"})
    ghost = await env.client.post("/auth/login", data={"username": "ghost", "password": "wrong"})
    assert bad.status_code == ghost.status_code == 401 and bad.json() == ghost.json()
    actions = [a.action for a in env.db.state.audit]
    assert actions.count("auth.login") == 2 and actions.count("auth.login_failed") == 2


async def test_put_risk_limits_requires_admin_role_403_for_analyst(env: Env) -> None:
    current = (await env.client.get("/risk/limits", headers=env.h(Role.ANALYST))).json()
    body = {**current["config"], "expected_sha256": current["sha256"]}
    body["limits"]["max_daily_loss"]["value"] = "0.01"
    before_text = env.limits_path.read_text(encoding="utf-8")
    for role in (Role.ANALYST, Role.OPERATOR, Role.AUDITOR):
        r = await env.client.put("/risk/limits", json=body, headers=env.h(role))
        assert r.status_code == 403, (role, r.text)
    assert env.limits_path.read_text(encoding="utf-8") == before_text  # файл не змінено
    assert not [a for a in env.db.state.audit if a.action == "risk.limits.update"]
    # без токена — 401, а не 403; admin — дозволено
    assert (await env.client.put("/risk/limits", json=body)).status_code == 401
    ok = await env.client.put("/risk/limits", json=body, headers=env.h(Role.ADMIN))
    assert ok.status_code == 200, ok.text


async def test_limit_change_written_to_audit_log_with_before_after(env: Env) -> None:
    current = (await env.client.get("/risk/limits", headers=env.h(Role.ADMIN))).json()
    body = {**current["config"], "expected_sha256": current["sha256"]}
    body["limits"]["max_daily_loss"]["value"] = "0.015"
    body["state_machine"]["halt_enter"] = 0.11
    r = await env.client.put("/risk/limits", json=body, headers=env.h(Role.ADMIN))
    assert r.status_code == 200, r.text
    assert r.json()["changed"] == ["limits.max_daily_loss.value", "state_machine.halt_enter"]

    rows = [a for a in env.db.state.audit if a.action == "risk.limits.update"]
    assert len(rows) == 1
    row = rows[0]
    assert row.id == r.json()["audit_id"] and row.user_id == env.uids[Role.ADMIN] and row.ip == "203.0.113.7"
    assert row.target == "config/risk_limits.yaml"
    assert row.before_json is not None and row.after_json is not None
    assert row.before_json["limits"]["max_daily_loss"]["value"] == "0.02"
    assert row.after_json["limits"]["max_daily_loss"]["value"] == "0.015"
    assert row.before_json["state_machine"]["halt_enter"] == "0.12"
    assert row.after_json["state_machine"]["halt_enter"] == "0.11"
    assert row.after_json["_sha256_before"] == current["sha256"]
    # у before/after — повні конфігурації: усе, крім двох змінених значень, однакове
    after_plain = {k: v for k, v in row.after_json.items() if not k.startswith("_")}
    assert set(after_plain) == set(row.before_json)
    # файл реально змінено, і те, що в ньому, — це і є «after» (перечитано тим самим валідатором)
    cfg = load_risk_config(env.limits_path.read_text(encoding="utf-8"))
    assert cfg.limits.max_daily_loss.value == Decimal("0.015") and cfg.state_machine.halt_enter == Decimal(
        "0.11"
    )
    assert env.limits_path.read_text(encoding="utf-8").startswith("# Ліміти ризик-контуру")
    # воркер отримав команду перечитати ліміти каналом керування
    assert ("fuzzhelm_control", "risk.limits.changed") in [(c, k) for c, k, _ in env.db.state.control]
    # аудитор бачить запис через API
    audit = await env.client.get(
        "/audit", params={"action": "risk.limits.update"}, headers=env.h(Role.AUDITOR)
    )
    assert (
        audit.status_code == 200
        and audit.json()[0]["before_json"]["limits"]["max_daily_loss"]["value"] == "0.02"
    )


async def test_get_explain_returns_fired_rules_and_memberships(env: Env) -> None:
    run_id = add_run(env.db)
    did, trace = await add_decision(env.db, run_id)
    r = await env.client.get(f"/decisions/{did}/explain", headers=env.h(Role.ANALYST))
    assert r.status_code == 200, r.text
    ex = r.json()
    assert is_finite_json(ex)
    mus = ex["memberships"]
    assert set(mus) == {"T", "R", "V"}
    assert set(mus["T"]) == {"STRONG_DOWN", "WEAK_DOWN", "NEUTRAL", "WEAK_UP", "STRONG_UP"}
    assert set(mus["R"]) == {"SELL_PRESSURE", "NO_PRESSURE", "BUY_PRESSURE"} and set(mus["V"]) == {
        "LO",
        "MID",
        "HI",
    }
    assert all(0.0 <= m <= 1.0 for var in mus.values() for m in var.values())

    fired = ex["fired_rules"]
    assert len(fired) == len(trace.fired_rules) >= 2 and ex["n_fired"] == len(fired)
    alphas = [f["alpha"] for f in fired]
    assert alphas == sorted(alphas, reverse=True)  # за спаданням α
    assert [f["rule_id"] for f in fired] == [fr.rule_id for fr in trace.fired_rules]
    for f in fired:
        # α_r = min(μ_T, μ_R, μ_V) — перевіряємо з тих самих належностей, що повернув API
        a = f["antecedent"]
        assert f["alpha"] == pytest.approx(
            min(mus["T"][a["T"]], mus["R"][a["R"]], mus["V"][a["V"]]), abs=1e-12
        )
        assert f["text_uk"].startswith(f"{f['rule_id']}: ЯКЩО тренд ") and " ТО сигнал " in f["text_uk"]
        assert CYRILLIC.search(f["consequent_uk"])
    # μ_agg: 201 вузол на [−1; 1], обрізання ≤ max α, центроїд = u_raw (перераховано незалежно)
    agg = ex["aggregate"]
    grid, mu = np.array(agg["grid"]), np.array(agg["mu"])
    assert agg["nodes"] == 201 == len(grid) == len(mu) and grid[0] == -1.0 and grid[-1] == 1.0
    assert mu.max() <= max(alphas) + 1e-12 and mu.min() >= 0.0
    assert centroid(grid, mu, "trapezoid") == pytest.approx(agg["centroid"], abs=1e-12) == ex["u_raw"]
    assert ex["u_raw"] == pytest.approx(trace.u_raw, abs=1e-12)
    assert ex["consistency"]["ok"] is True and ex["consistency"]["fired_rules_match"] is True
    assert ex["inputs_source"] == "detector_outputs"
    assert ex["inputs"] == pytest.approx({"T": trace.T, "R": trace.R, "V": trace.V}, abs=1e-15)
    # графіки МФ: значення входу й μ активних термів збігаються з таблицею належностей
    t_var = ex["variables"]["T"]
    assert t_var["value"] == ex["inputs"]["T"] and len(t_var["x"]) == 101
    assert {t["name"] for t in t_var["terms"] if t["active"]} == {k for k, v in mus["T"].items() if v > 0}
    # розкладка сайзера і ціни
    assert ex["sizing"]["binding_constraint"] in {"ATR_RISK", "VOL_TARGET", "LEVERAGE"}
    assert ex["target"]["binding_constraint"] == ex["sizing"]["binding_constraint"]
    assert ex["prices"] == {"stop": "59930.1", "tp": "60140.1", "liq": "40180.5"}
    assert ex["strategy"]["source"] == "default_config"


async def test_explain_narrative_is_ukrainian_and_non_empty(env: Env) -> None:
    run_id = add_run(env.db)
    did, trace = await add_decision(env.db, run_id)
    ex = (await env.client.get(f"/decisions/{did}/explain", headers=env.h(Role.AUDITOR))).json()
    text = ex["narrative_uk"]
    assert ex["narrative_source"] == "recomputed" and len(text) > 100
    top = trace.fired_rules[0]
    assert text.startswith(f"Спрацювало правило {top.rule_id} з α = {top.alpha:.2f}: ЯКЩО тренд ")
    letters = [ch for ch in text if ch.isalpha()]
    cyr = sum(1 for ch in letters if CYRILLIC.match(ch))
    assert cyr / len(letters) > 0.6  # переважно українська
    assert "Сайзер: обмежувальний чинник" in text and "Ризик-контур:" in text
    assert f"u_final = {trace.u_final:.2f}" in text
    # збережений під час прогону текст має пріоритет над перерахованим
    did2, _ = await add_decision(env.db, add_run(env.db, rid=2), narrative="Збережене трасування рішення.")
    ex2 = (await env.client.get(f"/decisions/{did2}/explain", headers=env.h(Role.AUDITOR))).json()
    assert ex2["narrative_uk"] == "Збережене трасування рішення." and ex2["narrative_source"] == "stored"


async def test_strategy_post_invalid_yaml_returns_422_with_field_path(env: Env) -> None:
    rules, membership = strategy_texts()
    tree = yaml.safe_load(rules)
    tree["rules"][3]["if"]["T"] = "SIDEWAYS"  # неіснуючий терм
    bad_rules = yaml.safe_dump(tree, allow_unicode=True, sort_keys=False)
    r = await env.client.post(
        "/strategies",
        headers=env.h(Role.OPERATOR),
        json={"name": "demo", "rules_yaml": bad_rules, "membership_yaml": membership},
    )
    assert r.status_code == 422, r.text
    item = r.json()["detail"][0]
    assert item["path"] == "rules[3].if.T" and item["loc"] == ["body", "rules_yaml"]
    assert "SIDEWAYS" in item["msg"] and item["type"] == "config_validation"
    # інші типові помилки теж мають точний шлях
    tree = yaml.safe_load(rules)
    tree["rules"][7]["w"] = 0.8
    r_w = await env.client.post(
        "/strategies",
        headers=env.h(Role.OPERATOR),
        json={"name": "demo", "rules_yaml": yaml.safe_dump(tree), "membership_yaml": membership},
    )
    assert r_w.status_code == 422 and r_w.json()["detail"][0]["path"] == "rules[7].w"
    m_tree = yaml.safe_load(membership)
    m_tree["variables"]["T"]["terms"]["WEAK_UP"]["points"] = [0.7, 0.35, 0.0]
    r_m = await env.client.post(
        "/strategies",
        headers=env.h(Role.OPERATOR),
        json={"name": "demo", "rules_yaml": rules, "membership_yaml": yaml.safe_dump(m_tree)},
    )
    assert r_m.status_code == 422
    assert r_m.json()["detail"][0]["path"] == "variables.T.terms.WEAK_UP.points"
    assert r_m.json()["detail"][0]["loc"] == ["body", "membership_yaml"]
    syntax = await env.client.post(
        "/strategies",
        headers=env.h(Role.OPERATOR),
        json={"name": "demo", "rules_yaml": "rules: [\n  {id: R01", "membership_yaml": membership},
    )
    assert syntax.status_code == 422 and syntax.json()["detail"][0]["type"] == "yaml_syntax"
    assert syntax.json()["detail"][0]["line"] >= 1
    # нічого з невалідного не збережено і не записано в аудит
    assert env.db.state.strategies == [] and not [
        a for a in env.db.state.audit if a.action.startswith("strategy")
    ]


# ------------------------------------------------------------------ матриця доступу (endpoint × роль)

SAMPLE_PATH_PARAMS = {"name": "demo", "version": "1", "run_id": str(UUID(int=1)), "decision_id": "1"}


def _route_cases() -> list[tuple[str, str, Permission]]:
    cases = []
    for route in iter_api_routes():
        perm = route_permission(route)
        if perm is None:
            continue
        for method in sorted(route.methods):
            cases.append((method, route.path, perm))
    return cases


ROUTE_CASES = _route_cases()


def test_every_route_is_protected_or_explicitly_public() -> None:
    routes = list(iter_api_routes())
    assert len(routes) == len(ROUTE_CASES) + 1  # +1: POST /auth/login
    for route in routes:
        if route_permission(route) is None:
            assert route.path in PUBLIC_PATHS, f"{route.path} has no permission dependency"
    # матриця покриває всі §8.1 маршрути брифінгу
    paths = {(m, p) for m, p, _ in ROUTE_CASES}
    for expected in [
        ("GET", "/market/candles"),
        ("GET", "/market/health"),
        ("GET", "/dq/score"),
        ("GET", "/strategies"),
        ("POST", "/strategies"),
        ("PUT", "/strategies/{name}"),
        ("POST", "/backtests"),
        ("GET", "/runs/{run_id}"),
        ("GET", "/runs/{run_id}/metrics"),
        ("GET", "/runs/{run_id}/equity"),
        ("GET", "/decisions/{decision_id}/explain"),
        ("GET", "/risk/state"),
        ("GET", "/risk/events"),
        ("PUT", "/risk/limits"),
        ("POST", "/risk/killswitch/release"),
        ("GET", "/stream/live"),
    ]:
        assert expected in paths, expected


@pytest.mark.parametrize("role", list(Role), ids=lambda r: r.value)
@pytest.mark.parametrize(("method", "path", "perm"), ROUTE_CASES, ids=[f"{m} {p}" for m, p, _ in ROUTE_CASES])
async def test_access_matrix_enforced_for_every_route(
    env: Env, method: str, path: str, perm: Permission, role: Role
) -> None:
    env.services.live.close()  # SSE-потік завершиться одразу
    url = path.format(**SAMPLE_PATH_PARAMS)
    kwargs: dict[str, Any] = {"headers": env.h(role)}
    if method in ("POST", "PUT"):
        kwargs["json"] = {}  # валідний JSON, невалідна схема
    r = await env.client.request(method, url, **kwargs)
    allowed = role in ACCESS_MATRIX[perm]
    assert r.status_code != 401, r.text
    assert (r.status_code == 403) is (not allowed), (method, path, role, r.status_code, r.text)
    # жоден запит матриці не змінив стан (тіла свідомо невалідні)
    assert not [a for a in env.db.state.audit if not a.action.startswith("auth.")]


def test_matrix_encodes_brief_rules() -> None:
    assert ACCESS_MATRIX[Permission.RISK_LIMITS_WRITE] == {Role.ADMIN}
    assert ACCESS_MATRIX[Permission.KILLSWITCH_RELEASE] == {Role.ADMIN}
    assert ACCESS_MATRIX[Permission.STRATEGY_WRITE] == {Role.OPERATOR, Role.ADMIN}
    assert (
        Role.AUDITOR in ACCESS_MATRIX[Permission.AUDIT_READ]
        and Role.ANALYST not in ACCESS_MATRIX[Permission.AUDIT_READ]
    )
    for perm in (
        Permission.MARKET_READ,
        Permission.RUN_READ,
        Permission.DECISION_READ,
        Permission.RISK_READ,
        Permission.STRATEGY_READ,
        Permission.STREAM_READ,
    ):
        assert ACCESS_MATRIX[perm] == set(Role)  # «analyst+» читає все


# ------------------------------------------------------------------ токени


async def test_expired_tampered_and_alg_none_tokens_are_rejected(env: Env) -> None:
    good = env.tokens[Role.ANALYST]
    header, payload, sig = good.split(".")
    claims = jwt.decode(good, options={"verify_signature": False})
    # підвищення ролі в payload: підпис не сходиться
    forged_payload = base64.urlsafe_b64encode(json.dumps({**claims, "role": "admin"}).encode()).rstrip(b"=")
    forged = f"{header}.{forged_payload.decode()}.{sig}"
    # той самий payload, підписаний чужим ключем
    wrong_key = jwt.encode({**claims, "role": "admin"}, "attacker-key-" + "k" * 32, algorithm="HS256")
    # alg=none без підпису (класична атака на бібліотеки JWT)
    none_header = base64.urlsafe_b64encode(b'{"alg":"none","typ":"JWT"}').rstrip(b"=").decode()
    none_alg = f"{none_header}.{forged_payload.decode()}."
    for token in (forged, wrong_key, none_alg, f"{header}.{payload}.{sig[:-2]}AA", "garbage"):
        r = await env.client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 401, token
    assert (await env.client.get("/auth/me", headers=env.h(Role.ANALYST))).status_code == 200
    env.clock.advance(8 * 3600 * 1_000_000_000)  # рівно TTL → прострочено
    expired = await env.client.get("/auth/me", headers=env.h(Role.ANALYST))
    assert expired.status_code == 401 and expired.headers["www-authenticate"] == "Bearer"


async def test_demoted_user_loses_write_access_before_token_expiry(env: Env) -> None:
    rules, membership = strategy_texts()
    body = {"name": "demo", "rules_yaml": rules, "membership_yaml": membership}
    await env.db.repos().users.set_role(env.uids[Role.OPERATOR], Role.ANALYST)
    r = await env.client.post("/strategies", json=body, headers=env.h(Role.OPERATOR))
    assert r.status_code == 403 and "revoked" in r.json()["detail"]
    # читання за старим токеном ще працює (stateless JWT), зміна — ні
    assert (await env.client.get("/strategies", headers=env.h(Role.OPERATOR))).status_code == 200


async def test_login_rate_limited_after_repeated_failures(env: Env) -> None:
    for _ in range(10):
        r = await env.client.post("/auth/login", data={"username": "admin", "password": "nope"})
        assert r.status_code == 401
    blocked = await env.client.post(
        "/auth/login", data={"username": "admin", "password": PASSWORDS[Role.ADMIN]}
    )
    assert blocked.status_code == 429 and int(blocked.headers["retry-after"]) > 0
    env.clock.advance(301 * 1_000_000_000)  # вікно минуло — вхід знову можливий
    ok = await env.client.post("/auth/login", data={"username": "admin", "password": PASSWORDS[Role.ADMIN]})
    assert ok.status_code == 200


# ------------------------------------------------------------------ стратегії: версіонування


async def test_strategy_crud_versions_and_audit(env: Env) -> None:
    rules, membership = strategy_texts()
    r1 = await env.client.post(
        "/strategies",
        headers=env.h(Role.OPERATOR),
        json={"name": "demo", "rules_yaml": rules, "membership_yaml": membership, "activate": True},
    )
    assert r1.status_code == 201, r1.text
    assert r1.json()["version"] == 1 and r1.json()["is_active"] is True and r1.json()["n_rules"] == 45
    # той самий набір текстів — 409; ім'я вже є — POST 409, треба PUT
    assert (
        await env.client.post(
            "/strategies",
            headers=env.h(Role.OPERATOR),
            json={"name": "other", "rules_yaml": rules, "membership_yaml": membership},
        )
    ).status_code == 409
    assert (
        await env.client.post(
            "/strategies",
            headers=env.h(Role.OPERATOR),
            json={"name": "demo", "rules_yaml": rules + "\n# v2", "membership_yaml": membership},
        )
    ).status_code == 409
    m_tree = yaml.safe_load(membership)
    m_tree["variables"]["V"]["terms"]["MID"]["m"] = 0.5
    r2 = await env.client.put(
        "/strategies/demo",
        headers=env.h(Role.ADMIN),
        json={"rules_yaml": rules, "membership_yaml": yaml.safe_dump(m_tree)},
    )
    assert r2.status_code == 200 and r2.json()["version"] == 2 and r2.json()["is_active"] is False
    assert (
        await env.client.put(
            "/strategies/nope",
            headers=env.h(Role.ADMIN),
            json={"rules_yaml": rules, "membership_yaml": membership},
        )
    ).status_code == 404
    act = await env.client.post(
        "/strategies/demo/activate", params={"version": 2}, headers=env.h(Role.OPERATOR)
    )
    assert act.status_code == 200 and act.json()["is_active"] is True
    listing = (await env.client.get("/strategies", headers=env.h(Role.ANALYST))).json()
    assert listing == [{"name": "demo", "versions": 2, "latest_version": 2, "active_version": 2}]
    v1 = (await env.client.get("/strategies/demo/versions/1", headers=env.h(Role.ANALYST))).json()
    assert v1["rules_yaml"] == rules and v1["is_active"] is False  # стара версія незмінна
    audit = [a for a in env.db.state.audit if a.action.startswith("strategy.")]
    assert [a.action for a in audit] == ["strategy.create", "strategy.update", "strategy.activate"]
    assert audit[1].before_json is not None and audit[1].before_json["latest"]["version"] == 1
    assert audit[1].after_json is not None and audit[1].after_json["version"] == 2
    assert audit[2].before_json == {"active_version": 1} and audit[2].after_json["active_version"] == 2


async def test_strategy_yaml_aliases_and_path_like_text_rejected(env: Env) -> None:
    _, membership = strategy_texts()
    bomb = "a: &a [x, x]\nb: &b [*a, *a]\nrules: *b\n"
    r = await env.client.post(
        "/strategies",
        headers=env.h(Role.OPERATOR),
        json={"name": "demo", "rules_yaml": bomb, "membership_yaml": membership},
    )
    assert r.status_code == 422 and r.json()["detail"][0]["msg"] == "YAML aliases are not allowed"
    # однорядковий «шлях до файлу» — не читається як файл сервера, а розбирається як YAML-скаляр
    r2 = await env.client.post(
        "/strategies",
        headers=env.h(Role.OPERATOR),
        json={"name": "demo", "rules_yaml": "config/rules_mamdani.yaml", "membership_yaml": membership},
    )
    assert r2.status_code == 422 and r2.json()["detail"][0]["msg"] == "top-level YAML must be a mapping"


class DriverError(Exception):
    """Виняток драйвера з SQLSTATE (як asyncpg): текст — «деталь драйвера», що не має потрапити клієнту."""

    def __init__(self, code: str) -> None:
        super().__init__("driver detail that must not leak")
        self.sqlstate = code


async def test_strategy_compute_budget_is_bounded(env: Env) -> None:
    """DoS через дані: grid_nodes завантажувач fuzzy обмежує лише знизу; 10⁹ вузлів пройшли б валідацію й
    вичерпали пам'ять на першому /explain чи бектесті. Межі — 2001 вузол і 15 термів на змінну."""
    rules, membership = strategy_texts()
    tree = yaml.safe_load(membership)
    tree["defuzz"] = {"scheme": "trapezoid", "grid_nodes": 10**9}
    r = await env.client.post(
        "/strategies",
        headers=env.h(Role.OPERATOR),
        json={"name": "huge", "rules_yaml": rules, "membership_yaml": yaml.safe_dump(tree)},
    )
    assert r.status_code == 422 and r.json()["detail"][0]["path"] == "defuzz.grid_nodes"
    wide = yaml.safe_load(membership)
    for i in range(11):  # 5 термів U + 11 = 16 > 15
        wide["variables"]["U"]["terms"][f"X{i}"] = {"type": "tri", "points": [-0.1, 0.0, 0.1]}
    r_u = await env.client.post(
        "/strategies",
        headers=env.h(Role.OPERATOR),
        json={"name": "wide", "rules_yaml": rules, "membership_yaml": yaml.safe_dump(wide)},
    )
    assert r_u.status_code == 422 and r_u.json()["detail"][0]["path"] == "variables.U.terms"
    # межа включна: найдрібніша сітка дослідження збіжності (Δ = 0.001) приймається
    tree["defuzz"] = {"scheme": "trapezoid", "grid_nodes": 2001}
    ok = await env.client.post(
        "/strategies",
        headers=env.h(Role.OPERATOR),
        json={"name": "fine", "rules_yaml": rules, "membership_yaml": yaml.safe_dump(tree)},
    )
    assert ok.status_code == 201, ok.text
    assert [s.name for s in env.db.state.strategies] == ["fine"]


async def test_out_of_range_integers_are_422_not_500(env: Env) -> None:
    """Цілі поза типами колонок (INT/BIGINT) і поза діапазоном datetime відсікаються схемою (422), а не
    доходять до драйвера (503 «database error») чи datetime (500); межові значення приймаються."""
    add_instrument(env.db)
    run_id = add_run(env.db)
    h = env.h(Role.ADMIN)
    big, i64, i32 = 10**30, 2**63, 2**31
    for url in (
        f"/market/candles?from_ns={big}",
        f"/market/candles?after_ns={i64}",
        f"/market/candles?instrument_id={i32}",
        f"/dq/score?to_ns={big}",
        f"/dq/score?instrument_id={i32}",
        f"/runs/{run_id}/equity?from_ns={big}",
        f"/risk/events?since_ns={big}",
        f"/risk/events?until_ns={i64}",
        f"/risk/events?cursor={i64}:1",
        f"/risk/events?cursor=1:{i64}",
        f"/decisions/{i64}/explain",
        f"/strategies/demo/versions/{i32}",
        f"/audit?user_id={i32}",
    ):
        r = await env.client.get(url, headers=h)
        assert r.status_code == 422, (url, r.status_code, r.text)
    act = await env.client.post("/strategies/demo/activate", params={"version": i32}, headers=h)
    assert act.status_code == 422
    base = {"symbol": "BTC-USDT-PERP", "ts_from_ns": T0_NS, "ts_to_ns": T0_NS + 60 * NS_PER_MIN}
    out_of_range: list[dict[str, int]] = [
        {"seed": 2**70},
        {"seed": -1},
        {"strategy_id": i32},
        {"ts_from_ns": i64, "ts_to_ns": i64 + 60},
    ]
    for extra in out_of_range:
        r = await env.client.post("/backtests", json={**base, **extra}, headers=h)
        assert r.status_code == 422, (extra, r.status_code, r.text)
    assert not [a for a in env.db.state.audit if a.action == "backtest.submit"]
    # межові значення типів — валідний ввід
    edge = await env.client.get(f"/market/candles?to_ns={i64 - 1}", headers=h)
    assert edge.status_code == 200
    ok = await env.client.post("/backtests", json={**base, "seed": i64 - 1}, headers=h)
    assert ok.status_code == 202, ok.text


async def test_database_data_exception_maps_to_422_not_503() -> None:
    """Страховка для параметрів без меж: SQLSTATE класу 22 (data exception) — помилка вводу (422);
    справжня недоступність БД (інші DBAPIError) лишається 503 без деталей драйвера."""

    app = create_app(build_default=False)

    @app.get("/boom/{code}")
    async def boom(code: str) -> None:
        raise DBAPIError("SELECT $1", None, DriverError(code))

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://api.test") as client:
        r22 = await client.get("/boom/22003")
        r08 = await client.get("/boom/08006")
    assert r22.status_code == 422 and "driver detail" not in r22.text
    assert r08.status_code == 503 and r08.json() == {"detail": "database error"}


async def test_explain_flags_inconsistency_when_strategy_config_differs(env: Env) -> None:
    rules, membership = strategy_texts()
    tree = yaml.safe_load(rules)
    # інша політика: усі консеквенти → HOLD, тож центроїд прогону з цією стратегією інакший
    for rule in tree["rules"]:
        rule["then"] = "HOLD"
    s = await env.db.repos().strategies.create_version("hold", yaml.safe_dump(tree), membership)
    did, _ = await add_decision(env.db, add_run(env.db, strategy_id=s.id))
    ex = (await env.client.get(f"/decisions/{did}/explain", headers=env.h(Role.ANALYST))).json()
    assert ex["strategy"] == {"id": s.id, "name": "hold", "version": 1, "source": "strategy"}
    assert ex["consistency"]["ok"] is False and ex["consistency"]["u_raw_abs_diff"] > 1e-3
    assert ex["u_raw"] == pytest.approx(0.0, abs=1e-12)  # симетрична HOLD-фігура


async def test_explain_404_and_stored_columns_fallback(env: Env) -> None:
    assert (await env.client.get("/decisions/99/explain", headers=env.h(Role.ANALYST))).status_code == 404
    did, trace = await add_decision(env.db, add_run(env.db))
    row = env.db.state.decisions[did]
    env.db.state.decisions[did] = replace(row, detector_outputs=[])  # немає точних виходів детекторів
    ex = (await env.client.get(f"/decisions/{did}/explain", headers=env.h(Role.ANALYST))).json()
    assert ex["inputs_source"] == "stored_columns"
    assert ex["inputs"]["T"] == pytest.approx(trace.T, abs=5e-6)  # NUMERIC(8,5)
    assert ex["narrative_source"] == "recomputed(partial)" and ex["narrative_uk"].startswith(
        "Спрацювало правило"
    )


# ------------------------------------------------------------------ ринок, прогони, ризик, SSE


async def test_candles_keyset_pagination_and_decimal_strings(env: Env) -> None:
    add_instrument(env.db)
    rows = [
        CandleRow(
            instrument_id=1,
            tf="1m",
            open_time_ns=T0_NS + i * NS_PER_MIN,
            close_time_ns=T0_NS + (i + 1) * NS_PER_MIN - 1_000_000,
            o=Decimal("60000.100000000000000000"),
            h=Decimal("60010.5"),
            l=Decimal("59990.0"),
            c=Decimal("60005.2"),
            volume=Decimal("1.5"),
            quote_volume=Decimal("90000"),
            trades_count=10,
            vwap=None,
            is_closed=True,
            is_synthetic=False,
            src=1,
            anomaly_score=None,
            ingested_at_ns=None,
        )
        for i in range(5)
    ]
    env.db.repos().candles.add(rows)
    seen: list[int] = []
    after = None
    while True:
        params: dict[str, Any] = {"symbol": "BTC-USDT-PERP", "limit": 2}
        if after is not None:
            params["after_ns"] = after
        page = (await env.client.get("/market/candles", params=params, headers=env.h(Role.ANALYST))).json()
        seen += [c["open_time_ns"] for c in page["items"]]
        after = page["next_after_ns"]
        if after is None:
            break
    assert seen == [r.open_time_ns for r in rows]  # без пропусків і дублікатів
    assert page["items"][-1]["o"] == "60000.100000000000000000"
    assert (
        await env.client.get("/market/candles", params={"symbol": "NOPE"}, headers=env.h(Role.ANALYST))
    ).status_code == 404


async def test_health_reports_lag_gaps_and_pipeline_snapshot(env: Env) -> None:
    add_instrument(env.db)
    env.db.repos().candles.add(
        [
            CandleRow(
                instrument_id=1,
                tf="1m",
                open_time_ns=T0_NS,
                close_time_ns=T0_NS + NS_PER_MIN - 1_000_000,
                o=Decimal(1),
                h=Decimal(1),
                l=Decimal(1),
                c=Decimal(1),
                volume=Decimal(0),
                quote_volume=Decimal(0),
                trades_count=0,
                vwap=None,
                is_closed=True,
                is_synthetic=False,
                src=1,
                anomaly_score=None,
                ingested_at_ns=None,
            )
        ]
    )
    await env.db.repos().gaps.open(1, "klines", T0_NS, T0_NS + 5 * NS_PER_MIN)
    env.services.live.publish("health", {"frames": 1200, "reconnects": 1, "q": 0.97})
    env.clock.advance(3 * NS_PER_MIN)
    h = (await env.client.get("/market/health", headers=env.h(Role.AUDITOR))).json()
    inst = h["instruments"][0]
    assert inst["data_lag_s"] == pytest.approx(120.0) and inst["open_gaps"] == 1 and h["open_gaps_total"] == 1
    assert h["pipeline"]["frames"] == 1200 and h["pipeline"]["q"] == 0.97
    # два видавці health (ingest- і торговий воркер) не затирають один одного: pipelines — за `source`
    env.services.live.publish("health", {"source": "trading_worker", "header": "MODE: PAPER · FEED: REPLAY"})
    env.services.live.publish("health", {"source": "ingest_worker", "frames": 1300})
    h = (await env.client.get("/market/health", headers=env.h(Role.AUDITOR))).json()
    assert h["pipeline"]["source"] == "ingest_worker"                     # останній знімок будь-якого видавця
    assert set(h["pipelines"]) == {"ingest_worker", "trading_worker"}
    assert h["pipelines"]["trading_worker"]["header"].startswith("MODE: PAPER")
    assert h["pipelines"]["ingest_worker"]["frames"] == 1300


async def test_runs_metrics_and_equity_decimation(env: Env) -> None:
    run_id = add_run(env.db)
    env.db.state.metrics[run_id] = {"sharpe": 1.25, "psr": float("nan"), "n_trades": 7.0}
    env.db.state.equity[run_id] = [
        EquityRow(
            run_id=run_id,
            ts_ns=T0_NS + i * NS_PER_MIN,
            equity=Decimal("10000") + i,
            cash=None,
            unrealized=None,
            gross_exposure=None,
            leverage=None,
            drawdown=Decimal("0.001"),
            risk_state="NORMAL",
            kappa=Decimal("1"),
            var95=None,
            cvar95=None,
        )
        for i in range(10)
    ]
    run = (await env.client.get(f"/runs/{run_id}", headers=env.h(Role.ANALYST))).json()
    assert (
        run["status"] == "DONE"
        and run["equity_hash"] == "01" * 32
        and run["dataset_hash"] == bytes(range(32)).hex()
    )
    m = (await env.client.get(f"/runs/{run_id}/metrics", headers=env.h(Role.ANALYST))).json()
    assert m["metrics"] == {"n_trades": 7.0, "psr": None, "sharpe": 1.25}  # NaN → null (JSON без NaN)
    eq = (
        await env.client.get(f"/runs/{run_id}/equity", params={"max_points": 4}, headers=env.h(Role.ANALYST))
    ).json()
    assert eq["n_total"] == 10 and eq["stride"] == 3
    assert [p["equity"] for p in eq["items"]] == ["10000", "10003", "10006", "10009"]
    assert (await env.client.get(f"/runs/{UUID(int=77)}", headers=env.h(Role.ANALYST))).status_code == 404


async def test_backtest_submit_returns_run_id_and_is_audited(env: Env) -> None:
    add_instrument(env.db)
    body = {"symbol": "BTC-USDT-PERP", "ts_from_ns": T0_NS, "ts_to_ns": T0_NS + 60 * NS_PER_MIN}
    r = await env.client.post("/backtests", json=body, headers=env.h(Role.ANALYST))
    assert r.status_code == 202, r.text
    run_id = UUID(r.json()["run_id"])
    assert r.json()["status_url"] == f"/runs/{run_id}" and r.json()["status"] == "PENDING"
    status = (await env.client.get(f"/runs/{run_id}", headers=env.h(Role.ANALYST))).json()
    assert status["status"] == "PENDING" and status["job"]["spec"]["seed"] == env.services.settings.seed
    audit = [a for a in env.db.state.audit if a.action == "backtest.submit"]
    assert (
        len(audit) == 1 and audit[0].target == f"run/{run_id}" and audit[0].user_id == env.uids[Role.ANALYST]
    )
    bad = await env.client.post("/backtests", json={**body, "ts_to_ns": T0_NS}, headers=env.h(Role.ANALYST))
    assert bad.status_code == 422
    assert (
        await env.client.post("/backtests", json={**body, "symbol": "NOPE"}, headers=env.h(Role.ANALYST))
    ).status_code == 404


async def test_killswitch_release_admin_only_writes_audit_and_command(env: Env) -> None:
    run_id = add_run(env.db, kind=RunKind.PAPER, status=RunStatus.RUNNING)
    env.db.repos().risk.add(
        run_id=run_id,
        ts_ns=T0_NS,
        rule="risk_state",
        state_from="COOLDOWN",
        state_to="HALTED",
        dwell_bars=4,
        observed=Decimal("0.121"),
    )
    body = {"reason": "drawdown investigated, manual restart"}
    assert (
        await env.client.post("/risk/killswitch/release", json=body, headers=env.h(Role.OPERATOR))
    ).status_code == 403
    r = await env.client.post("/risk/killswitch/release", json=body, headers=env.h(Role.ADMIN))
    assert r.status_code == 202, r.text
    assert r.json()["observed_state"] == "HALTED" and r.json()["run_id"] == str(run_id)
    row = next(a for a in env.db.state.audit if a.action == "risk.killswitch.release")
    assert row.before_json == {"state": "HALTED", "run_id": str(run_id)}
    assert row.after_json is not None and row.after_json["reason"] == body["reason"]
    assert row.user_id == env.uids[Role.ADMIN] and row.ip == "203.0.113.7"
    cmd = [p for c, k, p in env.db.state.control if (c, k) == ("fuzzhelm_control", "killswitch.release")]
    assert cmd == [{"audit_id": row.id, "run_id": str(run_id), "actor": "admin", "role": "admin"}]
    state = (await env.client.get("/risk/state", headers=env.h(Role.ANALYST))).json()
    assert state["state"] == "HALTED" and state["kappa_mode"] == 0.0
    assert state["last_release_request"]["audit_id"] == row.id


async def test_risk_events_keep_exact_shrink_factor(env: Env) -> None:
    run_id = add_run(env.db, kind=RunKind.PAPER, status=RunStatus.RUNNING)
    env.db.repos().risk.add(
        run_id=run_id,
        ts_ns=T0_NS,
        rule="max_gross_leverage",
        verdict=VerdictKind.SHRINK,
        factor=Decimal("0.99996"),
        observed=Decimal("3.0001"),
        limit_value=Decimal("3"),
    )
    env.db.repos().risk.add(
        run_id=run_id,
        ts_ns=T0_NS + 1,
        rule="stale_data",
        verdict=VerdictKind.VETO,
        factor=Decimal("0"),
        observed=Decimal("6.5"),
        limit_value=Decimal("5"),
    )
    events = (await env.client.get("/risk/events", headers=env.h(Role.ANALYST))).json()["items"]
    shrink = next(e for e in events if e["rule"] == "max_gross_leverage")
    assert shrink["factor"] == "0.99996"  # колонка NUMERIC(6,4) дала б 1.0000
    assert env.db.state.risk_events[0].factor == Decimal("1.0000")
    vetoes = (
        await env.client.get("/risk/events", params={"only": "veto"}, headers=env.h(Role.ANALYST))
    ).json()["items"]
    assert [v["rule"] for v in vetoes] == ["stale_data"] and vetoes[0]["payload"] == {}


async def walk_risk_events(env: Env, params: Mapping[str, Any]) -> tuple[list[int], list[dict[str, Any]]]:
    """Пройти всі сторінки /risk/events курсором; повертає id записів і самі сторінки."""
    ids: list[int] = []
    pages: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        q = {**params, **({"cursor": cursor} if cursor is not None else {})}
        r = await env.client.get("/risk/events", params=q, headers=env.h(Role.AUDITOR))
        assert r.status_code == 200, r.text
        page = r.json()
        pages.append(page)
        ids += [e["id"] for e in page["items"]]
        cursor = page["next_cursor"]
        if cursor is None:
            return ids, pages
        assert len(pages) < 100, "pagination does not terminate"


async def test_risk_events_keyset_pages_reach_every_record_with_until_ns(env: Env) -> None:
    """Довгий журнал: кожен запис досяжний курсором (ts, id) без пропусків і дублікатів, зокрема при рівних
    ts і при дописуванні нових записів під час гортання; until_ns — виключна верхня межа, since_ns —
    включна."""
    run_id = add_run(env.db, kind=RunKind.PAPER, status=RunStatus.RUNNING)
    risk = env.db.repos().risk
    rows = [
        risk.add(
            run_id=run_id,
            ts_ns=T0_NS + (i // 3) * NS_PER_MIN,  # по 3 записи на один ts: порядок усередині — за id
            rule="stale_data" if i % 2 else "max_daily_loss",
            verdict=VerdictKind.VETO if i % 5 == 0 else VerdictKind.ALLOW,
            factor=Decimal(0) if i % 5 == 0 else Decimal(1),
        )
        for i in range(25)
    ]
    newest_first = [r.id for r in sorted(rows, key=lambda r: (r.ts_ns or 0, r.id), reverse=True)]
    ids, pages = await walk_risk_events(env, {"limit": 4})
    assert ids == newest_first and len(pages) == 7  # 6 × 4 + 1, без порожньої останньої сторінки
    assert all(p["page_size"] == 4 and p["run_id"] == str(run_id) for p in pages)
    assert [len(p["items"]) for p in pages] == [4] * 6 + [1]
    first = pages[0]["items"][-1]
    assert pages[0]["next_cursor"] == f"{first['ts_ns']}:{first['id']}"
    ids5, pages5 = await walk_risk_events(env, {"limit": 5})
    assert ids5 == newest_first and len(pages5) == 5 and pages5[-1]["next_cursor"] is None
    # запис, дописаний під час гортання, не зсуває сторінок: старші сторінки ті самі, дублікатів немає
    p1 = (await env.client.get("/risk/events", params={"limit": 4}, headers=env.h(Role.ANALYST))).json()
    risk.add(run_id=run_id, ts_ns=T0_NS + 60 * NS_PER_MIN, rule="stale_data", verdict=VerdictKind.VETO)
    rest, _ = await walk_risk_events(env, {"limit": 4, "cursor": p1["next_cursor"]})
    assert [e["id"] for e in p1["items"]] + rest == newest_first
    # вікно часу: until_ns — виключно, since_ns — включно; ранні записи досяжні без гортання всього журналу
    lo, hi = T0_NS + 2 * NS_PER_MIN, T0_NS + 5 * NS_PER_MIN
    in_window = [r.id for r in rows if lo <= (r.ts_ns or 0) < hi]
    got, _ = await walk_risk_events(env, {"since_ns": lo, "until_ns": hi, "limit": 2})
    assert sorted(got) == sorted(in_window) and len(got) == 9
    early, _ = await walk_risk_events(env, {"until_ns": T0_NS + NS_PER_MIN, "limit": 2000})
    assert sorted(early) == sorted(r.id for r in rows[:3])
    # фільтри діють разом із курсором і вікном (очікування — з усіх 26 записів сховища)
    def expected(pred: Any) -> list[int]:
        ordered = sorted(env.db.state.risk_events, key=lambda r: (r.ts_ns or 0, r.id), reverse=True)
        return [r.id for r in ordered if pred(r)]

    vetoes, _ = await walk_risk_events(env, {"only": "veto", "until_ns": hi, "limit": 2})
    assert vetoes == expected(lambda r: r.verdict == "VETO" and (r.ts_ns or 0) < hi) == [11, 6, 1]
    rule_ids, _ = await walk_risk_events(env, {"rule": "stale_data", "limit": 3})
    assert rule_ids == expected(lambda r: r.rule == "stale_data") and len(rule_ids) == 13
    none = await env.client.get(
        "/risk/events", params={"only": "transitions", "rule": "stale_data"}, headers=env.h(Role.ANALYST)
    )
    assert none.status_code == 200 and none.json()["items"] == [] and none.json()["next_cursor"] is None
    for bad in ("abc", "1:2:3", "-1:5", "1", ":1", f"{2**63}:1"):
        r = await env.client.get("/risk/events", params={"cursor": bad}, headers=env.h(Role.ANALYST))
        assert r.status_code == 422, (bad, r.text)
    too_big = await env.client.get("/risk/events", params={"limit": 2001}, headers=env.h(Role.ANALYST))
    assert too_big.status_code == 422


async def test_limits_rejected_with_path_and_conflict_on_stale_hash(env: Env) -> None:
    current = (await env.client.get("/risk/limits", headers=env.h(Role.ADMIN))).json()
    bad = {**current["config"]}
    bad["state_machine"] = {**bad["state_machine"], "warn_exit": "0.05"}  # ≥ warn_enter — без петлі
    r = await env.client.put("/risk/limits", json=bad, headers=env.h(Role.ADMIN))
    assert r.status_code == 422 and r.json()["detail"][0]["path"] == "state_machine"
    bad2 = {
        **current["config"],
        "limits": {
            **current["config"]["limits"],
            "max_daily_loss": {"value": "1.5", "unit": "equity_fraction"},
        },
    }
    r2 = await env.client.put("/risk/limits", json=bad2, headers=env.h(Role.ADMIN))
    assert r2.status_code == 422 and r2.json()["detail"][0]["path"] == "limits.max_daily_loss.value"
    stale = await env.client.put(
        "/risk/limits", json={**current["config"], "expected_sha256": "0" * 64}, headers=env.h(Role.ADMIN)
    )
    assert stale.status_code == 409
    assert not [a for a in env.db.state.audit if a.action == "risk.limits.update"]


async def test_limits_file_restored_when_audit_commit_fails(env: Env) -> None:
    current = (await env.client.get("/risk/limits", headers=env.h(Role.ADMIN))).json()
    body = {**current["config"]}
    body["limits"]["max_daily_loss"]["value"] = "0.01"
    before = env.limits_path.read_text(encoding="utf-8")
    env.db.fail_next_commit = True
    r = await env.client.put("/risk/limits", json=body, headers=env.h(Role.ADMIN))
    assert r.status_code == 503
    assert env.limits_path.read_text(encoding="utf-8") == before  # компенсація: файл повернуто
    assert not [a for a in env.db.state.audit if a.action == "risk.limits.update"]  # відкат аудиту


async def test_sse_stream_delivers_published_events_with_ids(env: Env) -> None:
    hub = env.services.live
    hub.publish("decision", {"id": 1, "u_final": 0.31})
    hub.publish("risk", {"rule": "stale_data", "verdict": "VETO"})
    hub.publish("decision", {"id": 2, "u_final": -0.12})
    r = await env.client.get(
        "/stream/live",
        params={"max_events": 2, "kinds": ["decision"]},
        headers={**env.h(Role.ANALYST), "Last-Event-ID": "0"},
    )
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    blocks = [b for b in r.text.split("\n\n") if b.strip()]
    assert blocks[0].startswith(": fuzzhelm live stream") and "retry: 3000" in blocks[0]
    events = [dict(line.split(": ", 1) for line in b.splitlines()) for b in blocks[1:]]
    assert [(e["event"], e["id"]) for e in events] == [("decision", "1"), ("decision", "3")]
    assert events[1]["data"] == '{"id":2,"u_final":-0.12}'
    # після розриву клієнт продовжує з Last-Event-ID і не отримує повторів
    hub.close()
    again = await env.client.get("/stream/live", headers={**env.h(Role.ANALYST), "Last-Event-ID": "2"})
    ids = re.findall(r"^id: (\d+)$", again.text, flags=re.M)
    assert ids == ["3"]
    assert (await env.client.get("/stream/live")).status_code == 401


async def test_openapi_is_complete_and_english(env: Env) -> None:
    spec = (await env.client.get("/openapi.json")).json()
    assert spec["info"]["title"] == "FuzzHelm API"
    ops = [(p, m, op) for p, item in spec["paths"].items() for m, op in item.items()]
    assert len(ops) == len(ROUTE_CASES) + 2  # + login + healthz
    for path, method, op in ops:
        assert op.get("summary") and op.get("description") and op.get("tags"), (method, path)
        assert not CYRILLIC.search(op["summary"]), (method, path)
    assert "OAuth2PasswordBearer" in spec["components"]["securitySchemes"]
    docs = await env.client.get("/docs")
    assert docs.status_code == 200 and "swagger" in docs.text.lower()
    assert (await env.client.get("/healthz")).json()["status"] == "ok"
    r = await env.client.get("/healthz")
    assert r.headers["x-content-type-options"] == "nosniff" and r.headers["cache-control"] == "no-store"


# ------------------------------------------------------------------ складові API (без HTTP)


async def test_live_hub_fanout_filter_overflow_and_replay() -> None:
    hub = LiveHub(queue_size=2, replay_size=3)
    got_all: list[int] = []
    got_risk: list[int] = []
    async with hub.subscribe() as all_events, hub.subscribe(["risk"]) as risk_events:
        for i in range(4):
            hub.publish("risk" if i % 2 else "decision", {"i": i})
        hub.close()
        got_all = [e.seq async for e in all_events]
        got_risk = [e.seq async for e in risk_events]
    assert got_all == [3, 4]  # черга на 2: найстаріші викинуто, а не заблоковано видавця
    assert got_risk == [2, 4]
    assert hub.stats()["subscribers"] == 0 and hub.last("risk") is not None
    assert [e.seq for e in hub.replay_after(1)] == [2, 3, 4]  # кільцевий буфер на 3
    async with hub.subscribe(last_event_id=3) as late:  # хаб закрито: лише пропущене
        assert [e.seq async for e in late] == [4]


def test_notify_payload_roundtrip_and_limits() -> None:
    raw = encode_notify_payload("decision", {"id": 7, "qty": Decimal("0.012")})
    assert decode_notify_payload(raw) == ("decision", {"id": 7, "qty": "0.012"})
    with pytest.raises(ValueError):
        encode_notify_payload("decision", {"blob": "x" * 9000})  # > 8000 байт NOTIFY
    with pytest.raises(ValueError):
        encode_notify_payload("bad\nkind", {})
    assert decode_notify_payload("not json") is None and decode_notify_payload('{"data": 1}') is None


def test_file_limits_store_atomic_replace_conflict_and_restore(tmp_path: Path) -> None:
    path = tmp_path / "risk_limits.yaml"
    original = (CONFIG / "risk_limits.yaml").read_text(encoding="utf-8")
    path.write_text(original, encoding="utf-8")
    store = FileLimitsStore(path)
    snap = store.read()
    assert snap.sha256 == sha256_text(original)
    cfg = snap.config.model_copy(
        update={
            "limits": snap.config.limits.model_copy(
                update={
                    "max_daily_loss": snap.config.limits.max_daily_loss.model_copy(
                        update={"value": Decimal("0.015")}
                    )
                }
            )
        }
    )
    text = render_limits_yaml(cfg, leading_comments(original))
    assert load_risk_config(text) == cfg  # YAML ↔ конфіг без втрат
    with pytest.raises(LimitsConflictError):
        store.replace(text, expected_sha256="0" * 64)
    assert path.read_text(encoding="utf-8") == original
    new = store.replace(text, expected_sha256=snap.sha256)
    assert store.read().config.limits.max_daily_loss.value == Decimal("0.015") and new.sha256 != snap.sha256
    assert not [p for p in tmp_path.iterdir() if p.name.endswith(".tmp")]  # тимчасових файлів не лишилось
    with pytest.raises(ConfigValidationError):
        store.replace("limits: {}\n", expected_sha256=None)  # невалідне не пишеться
    store.restore(original)
    assert path.read_text(encoding="utf-8") == original
    assert diff_paths({"a": {"b": 1, "c": 2}}, {"a": {"b": 1, "c": 3}, "d": 0}) == ["a.c", "d"]


async def test_engine_backtest_service_runs_in_background_and_reports_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[tuple[UUID, dict[str, Any]]] = []

    async def runner(run_id: UUID, spec: Mapping[str, Any]) -> str:
        seen.append((run_id, dict(spec)))
        if spec.get("boom"):
            raise RuntimeError("engine exploded")
        return "ok"

    svc = EngineBacktestService(runner, max_pending=2)
    j1 = await svc.submit(UUID(int=1), {"symbol": "BTC-USDT-PERP"}, actor="analyst")
    j2 = await svc.submit(UUID(int=2), {"boom": True}, actor="analyst")
    with pytest.raises(BacktestQueueFull):
        await svc.submit(UUID(int=3), {}, actor="analyst")  # 2 незавершені — межа
    await svc.wait(UUID(int=1))
    await svc.wait(UUID(int=2))
    assert (j1.status, j1.result) == (JobStatus.DONE, "ok")
    assert j2.status is JobStatus.FAILED and j2.error == "RuntimeError: engine exploded"
    assert [r for r, _ in seen] == [UUID(int=1), UUID(int=2)]
    # рушія ще немає (хвиля 2) — задача чесно FAILED із поясненням, а не «вічний RUNNING»
    monkeypatch.setattr(backtests_mod, "ENGINE_MODULE", "fuzzhelm.backtest.no_such_engine")
    svc2 = EngineBacktestService()
    j3 = await svc2.submit(UUID(int=4), {}, actor="analyst")
    await svc2.wait(UUID(int=4))
    assert j3.status is JobStatus.FAILED and "not available" in (j3.error or "")
    await svc.aclose()
    await svc2.aclose()


async def test_backtest_failure_text_hides_driver_details() -> None:
    """job.error і run.error бачить користувач (GET /runs/{id}); str(DBAPIError) містить SQL і параметри."""

    async def runner(run_id: UUID, spec: Mapping[str, Any]) -> None:
        raise DBAPIError("INSERT INTO run VALUES ($1)", {"p": "param-value"}, DriverError("23505"))

    svc = EngineBacktestService(runner)
    job = await svc.submit(UUID(int=9), {}, actor="analyst")
    await svc.wait(UUID(int=9))
    assert job.status is JobStatus.FAILED and job.error == "DBAPIError: database error (sqlstate 23505)"
    assert backtests_mod.public_error(ValueError("no closed candles")) == "ValueError: no closed candles"
    await svc.aclose()


def test_detector_outputs_parser_rejects_incomplete_rows() -> None:
    good = [
        {
            "name": "ema_slope",
            "group": "TREND",
            "s": 0.5,
            "c": 0.9,
            "weight": 1.0,
            "features": {"slope": 0.1, "flag": True, "V": None},
        }
    ]
    parsed = parse_detector_outputs(good)
    assert parsed is not None and parsed[0].group is DetectorGroup.TREND
    assert dict(parsed[0].features) == {"slope": 0.1, "V": None}  # bool відкинуто
    assert parse_detector_outputs([]) is None
    assert parse_detector_outputs([{"name": "x", "s": 2.0, "c": 0.5}]) is None  # s поза [−1; 1]
    assert parse_detector_outputs([{"s": 0.1, "c": 0.5}]) is None  # немає імені
