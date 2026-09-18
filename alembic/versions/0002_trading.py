"""0002_trading — стратегії, паспорти прогонів, рішення, ордери, позиції, ризик-події, капітал, метрики.

Найменування: alembic/versions/0002_trading.py
Призначення: фізична модель (брифінг §6, таблиці 6–12) дослівним DDL PostgreSQL 16.
Автор: Андрій Жук, 2026.

Відхилення від DDL брифінгу (docs/deviations.d/storage.md, ST-01): sim_order.decision_id NOT NULL —
коментар брифінгу «жоден ордер не існує без рішення ядра» лише FK не забезпечує (NULL проходить FK).

Revision ID: 0002_trading
Revises: 0001_core
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0002_trading"
down_revision: str | None = "0001_core"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UPGRADE: tuple[str, ...] = (
    # 6. Стратегії (правила як дані, версіоновані через UI)
    """
    CREATE TABLE strategy (
      id SERIAL PRIMARY KEY, name TEXT NOT NULL, version INT NOT NULL,
      rules_yaml TEXT NOT NULL, membership_yaml TEXT NOT NULL,
      rules_hash BYTEA NOT NULL, created_by TEXT, created_at TIMESTAMPTZ DEFAULT now(),
      is_active BOOLEAN DEFAULT FALSE,
      UNIQUE (name, version), UNIQUE (rules_hash)
    )
    """,
    # 7. Паспорт прогону (відтворюваність)
    """
    CREATE TABLE run (
      id UUID PRIMARY KEY,
      kind TEXT CHECK (kind IN ('backtest','paper','replay','grid_cell','testnet')),
      strategy_id INT REFERENCES strategy, instrument_id INT REFERENCES instrument,
      tf TEXT, ts_from TIMESTAMPTZ, ts_to TIMESTAMPTZ,
      config JSONB NOT NULL, config_hash BYTEA NOT NULL,
      dataset_hash BYTEA NOT NULL, git_sha CHAR(40), seed BIGINT NOT NULL,
      engine TEXT CHECK (engine IN ('mamdani','linear')),
      journal_head_hash BYTEA, equity_hash BYTEA,
      status TEXT CHECK (status IN ('RUNNING','DONE','FAILED')), error TEXT,
      started_at TIMESTAMPTZ, finished_at TIMESTAMPTZ
    )
    """,
    "CREATE UNIQUE INDEX ux_run_identity ON run (config_hash, dataset_hash, seed, engine, git_sha)",
    # 8. Рішення з повним трасуванням (джерело /explain)
    """
    CREATE TABLE decision (
      id BIGSERIAL PRIMARY KEY,
      run_id UUID NOT NULL REFERENCES run, instrument_id INT, open_time TIMESTAMPTZ,
      t_in NUMERIC(8,5), r_in NUMERIC(8,5), v_in NUMERIC(8,5),
      agreement NUMERIC(6,4), kappa NUMERIC(6,4),
      u_raw NUMERIC(8,5), u_final NUMERIC(8,5),
      detector_outputs JSONB NOT NULL,
      memberships      JSONB NOT NULL,
      fired_rules      JSONB NOT NULL,
      target_side SMALLINT CHECK (target_side IN (-1,0,1)),
      target_qty NUMERIC(38,18), binding_constraint TEXT,
      stop_price NUMERIC(38,18), tp_price NUMERIC(38,18), liq_price NUMERIC(38,18),
      UNIQUE (run_id, instrument_id, open_time)
    )
    """,
    # GIN jsonb_path_ops: запити «де спрацювало правило R07» — fired_rules @> '[{"rule_id":"R07"}]'
    "CREATE INDEX ix_decision_rules_gin ON decision USING GIN (fired_rules jsonb_path_ops)",
    # 9. Ордери (з полями виконання); decision_id NOT NULL — див. ST-01
    """
    CREATE TABLE sim_order (
      id BIGSERIAL PRIMARY KEY,
      run_id UUID REFERENCES run, decision_id BIGINT NOT NULL REFERENCES decision,
      client_order_id UUID UNIQUE NOT NULL, venue_order_id TEXT,
      instrument_id INT, side SMALLINT, otype TEXT CHECK (otype IN ('MARKET','STOP_MARKET')),
      qty NUMERIC(38,18), filled_qty NUMERIC(38,18) DEFAULT 0,
      avg_fill_price NUMERIC(38,18), fee NUMERIC(38,18) DEFAULT 0,
      slippage_bps NUMERIC(12,4), liquidity TEXT CHECK (liquidity IN ('maker','taker')),
      status TEXT CHECK (status IN ('NEW','PARTIAL','FILLED','REJECTED','CANCELED')),
      reject_code TEXT, ts_created TIMESTAMPTZ, ts_filled TIMESTAMPTZ
    )
    """,
    # 10. Позиції
    """
    CREATE TABLE position (
      id BIGSERIAL PRIMARY KEY, run_id UUID, instrument_id INT,
      side SMALLINT, qty NUMERIC(38,18), avg_entry NUMERIC(38,18),
      leverage NUMERIC(8,3), allocated_margin NUMERIC(38,18),
      stop_price NUMERIC(38,18), tp_price NUMERIC(38,18), liq_price NUMERIC(38,18),
      realized_pnl NUMERIC(38,18) DEFAULT 0, funding_paid NUMERIC(38,18) DEFAULT 0,
      max_adverse_excursion NUMERIC(38,18),
      opened_at TIMESTAMPTZ, closed_at TIMESTAMPTZ,
      exit_reason TEXT CHECK (exit_reason IN ('SIGNAL','STOP','TP','RISK_VETO','HALT','LIQUIDATION'))
    )
    """,
    # 11. Ризик: журнал вердиктів і переходів
    """
    CREATE TABLE risk_event (
      id BIGSERIAL PRIMARY KEY, run_id UUID, ts TIMESTAMPTZ, instrument_id INT,
      rule TEXT NOT NULL, verdict TEXT CHECK (verdict IN ('ALLOW','SHRINK','VETO')),
      factor NUMERIC(6,4), observed NUMERIC(38,18), limit_value NUMERIC(38,18),
      state_from TEXT, state_to TEXT, dwell_bars INT, actor TEXT, payload JSONB
    )
    """,
    "CREATE INDEX ix_risk_run_ts ON risk_event (run_id, ts DESC)",
    # частковий індекс лише для VETO: «журнал відхилень» панелі читає тільки їх
    "CREATE INDEX ix_risk_veto   ON risk_event (run_id) WHERE verdict = 'VETO'",
    # 12. Крива капіталу + метрики
    """
    CREATE TABLE equity_point (
      run_id UUID, ts TIMESTAMPTZ, equity NUMERIC(38,18), cash NUMERIC(38,18),
      unrealized NUMERIC(38,18), gross_exposure NUMERIC(38,18),
      leverage NUMERIC(8,4), drawdown NUMERIC(10,6), risk_state TEXT,
      kappa NUMERIC(6,4), var95 NUMERIC(38,18), cvar95 NUMERIC(38,18),
      PRIMARY KEY (run_id, ts)
    )
    """,
    """
    CREATE TABLE run_metric (run_id UUID, name TEXT, value DOUBLE PRECISION,
      PRIMARY KEY (run_id, name))
    """,
)

DOWNGRADE: tuple[str, ...] = (
    "DROP TABLE run_metric",
    "DROP TABLE equity_point",
    "DROP TABLE risk_event",
    "DROP TABLE position",
    "DROP TABLE sim_order",
    "DROP TABLE decision",
    "DROP TABLE run",
    "DROP TABLE strategy",
)


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)
