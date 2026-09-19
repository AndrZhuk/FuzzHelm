"""Інвентар тестів за групами A–N брифінгу (§10): дослівні назви, їх наявність і фактичні кількості pytest.

Найменування: tests/helpers/brief_test_groups.py
Призначення: (1) спільна логіка для tests/arch/test_brief_test_inventory.py (кожна названа в §10 тест-функція
    визначена рівно один раз — правило contracts §0 п. 7); (2) генератор docs/report_tables/test_groups.md:
    для кожної групи — назви дослівно, чи є (grep `def <назва>(` у tests/), скільки вузлів pytest вони дають
    (параметризація), скільки додаткових тестів у файлах групи; загальні числа — з `pytest --collect-only`.
Запуск: uv run python -m tests.helpers.brief_test_groups [--out docs/report_tables/test_groups.md]
Автор: Андрій Жук, 2026.

Прив'язка файлу до групи — та сама евристика, що в scripts/export_report_tables.py (XA-13): файл належить
групі, чиїх дослівних тестів у ньому найбільше; тести файлу поза брифінгом — «додаткові» цієї групи. Файли без
жодного дослівного тесту — окремий рядок «поза групами». Сума по групах + поза групами = усі зібрані вузли.
"""

from __future__ import annotations

import argparse
import functools
import re
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from fuzzhelm.backtest.experiments_analysis import BriefGroup, parse_brief_test_groups, parse_collected

ROOT = Path(__file__).resolve().parents[2]
TESTS = ROOT / "tests"
BRIEF = ROOT / "docs" / "BRIEF.md"
_DEF_RE = re.compile(r"^\s*(?:async\s+)?def (test_\w+)\(", re.MULTILINE)


def brief_groups() -> list[BriefGroup]:
    return parse_brief_test_groups(BRIEF.read_text(encoding="utf-8"))


@functools.cache
def defined_tests() -> dict[str, list[str]]:
    """Ім'я тест-функції → місця визначення `шлях:рядок` (grep по tests/, без __pycache__)."""
    out: dict[str, list[str]] = {}
    for p in sorted(TESTS.rglob("test_*.py")):
        text = p.read_text(encoding="utf-8")
        for m in _DEF_RE.finditer(text):
            line = text.count("\n", 0, m.start()) + 1
            out.setdefault(m.group(1), []).append(f"{p.relative_to(ROOT)}:{line}")
    return out


def collect(marker: str | None) -> tuple[list[str], str]:
    """Вузли `pytest --collect-only -q` (None — типовий відбір addopts; "" — без відбору за маркерами)."""
    cmd = [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider",
           *(["-m", marker] if marker is not None else [])]
    res = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=600, check=True)
    tail = next((ln for ln in reversed(res.stdout.splitlines()) if "collected" in ln), "")
    return parse_collected(res.stdout), re.sub(r"\s+in [0-9.]+s\b", "", tail).strip()


def fn_name(nodeid: str) -> str:
    return nodeid.rsplit("::", 1)[-1].split("[", 1)[0]


def file_of(nodeid: str) -> str:
    return nodeid.split("::", 1)[0]


@dataclass(frozen=True)
class GroupRow:
    group: BriefGroup
    present: list[str]
    missing: list[str]
    named_nodes: int          # вузли pytest від дослівних назв (з параметризацією)
    files: list[str]          # файли, прив'язані до групи
    file_nodes: int           # усі вузли цих файлів

    @property
    def extra_nodes(self) -> int:
        return self.file_nodes - self.named_nodes


def group_rows(groups: list[BriefGroup], nodeids: list[str]) -> tuple[list[GroupRow], dict[str, int]]:
    by_name = Counter(fn_name(n) for n in nodeids)
    group_of = {n: g.letter for g in groups for n in g.names}
    votes: dict[str, Counter[str]] = {}
    for nid in nodeids:
        letter = group_of.get(fn_name(nid))
        if letter is not None:
            votes.setdefault(file_of(nid), Counter())[letter] += 1
    order = {g.letter: i for i, g in enumerate(groups)}
    file_group = {f: min(v, key=lambda g: (-v[g], order[g])) for f, v in votes.items()}
    per_file = Counter(file_of(n) for n in nodeids)
    rows = []
    for g in groups:
        present = [n for n in g.names if by_name[n] > 0]
        files = sorted(f for f, grp in file_group.items() if grp == g.letter)
        rows.append(GroupRow(g, present, [n for n in g.names if by_name[n] == 0],
                             sum(by_name[n] for n in present), files, sum(per_file[f] for f in files)))
    outside = {f: c for f, c in sorted(per_file.items()) if f not in file_group}
    return rows, outside


