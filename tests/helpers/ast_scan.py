"""Утиліти AST-сканування для архітектурних тестів."""

from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "fuzzhelm"


def py_files(*packages: str) -> Iterator[Path]:
    for pkg in packages:
        base = SRC / pkg
        if base.is_file():
            yield base
            continue
        yield from sorted(base.rglob("*.py"))


def parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def dotted(node: ast.AST) -> str | None:
    """`a.b.c` для Attribute/Name, інакше None."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


def rel(path: Path) -> str:
    return str(path.relative_to(SRC))
