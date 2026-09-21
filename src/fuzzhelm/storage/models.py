"""ORM-моделі SQLAlchemy 2.0 — дзеркало нормативного DDL (брифінг §6) для 15 таблиць.

Найменування: storage/models.py
Призначення: типізований опис фізичної моделі БД для репозиторіїв і для autogenerate-порівняння
з мігрованою схемою (тест test_models_match_migrated_schema). Самі таблиці створюють міграції
Alembic 0001–0003 дослівним DDL (+ 0004: decision.sizing/risk/narrative, deviations API-02);
ці класи схему НЕ створюють і повинні збігатися з нею.
Автор: Андрій Жук, 2026.

Типи: гроші/ціни/обсяги — NUMERIC(38,18) → Decimal; час — TIMESTAMPTZ (мікросекунди UTC) → aware
datetime; наносекундний порядок — лише там, де DDL має BIGINT (event_journal.ts_event_ns/ts_ingest_ns).
Конвертацію ns ↔ TIMESTAMPTZ роблять репозиторії (storage/repositories/common.py).

Єдине свідоме відхилення від DDL: sim_order.decision_id NOT NULL (див. docs/deviations.d/storage.md, ST-01).
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import (
    CHAR,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Double,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    PrimaryKeyConstraint,
    SmallInteger,
    Table,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import INET, JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Базовий клас моделей FuzzHelm (одна MetaData на всі 15 таблиць)."""


def _money() -> Numeric[Decimal]:
    return Numeric(38, 18)


TSTZ = DateTime(timezone=True)


# ============================================================ 0001_core


