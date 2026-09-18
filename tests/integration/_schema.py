"""Відбиток фізичної схеми PostgreSQL з каталогу (для порівнянь «міграції ↔ DDL брифінгу» і up/down/up).

Найменування: tests/integration/_schema.py
Автор: Андрій Жук, 2026.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from sqlalchemy import Connection, text

_QUERIES: dict[str, str] = {
    "tables": """
        SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = :schema AND c.relkind IN ('r', 'p') ORDER BY 1""",
    "columns": """
        SELECT c.relname, a.attnum, a.attname, format_type(a.atttypid, a.atttypmod), a.attnotnull,
               pg_get_expr(d.adbin, d.adrelid)
        FROM pg_attribute a
        JOIN pg_class c ON c.oid = a.attrelid JOIN pg_namespace n ON n.oid = c.relnamespace
        LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
        WHERE n.nspname = :schema AND c.relkind = 'r' AND a.attnum > 0 AND NOT a.attisdropped
        ORDER BY 1, 2""",
    "constraints": """
        SELECT c.relname, con.conname, con.contype, pg_get_constraintdef(con.oid)
        FROM pg_constraint con JOIN pg_class c ON c.oid = con.conrelid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = :schema ORDER BY 1, 2""",
    "indexes": (
        "SELECT tablename, indexname, indexdef FROM pg_indexes WHERE schemaname = :schema ORDER BY 1, 2"
    ),
    "sequences": "SELECT sequencename FROM pg_sequences WHERE schemaname = :schema ORDER BY 1",
}


def fingerprint(
    conn: Connection, schema: str = "public", exclude: frozenset[str] = frozenset({"alembic_version"})
) -> dict[str, list[tuple[Any, ...]]]:
    """Таблиці, колонки (тип, NOT NULL, DEFAULT), обмеження, індекси, послідовності схеми.

    Кваліфікатор `<schema>.` вирізається, щоб схеми з однаковим DDL давали однаковий відбиток.
    """
    out: dict[str, list[tuple[Any, ...]]] = {}
    for key, sql in _QUERIES.items():
        rows = conn.execute(text(sql), {"schema": schema}).all()
        norm: list[tuple[Any, ...]] = []
        for r in rows:
            if key != "sequences" and r[0] in exclude:
                continue
            norm.append(tuple(v.replace(f"{schema}.", "") if isinstance(v, str) else v for v in r))
        out[key] = norm
    return out


def brief_ddl(brief: Path) -> list[str]:
    """SQL-блок розділу «## 6. Модель даних» брифінгу як список команд (коментарі `--` вирізано)."""
    md = brief.read_text(encoding="utf-8")
    section = md.split("## 6. Модель даних", 1)[1].split("\n## 7.", 1)[0]
    m = re.search(r"```sql\n(.*?)```", section, flags=re.S)
    if m is None:
        raise AssertionError("SQL block not found in brief §6")
    sql = re.sub(r"--[^\n]*", "", m.group(1))
    return [stmt.strip() for stmt in sql.split(";") if stmt.strip()]
