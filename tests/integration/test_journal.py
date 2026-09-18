"""M4. Журнал подій append-only на рівні СУБД + збереження/перевірка ланцюга хешів.

Найменування: tests/integration/test_journal.py
Автор: Андрій Жук, 2026.
"""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy import make_url, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from tests.integration._data import T0_NS, add_decision, add_instrument, add_run, candle

from fuzzhelm.core.clock import NS_PER_SEC, SeededIdGenerator
from fuzzhelm.core.dto import OrderRequest
from fuzzhelm.core.enums import OrderStatus, OrderType, Side, Src
from fuzzhelm.core.journal import EventJournal, JournalEntry
from fuzzhelm.storage.models import APP_ROLE
from fuzzhelm.storage.repositories.candle import CandleRepo
from fuzzhelm.storage.repositories.common import SQLSTATE_INSUFFICIENT_PRIVILEGE, BufferedSink, sqlstate
from fuzzhelm.storage.repositories.decision import DecisionRepo
from fuzzhelm.storage.repositories.journal import JournalRepo
from fuzzhelm.storage.repositories.order import OrderRepo
from fuzzhelm.storage.session import make_engine, session_factory, session_scope

pytestmark = pytest.mark.integration

IDS = SeededIdGenerator(7, b"journal")


def _chain(run_id: UUID, n: int, sink: BufferedSink[JournalEntry]) -> EventJournal:
    j = EventJournal(run_id, sink=sink)
    for i in range(n):
        j.append(
            "candle",
            {"i": i, "c": Decimal("60000.10") + i, "side": Side.LONG, "px": Decimal("1E+2")},
            ts_event_ns=T0_NS + i * NS_PER_SEC + 123,
            ts_ingest_ns=T0_NS + i * NS_PER_SEC + 456,
        )
    return j


async def _denied(factory: async_sessionmaker[AsyncSession], sql: str, run_id: UUID) -> None:
    async with factory() as s:
        await s.execute(text(f"SET LOCAL ROLE {APP_ROLE}"))
        with pytest.raises(DBAPIError) as ei:
            await s.execute(text(sql), {"r": run_id})
        assert sqlstate(ei.value) == SQLSTATE_INSUFFICIENT_PRIVILEGE, sql
        await s.rollback()


async def test_journal_append_only_revoked_update(
    factory: async_sessionmaker[AsyncSession], db_url: str
) -> None:
    run_id = IDS.next_uuid()
    buf: BufferedSink[JournalEntry] = BufferedSink()
    j = _chain(run_id, 5, buf)
    async with session_scope(factory) as s:  # власник схеми пише початок ланцюга
        assert await JournalRepo(s).append_many(buf.drain()) == 5

    # роль застосунку: INSERT і SELECT дозволені
    async with session_scope(factory) as s:
        await s.execute(text(f"SET LOCAL ROLE {APP_ROLE}"))
        assert (await s.execute(text("SELECT current_user"))).scalar_one() == APP_ROLE
        repo = JournalRepo(s)
        head = await repo.head(run_id)
        assert (head.next_seq, head.head) == (j.next_seq, j.head)
        resumed = await repo.resume(run_id, sink=buf)
        resumed.append("order", {"qty": Decimal("0.010")}, ts_event_ns=T0_NS + 99, ts_ingest_ns=T0_NS + 100)
        await repo.append_many(buf.drain())
        assert await repo.count(run_id) == 6

    # ... а UPDATE / DELETE / TRUNCATE відхиляє сама СУБД (42501 insufficient_privilege)
    await _denied(factory, "UPDATE event_journal SET payload = '{}'::jsonb WHERE run_id = :r", run_id)
    await _denied(factory, "UPDATE event_journal SET kind = 'x' WHERE run_id = :r AND seq = 0", run_id)
    await _denied(factory, "DELETE FROM event_journal WHERE run_id = :r", run_id)
    await _denied(factory, "TRUNCATE event_journal", run_id)

    # те саме через engine, що одразу підключається роллю застосунку (так його мають брати API/воркер)
    app_engine = make_engine(db_url, role=APP_ROLE, null_pool=True)
    try:
        app_factory = session_factory(app_engine)
        async with app_factory() as s:
            assert (await s.execute(text("SELECT current_user"))).scalar_one() == APP_ROLE
            with pytest.raises(DBAPIError) as ei:
                await s.execute(text("DELETE FROM event_journal WHERE run_id = :r"), {"r": run_id})
            assert sqlstate(ei.value) == SQLSTATE_INSUFFICIENT_PRIVILEGE
    finally:
        await app_engine.dispose()

    async with session_scope(factory) as s:
        repo = JournalRepo(s)
        assert await repo.count(run_id) == 6
        assert await repo.verify(run_id) is None  # ланцюг цілий після всіх спроб
        privileges = (
            await s.execute(
                text(
                    "SELECT has_table_privilege(:role, 'event_journal', 'INSERT'),"
                    "       has_table_privilege(:role, 'event_journal', 'SELECT'),"
                    "       has_table_privilege(:role, 'event_journal', 'UPDATE'),"
                    "       has_table_privilege(:role, 'event_journal', 'DELETE'),"
                    "       has_table_privilege(:role, 'candle', 'UPDATE')"
                ),
                {"role": APP_ROLE},
            )
        ).one()
        assert tuple(privileges) == (True, True, False, False, True)


LOGIN_ROLE = "fh_login_probe"  # одноразова логін-роль лише в тестовому кластері (tmpfs)


