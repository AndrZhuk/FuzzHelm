"""M5. Торговельні таблиці: ордер без рішення неможливий (NOT NULL + FK), решта репозиторіїв туди й назад.

Найменування: tests/integration/test_trading.py
Автор: Андрій Жук, 2026.
"""

from __future__ import annotations

import math
from decimal import Decimal
from typing import Any

import numpy as np
import pytest
from sqlalchemy import delete, insert, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from tests.integration._data import SYMBOL, T0_NS, add_decision, add_instrument, add_run, trace_dict

from fuzzhelm.backtest.manifest import build_manifest, equity_hash
from fuzzhelm.core.clock import NS_PER_MIN, SeededIdGenerator
from fuzzhelm.core.dto import Fill, OrderAck, OrderRequest
from fuzzhelm.core.enums import (
    EngineKind,
    ExitReason,
    GapStatus,
    Liquidity,
    OrderStatus,
    OrderType,
    RiskState,
    RunKind,
    RunStatus,
    Side,
    Stream,
    VerdictKind,
)
from fuzzhelm.core.money import quantize_internal
from fuzzhelm.risk.journal import RiskEventRecord, RiskJournal
from fuzzhelm.storage.models import DecisionModel, SimOrderModel
from fuzzhelm.storage.repositories.common import (
    SQLSTATE_FOREIGN_KEY_VIOLATION,
    SQLSTATE_NOT_NULL_VIOLATION,
    SQLSTATE_UNIQUE_VIOLATION,
    BufferedSink,
    ns_to_dt,
    sqlstate,
)
from fuzzhelm.storage.repositories.decision import DecisionRecord, DecisionRepo
from fuzzhelm.storage.repositories.dq import DqRepo, DqRow
from fuzzhelm.storage.repositories.equity import EquityPoint, EquityRepo
from fuzzhelm.storage.repositories.gap import GapRepo
from fuzzhelm.storage.repositories.order import OrderRepo
from fuzzhelm.storage.repositories.position import PositionRepo
from fuzzhelm.storage.repositories.risk import RiskEventRepo
from fuzzhelm.storage.repositories.run import RunRepo
from fuzzhelm.storage.repositories.strategy import StrategyConflictError, StrategyRepo
from fuzzhelm.storage.session import session_scope

pytestmark = pytest.mark.integration

IDS = SeededIdGenerator(11, b"trading")
_O = SimOrderModel.__table__


def _order_values(run_id: Any, decision_id: int | None, iid: int) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "decision_id": decision_id,
        "client_order_id": IDS.next_uuid(),
        "instrument_id": iid,
        "side": 1,
        "otype": "MARKET",
        "qty": Decimal("0.010"),
        "status": "NEW",
        "ts_created": ns_to_dt(T0_NS),
    }


async def _violation(factory: async_sessionmaker[AsyncSession], stmt: Any) -> str | None:
    async with factory() as s:
        with pytest.raises(IntegrityError) as ei:
            await s.execute(stmt)
        await s.rollback()
    return sqlstate(ei.value)