class InstrumentModel(Base):
    __tablename__ = "instrument"
    __table_args__ = (
        CheckConstraint("contract_type IN ('SPOT','PERP')", name="instrument_contract_type_check"),
        UniqueConstraint("venue", "symbol_venue", name="instrument_venue_symbol_venue_key"),
        UniqueConstraint("symbol_canon", name="instrument_symbol_canon_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    venue: Mapped[str] = mapped_column(Text, nullable=False)
    symbol_venue: Mapped[str] = mapped_column(Text, nullable=False)
    symbol_canon: Mapped[str] = mapped_column(Text, nullable=False)
    base_asset: Mapped[str | None] = mapped_column(Text)
    quote_asset: Mapped[str | None] = mapped_column(Text)
    contract_type: Mapped[str | None] = mapped_column(Text)
    tick_size: Mapped[Decimal] = mapped_column(_money(), nullable=False)
    step_size: Mapped[Decimal] = mapped_column(_money(), nullable=False)
    min_notional: Mapped[Decimal] = mapped_column(_money(), nullable=False)
    mmr: Mapped[Decimal] = mapped_column(Numeric(10, 8), nullable=False, server_default=text("0.005"))
    maint_amount: Mapped[Decimal] = mapped_column(_money(), nullable=False, server_default=text("0"))
    max_leverage: Mapped[int | None] = mapped_column(SmallInteger, server_default=text("3"))
    active: Mapped[bool | None] = mapped_column(Boolean, server_default=text("true"))
    spec_fetched_at: Mapped[datetime | None] = mapped_column(TSTZ)


class CandleModel(Base):
    __tablename__ = "candle"
    __table_args__ = (
        PrimaryKeyConstraint("instrument_id", "tf", "open_time", name="candle_pkey"),
        CheckConstraint("volume >= 0", name="candle_volume_check"),
        CheckConstraint("quote_volume >= 0", name="candle_quote_volume_check"),
        CheckConstraint("trades_count >= 0", name="candle_trades_count_check"),
        CheckConstraint("src IN (1,2,3)", name="candle_src_check"),
        CheckConstraint("h >= l", name="ck_hl"),
        CheckConstraint("h >= GREATEST(o,c)", name="ck_h"),
        CheckConstraint("l <= LEAST(o,c)", name="ck_l"),
        CheckConstraint("vwap IS NULL OR (vwap >= l AND vwap <= h)", name="ck_vwap"),
        Index("ix_candle_time_brin", "open_time", postgresql_using="brin",
              postgresql_with={"pages_per_range": 32}),
        Index("ix_candle_lookup", "instrument_id", "tf", text("open_time DESC")),
    )

    instrument_id: Mapped[int] = mapped_column(Integer, ForeignKey("instrument.id"), nullable=False)
    tf: Mapped[str] = mapped_column(Text, nullable=False)
    open_time: Mapped[datetime] = mapped_column(TSTZ, nullable=False)
    close_time: Mapped[datetime] = mapped_column(TSTZ, nullable=False)
    o: Mapped[Decimal | None] = mapped_column(_money())
    h: Mapped[Decimal | None] = mapped_column(_money())
    l: Mapped[Decimal | None] = mapped_column(_money())
    c: Mapped[Decimal | None] = mapped_column(_money())
    volume: Mapped[Decimal | None] = mapped_column(_money())
    quote_volume: Mapped[Decimal | None] = mapped_column(_money())
    trades_count: Mapped[int | None] = mapped_column(Integer)
    vwap: Mapped[Decimal | None] = mapped_column(_money())
    is_closed: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    is_synthetic: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    src: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    ingested_at: Mapped[datetime | None] = mapped_column(TSTZ, server_default=text("now()"))


class EventJournalModel(Base):
    __tablename__ = "event_journal"
    __table_args__ = (PrimaryKeyConstraint("run_id", "seq", name="event_journal_pkey"),)

    run_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    seq: Mapped[int] = mapped_column(BigInteger, nullable=False)
    ts_event_ns: Mapped[int] = mapped_column(BigInteger, nullable=False)
    ts_ingest_ns: Mapped[int] = mapped_column(BigInteger, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    prev_hash: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    hash: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)


class IngestGapModel(Base):
    __tablename__ = "ingest_gap"
    __table_args__ = (
        CheckConstraint("stream IN ('klines','trades','depth')", name="ingest_gap_stream_check"),
        CheckConstraint("detector IN ('seq','bucket_count','time')", name="ingest_gap_detector_check"),
        CheckConstraint("status IN ('OPEN','FILLING','FILLED','PARTIAL','UNFILLABLE')",
                        name="ingest_gap_status_check"),
        Index("ix_gap_open", "instrument_id", text("detected_at DESC"),
              postgresql_where=text("status IN ('OPEN','FILLING')")),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    instrument_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("instrument.id"))
    stream: Mapped[str | None] = mapped_column(Text)
    ts_lo: Mapped[datetime | None] = mapped_column(TSTZ)
    ts_hi: Mapped[datetime | None] = mapped_column(TSTZ)
    expected_count: Mapped[int | None] = mapped_column(Integer)
    filled_rows: Mapped[int | None] = mapped_column(Integer, server_default=text("0"))
    detector: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int | None] = mapped_column(SmallInteger, server_default=text("0"))
    detected_at: Mapped[datetime | None] = mapped_column(TSTZ, server_default=text("now()"))
    closed_at: Mapped[datetime | None] = mapped_column(TSTZ)


class DqScoreModel(Base):
    __tablename__ = "dq_score"
    __table_args__ = (
        PrimaryKeyConstraint("instrument_id", "hour_start", name="dq_score_pkey"),
        CheckConstraint("score BETWEEN 0 AND 1", name="dq_score_score_check"),
    )

    instrument_id: Mapped[int] = mapped_column(Integer, nullable=False)
    hour_start: Mapped[datetime] = mapped_column(TSTZ, nullable=False)
    expected_buckets: Mapped[int | None] = mapped_column(Integer)
    observed_buckets: Mapped[int | None] = mapped_column(Integer)
    invalid_count: Mapped[int | None] = mapped_column(Integer)
    gap_seconds: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    lag_p95_ms: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    completeness: Mapped[Decimal | None] = mapped_column(Numeric(6, 4))
    validity: Mapped[Decimal | None] = mapped_column(Numeric(6, 4))
    timeliness: Mapped[Decimal | None] = mapped_column(Numeric(6, 4))
    continuity: Mapped[Decimal | None] = mapped_column(Numeric(6, 4))
    score: Mapped[Decimal | None] = mapped_column(Numeric(6, 4))


# ============================================================ 0002_trading


class StrategyModel(Base):
    __tablename__ = "strategy"
    __table_args__ = (
        UniqueConstraint("name", "version", name="strategy_name_version_key"),
        UniqueConstraint("rules_hash", name="strategy_rules_hash_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    rules_yaml: Mapped[str] = mapped_column(Text, nullable=False)
    membership_yaml: Mapped[str] = mapped_column(Text, nullable=False)
    rules_hash: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    created_by: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime | None] = mapped_column(TSTZ, server_default=text("now()"))
    is_active: Mapped[bool | None] = mapped_column(Boolean, server_default=text("false"))


class RunModel(Base):
    __tablename__ = "run"
    __table_args__ = (
        CheckConstraint("kind IN ('backtest','paper','replay','grid_cell','testnet')", name="run_kind_check"),
        CheckConstraint("engine IN ('mamdani','linear')", name="run_engine_check"),
        CheckConstraint("status IN ('RUNNING','DONE','FAILED')", name="run_status_check"),
        Index("ux_run_identity", "config_hash", "dataset_hash", "seed", "engine", "git_sha", unique=True),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    kind: Mapped[str | None] = mapped_column(Text)
    strategy_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("strategy.id"))
    instrument_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("instrument.id"))
    tf: Mapped[str | None] = mapped_column(Text)
    ts_from: Mapped[datetime | None] = mapped_column(TSTZ)
    ts_to: Mapped[datetime | None] = mapped_column(TSTZ)
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    config_hash: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    dataset_hash: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    git_sha: Mapped[str | None] = mapped_column(CHAR(40))
    seed: Mapped[int] = mapped_column(BigInteger, nullable=False)
    engine: Mapped[str | None] = mapped_column(Text)
    journal_head_hash: Mapped[bytes | None] = mapped_column(LargeBinary)
    equity_hash: Mapped[bytes | None] = mapped_column(LargeBinary)
    status: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(TSTZ)
    finished_at: Mapped[datetime | None] = mapped_column(TSTZ)


class DecisionModel(Base):
    __tablename__ = "decision"
    __table_args__ = (
        CheckConstraint("target_side IN (-1,0,1)", name="decision_target_side_check"),
        UniqueConstraint("run_id", "instrument_id", "open_time",
                         name="decision_run_id_instrument_id_open_time_key"),
        Index("ix_decision_rules_gin", "fired_rules", postgresql_using="gin",
              postgresql_ops={"fired_rules": "jsonb_path_ops"}),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("run.id"), nullable=False)
    instrument_id: Mapped[int | None] = mapped_column(Integer)
    open_time: Mapped[datetime | None] = mapped_column(TSTZ)
    t_in: Mapped[Decimal | None] = mapped_column(Numeric(8, 5))
    r_in: Mapped[Decimal | None] = mapped_column(Numeric(8, 5))
    v_in: Mapped[Decimal | None] = mapped_column(Numeric(8, 5))
    agreement: Mapped[Decimal | None] = mapped_column(Numeric(6, 4))
    kappa: Mapped[Decimal | None] = mapped_column(Numeric(6, 4))
    u_raw: Mapped[Decimal | None] = mapped_column(Numeric(8, 5))
    u_final: Mapped[Decimal | None] = mapped_column(Numeric(8, 5))
    detector_outputs: Mapped[list[Any]] = mapped_column(JSONB, nullable=False)
    memberships: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    fired_rules: Mapped[list[Any]] = mapped_column(JSONB, nullable=False)
    target_side: Mapped[int | None] = mapped_column(SmallInteger)
    target_qty: Mapped[Decimal | None] = mapped_column(_money())
    binding_constraint: Mapped[str | None] = mapped_column(Text)
    stop_price: Mapped[Decimal | None] = mapped_column(_money())
    tp_price: Mapped[Decimal | None] = mapped_column(_money())
    liq_price: Mapped[Decimal | None] = mapped_column(_money())
    # 0004_decision_trace_extras (deviations API-02): розкладка сайзера, ризик-ланцюг, текст трасування
    sizing: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    risk: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    narrative: Mapped[str | None] = mapped_column(Text)


class SimOrderModel(Base):
    __tablename__ = "sim_order"
    __table_args__ = (
        CheckConstraint("otype IN ('MARKET','STOP_MARKET')", name="sim_order_otype_check"),
        CheckConstraint("liquidity IN ('maker','taker')", name="sim_order_liquidity_check"),
        CheckConstraint("status IN ('NEW','PARTIAL','FILLED','REJECTED','CANCELED')",
                        name="sim_order_status_check"),
        UniqueConstraint("client_order_id", name="sim_order_client_order_id_key"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("run.id"))
    # ST-01: NOT NULL — «жоден ордер не існує без рішення ядра» (у DDL брифінгу FK без NOT NULL)
    decision_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("decision.id"), nullable=False)
    client_order_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    venue_order_id: Mapped[str | None] = mapped_column(Text)
    instrument_id: Mapped[int | None] = mapped_column(Integer)
    side: Mapped[int | None] = mapped_column(SmallInteger)
    otype: Mapped[str | None] = mapped_column(Text)
    qty: Mapped[Decimal | None] = mapped_column(_money())
    filled_qty: Mapped[Decimal | None] = mapped_column(_money(), server_default=text("0"))
    avg_fill_price: Mapped[Decimal | None] = mapped_column(_money())
    fee: Mapped[Decimal | None] = mapped_column(_money(), server_default=text("0"))
    slippage_bps: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))
    liquidity: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str | None] = mapped_column(Text)
    reject_code: Mapped[str | None] = mapped_column(Text)
    ts_created: Mapped[datetime | None] = mapped_column(TSTZ)
    ts_filled: Mapped[datetime | None] = mapped_column(TSTZ)


