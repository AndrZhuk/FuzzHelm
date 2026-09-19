"""0004_decision_trace_extras — розкладка сайзера, записи ризик-контуру і текст трасування в decision.

Найменування: alembic/versions/0004_decision_trace_extras.py
Призначення: DDL §6 брифінгу зберігає в `decision` лише ціль сайзера (target_qty, binding_constraint,
stop/tp/liq), а /explain (§8.1, §8.2 «розкладка сайзера з binding_constraint») і україномовне
трасування потребують повного запису: q_atr/q_vt/q_lev/κ_mode/reject_code, вердикти ризик-ланцюга і
готовий текст narrative_uk. Три nullable-колонки додаються без зміни наявних (старі рядки — NULL).
Автор: Андрій Жук, 2026.

Відхилення від брифінгу («Alembic: 3 ревізії») — docs/deviations.d/api.md (API-02).
Права: гранти ролі fuzzhelm_app на decision — табличні (0003), тож нові колонки покриті ними автоматично.

Revision ID: 0004_decision_trace_extras
Revises: 0003_auth_audit
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0004_decision_trace_extras"
down_revision: str | None = "0003_auth_audit"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UPGRADE: tuple[str, ...] = (
    "ALTER TABLE decision ADD COLUMN sizing JSONB, ADD COLUMN risk JSONB, ADD COLUMN narrative TEXT",
)

DOWNGRADE: tuple[str, ...] = (
    "ALTER TABLE decision DROP COLUMN narrative, DROP COLUMN risk, DROP COLUMN sizing",
)


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)
