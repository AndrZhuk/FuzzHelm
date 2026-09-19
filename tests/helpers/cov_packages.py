"""Покриття по пакетах src/fuzzhelm і пороги брифінгу §10 (≥ 80 % загалом, ≥ 90 % fuzzy/risk/decision/sizing).

Найменування: tests/helpers/cov_packages.py
Призначення: coverage рахує fail_under лише для загального відсотка; поріг 90 % на чотири ключові пакети
    перевіряється тут — з того самого файлу даних .coverage, яким користується звіт `make cov`.
    Відсоток пакета — як у coverage: (виконані рядки + виконані гілки) / (рядки + гілки).
Запуск (після `make cov` або прогону з --cov):
    uv run python -m tests.helpers.cov_packages [--data-file .coverage] [--no-check]
Код виходу: 0 — пороги виконано (або --no-check), 1 — ні (перелік порушень у stderr).
Автор: Андрій Жук, 2026.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
KEY_PACKAGES = ("fuzzy", "risk", "decision", "sizing")
TOTAL_MIN, KEY_MIN = 80.0, 90.0


def package_of(path: str) -> str:
    rel = path.split("src/fuzzhelm/", 1)[-1]
    return rel.split("/", 1)[0] if "/" in rel else "(корінь)"


def per_package(report: dict[str, Any]) -> dict[str, list[int]]:
    """{пакет: [рядків, пропущено рядків, гілок, пропущено гілок]} із `coverage json`."""
    agg: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0, 0])
    for path, f in report["files"].items():
        s, a = f["summary"], agg[package_of(path)]
        a[0] += s["num_statements"]
        a[1] += s["missing_lines"]
        a[2] += s.get("num_branches", 0)
        a[3] += s.get("missing_branches", 0)
    return dict(agg)


def percent(a: list[int]) -> float:
    total = a[0] + a[2]
    return 100.0 * (total - a[1] - a[3]) / total if total else 100.0


def table(agg: dict[str, list[int]]) -> str:
    rows = ["| пакет | рядків | пропущено | гілок | пропущено гілок | покриття |",
            "|---|---:|---:|---:|---:|---:|"]
    tot = [0, 0, 0, 0]
    for pkg in sorted(agg):
        a = agg[pkg]
        tot = [x + y for x, y in zip(tot, a, strict=True)]
        mark = " **(ключовий)**" if pkg in KEY_PACKAGES else ""
        rows.append(f"| {pkg}{mark} | {a[0]} | {a[1]} | {a[2]} | {a[3]} | {percent(a):.1f} % |")
    rows.append(f"| **разом** | {tot[0]} | {tot[1]} | {tot[2]} | {tot[3]} | **{percent(tot):.2f} %** |")
    return "\n".join(rows)


def violations(agg: dict[str, list[int]]) -> list[str]:
    tot = [sum(a[i] for a in agg.values()) for i in range(4)]
    out = [] if percent(tot) >= TOTAL_MIN else [f"total {percent(tot):.2f} % < {TOTAL_MIN} %"]
    out += [f"{p} {percent(agg[p]):.2f} % < {KEY_MIN} %" for p in KEY_PACKAGES
            if p not in agg or percent(agg[p]) < KEY_MIN]
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--data-file", type=Path, default=ROOT / ".coverage")
    ap.add_argument("--no-check", action="store_true", help="print the table only")
    args = ap.parse_args(argv)
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "cov.json"
        # --fail-under=0: пороги перевіряє цей скрипт (інакше coverage json сам виходить з кодом 2)
        subprocess.run([sys.executable, "-m", "coverage", "json", "--quiet", "--fail-under=0",
                        "--data-file", str(args.data_file), "-o", str(out)], cwd=ROOT, check=True)
        agg = per_package(json.loads(out.read_text(encoding="utf-8")))
    print(table(agg))
    bad = [] if args.no_check else violations(agg)
    for v in bad:
        print(f"coverage threshold violated: {v}", file=sys.stderr)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