async def test_set_role_is_not_a_security_boundary_documented(
    factory: async_sessionmaker[AsyncSession], db_url: str
) -> None:
    """ST-02: `make_engine(role=...)` поверх з'єднання власника — захист від помилок, а не межа безпеки.

    Власник з'єднання повертає собі права через `SET ROLE <власник>`; логін-роль у складі fuzzhelm_app — ні.
    """
    run_id = IDS.next_uuid()
    buf: BufferedSink[JournalEntry] = BufferedSink()
    _chain(run_id, 2, buf)
    async with session_scope(factory) as s:
        await JournalRepo(s).append_many(buf.drain())
        owner = (await s.execute(text("SELECT session_user"))).scalar_one()

    # 1) підключення власником + role=fuzzhelm_app: SET ROLE назад дає право DELETE на журнал
    app_engine = make_engine(db_url, role=APP_ROLE, null_pool=True)
    try:
        async with app_engine.connect() as c:
            priv = "SELECT has_table_privilege('event_journal', 'DELETE')"
            assert (await c.execute(text(priv))).scalar_one() is False
            await c.execute(text(f'SET ROLE "{owner}"'))
            assert (await c.execute(text(priv))).scalar_one() is True
            await c.rollback()
    finally:
        await app_engine.dispose()

    # 2) окрема LOGIN-роль у складі fuzzhelm_app: ні SET ROLE до власника, ні DELETE
    async with factory() as s, s.begin():
        await s.execute(text(f"DROP ROLE IF EXISTS {LOGIN_ROLE}"))
        await s.execute(text(f"CREATE ROLE {LOGIN_ROLE} LOGIN PASSWORD '{LOGIN_ROLE}' IN ROLE {APP_ROLE}"))
    login_url = make_url(db_url).set(username=LOGIN_ROLE, password=LOGIN_ROLE).render_as_string(False)
    login_engine = make_engine(login_url, null_pool=True)
    try:
        for sql in (f'SET ROLE "{owner}"', "DELETE FROM event_journal WHERE run_id = :r"):
            async with login_engine.connect() as c:
                assert (await c.execute(text("SELECT count(*) FROM event_journal WHERE run_id = :r"),
                                        {"r": run_id})).scalar_one() == 2
                with pytest.raises(DBAPIError) as ei:
                    await c.execute(text(sql), {"r": run_id})
                assert sqlstate(ei.value) == SQLSTATE_INSUFFICIENT_PRIVILEGE, sql
    finally:
        await login_engine.dispose()
        async with factory() as s, s.begin():
            await s.execute(text(f"DROP ROLE IF EXISTS {LOGIN_ROLE}"))
    async with session_scope(factory) as s:
        assert await JournalRepo(s).verify(run_id) is None and await JournalRepo(s).count(run_id) == 2


async def test_journal_roundtrip_verify_and_tamper_detected_at_exact_seq(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    run_id = IDS.next_uuid()
    buf: BufferedSink[JournalEntry] = BufferedSink()
    j = _chain(run_id, 20, buf)
    written = buf.drain()
    async with session_scope(factory) as s:
        await JournalRepo(s).append_many(written)
    async with session_scope(factory) as s:
        repo = JournalRepo(s)
        back = await repo.read_chain(run_id)
        assert [(e.seq, e.hash, e.prev_hash, e.ts_event_ns, e.ts_ingest_ns) for e in back] == [
            (e.seq, e.hash, e.prev_hash, e.ts_event_ns, e.ts_ingest_ns) for e in written
        ]
        # Decimal у JSONB — канонічний рядок без експоненти (Decimal('1E+2') → "100")
        assert back[3].payload == {"i": 3, "c": "60003.10", "side": 1, "px": "100"}
        assert await repo.verify(run_id) is None
        assert (await repo.head(run_id)).head == j.head
        assert await repo.kinds(run_id) == {"candle": 20}
    # власник (не роль застосунку) технічно може переписати рядок — тоді ланцюг рветься рівно на цьому seq
    async with factory() as s:
        await s.execute(
            text(
                "UPDATE event_journal SET payload = jsonb_set(payload, '{c}', '\"1.00\"') "
                "WHERE run_id = :r AND seq = 13"
            ),
            {"r": run_id},
        )
        assert await JournalRepo(s).verify(run_id) == 13
        await s.execute(text("DELETE FROM event_journal WHERE run_id = :r AND seq = 13"), {"r": run_id})
        assert await JournalRepo(s).verify(run_id) == 13  # пропуск seq теж виявлено
        await s.rollback()


async def test_app_role_has_sufficient_grants_for_repositories(
    factory: async_sessionmaker[AsyncSession], db_url: str
) -> None:
    """Гранти 0003 достатні для робочих шляхів: COPY-upsert свічок (тимчасова таблиця), рішення, ордер."""
    async with session_scope(factory) as s:
        iid = await add_instrument(s)
        run_id = await add_run(s, iid)
    app_engine = make_engine(db_url, role=APP_ROLE, null_pool=True)
    try:
        async with session_scope(session_factory(app_engine)) as s:
            res = await CandleRepo(s).upsert(
                [candle(i, src=Src.REST) for i in range(600)], iid, use_copy=True
            )
            assert res.inserted == 600
            did = await add_decision(s, run_id, iid)
            assert (await DecisionRepo(s).get(did)) is not None
            req = OrderRequest(
                client_order_id=IDS.next_uuid(),
                instrument="BTC-USDT-PERP",
                side=Side.LONG,
                otype=OrderType.MARKET,
                qty=Decimal("0.010"),
                ts_created_ns=T0_NS,
            )
            oid = await OrderRepo(s).create(req, decision_id=did, run_id=run_id, instrument_id=iid)
            row = await OrderRepo(s).get(oid)
            assert row is not None and row.status == OrderStatus.NEW
    finally:
        await app_engine.dispose()
