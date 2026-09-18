"""0003_auth_audit — користувачі й аудит; роль застосунку fuzzhelm_app з append-only журналом.

Найменування: alembic/versions/0003_auth_audit.py
Призначення: таблиці app_user/audit_log (брифінг §6, «Auth (ревізія 0003)») і мінімальні права
ролі застосунку: CRUD на робочі таблиці, але `REVOKE UPDATE, DELETE ON event_journal` — застосунок,
що працює від імені fuzzhelm_app, не може переписати ланцюг хешів (лише власник схеми може).
Автор: Андрій Жук, 2026.

Роль створюється NOLOGIN (групова). Два способи працювати від її імені, з різною силою гарантії:
  * логін-роль `CREATE ROLE ... LOGIN PASSWORD ... IN ROLE fuzzhelm_app` (додає адміністратор БД) —
    межа безпеки: навіть скомпрометований застосунок не має прав власника і не може SET ROLE вище;
  * підключення власником + стартовий параметр role (storage.session.make_engine(url, role=...)) —
    лише захист від помилкових записів: власник з'єднання може виконати `SET ROLE <власник>` назад
    (перевірено тестом test_set_role_is_not_a_security_boundary_documented, deviations ST-02).
Пароль у міграції не живе.

Revision ID: 0003_auth_audit
Revises: 0002_trading
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0003_auth_audit"
down_revision: str | None = "0002_trading"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "fuzzhelm_app"

APP_TABLES: tuple[str, ...] = (
    "instrument", "candle", "event_journal", "ingest_gap", "dq_score",
    "strategy", "run", "decision", "sim_order", "position", "risk_event", "equity_point", "run_metric",
    "app_user", "audit_log",
)
APP_SEQUENCES: tuple[str, ...] = (
    "instrument_id_seq", "ingest_gap_id_seq", "strategy_id_seq", "decision_id_seq", "sim_order_id_seq",
    "position_id_seq", "risk_event_id_seq", "app_user_id_seq", "audit_log_id_seq",
)
_TABLES = ", ".join(APP_TABLES)
_SEQUENCES = ", ".join(APP_SEQUENCES)

UPGRADE: tuple[str, ...] = (
    """
    CREATE TABLE app_user (id SERIAL PRIMARY KEY, login TEXT UNIQUE, pwd_hash TEXT,
      role TEXT CHECK (role IN ('operator','analyst','auditor','admin')), created_at TIMESTAMPTZ)
    """,
    """
    CREATE TABLE audit_log (id BIGSERIAL PRIMARY KEY, ts TIMESTAMPTZ, user_id INT,
      action TEXT, target TEXT, before_json JSONB, after_json JSONB, ip INET)
    """,
    # роль — об'єкт кластера (спільна для всіх БД), тому IF NOT EXISTS: повторний upgrade не падає
    f"""
    DO $$
    BEGIN
      IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{APP_ROLE}') THEN
        CREATE ROLE {APP_ROLE} NOLOGIN;
      END IF;
      EXECUTE format('GRANT USAGE ON SCHEMA %I TO {APP_ROLE}', current_schema());
    END
    $$
    """,
    f"GRANT SELECT, INSERT, UPDATE, DELETE ON {_TABLES} TO {APP_ROLE}",
    f"GRANT USAGE, SELECT ON SEQUENCE {_SEQUENCES} TO {APP_ROLE}",
    # append-only журнал подій (брифінг §6, таблиця 3)
    f"REVOKE UPDATE, DELETE ON event_journal FROM {APP_ROLE}",
    # аудит дій теж лише дописується: слід зміни лімітів не можна стерти з-під застосунку
    f"REVOKE UPDATE, DELETE ON audit_log FROM {APP_ROLE}",
)

DOWNGRADE: tuple[str, ...] = (
    f"""
    DO $$
    BEGIN
      IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{APP_ROLE}') THEN
        EXECUTE 'REVOKE ALL ON {_TABLES} FROM {APP_ROLE}';
        EXECUTE 'REVOKE ALL ON SEQUENCE {_SEQUENCES} FROM {APP_ROLE}';
        EXECUTE format('REVOKE USAGE ON SCHEMA %I FROM {APP_ROLE}', current_schema());
      END IF;
    END
    $$
    """,
    "DROP TABLE audit_log",
    "DROP TABLE app_user",
    # роль видаляємо, лише якщо на неї більше ніщо не посилається (напр. гранти в іншій БД кластера)
    f"""
    DO $$
    BEGIN
      IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{APP_ROLE}') THEN
        BEGIN
          DROP ROLE {APP_ROLE};
        EXCEPTION WHEN dependent_objects_still_exist THEN
          RAISE NOTICE 'role {APP_ROLE} is still referenced by other objects; kept';
        END;
      END IF;
    END
    $$
    """,
)


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)