def render(groups: list[BriefGroup], defs: dict[str, list[str]], all_ids: list[str], default_ids: list[str],
           integ_ids: list[str], tails: dict[str, str], head: str) -> str:
    rows, outside = group_rows(groups, all_ids)
    per_name = Counter(fn_name(n) for n in all_ids)
    default_set = set(default_ids)
    named_total = sum(len(g.names) for g in groups)
    present_total = sum(len(r.present) for r in rows)
    named_nodes = sum(r.named_nodes for r in rows)
    md: list[str] = [
        "# Тести за групами A–N брифінгу (§10)",
        "",
        "Автор: Андрій Жук, 2026. Згенеровано `uv run python -m tests.helpers.brief_test_groups` "
        f"на `{head}`. Жодне число не введено вручну.",
        "",
        "## Підсумок",
        "",
        f"* Зібрано всього (`pytest --collect-only -q -m \"\"`): **{len(all_ids)}** вузлів ({tails['all']}).",
        f"* Типовий прогін `make test` (`uv run pytest`, відбір `addopts`: не `integration`/`live`/`slow`): "
        f"**{len(default_ids)}** ({tails['default']}).",
        f"* Інтеграційні (`-m integration`, PostgreSQL із docker-compose.test.yml): **{len(integ_ids)}** "
        f"({tails['integration']}); решта поза типовим прогоном — "
        f"{len(all_ids) - len(default_ids) - len(integ_ids)} (маркер `slow`).",
        f"* Названих у §10 тест-функцій: **{named_total}** (сума заголовків груп — "
        f"{sum(g.declared for g in groups)}; заголовок §10 — «92 кейси», див. D-02). Визначено в tests/ "
        f"(grep `def <назва>(`): **{sum(1 for g in groups for n in g.names if n in defs)}**; "
        f"зібрано pytest: **{present_total}**; вузлів від них (з параметризацією): "
        f"**{sum(r.named_nodes for r in rows)}**.",
        f"* Додаткових вузлів (не названих у брифінгу): **{len(all_ids) - named_nodes}** "
        f"(у файлах груп — {sum(r.extra_nodes for r in rows)}, у файлах поза групами — "
        f"{sum(outside.values())}).",
        "",
        "| група | назва | у заголовку §10 | названо | є (grep) | зібрано | вузлів названих | "
        "усіх вузлів у файлах групи | додаткових |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for r in rows:
        g = r.group
        md.append(f"| {g.letter} | {g.title} | {g.declared} | {len(g.names)} | "
                  f"{sum(1 for n in g.names if n in defs)} | {len(r.present)} | {r.named_nodes} | "
                  f"{r.file_nodes} | {r.extra_nodes} |")
    md.append(f"| — | поза групами | — | — | — | — | — | {sum(outside.values())} | {sum(outside.values())} |")
    md.append(f"| **Σ** | | {sum(g.declared for g in groups)} | {named_total} | "
              f"{sum(1 for g in groups for n in g.names if n in defs)} | {present_total} | "
              f"{sum(r.named_nodes for r in rows)} | {len(all_ids)} | "
              f"{len(all_ids) - sum(r.named_nodes for r in rows)} |")
    md += ["", "«Додаткових» = усі вузли файлів групи − вузли дослівних назв (евристика прив'язки файлу — "
           "див. заголовок генератора, XA-13).", ""]
    for r in rows:
        g = r.group
        md += [f"## {g.letter}. {g.title}", "",
               f"Файли групи: {', '.join(f'`{f}`' for f in r.files) or '—'}. "
               f"Дослівних: {len(r.present)}/{len(g.names)}; додаткових вузлів: {r.extra_nodes}.", "",
               "| тест (дослівно з §10) | визначено (grep) | вузлів | у `make test` |", "|---|---|---:|---|"]
        for n in g.names:
            where = "<br>".join(f"`{d}`" for d in defs.get(n, [])) or "**немає**"
            nodes = [x for x in all_ids if fn_name(x) == n]
            in_default = sum(1 for x in nodes if x in default_set)
            flag = ("так" if in_default == len(nodes) else
                    "ні (інтеграційний)" if in_default == 0 else f"{in_default}/{len(nodes)}")
            md.append(f"| `{n}` | {where} | {per_name[n]} | {flag if nodes else '—'} |")
        md.append("")
    md += ["## Файли поза групами", "",
           "Файли без жодного дослівного тесту §10 (додаткові перевірки модулів).", "",
           "| файл | вузлів |", "|---|---:|", *[f"| `{f}` | {c} |" for f, c in outside.items()], ""]
    return "\n".join(md)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", type=Path, default=ROOT / "docs" / "report_tables" / "test_groups.md")
    args = ap.parse_args(argv)
    groups, defs = brief_groups(), defined_tests()
    all_ids, t_all = collect("")
    default_ids, t_def = collect(None)
    integ_ids, t_int = collect("integration")
    head = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True,
                          check=False).stdout.strip() or "?"
    dirty = subprocess.run(["git", "status", "--porcelain", "--", "tests"], cwd=ROOT, capture_output=True,
                           text=True, check=False).stdout.strip()
    md = render(groups, defs, all_ids, default_ids, integ_ids,
                {"all": t_all, "default": t_def, "integration": t_int},
                f"HEAD {head}" + (" + незакомічені зміни tests/" if dirty else ""))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(md, encoding="utf-8")
    print(f"wrote {args.out}: {len(all_ids)} collected, {len(default_ids)} default, "
          f"{len(integ_ids)} integration")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
