"""A2. Межа типів Decimal↔float проходить рівно через дві точки конвертації."""

from __future__ import annotations

import ast

from tests.helpers.ast_scan import parse, py_files, rel

CHOKE_POINTS = {"features/convert.py", "sizing/convert.py"}
FLOAT_DOMAIN = ("features", "detectors", "fuzzy", "decision", "regimes")
DECIMAL_DOMAIN = ("core", "sizing", "risk", "execution", "backtest")
ALLOWED_DECIMAL_CTORS = {"core/money.py"}  # dec() — рантайм-конструктор, що відкидає float


def _is_literal(arg: ast.expr) -> bool:
    if isinstance(arg, ast.Constant):
        return isinstance(arg.value, str | int) and not isinstance(arg.value, bool)
    if isinstance(arg, ast.UnaryOp) and isinstance(arg.op, ast.USub):
        return _is_literal(arg.operand)
    return False


def test_decimal_float_boundary_is_single_choke_point() -> None:
    violations: list[str] = []
    for path in py_files(*FLOAT_DOMAIN):
        r = rel(path)
        if r in CHOKE_POINTS:
            continue
        for node in ast.walk(parse(path)):
            if isinstance(node, ast.ImportFrom) and node.module == "decimal":
                violations.append(f"{r}:{node.lineno} float-domain module imports decimal")
            if isinstance(node, ast.Import) and any(a.name == "decimal" for a in node.names):
                violations.append(f"{r}:{node.lineno} float-domain module imports decimal")
    for path in py_files(*DECIMAL_DOMAIN):
        r = rel(path)
        if r in CHOKE_POINTS:
            continue
        for node in ast.walk(parse(path)):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
                continue
            if node.func.id == "float" and node.args and not _is_literal(node.args[0]):
                violations.append(f"{r}:{node.lineno} float(<expr>) — use features.convert.to_float")
            if (node.func.id == "Decimal" and node.args and not _is_literal(node.args[0])
                    and r not in ALLOWED_DECIMAL_CTORS):
                violations.append(f"{r}:{node.lineno} Decimal(<expr>) — use core.money.dec / sizing.convert")
    assert not violations, "Decimal/float boundary crossed outside choke points:\n" + "\n".join(violations)
