"""0001_core — довідник інструментів, свічки, журнал подій, прогалини інжесту, скор якості.

Найменування: alembic/versions/0001_core.py
Призначення: фізична модель (брифінг §6, таблиці 1–5) дослівним DDL PostgreSQL 16 без розширень.
Автор: Андрій Жук, 2026.

Revision ID: 0001_core
Revises: —
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0001_core"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Кожен елемент — рівно одна SQL-команда (asyncpg виконує лише одну команду на prepared statement).
UPGRADE: tuple[str, ...] = (
    # 1. Довідник інструментів
    """
    CREATE TABLE instrument (
      id            SERIAL PRIMARY KEY,
      venue         TEXT NOT NULL,
      symbol_venue  TEXT NOT NULL,
      symbol_canon  TEXT NOT NULL,
      base_asset    TEXT, quote_asset TEXT,
      contract_type TEXT CHECK (contract_type IN ('SPOT','PERP')),
      tick_size     NUMERIC(38,18) NOT NULL,
      step_size     NUMERIC(38,18) NOT NULL,
      min_notional  NUMERIC(38,18) NOT NULL,
      mmr           NUMERIC(10,8)  NOT NULL DEFAULT 0.005,
      maint_amount  NUMERIC(38,18) NOT NULL DEFAULT 0,
      max_leverage  SMALLINT DEFAULT 3,
      active        BOOLEAN DEFAULT TRUE,
      spec_fetched_at TIMESTAMPTZ,
      UNIQUE (venue, symbol_venue), UNIQUE (symbol_canon)
    )
    """,
    # 2. Свічки (ядро даних)
    """
    CREATE TABLE candle (
      instrument_id INT NOT NULL REFERENCES instrument,
      tf            TEXT NOT NULL,
      open_time     TIMESTAMPTZ NOT NULL,
      close_time    TIMESTAMPTZ NOT NULL,
      o NUMERIC(38,18), h NUMERIC(38,18), l NUMERIC(38,18), c NUMERIC(38,18),
      volume        NUMERIC(38,18) CHECK (volume >= 0),
      quote_volume  NUMERIC(38,18) CHECK (quote_volume >= 0),
      trades_count  INT CHECK (trades_count >= 0),
      vwap          NUMERIC(38,18),
      is_closed     BOOLEAN NOT NULL DEFAULT FALSE,
      is_synthetic  BOOLEAN NOT NULL DEFAULT FALSE,
      src           SMALLINT NOT NULL CHECK (src IN (1,2,3)),
      anomaly_score NUMERIC(10,6),
      ingested_at   TIMESTAMPTZ DEFAULT now(),
      PRIMARY KEY (instrument_id, tf, open_time),
      CONSTRAINT ck_hl   CHECK (h >= l),
      CONSTRAINT ck_h    CHECK (h >= GREATEST(o,c)),
      CONSTRAINT ck_l    CHECK (l <= LEAST(o,c)),
      CONSTRAINT ck_vwap CHECK (vwap IS NULL OR (vwap >= l AND vwap <= h))
    )
    """,
    # BRIN: open_time корелює з фізичним порядком вставки (дозапис у кінець), 32 сторінки на діапазон
    "CREATE INDEX ix_candle_time_brin ON candle USING BRIN (open_time) WITH (pages_per_range=32)",
    "CREATE INDEX ix_candle_lookup ON candle (instrument_id, tf, open_time DESC)",
    # 3. Журнал подій з ланцюгом хешів (append-only забезпечує REVOKE у 0003_auth_audit)
    """
    CREATE TABLE event_journal (
      run_id       UUID   NOT NULL,
      seq          BIGINT NOT NULL,
      ts_event_ns  BIGINT NOT NULL,
      ts_ingest_ns BIGINT NOT NULL,
      kind         TEXT   NOT NULL,
      payload      JSONB  NOT NULL,
      prev_hash    BYTEA  NOT NULL,
      hash         BYTEA  NOT NULL,
      PRIMARY KEY (run_id, seq)
    )
    """,
    # 4. Прогалини інжесту
    """
    CREATE TABLE ingest_gap (
      id BIGSERIAL PRIMARY KEY,
      instrument_id INT REFERENCES instrument,
      stream TEXT CHECK (stream IN ('klines','trades','depth')),
      ts_lo TIMESTAMPTZ, ts_hi TIMESTAMPTZ,
      expected_count INT, filled_rows INT DEFAULT 0,
      detector TEXT CHECK (detector IN ('seq','bucket_count','time')),
      status TEXT CHECK (status IN ('OPEN','FILLING','FILLED','PARTIAL','UNFILLABLE')),
      attempts SMALLINT DEFAULT 0,
      detected_at TIMESTAMPTZ DEFAULT now(), closed_at TIMESTAMPTZ
    )
    """,
    # частковий індекс: у ньому лише відкриті прогалини, тому він крихітний навіть за довгої історії
    """
    CREATE INDEX ix_gap_open ON ingest_gap (instrument_id, detected_at DESC)
      WHERE status IN ('OPEN','FILLING')
    """,
    # 5. Погодинний скор якості
    """
    CREATE TABLE dq_score (
      instrument_id INT, hour_start TIMESTAMPTZ,
      expected_buckets INT, observed_buckets INT, invalid_count INT,
      anomaly_count INT, gap_seconds NUMERIC(10,2), lag_p95_ms NUMERIC(12,2),
      completeness NUMERIC(6,4), validity NUMERIC(6,4),
      timeliness NUMERIC(6,4), continuity NUMERIC(6,4),
      score NUMERIC(6,4) CHECK (score BETWEEN 0 AND 1),
      PRIMARY KEY (instrument_id, hour_start)
    )
    """,
)

# DROP TABLE прибирає індекси, обмеження і послідовності SERIAL/BIGSERIAL (вони OWNED BY колонкою).
DOWNGRADE: tuple[str, ...] = (
    "DROP TABLE dq_score",
    "DROP TABLE ingest_gap",
    "DROP TABLE event_journal",
    "DROP TABLE candle",
    "DROP TABLE instrument",
)


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)