class PositionModel(Base):
    __tablename__ = "position"
    __table_args__ = (
        CheckConstraint("exit_reason IN ('SIGNAL','STOP','TP','RISK_VETO','HALT','LIQUIDATION')",
                        name="position_exit_reason_check"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    instrument_id: Mapped[int | None] = mapped_column(Integer)
    side: Mapped[int | None] = mapped_column(SmallInteger)
    qty: Mapped[Decimal | None] = mapped_column(_money())
    avg_entry: Mapped[Decimal | None] = mapped_column(_money())
    leverage: Mapped[Decimal | None] = mapped_column(Numeric(8, 3))
    allocated_margin: Mapped[Decimal | None] = mapped_column(_money())
    stop_price: Mapped[Decimal | None] = mapped_column(_money())
    tp_price: Mapped[Decimal | None] = mapped_column(_money())
    liq_price: Mapped[Decimal | None] = mapped_column(_money())
    realized_pnl: Mapped[Decimal | None] = mapped_column(_money(), server_default=text("0"))
    funding_paid: Mapped[Decimal | None] = mapped_column(_money(), server_default=text("0"))
    max_adverse_excursion: Mapped[Decimal | None] = mapped_column(_money())
    opened_at: Mapped[datetime | None] = mapped_column(TSTZ)
    closed_at: Mapped[datetime | None] = mapped_column(TSTZ)
    exit_reason: Mapped[str | None] = mapped_column(Text)


class RiskEventModel(Base):
    __tablename__ = "risk_event"
    __table_args__ = (
        CheckConstraint("verdict IN ('ALLOW','SHRINK','VETO')", name="risk_event_verdict_check"),
        Index("ix_risk_run_ts", "run_id", text("ts DESC")),
        Index("ix_risk_veto", "run_id", postgresql_where=text("verdict = 'VETO'")),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    ts: Mapped[datetime | None] = mapped_column(TSTZ)
    instrument_id: Mapped[int | None] = mapped_column(Integer)
    rule: Mapped[str] = mapped_column(Text, nullable=False)
    verdict: Mapped[str | None] = mapped_column(Text)
    factor: Mapped[Decimal | None] = mapped_column(Numeric(6, 4))
    observed: Mapped[Decimal | None] = mapped_column(_money())
    limit_value: Mapped[Decimal | None] = mapped_column(_money())
    state_from: Mapped[str | None] = mapped_column(Text)
    state_to: Mapped[str | None] = mapped_column(Text)
    dwell_bars: Mapped[int | None] = mapped_column(Integer)
    actor: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB)


class EquityPointModel(Base):
    __tablename__ = "equity_point"
    __table_args__ = (PrimaryKeyConstraint("run_id", "ts", name="equity_point_pkey"),)

    run_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    ts: Mapped[datetime] = mapped_column(TSTZ, nullable=False)
    equity: Mapped[Decimal | None] = mapped_column(_money())
    cash: Mapped[Decimal | None] = mapped_column(_money())
    unrealized: Mapped[Decimal | None] = mapped_column(_money())
    gross_exposure: Mapped[Decimal | None] = mapped_column(_money())
    leverage: Mapped[Decimal | None] = mapped_column(Numeric(8, 4))
    drawdown: Mapped[Decimal | None] = mapped_column(Numeric(10, 6))
    risk_state: Mapped[str | None] = mapped_column(Text)
    kappa: Mapped[Decimal | None] = mapped_column(Numeric(6, 4))


class RunMetricModel(Base):
    __tablename__ = "run_metric"
    __table_args__ = (PrimaryKeyConstraint("run_id", "name", name="run_metric_pkey"),)

    run_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    value: Mapped[float | None] = mapped_column(Double)


# ============================================================ 0003_auth_audit


class AppUserModel(Base):
    __tablename__ = "app_user"
    __table_args__ = (
        CheckConstraint("role IN ('operator','analyst','auditor','admin')", name="app_user_role_check"),
        UniqueConstraint("login", name="app_user_login_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    login: Mapped[str | None] = mapped_column(Text)
    pwd_hash: Mapped[str | None] = mapped_column(Text)
    role: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime | None] = mapped_column(TSTZ)


class AuditLogModel(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    ts: Mapped[datetime | None] = mapped_column(TSTZ)
    user_id: Mapped[int | None] = mapped_column(Integer)
    action: Mapped[str | None] = mapped_column(Text)
    target: Mapped[str | None] = mapped_column(Text)
    before_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    after_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    ip: Mapped[str | None] = mapped_column(INET)


metadata = Base.metadata


def table_of(model: type[Base]) -> Table:
    """Core-таблиця моделі з точним типом Table (для insert/update/select у репозиторіях)."""
    t = model.__table__
    assert isinstance(t, Table)
    return t


# Порядок створення (FK-залежності) — для TRUNCATE у тестах і для документації.
CORE_TABLES: tuple[str, ...] = ("instrument", "candle", "event_journal", "ingest_gap", "dq_score")
TRADING_TABLES: tuple[str, ...] = (
    "strategy", "run", "decision", "sim_order", "position", "risk_event", "equity_point", "run_metric",
)
AUTH_TABLES: tuple[str, ...] = ("app_user", "audit_log")
ALL_TABLES: tuple[str, ...] = CORE_TABLES + TRADING_TABLES + AUTH_TABLES

# Роль застосунку з мінімальними правами (ревізія 0003): event_journal — лише INSERT/SELECT.
APP_ROLE = "fuzzhelm_app"
