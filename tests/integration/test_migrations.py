"""M6. Міграції Alembic: up → down → up без залишків; схема = нормативний DDL брифінгу = ORM-моделі.

Найменування: tests/integration/test_migrations.py
Автор: Андрій Жук, 2026.

Тести синхронні (psycopg): Alembic env.py сам запускає власний event loop для asyncpg.
"""

from __future__ import annotations

from typing import Any

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import Column, Connection, Engine, Integer, MetaData, text
from tests.integration._schema import brief_ddl, fingerprint
from tests.integration.conftest import ROOT, alembic_config

from fuzzhelm.storage.models import ALL_TABLES, APP_ROLE, AUTH_TABLES, CORE_TABLES, TRADING_TABLES, metadata

pytestmark = pytest.mark.integration

HEAD = "0003_auth_audit"


def _tables(c: Connection) -> set[str]:
    return set(c.execute(text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")).scalars())


def _role_exists(c: Connection) -> bool:
    return bool(
        c.execute(text("SELECT count(*) FROM pg_roles WHERE rolname = :r"), {"r": APP_ROLE}).scalar_one()
    )


def _revision(c: Connection) -> str | None:
    return c.execute(text("SELECT max(version_num) FROM alembic_version")).scalar_one()


def _grants(c: Connection) -> list[tuple[Any, ...]]:
    return [
        tuple(r)
        for r in c.execute(
            text(
                "SELECT table_name, privilege_type FROM information_schema.role_table_grants "
                "WHERE grantee = :r ORDER BY 1, 2"
            ),
            {"r": APP_ROLE},
        ).all()
    ]


def _public_types(c: Connection) -> set[str]:
    return set(
        c.execute(
            text(
                "SELECT t.typname FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace "
                "WHERE n.nspname = 'public'"
            )
        ).scalars()
    )


def test_migrations_up_and_down_clean(db_url: str, sync_engine: Engine) -> None:
    cfg = alembic_config(db_url)
    command.upgrade(cfg, "head")
    with sync_engine.connect() as c:
        head_fp = fingerprint(c)
        head_grants = _grants(c)
        assert _revision(c) == HEAD
        assert _tables(c) == {*ALL_TABLES, "alembic_version"}
        assert _role_exists(c)
        # append-only журнал: у ролі застосунку немає UPDATE/DELETE саме на event_journal (і audit_log)
        journal_privs = {p for t, p in head_grants if t == "event_journal"}
        assert journal_privs == {"INSERT", "SELECT"}
        assert {p for t, p in head_grants if t == "candle"} == {"INSERT", "SELECT", "UPDATE", "DELETE"}
        indexes = {name: ddl for _, name, ddl in head_fp["indexes"]}
        assert "USING brin (open_time) WITH (pages_per_range='32')" in indexes["ix_candle_time_brin"]
        assert "USING gin (fired_rules jsonb_path_ops)" in indexes["ix_decision_rules_gin"]
        assert "WHERE (status = ANY (ARRAY['OPEN'::text, 'FILLING'::text]))" in indexes["ix_gap_open"]
        assert "WHERE (verdict = 'VETO'::text)" in indexes["ix_risk_veto"]
        assert indexes["ux_run_identity"].startswith(
            "CREATE UNIQUE INDEX ux_run_identity ON run USING btree "
            "(config_hash, dataset_hash, seed, engine, git_sha)"
        )
        types_at_head = _public_types(c)

    # покроковий downgrade: після кожної ревізії лишаються рівно її попередники
    for target, expected, role in (
        ("0002_trading", {*CORE_TABLES, *TRADING_TABLES}, False),
        ("0001_core", set(CORE_TABLES), False),
        ("base", set(), False),
    ):
        command.downgrade(cfg, target)
        with sync_engine.connect() as c:
            assert _tables(c) - {"alembic_version"} == expected, target
            assert _role_exists(c) is role, target
            assert _revision(c) == (None if target == "base" else target)

    with sync_engine.connect() as c:
        base_fp = fingerprint(c)
        # нічого не лишилось: ні таблиць, ні послідовностей SERIAL, ні індексів, ні обмежень, ні типів рядків
        assert base_fp == {"tables": [], "columns": [], "constraints": [], "indexes": [], "sequences": []}
        assert _public_types(c) == {"alembic_version", "_alembic_version"}
        assert _grants(c) == []
    assert set(AUTH_TABLES) <= types_at_head

    command.upgrade(cfg, "head")
    with sync_engine.connect() as c:
        assert _revision(c) == HEAD
        assert fingerprint(c) == head_fp  # повторний upgrade дає побітово ту саму схему
        assert _grants(c) == head_grants
        assert _public_types(c) == types_at_head


def test_migrated_schema_matches_brief_ddl(db_url: str, sync_engine: Engine) -> None:
    """Нормативний DDL §6 брифінгу, виконаний у тимчасовій схемі, = схема після міграцій (крім ST-01)."""
    statements = brief_ddl(ROOT / "docs" / "BRIEF.md")
    assert len(statements) == 22  # 15 CREATE TABLE + 7 CREATE INDEX
    with sync_engine.connect() as c:
        tx = c.begin()
        try:
            c.execute(text("CREATE SCHEMA brief_ref"))
            c.execute(text("SET LOCAL search_path TO brief_ref"))
            for stmt in statements:
                c.exec_driver_sql(stmt)
            ref = fingerprint(c, "brief_ref")
            migrated = fingerprint(c, "public")
        finally:
            tx.rollback()  # тимчасова схема зникає разом із транзакцією

    assert [t for (t,) in ref["tables"]] == sorted(ALL_TABLES)
    named = {name for _, name, _ in ref["indexes"] if name.startswith(("ix_", "ux_"))}
    assert named == {
        "ix_candle_time_brin",
        "ix_candle_lookup",
        "ix_gap_open",
        "ux_run_identity",
        "ix_decision_rules_gin",
        "ix_risk_run_ts",
        "ix_risk_veto",
    }
    assert {name for _, name, _, _ in ref["constraints"]} >= {"ck_hl", "ck_h", "ck_l", "ck_vwap"}
    # єдине свідоме відхилення: sim_order.decision_id NOT NULL (ST-01)
    decision_fk_mig = [r for r in migrated["columns"] if r[0] == "sim_order" and r[2] == "decision_id"]
    decision_fk_ref = [r for r in ref["columns"] if r[0] == "sim_order" and r[2] == "decision_id"]
    assert decision_fk_mig[0][4] is True and decision_fk_ref[0][4] is False
    migrated["columns"] = [
        (*r[:4], False, *r[5:]) if (r[0], r[2]) == ("sim_order", "decision_id") else r
        for r in migrated["columns"]
    ]
    for key in ref:
        assert migrated[key] == ref[key], key


def _diffs(c: Connection, md: MetaData, only: set[str] | None = None) -> list[Any]:
    def include(name: str | None, type_: str, parent: Any) -> bool:
        if type_ != "table":
            return True
        return name != "alembic_version" and (only is None or name in only)

    ctx = MigrationContext.configure(
        c, opts={"compare_type": True, "compare_server_default": True, "include_name": include}
    )
    return list(compare_metadata(ctx, md))


def test_models_match_migrated_schema(db_url: str, sync_engine: Engine) -> None:
    """ORM-моделі (storage/models.py) не розійшлися з міграціями: autogenerate не бачить різниці."""
    with sync_engine.connect() as c:
        assert _diffs(c, metadata) == []
        # негативний контроль: порівняння справді чутливе — копія candle зі зміненою колонкою дає різницю
        drift = MetaData()
        metadata.tables["instrument"].to_metadata(drift)
        candle = metadata.tables["candle"].to_metadata(drift)
        candle.c.tf.nullable = True
        candle.append_column(Column("extra", Integer))
        kinds = {
            d[0] if isinstance(d, tuple) else d[0][0] for d in _diffs(c, drift, only={"candle", "instrument"})
        }
        assert {"add_column", "modify_nullable"} <= kinds
