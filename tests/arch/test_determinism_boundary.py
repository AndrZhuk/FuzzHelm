"""A1. Межа детермінізму: у чистих пакетах немає настінного часу, випадковості та uuid4."""

from __future__ import annotations

import ast

from tests.helpers.ast_scan import dotted, parse, py_files, rel

PURE_PACKAGES = ("core", "features", "detectors", "fuzzy", "decision", "risk", "sizing")
FORBIDDEN_CALLS = {
    "datetime.now", "datetime.utcnow", "datetime.datetime.now", "datetime.datetime.utcnow",
    "date.today", "datetime.date.today",
    "time.time", "time.time_ns", "time.monotonic", "time.perf_counter",
    "uuid4", "uuid.uuid4", "os.urandom", "secrets.token_bytes",
}
FORBIDDEN_MODULES = {"random", "secrets"}


def test_no_wallclock_in_core() -> None:
    violations: list[str] = []
    for path in py_files(*PURE_PACKAGES):
        tree = parse(path)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    if a.name.split(".")[0] in FORBIDDEN_MODULES or a.name == "fuzzhelm.infra.wallclock":
                        violations.append(f"{rel(path)}:{node.lineno} import {a.name}")
            elif isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                if mod.split(".")[0] in FORBIDDEN_MODULES or mod.startswith("fuzzhelm.infra"):
                    violations.append(f"{rel(path)}:{node.lineno} from {mod} import ...")
                if mod == "uuid" and any(a.name == "uuid4" for a in node.names):
                    violations.append(f"{rel(path)}:{node.lineno} from uuid import uuid4")
            elif isinstance(node, ast.Call):
                name = dotted(node.func)
                if name in FORBIDDEN_CALLS:
                    violations.append(f"{rel(path)}:{node.lineno} call {name}()")
            elif isinstance(node, ast.Attribute) and node.attr == "random":
                violations.append(f"{rel(path)}:{node.lineno} attribute .random ({dotted(node)})")
    assert not violations, "wall-clock/randomness in deterministic packages:\n" + "\n".join(violations)
