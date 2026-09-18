"""Тести storage без БД (у звичайному `make test`): конвертації типів, раунди upsert, моделі, Alembic.

Найменування: tests/integration/test_storage_offline.py
Автор: Андрій Жук, 2026.

Лежать поруч з інтеграційними, бо перевіряють той самий пакет storage, але маркера `integration`
не мають і PostgreSQL не потребують.
"""

from __future__ import annotations

import calendar
import io
from datetime import UTC, datetime, timedelta, timezone
from decimal import Context, Decimal, InvalidOperation, localcontext
from pathlib import Path
from uuid import UUID

import numpy as np
import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from hypothesis import given
from hypothesis import strategies as st
from tests.integration._data import T0_NS, candle, trace_dict

from fuzzhelm.core.clock import NS_PER_MIN
from fuzzhelm.core.enums import Side, Src
from fuzzhelm.core.journal import EventJournal, JournalEntry, verify_chain
from fuzzhelm.core.money import dec_str, quantize_internal
from fuzzhelm.storage import session as session_mod
from fuzzhelm.storage.models import ALL_TABLES, APP_ROLE, SimOrderModel, metadata
from fuzzhelm.storage.repositories.candle import WRITE_COLUMNS, CandleArrays, candle_record
from fuzzhelm.storage.repositories.common import (
    BufferedSink,
    dt_to_ns,
    ns_to_dt,
    split_unique_rounds,
    to_money18,
    to_numeric,
    trim_decimal,
)
from fuzzhelm.storage.repositories.decision import DecisionRecord
from fuzzhelm.storage.repositories.strategy import rules_hash
from fuzzhelm.storage.repositories.user import BCRYPT_MAX_BYTES, verify_password
from fuzzhelm.storage.session import json_dumps, json_loads, make_engine

ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------- час ns ↔ TIMESTAMPTZ


@given(st.integers(min_value=-(2**62), max_value=2**62))
def test_ns_timestamptz_roundtrip_exact_to_microsecond(ns: int) -> None:
    # TIMESTAMPTZ має мікросекундну роздільність: туди-назад = floor до мікросекунди;
    # для кратних 1 мкс — тотожність
    assert dt_to_ns(ns_to_dt(ns)) == ns - ns % 1_000
    us_aligned = ns - ns % 1_000
    assert dt_to_ns(ns_to_dt(us_aligned)) == us_aligned


def test_ns_to_dt_known_values_and_rejections() -> None:
    assert ns_to_dt(0) == datetime(1970, 1, 1, tzinfo=UTC)
    # незалежний еталон: calendar.timegm (цілі секунди UTC) + мікросекунди
    ref = calendar.timegm((2026, 9, 18, 12, 30, 15, 0, 0, 0)) * 1_000_000_000 + 123_456_000
    assert dt_to_ns(datetime(2026, 9, 18, 12, 30, 15, 123456, tzinfo=UTC)) == ref
    assert ns_to_dt(ref) == datetime(2026, 9, 18, 12, 30, 15, 123456, tzinfo=UTC)
    # інша зона — той самий момент часу
    kyiv = timezone(timedelta(hours=3))
    assert dt_to_ns(datetime(2026, 9, 18, 15, 30, 15, 123456, tzinfo=kyiv)) == ref
    # цілочисельна арифметика: субмікросекундна частина відкидається (floor), решта точна
    assert dt_to_ns(ns_to_dt(ref + 789)) == ref
    # а float уже не вміщує наносекунди епохи (крок float біля 1.8e18 — 256 нс), тому його тут і немає
    assert int(float(ref + 789)) != ref + 789
    with pytest.raises(ValueError, match="naive"):
        dt_to_ns(datetime(2026, 1, 1))
    with pytest.raises(TypeError):
        ns_to_dt(1.5e18)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        ns_to_dt(True)


# ---------------------------------------------------------------- раунди upsert


def _apply(state: dict[int, tuple[bool, int, int]], row: tuple[int, bool, int, int]) -> None:
    """Модель правила upsert: ON CONFLICT DO UPDATE ... WHERE NOT is_closed AND EXCLUDED.src <= src."""
    key, closed, src, val = row
    cur = state.get(key)
    if cur is None or (not cur[0] and src <= cur[1]):
        state[key] = (closed, src, val)