async def test_order_requires_decision_fk(factory: async_sessionmaker[AsyncSession]) -> None:
    async with session_scope(factory) as s:
        iid = await add_instrument(s)
        run_id = await add_run(s, iid)
        did = await add_decision(s, run_id, iid)

    # 1) ордер без рішення: NOT NULL (ST-01) — FK сам по собі NULL пропустив би
    assert (
        await _violation(factory, insert(_O).values(**_order_values(run_id, None, iid)))
        == SQLSTATE_NOT_NULL_VIOLATION
    )
    # 2) посилання на неіснуюче рішення: FK
    assert (
        await _violation(factory, insert(_O).values(**_order_values(run_id, did + 10_000, iid)))
        == SQLSTATE_FOREIGN_KEY_VIOLATION
    )

    # 3) з рішенням ядра — працює (через репозиторій)
    req = OrderRequest(
        client_order_id=IDS.next_uuid(),
        instrument=SYMBOL,
        side=Side.LONG,
        otype=OrderType.MARKET,
        qty=Decimal("0.010"),
        ts_created_ns=T0_NS + NS_PER_MIN,
    )
    async with session_scope(factory) as s:
        oid = await OrderRepo(s).create(req, decision_id=did, run_id=run_id, instrument_id=iid)
        row = await OrderRepo(s).get(oid)
        assert row is not None and row.decision_id == did and row.client_order_id == req.client_order_id

    # 4) рішення, на яке посилається ордер, не можна видалити (аудит лишається повним)
    assert (
        await _violation(factory, delete(DecisionModel.__table__).where(DecisionModel.__table__.c.id == did))
        == SQLSTATE_FOREIGN_KEY_VIOLATION
    )
    # 5) той самий client_order_id вдруге — UNIQUE (ідемпотентність подачі)
    dup = {**_order_values(run_id, did, iid), "client_order_id": req.client_order_id}
    assert await _violation(factory, insert(_O).values(**dup)) == SQLSTATE_UNIQUE_VIOLATION


async def test_order_ack_and_partial_fills_accumulate(factory: async_sessionmaker[AsyncSession]) -> None:
    async with session_scope(factory) as s:
        iid = await add_instrument(s)
        run_id = await add_run(s, iid)
        did = await add_decision(s, run_id, iid)
        cid = IDS.next_uuid()
        repo = OrderRepo(s)
        await repo.create(
            OrderRequest(
                client_order_id=cid,
                instrument=SYMBOL,
                side=Side.SHORT,
                otype=OrderType.MARKET,
                qty=Decimal("0.030"),
                ts_created_ns=T0_NS,
            ),
            decision_id=did,
            run_id=run_id,
            instrument_id=iid,
        )
        await repo.apply_ack(
            OrderAck(client_order_id=cid, venue_order_id="PAPER-1", status=OrderStatus.NEW, ts_ns=T0_NS)
        )

        def fill(q: str, p: str, fee: str) -> Fill:
            return Fill(
                client_order_id=cid,
                instrument=SYMBOL,
                side=Side.SHORT,
                qty=Decimal(q),
                price=Decimal(p),
                fee=Decimal(fee),
                liquidity=Liquidity.TAKER,
                slippage_bps=Decimal("1.25"),
                ts_fill_ns=T0_NS + NS_PER_MIN,
            )

        r1 = await repo.apply_fill(fill("0.010", "60000.0", "0.24"))
        assert r1.status == OrderStatus.PARTIAL and r1.filled_qty == Decimal("0.010")
        r2 = await repo.apply_fill(fill("0.020", "60003.0", "0.48"))
        assert r2.status == OrderStatus.FILLED and r2.filled_qty == Decimal("0.030")
        # середня ціна зважена за обсягом: (0.010·60000 + 0.020·60003) / 0.030 = 60002
        assert r2.avg_fill_price == Decimal("60002") and r2.fee == Decimal("0.72")
        assert (
            r2.venue_order_id == "PAPER-1"
            and r2.liquidity == "taker"
            and r2.ts_filled_ns == T0_NS + NS_PER_MIN
        )
        assert [o.id for o in await repo.list_for_decision(did)] == [r2.id]