rows_st = st.lists(
    st.tuples(st.integers(0, 5), st.booleans(), st.integers(1, 3), st.integers(0, 99)), max_size=40
)


@given(rows_st, st.randoms(use_true_random=False))
def test_split_unique_rounds_equivalent_to_sequential_upsert(
    rows: list[tuple[int, bool, int, int]], rnd: object
) -> None:
    rounds = split_unique_rounds(rows, key=lambda r: r[0])
    for rd in rounds:
        assert len({r[0] for r in rd}) == len(rd)  # у раунді ключі унікальні
    # порядок входжень кожного ключа збережено
    flat = [r for rd in rounds for r in rd]
    for k in {r[0] for r in rows}:
        assert [r for r in flat if r[0] == k] == [r for r in rows if r[0] == k]
    sequential: dict[int, tuple[bool, int, int]] = {}
    for r in rows:
        _apply(sequential, r)
    by_rounds: dict[int, tuple[bool, int, int]] = {}
    for rd in rounds:
        shuffled = list(rd)
        rnd.shuffle(shuffled)  # type: ignore[attr-defined]
        for r in shuffled:  # у межах раунду порядок не має значення
            _apply(by_rounds, r)
    assert by_rounds == sequential


# ---------------------------------------------------------------- числа


def test_to_numeric_uses_shortest_repr_and_rejects_non_values() -> None:
    assert to_numeric(0.1) == Decimal("0.1") and str(to_numeric(0.1)) == "0.1"
    # numpy 2: repr(np.float64(0.25)) == "np.float64(0.25)", тому numpy-скаляр спершу .item()
    assert to_numeric(np.float64(0.25)) == Decimal("0.25") and to_numeric(np.int64(3)) == Decimal(3)
    assert to_numeric(7) == Decimal(7) and to_numeric(None) is None
    d = Decimal("1.50")
    assert to_numeric(d) is d
    with pytest.raises(TypeError):
        to_numeric(True)
    with pytest.raises(ValueError):
        to_numeric(float("inf"))
    with pytest.raises(ValueError):
        to_numeric(float("nan"))


@pytest.mark.parametrize(
    "bad", [Decimal("NaN"), Decimal("sNaN"), Decimal("Infinity"), Decimal("-Infinity"), "NaN"]
)
def test_to_numeric_rejects_non_finite_decimal(bad: Decimal | str) -> None:
    # PostgreSQL прийняв би NUMERIC NaN мовчки (і NaN > будь-якого числа, ST-04) — межа мусить відкинути
    with pytest.raises(ValueError, match="non-finite"):
        to_numeric(bad)


def test_to_money18_rounds_half_even_like_core_money() -> None:
    # еталон — рукописні значення: рівно-половинний 19-й знак іде до парного (HALF_EVEN), а не «від нуля»,
    # як округлив би сам PostgreSQL (у СУБД 0.…25 → 0.…3; перевіряє інтеграційний тест кривої капіталу)
    cases = {
        "0.0000000000000000025": "0.000000000000000002",
        "0.0000000000000000035": "0.000000000000000004",
        "-0.0000000000000000025": "-0.000000000000000002",
        "10000.0000000000000000005": "10000.000000000000000000",
        "10000.0000000000000000015": "10000.000000000000000002",
        "1.5": "1.500000000000000000",
    }
    for src, want in cases.items():
        got = to_money18(Decimal(src))
        assert got is not None and dec_str(got) == want, src
        assert got == quantize_internal(Decimal(src))  # та сама функція, що в equity_hash
    assert to_money18(None) is None and to_money18(0.1) == Decimal("0.100000000000000000")
    # 20 цілих + 18 дробових = 38 розрядів проходить і під типовим контекстом процесу (prec=28)
    with localcontext(Context(prec=28)):
        big = to_money18(Decimal("12345678901234567890.1234567890123456785"))
    assert big is not None and dec_str(big) == "12345678901234567890.123456789012345678"
    with pytest.raises(InvalidOperation):  # 21 цілий розряд — переповнення NUMERIC(38,18)
        to_money18(Decimal("123456789012345678901"))
    with pytest.raises(ValueError, match="non-finite"):
        to_money18(Decimal("NaN"))


def test_default_engine_runs_as_app_role(monkeypatch: pytest.MonkeyPatch) -> None:
    # get_session (FastAPI) бере процесний engine: він мусить іти роллю застосунку, інакше REVOKE не діє
    seen: dict[str, object] = {}

    def fake_make_engine(url: str | None = None, **kwargs: object) -> object:
        seen.update(kwargs)
        return object()

    monkeypatch.setattr(session_mod, "make_engine", fake_make_engine)
    monkeypatch.setattr(session_mod, "session_factory", lambda engine: object())
    monkeypatch.setattr(session_mod._Default, "engine", None)
    monkeypatch.setattr(session_mod._Default, "factory", None)
    session_mod.get_default_engine()
    assert seen.get("role") == APP_ROLE


@given(st.decimals(min_value=Decimal("-1e19"), max_value=Decimal("1e19"), places=18, allow_nan=False))
def test_trim_decimal_is_exact_and_exponent_free(x: Decimal) -> None:
    t = trim_decimal(x)
    assert t == x
    exp = t.as_tuple().exponent
    assert isinstance(exp, int) and exp <= 0  # ніколи не 1E+2 (додатна експонента)
    s = dec_str(t)  # канонічна серіалізація core — без "E"
    assert "E" not in s and (("." not in s) or not s.endswith("0"))


def test_trim_decimal_keeps_all_38_digits_under_default_context() -> None:
    x = Decimal("12345678901234567890.123456789012345678")        # повна точність NUMERIC(38,18)
    with localcontext(Context(prec=28)):                           # типовий контекст процесу без setup
        assert trim_decimal(x) == x
    assert trim_decimal(Decimal("100.000000000000000000")) == Decimal(100)
    assert dec_str(trim_decimal(Decimal("100.000000000000000000"))) == "100"


def test_json_dumps_decimal_without_exponent_and_numpy() -> None:
    out = json_dumps(
        {
            "a": Decimal("1E+2"),
            "b": Decimal("0.000"),
            "s": Side.SHORT,
            "n": np.array([1.5, 2.0]),
            "u": UUID(int=1),
            "h": b"\x01\xff",
        }
    )
    assert json_loads(out) == {
        "a": "100",
        "b": "0.000",
        "s": -1,
        "n": [1.5, 2.0],
        "u": "00000000-0000-0000-0000-000000000001",
        "h": "01ff",
    }


# ---------------------------------------------------------------- моделі, записи, sink-и


def test_models_cover_all_ddl_tables_and_st01() -> None:
    assert set(metadata.tables) == set(ALL_TABLES) and len(ALL_TABLES) == 15
    assert SimOrderModel.__table__.c.decision_id.nullable is False  # ST-01
    candle_t = metadata.tables["candle"]
    assert [c.name for c in candle_t.primary_key.columns] == ["instrument_id", "tf", "open_time"]
    idx = {i.name for t in metadata.tables.values() for i in t.indexes}
    assert idx == {
        "ix_candle_time_brin",
        "ix_candle_lookup",
        "ix_gap_open",
        "ux_run_identity",
        "ix_decision_rules_gin",
        "ix_risk_run_ts",
        "ix_risk_veto",
    }
    assert APP_ROLE == "fuzzhelm_app"


def test_candle_record_matches_write_columns() -> None:
    c = candle(3, src=Src.WS, closed=False)
    rec = dict(zip(WRITE_COLUMNS, candle_record(c, 42), strict=True))
    assert rec["instrument_id"] == 42 and rec["src"] == 1 and rec["is_closed"] is False
    assert dt_to_ns(rec["open_time"]) == c.open_time_ns and rec["o"] == c.o and rec["anomaly_score"] is None


def test_candle_arrays_columns_and_bars() -> None:
    n = 3
    arr = CandleArrays(
        t_ns=np.arange(n, dtype=np.int64) * NS_PER_MIN,
        o=np.ones(n),
        h=np.full(n, 2.0),
        l=np.zeros(n),
        c=np.ones(n),
        v=np.full(n, 5.0),
        qv=np.full(n, 6.0),
        n=np.arange(n, dtype=np.int64),
    )
    assert len(arr) == 3 and set(arr.columns()) == {"t_ns", "o", "h", "l", "c", "v"}
    bars = arr.bars()
    assert bars[2].t_ns == 2 * NS_PER_MIN and bars[2].n == 2 and isinstance(bars[2].t_ns, int)