async def test_strategy_versioning_hash_uniqueness_and_activation(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_scope(factory) as s:
        repo = StrategyRepo(s)
        v1 = await repo.create_version(
            "fuzzhelm", "rules: [a]\n", "T: {}\n", created_by="admin", activate=True
        )
        v2 = await repo.create_version("fuzzhelm", "rules: [b]\n", "T: {}\n", created_by="admin")
        other = await repo.create_version("baseline", "rules: [a]\n", "T: {x: 1}\n")
        assert (v1.version, v2.version, other.version) == (1, 2, 1)
        with pytest.raises(StrategyConflictError) as ei:
            await repo.create_version("fuzzhelm", "rules: [a]\n", "T: {}\n")
        assert ei.value.existing_id == v1.id
        assert (await repo.get_active("fuzzhelm")).id == v1.id  # type: ignore[union-attr]
        await repo.activate(v2.id)
        versions = await repo.list_versions("fuzzhelm")
        assert [(v.version, v.is_active) for v in versions] == [(1, False), (2, True)]
        assert (await repo.latest("fuzzhelm")).id == v2.id  # type: ignore[union-attr]
        assert await repo.list_names() == ["baseline", "fuzzhelm"]
        assert (await repo.get(v2.id)).rules_yaml == "rules: [b]\n"  # type: ignore[union-attr]
        assert await repo.delete(other.id)


async def test_run_manifest_lifecycle_metrics_and_identity(factory: async_sessionmaker[AsyncSession]) -> None:
    manifest = build_manifest(
        kind=RunKind.BACKTEST,
        engine=EngineKind.MAMDANI,
        seed=42,
        config={"kappa_min": 0.35, "nodes": 201},
        dataset={"t_ns": np.arange(3, dtype=np.int64), "c": np.array([1.0, 2.0, 3.0])},
        git=False,
    )
    run_id = IDS.next_uuid()
    async with session_scope(factory) as s:
        iid = await add_instrument(s)
        repo = RunRepo(s)
        row = await repo.create_from_manifest(
            run_id,
            manifest,
            config={"kappa_min": 0.35, "nodes": 201},
            instrument_id=iid,
            tf="1m",
            ts_from_ns=T0_NS,
            ts_to_ns=T0_NS + 5,
        )
        assert row.status == RunStatus.RUNNING and row.config_hash.hex() == manifest.config_hash
        assert row.ts_from_ns == T0_NS and row.ts_to_ns == T0_NS  # TIMESTAMPTZ: floor до мікросекунди
        eq = [Decimal("10000"), Decimal("10001.5")]
        done = await repo.finish(
            run_id, RunStatus.DONE, journal_head_hash=bytes(range(32)), equity_hash=equity_hash(eq)
        )
        assert done.status == RunStatus.DONE and done.finished_at_ns is not None
        assert done.equity_hash is not None and done.equity_hash.hex() == equity_hash(eq)
        assert await repo.put_metrics(run_id, {"sharpe": 1.25, "max_dd": 0.031, "psr": math.nan}) == 3
        assert await repo.put_metrics(run_id, {"sharpe": 1.5}) == 1  # upsert
        metrics = await repo.get_metrics(run_id)
        assert metrics["sharpe"] == 1.5 and metrics["max_dd"] == 0.031 and math.isnan(metrics["psr"] or 0.0)
        found = await repo.find_by_identity(
            config_hash=manifest.config_hash,
            dataset_hash=manifest.dataset_hash,
            seed=42,
            engine="mamdani",
            git_sha=None,
        )
        assert found is not None and found.id == run_id
        assert [r.id for r in await repo.list(kind="backtest", status="DONE")] == [run_id]
        with pytest.raises(ValueError, match="DONE or FAILED"):
            await repo.finish(run_id, RunStatus.RUNNING)
    # ux_run_identity: той самий прогін (з git_sha) вдруге — порушення унікальності
    async with session_scope(factory) as s:
        await add_run(s, seed=7, git_sha="b" * 40)
    with pytest.raises(IntegrityError) as ei:
        async with session_scope(factory) as s:
            await add_run(s, seed=7, git_sha="b" * 40)
    assert sqlstate(ei.value) == SQLSTATE_UNIQUE_VIOLATION


async def test_decision_trace_roundtrip_and_rule_lookup(factory: async_sessionmaker[AsyncSession]) -> None:
    async with session_scope(factory) as s:
        iid = await add_instrument(s)
        run_id = await add_run(s, iid)
        repo = DecisionRepo(s)
        did = await add_decision(s, run_id, iid, 0, rule_id="R07")
        ids = await repo.insert_many(
            [
                DecisionRecord.from_trace(
                    trace_dict(T0_NS + i * NS_PER_MIN, "R33" if i % 2 else "R07"),
                    run_id=run_id,
                    instrument_id=iid,
                    target_side=0,
                )
                for i in range(1, 6)
            ]
        )
        assert ids == sorted(ids) and len(ids) == 5 and ids[0] > did
    async with session_scope(factory) as s:
        repo = DecisionRepo(s)
        row = await repo.get(did)
        assert row is not None
        # NUMERIC(8,5)/(6,4): СУБД округлює float ядра до масштабу колонки
        assert (row.t_in, row.r_in, row.v_in) == (Decimal("0.61235"), Decimal("-0.25000"), Decimal("0.50000"))
        assert (row.agreement, row.kappa) == (Decimal("0.8100"), Decimal("0.8765"))
        assert (row.u_raw, row.u_final) == (Decimal("0.44123"), Decimal("0.38675"))
        # JSONB зберігає float точно — /explain бачить ті самі α і μ
        assert row.fired_rules[0] == {
            "rule_id": "R07",
            "alpha": 0.62,
            "consequent": "SL",
            "antecedent": {"T": "UP", "R": "NO_PRESSURE"},
        }
        assert row.memberships["T"] == {"UP": 0.71, "ZERO": 0.29}
        assert row.detector_outputs[0]["features"] == {"slope": 1.5}
        assert (row.target_side, row.target_qty, row.binding_constraint) == (1, Decimal("0.012"), "ATR_RISK")
        assert row.open_time_ns == T0_NS
        hits = await repo.find_by_rule(run_id, "R33")
        assert [h.open_time_ns for h in hits] == [T0_NS + i * NS_PER_MIN for i in (1, 3, 5)]
        assert len(await repo.find_by_rule(run_id, "R07")) == 3
        assert (await repo.get_at(run_id, iid, T0_NS + 2 * NS_PER_MIN)) is not None
        assert [d.id for d in await repo.list_for_run(run_id, nonzero_only=True)] == [did]
        assert await repo.count(run_id) == 6
    # UNIQUE (run_id, instrument_id, open_time): друге рішення на той самий бар — помилка
    with pytest.raises(IntegrityError):
        async with session_scope(factory) as s:
            await add_decision(s, run_id, iid, 0)


async def test_risk_events_from_risk_journal(factory: async_sessionmaker[AsyncSession]) -> None:
    async with session_scope(factory) as s:
        iid = await add_instrument(s)
        run_id = await add_run(s, iid)
    sink: BufferedSink[RiskEventRecord] = BufferedSink()
    journal = RiskJournal(run_id, sink=sink, keep=False)
    for i, (verdict, factor) in enumerate(
        [(VerdictKind.ALLOW, None), (VerdictKind.SHRINK, Decimal("0.5")), (VerdictKind.VETO, Decimal("0"))]
    ):
        journal.append(
            RiskEventRecord(
                ts_ns=T0_NS + i * NS_PER_MIN,
                rule="max_gross_leverage",
                verdict=verdict,
                factor=factor,
                observed=Decimal("3.25") + i,
                limit_value=Decimal("3"),
                instrument=SYMBOL,
                payload={"requested": Decimal("0.012"), "note": "тест"},
                run_id=run_id,
            )
        )
    journal.append(
        RiskEventRecord(
            ts_ns=T0_NS + 5 * NS_PER_MIN,
            rule="risk_state",
            verdict=None,
            factor=None,
            observed=Decimal("0.041"),
            limit_value=None,
            state_from=RiskState.NORMAL,
            state_to=RiskState.WARNING,
            dwell_bars=17,
            actor=None,
            payload={"event": "WARN_BREACH"},
            run_id=run_id,
        )
    )
    async with session_scope(factory) as s:
        assert await RiskEventRepo(s).insert_many(sink.drain(), instrument_ids={SYMBOL: iid}) == 4
    async with session_scope(factory) as s:
        repo = RiskEventRepo(s)
        rows = await repo.list_for_run(run_id)
        assert [r.ts_ns for r in rows] == sorted((r.ts_ns for r in rows), reverse=True)  # новіші першими
        vetoes = await repo.vetoes(run_id)
        assert (
            len(vetoes) == 1
            and vetoes[0].observed == Decimal("5.25")
            and vetoes[0].limit_value == Decimal("3")
        )
        assert vetoes[0].instrument_id == iid and vetoes[0].payload == {"requested": "0.012", "note": "тест"}
        tr = await repo.transitions(run_id)
        assert [(t.state_from, t.state_to, t.dwell_bars) for t in tr] == [("NORMAL", "WARNING", 17)]
        with pytest.raises(LookupError):
            await repo.insert(
                RiskEventRecord(
                    ts_ns=T0_NS,
                    rule="x",
                    verdict=VerdictKind.ALLOW,
                    factor=None,
                    observed=None,
                    limit_value=None,
                    instrument="ETH-USDT-PERP",
                )
            )


async def test_equity_copy_and_hash_roundtrip(factory: async_sessionmaker[AsyncSession]) -> None:
    async with session_scope(factory) as s:
        iid = await add_instrument(s)
        run_id = await add_run(s, iid)
    points = [
        EquityPoint(
            ts_ns=T0_NS + i * NS_PER_MIN,
            equity=Decimal("10000") + Decimal(i) / 7,
            cash=Decimal("10000"),
            unrealized=Decimal(i) / 7,
            gross_exposure=Decimal("0"),
            leverage=0.123456,
            drawdown=i / 1000.0,
            risk_state=RiskState.NORMAL,
            kappa=0.87654,
        )
        for i in range(1_000)
    ]
    async with session_scope(factory) as s:
        assert await EquityRepo(s).insert_many(run_id, points) == 1_000  # COPY
    async with session_scope(factory) as s:
        repo = EquityRepo(s)
        curve = await repo.curve(run_id)
        assert len(curve) == 1_000 and curve[5].risk_state == "NORMAL"
        assert curve[5].leverage == Decimal("0.1235") and curve[5].kappa == Decimal("0.8765")
        assert curve[5].drawdown == Decimal("0.005000")
        ts, eq = await repo.equity_series(run_id)
        assert ts == [p.ts_ns for p in points]
        # значення з БД = quantize_internal(вхід) (record квантує HALF_EVEN) → той самий equity_hash
        assert equity_hash(eq, ts) == equity_hash([p.equity for p in points], ts)
        assert await repo.count(run_id) == 1_000
    # шлях без COPY дає той самий результат
    run2 = IDS.next_uuid()
    async with session_scope(factory) as s:
        await RunRepo(s).create(
            run2,
            kind="backtest",
            config={},
            config_hash=bytes(32),
            dataset_hash=bytes(32),
            seed=99,
            engine="linear",
            git_sha="c" * 40,
        )
        await EquityRepo(s).insert_many(run2, points[:10], use_copy=False)
        ts2, eq2 = await EquityRepo(s).equity_series(run2)
        assert eq2 == eq[:10] and ts2 == ts[:10]


@pytest.mark.parametrize("use_copy", [True, False], ids=["copy", "values"])
async def test_equity_hash_from_db_matches_on_half_ties(
    factory: async_sessionmaker[AsyncSession], use_copy: bool
) -> None:
    """Рівно-половинний 19-й знак: СУБД округлила б «від нуля», equity_hash — HALF_EVEN (core.money)."""
    ties = [
        Decimal("10000.0000000000000000005"),
        Decimal("0.0000000000000000025"),
        Decimal("-1.0000000000000000045"),
    ]
    async with session_scope(factory) as s:
        # контроль: власне округлення PostgreSQL тут справді інше — інакше тест нічого не доводить
        pg = [
            (await s.execute(text("SELECT CAST(:x AS NUMERIC(38,18))"), {"x": x})).scalar_one() for x in ties
        ]
        assert all(p != quantize_internal(x) for p, x in zip(pg, ties, strict=True))
        run_id = await add_run(s, seed=32 if use_copy else 31)
    points = [
        EquityPoint(ts_ns=T0_NS + i * NS_PER_MIN, equity=x, cash=x, var95=x) for i, x in enumerate(ties)
    ]
    ts_in = [p.ts_ns for p in points]
    async with session_scope(factory) as s:
        await EquityRepo(s).insert_many(run_id, points, use_copy=use_copy)
    async with session_scope(factory) as s:
        ts, eq = await EquityRepo(s).equity_series(run_id)
        curve = await EquityRepo(s).curve(run_id)
    assert eq == [quantize_internal(x) for x in ties]
    assert [r.cash for r in curve] == eq and [r.var95 for r in curve] == eq
    assert ts == ts_in and equity_hash(eq, ts) == equity_hash(ties, ts_in)


async def test_position_gap_and_dq_lifecycle(factory: async_sessionmaker[AsyncSession]) -> None:
    async with session_scope(factory) as s:
        iid = await add_instrument(s)
        run_id = await add_run(s, iid)
        pos = PositionRepo(s)
        pid = await pos.open(
            run_id=run_id,
            instrument_id=iid,
            side=Side.LONG,
            qty=Decimal("0.010"),
            avg_entry=Decimal("60000.0"),
            opened_at_ns=T0_NS,
            leverage=2.5,
            liq_price=Decimal("40000.0"),
        )
        assert [p.id for p in await pos.open_positions(run_id)] == [pid]
        upd = await pos.update(pid, stop_price=Decimal("59000.0"), max_adverse_excursion=Decimal("-12.5"))
        assert upd.stop_price == Decimal("59000.0") and upd.leverage == Decimal("2.500")
        with pytest.raises(ValueError, match="cannot update"):
            await pos.update(pid, opened_at=1)
        closed = await pos.close(
            pid,
            closed_at_ns=T0_NS + 10 * NS_PER_MIN,
            exit_reason=ExitReason.STOP,
            realized_pnl=Decimal("-10.00"),
        )
        assert closed.exit_reason == "STOP" and closed.closed_at_ns == T0_NS + 10 * NS_PER_MIN
        assert await pos.open_positions(run_id) == []

        gaps = GapRepo(s)
        gid = await gaps.open(
            iid, Stream.KLINES, T0_NS, T0_NS + 5 * NS_PER_MIN, expected_count=6, detector="time"
        )
        assert [g.id for g in await gaps.list_open(iid)] == [gid]
        filling = await gaps.update_status(gid, GapStatus.FILLING, count_attempt=True)
        assert filling.attempts == 1 and filling.closed_at_ns is None
        done = await gaps.update_status(gid, GapStatus.FILLED, filled_rows=6, at_ns=T0_NS + NS_PER_MIN)
        assert done.attempts == 2 and done.filled_rows == 6 and done.closed_at_ns == T0_NS + NS_PER_MIN
        assert await gaps.list_open(iid) == [] and await gaps.stats() == {"FILLED": 1}

        dq = DqRepo(s)
        await dq.upsert(
            DqRow(
                instrument_id=iid,
                hour_start_ns=T0_NS,
                expected_buckets=60,
                observed_buckets=59,
                completeness=59 / 60,
                score=0.98765,
            )
        )
        await dq.upsert(
            DqRow(
                instrument_id=iid,
                hour_start_ns=T0_NS,
                expected_buckets=60,
                observed_buckets=60,
                completeness=1.0,
                score=1.0,
            )
        )
        rows = await dq.range(iid)
        assert len(rows) == 1 and rows[0].score == Decimal("1.0000") and rows[0].observed_buckets == 60
        assert (await dq.latest(iid)).hour_start_ns == T0_NS  # type: ignore[union-attr]
    with pytest.raises(IntegrityError):  # CHECK score BETWEEN 0 AND 1
        async with session_scope(factory) as s:
            await DqRepo(s).upsert(DqRow(instrument_id=iid, hour_start_ns=T0_NS + 3_600 * 10**9, score=1.5))