def test_decision_record_from_trace_maps_columns() -> None:
    d = trace_dict(T0_NS, "R07")

    class Tr:
        def to_dict(self) -> dict[str, object]:
            return d

    run_id = UUID(int=5)
    for src in (d, Tr()):
        rec = DecisionRecord.from_trace(src, run_id=run_id, instrument_id=1, target_side=-1)
        v = rec.to_values()
        assert v["t_in"] == Decimal("0.6123456789") and v["agreement"] == Decimal("0.8100001")
        assert v["kappa"] == Decimal("0.8765432") and dt_to_ns(v["open_time"]) == T0_NS
        assert v["fired_rules"][0]["rule_id"] == "R07" and v["target_side"] == -1
        assert set(v["detector_outputs"][0]) == {"name", "group", "s", "c", "weight", "features"}


def test_buffered_sink_bridges_event_journal() -> None:
    buf: BufferedSink[JournalEntry] = BufferedSink()
    j = EventJournal(UUID(int=9), sink=buf, keep=False)
    for i in range(3):
        j.append("k", {"i": i}, ts_event_ns=i, ts_ingest_ns=i)
    assert len(buf) == 3
    drained = buf.drain()
    assert len(buf) == 0 and verify_chain(drained) is None and drained[-1].hash == j.head


def test_rules_hash_distinguishes_pair_boundaries() -> None:
    # канонічне JSON-кодування пари: зсув межі між текстами дає інший хеш
    assert rules_hash("ab", "c") != rules_hash("a", "bc")
    assert rules_hash("a", "b") == rules_hash("a", "b") and len(rules_hash("a", "b")) == 32


def test_verify_password_rejects_overlong_and_empty() -> None:
    assert verify_password("x" * (BCRYPT_MAX_BYTES + 1), "$2b$04$abc") is False
    assert verify_password("", "$2b$04$abc") is False and verify_password("x", None) is False


def test_make_engine_role_requires_asyncpg() -> None:
    with pytest.raises(ValueError, match="asyncpg"):
        make_engine("postgresql+psycopg://u:p@localhost:1/db", role=APP_ROLE)


# ---------------------------------------------------------------- Alembic без БД


def _cfg(buf: io.StringIO) -> Config:
    cfg = Config(str(ROOT / "alembic.ini"), output_buffer=buf)
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", "postgresql+asyncpg://u:p@localhost:1/offline")
    cfg.attributes["configure_logger"] = False
    return cfg


def test_alembic_revisions_form_linear_chain() -> None:
    script = ScriptDirectory.from_config(_cfg(io.StringIO()))
    chain = [r.revision for r in script.walk_revisions("base", "heads")]
    assert chain == ["0003_auth_audit", "0002_trading", "0001_core"]
    assert script.get_revision("0002_trading").down_revision == "0001_core"  # type: ignore[union-attr]


def test_alembic_offline_sql_contains_normative_ddl() -> None:
    up = io.StringIO()
    command.upgrade(_cfg(up), "head", sql=True)
    sql = up.getvalue()
    assert (
        "CREATE INDEX ix_candle_time_brin ON candle USING BRIN (open_time) WITH (pages_per_range=32)" in sql
    )
    assert "CREATE INDEX ix_decision_rules_gin ON decision USING GIN (fired_rules jsonb_path_ops)" in sql
    assert (
        "CREATE UNIQUE INDEX ux_run_identity ON run (config_hash, dataset_hash, seed, engine, git_sha)" in sql
    )
    assert "decision_id BIGINT NOT NULL REFERENCES decision" in sql
    assert f"REVOKE UPDATE, DELETE ON event_journal FROM {APP_ROLE}" in sql
    down = io.StringIO()
    command.downgrade(_cfg(down), "head:base", sql=True)
    dsql = down.getvalue()
    for table in ALL_TABLES:
        assert f"DROP TABLE {table}" in dsql
    assert f"DROP ROLE {APP_ROLE}" in dsql
