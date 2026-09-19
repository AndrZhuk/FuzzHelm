"""Зведення таблиць звіту (брифінг §12 фаза 11 «усі таблиці з scripts/export_report_tables.py», §14, §2).

Найменування: export_report_tables.py
Призначення: зібрати файли результатів експериментних скриптів (JSON/MD у --results-dir), рядки БД (--db:
    паспорти й метрики прогонів, прогони сітки для DSR, лічильники) і готові звіти docs/figures/* у
    docs/report_tables/*.md (+ .csv): 17 метрик + PSR/DSR, IS/OOS по фолдах, Парето, чутливість, три моделі
    витрат, ablation, VaR/Купець, ціна гістерезису, Амдал, патологічні сесії, ROC-AUC MLP, калібрування МФ,
    групи тестів A–N з ФАКТИЧНИМИ кількостями (pytest --collect-only), таблиця трасування §2 (11 рядків) і
    перелік усіх розходжень. Відсутній вхід → явний маркер <<TBD:експеримент>>, жодних вигаданих чисел.
Автор: Андрій Жук, 2026.

Запуск (димовий): uv run python scripts/export_report_tables.py --smoke --results-dir artifacts/tmp --db
Повний (наступна хвиля): uv run python scripts/export_report_tables.py --db \
    --results-dir docs/report_tables/raw --results-dir <каталог виводу exp_search> --out docs/report_tables
Вивід (--out): index.md, metrics, wf_folds, pareto, sensitivity, amdahl, cost_models, ablation, var_kupiec,
    hysteresis, pathological, mlp_rocauc, calibration, test_groups, traceability, deviations (.md + .csv).
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import re
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from fuzzhelm.backtest import experiments_analysis as ea
from fuzzhelm.backtest.metrics import METRIC_NAMES
from fuzzhelm.config import ROOT

MINE = ("cost_models", "ablation", "var_backtest", "hysteresis_cost", "plot_equity", "plot_var")
# результати exp_search (пишеться паралельно): ключ `experiment` у JSON або підрядок імені файлу
SEARCH_KINDS: dict[str, tuple[str, ...]] = {
    "wf_folds": ("walkforward", "walk_forward", "wf_folds", "wf"),
    "pareto": ("pareto",),
    "sensitivity": ("sensitivity",),
    "amdahl": ("amdahl", "speedup", "bench"),
    "grid": ("grid",),
}
SEARCH_TITLES = {
    "wf_folds": "Walk-forward: IS/OOS по фолдах",
    "pareto": "Парето-фронт (SR_OOS ↑, MaxDD_OOS ↓, Turnover ↓)",
    "sensitivity": "Аналіз чутливості до 8 параметрів",
    "amdahl": "Прискорення S(p) проти межі Амдала",
}


# ====================================================================== пошук результатів


def classify(path: Path, data: Any) -> str | None:
    exp = None
    if isinstance(data, dict):
        exp = data.get("experiment") or (data.get("passport") or {}).get("experiment")
    names = [str(exp).lower()] if exp else []
    names.append(path.stem.lower())
    for n in names:
        if n in MINE:
            return n
        for kind, keys in SEARCH_KINDS.items():
            if any(k == n or k in re.split(r"[^a-z0-9]+", n) or n.startswith(k) for k in keys):
                return kind
    return None


def discover(dirs: Sequence[Path], explicit: Sequence[str]) -> dict[str, list[dict[str, Any]]]:
    """{вид: [{path, data, md}]} — JSON-файли в каталогах (рекурсивно) + явні --input KIND=PATH."""
    found: dict[str, list[dict[str, Any]]] = {}

    def add(kind: str, path: Path, data: Any) -> None:
        md = path.with_suffix(".md")
        found.setdefault(kind, []).append({"path": path, "data": data, "md": md if md.exists() else None})

    for spec in explicit:
        kind, _, p = spec.partition("=")
        path = Path(p)
        add(kind, path, json.loads(path.read_text(encoding="utf-8")) if path.suffix == ".json" else None)
    for d in dirs:
        if not d.exists():
            continue
        for path in sorted(d.rglob("*.json")):
            if path.name.endswith((".timing.json", "_folds.json")):
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            found_kind = classify(path, data)
            if found_kind is not None:
                add(found_kind, path, data)
    return found


def passport_of(data: Any) -> dict[str, Any]:
    """Паспорт виводу: `passport` (exp_analysis) або `provenance` (exp_search, набір — у `dataset`)."""
    if not isinstance(data, dict):
        return {}
    p = dict(data.get("passport") or data.get("provenance") or {})
    ds = p.get("dataset")
    if isinstance(ds, dict):
        p.setdefault("dataset_hash", ds.get("dataset_hash"))
        p.setdefault("symbol", ds.get("symbol"))
    return p


def passport_line(data: Any, path: Path) -> str:
    p = passport_of(data)
    bits = [f"джерело `{rel(path)}`"]
    for k in ("git_sha", "git_dirty", "dataset_hash", "symbol", "run_id"):
        if p.get(k) is not None:
            v = p[k]
            bits.append(f"{k} `{v[:16] + '…' if isinstance(v, str) and len(v) > 20 else v}`")
    if p.get("command"):
        bits.append(f"команда `{p['command']}`")
    return "; ".join(bits)


def rel(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(ROOT))
    except ValueError:
        return str(path)


# ====================================================================== БД (лише читання)


async def db_facts(database_url: str | None, run_ids: Sequence[str]) -> dict[str, Any]:  # pragma: no cover
    from sqlalchemy import text  # noqa: PLC0415

    from fuzzhelm.storage.models import APP_ROLE  # noqa: PLC0415
    from fuzzhelm.storage.session import make_engine, session_factory, session_scope  # noqa: PLC0415

    engine = make_engine(database_url, role=APP_ROLE, null_pool=True)
    out: dict[str, Any] = {}
    try:
        async with session_scope(session_factory(engine)) as s:
            async def rows(q: str, **kw: Any) -> list[Any]:
                return list((await s.execute(text(q), kw)).mappings().all())

            out["runs_by_kind"] = [dict(r) for r in await rows(
                "SELECT kind, status, count(*) AS n FROM run GROUP BY 1, 2 ORDER BY 1, 2")]
            # прогони фолдів walk-forward exp_search теж kind = backtest (теги run_metric wf_fold/wf_oos) —
            # стовпцем «типового» бектесту їх не беремо (інакше найновішим виявився б фолд)
            out["backtests"] = [dict(r) for r in await rows(
                "SELECT id::text AS id, git_sha, seed, engine, encode(config_hash, 'hex') AS config_hash, "
                "encode(dataset_hash, 'hex') AS dataset_hash, encode(equity_hash, 'hex') AS equity_hash, "
                "started_at::text AS started_at FROM run r WHERE kind = 'backtest' AND status = 'DONE' "
                "AND NOT EXISTS (SELECT 1 FROM run_metric m WHERE m.run_id = r.id AND m.name = 'wf_fold') "
                "ORDER BY started_at DESC, id")]
            out["wf_fold_runs"] = (await rows(
                "SELECT count(DISTINCT r.id) AS n FROM run r JOIN run_metric m ON m.run_id = r.id "
                "AND m.name = 'wf_fold' WHERE r.status = 'DONE'"))[0]["n"]
            ids = list(run_ids) or [b["id"] for b in out["backtests"][:1]]
            out["metrics"] = {}
            for rid in ids:
                m = await rows("SELECT name, value FROM run_metric WHERE run_id = CAST(:r AS uuid)", r=rid)
                meta = await rows("SELECT id::text AS id, kind, status, git_sha, seed, engine, "
                                  "started_at::text AS started_at, "
                                  "encode(dataset_hash, 'hex') AS dataset_hash, "
                                  "encode(config_hash, 'hex') AS config_hash FROM run "
                                  "WHERE id = CAST(:r AS uuid)",
                                  r=rid)
                ev = await rows("SELECT count(*) AS n FROM risk_event WHERE run_id = CAST(:r AS uuid)", r=rid)
                out["metrics"][rid] = {"run": dict(meta[0]) if meta else None,
                                       "values": {r["name"]: r["value"] for r in m},
                                       "risk_events": ev[0]["n"] if ev else None}
            out["grid_srs"] = [dict(r) for r in await rows(
                "SELECT encode(r.dataset_hash, 'hex') AS dataset_hash, r.git_sha, r.seed, r.engine, "
                "r.started_at::text AS started_at, m.value AS sr_period FROM run r "
                "LEFT JOIN run_metric m ON m.run_id = r.id AND m.name = 'sr_period' "
                "WHERE r.kind = 'grid_cell' AND r.status = 'DONE' ORDER BY r.started_at, r.id")]
            win = json.loads((ROOT / "data" / "dataset_window.json").read_text(encoding="utf-8"))["window"]
            out["candles"] = [dict(r) for r in await rows(
                "SELECT i.symbol_canon AS symbol, c.tf, count(*) AS n, count(*) FILTER (WHERE c.open_time >= "
                "to_timestamp(:lo / 1000.0) AND c.open_time < to_timestamp(:hi / 1000.0) AND c.is_closed) "
                "AS n_window FROM candle c JOIN instrument i ON i.id = c.instrument_id GROUP BY 1, 2 "
                "ORDER BY 1, 2", lo=win["start_ms"], hi=win["end_ms"])]
            out["tables"] = (await rows(
                "SELECT count(*) AS n FROM information_schema.tables WHERE table_schema = 'public' "
                "AND table_type = 'BASE TABLE' AND table_name <> 'alembic_version'"))[0]["n"]
    finally:
        await engine.dispose()
    return out


# ====================================================================== pytest --collect-only


def collect(marker: str | None) -> tuple[list[str], str]:
    """Вузли `pytest --collect-only -q` з маркер-виразом (None — типовий відбір addopts pyproject)."""
    cmd = [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider",
           *(["-m", marker] if marker is not None else [])]
    res = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=300, check=False)
    tail = next((ln for ln in reversed(res.stdout.splitlines()) if "collected" in ln), "")
    tail = re.sub(r"\s+in [0-9.]+s\b", "", tail).strip()        # час збору — не результат (детермінізм)
    return ea.parse_collected(res.stdout), tail


# ====================================================================== будівники розділів


class Report:
    def __init__(self, out: Path) -> None:
        self.out = out
        self.index: list[tuple[str, str, int]] = []      # (файл, заголовок, скільки TBD)

    def write(self, stem: str, title: str, body: str, rows: Sequence[dict[str, Any]] | None = None) -> None:
        md = (f"# {title}\n\nЗгенеровано `scripts/export_report_tables.py`. Автор: Андрій Жук, 2026.\n\n"
              f"{body}\n")
        self.out.mkdir(parents=True, exist_ok=True)
        (self.out / f"{stem}.md").write_text(md, encoding="utf-8")
        if rows:
            keys: list[str] = []
            for r in rows:
                keys += [k for k in r if k not in keys]
            with (self.out / f"{stem}.csv").open("w", encoding="utf-8", newline="") as fh:
                w = csv.DictWriter(fh, fieldnames=keys)
                w.writeheader()
                for r in rows:
                    w.writerow({k: _cell(r.get(k)) for k in keys})
        self.index.append((f"{stem}.md", title, md.count("<<TBD:")))


def _cell(v: Any) -> Any:
    s = ea.sanitize(v)
    if isinstance(s, dict | list):
        return json.dumps(s, ensure_ascii=False)
    return "" if s is None else s


def embed_md(entry: dict[str, Any]) -> str:
    """Таблиці з markdown-виводу експерименту (або рядки JSON `rows`/`summary`), з рядком паспорта."""
    parts = [passport_line(entry["data"], entry["path"]), ""]
    if entry["md"] is not None:
        tables = ea.extract_md_tables(entry["md"].read_text(encoding="utf-8"))
        if tables:
            return "\n".join([*parts, *[t + "\n" for t in tables]])
    data = entry["data"]
    rows = None
    if isinstance(data, dict):
        rows = next((data[k] for k in ("rows", "summary", "folds", "cells", "results")
                     if isinstance(data.get(k), list) and data[k] and isinstance(data[k][0], dict)), None)
    elif isinstance(data, list) and data and isinstance(data[0], dict):
        rows = data
    if rows:
        keys = list(rows[0])[:14]
        return "\n".join([*parts, ea.md_table(keys, [[r.get(k) for k in keys] for r in rows]), ""])
    marker = ea.tbd("schema:" + rel(entry["path"]))
    return "\n".join([*parts, f"Файл знайдено, але схема невідома — {marker}", ""])


def section_search(rep: Report, found: dict[str, list[dict[str, Any]]], kind: str) -> None:
    entries = found.get(kind, [])
    if not entries and kind == "pareto" and found.get("grid"):
        # фронт Парето і робоча точка — розділ виводу сітки exp_search
        parts = []
        for e in found["grid"]:
            tables = (ea.extract_md_tables(e["md"].read_text(encoding="utf-8"), "Парето") if e["md"] else [])
            parts += [passport_line(e["data"], e["path"]), "",
                      *([t + "\n" for t in tables] or [ea.tbd("pareto_tables")]), ""]
            choice = (e["data"] or {}).get("choice") or {}
            if choice.get("params"):
                parts += [f"Робоча точка (`choice.params`): `{json.dumps(choice['params'])}`, правило "
                          f"`{choice.get('rule')}`, dd_cap {choice.get('dd_cap')}.", ""]
        rep.write(kind, SEARCH_TITLES[kind], "\n".join(parts))
        return
    if not entries:
        rep.write(kind, SEARCH_TITLES[kind], f"{ea.tbd(kind)} — результатів цього експерименту (exp_search) "
                                            "у --results-dir не знайдено.")
        return
    rep.write(kind, SEARCH_TITLES[kind], "\n".join(embed_md(e) for e in entries))


def section_metrics(rep: Report, found: dict[str, list[dict[str, Any]]], db: dict[str, Any] | None) -> None:
    # (заголовок, метрики, походження, dataset_hash, git_sha, рушій)
    cols: list[tuple[str, dict[str, Any], str, str, str | None, str | None]] = []
    if db:
        for rid, m in db.get("metrics", {}).items():
            run = m["run"] or {}
            dh = run.get("dataset_hash", "")
            cols.append((f"прогін `{rid[:8]}…` (БД, {run.get('kind', '?')})", m["values"],
                         f"run {rid}, kind {run.get('kind')}, git_sha `{str(run.get('git_sha'))[:12]}…`, "
                         f"dataset_hash `{dh[:16]}…`", dh, run.get("git_sha"), run.get("engine")))
    for e in found.get("var_backtest", []):
        ref = (e["data"] or {}).get("reference_metrics", {})
        if ref.get("full_engine_metrics"):
            p = passport_of(e["data"])
            cols.append((f"обрана конфігурація, повне вікно ({p.get('symbol', '?')}, {p.get('source', '?')}, "
                         f"{p.get('n_bars', '?')} барів)",
                         ref["full_engine_metrics"], passport_line(e["data"], e["path"]),
                         str(p.get("dataset_hash") or ""), p.get("git_sha"), p.get("engine") or "mamdani"))
    if not cols:
        rep.write("metrics", "17 метрик бектесту + PSR/DSR",
                  f"{ea.tbd('metrics_reference_run')} — немає ні --run-id/--db, ні результату var_backtest.")
        return
    heads = ["метрика", *[c[0] for c in cols]]
    rows_md, rows_csv = [], []
    trials = [trial_srs(found, db, c[3], git_sha=c[4], engine=c[5]) for c in cols]
    dsr_info = [ea.dsr_for(c[1], t[0]) for c, t in zip(cols, trials, strict=True)]
    for key in (*METRIC_NAMES, "psr", "dsr"):
        vals = [(d["dsr"] if d["dsr"] is not None else ea.tbd("grid_108_dsr")) if key == "dsr"
                else c[1].get(key) for c, d in zip(cols, dsr_info, strict=True)]
        rows_md.append([f"{ea.METRIC_LABELS_UK.get(key, key)} (`{key}`)", *vals])
        rows_csv.append({"metric": key, **{c[0]: v for c, v in zip(cols, vals, strict=True)}})
    notes = [f"* {c[0]}: {c[2]}" for c in cols]
    dsr_notes = [f"* DSR «{c[0]}»: N = {d['n_trials']} (скінченних Шарпів {d['n_finite']}; {t[1]}), "
                 f"SR₀ = {ea.fmt(d['sr0'])} (Шарпи за період)"
                 + (f"; не обчислено: {d['reason']}" if d["reason"] else "") + "."
                 for c, d, t in zip(cols, dsr_info, trials, strict=True)]
    grid_dsr = []
    for e in found.get("grid", []):
        tables = ea.extract_md_tables(e["md"].read_text(encoding="utf-8"), "DSR") if e["md"] else []
        if tables:
            grid_dsr += ["", "DSR обраної точки сітки (вивід exp_search):", "",
                         passport_line(e["data"], e["path"]), "", *tables]
    body = "\n".join(["Стовпці:", *notes, "", ea.md_table(heads, rows_md), "", *dsr_notes,
                      "", "Якщо PSR < 0.95 — перевага статистично не встановлена (§5.15). DSR стовпця — "
                      "проти Шарпів прогонів сітки на ТОМУ САМОМУ наборі (dataset_hash), однієї сітки "
                      "(git_sha, seed, рушій).", *grid_dsr, ""])
    rep.write("metrics", "17 метрик бектесту + PSR/DSR", body, rows_csv)


def trial_srs(found: dict[str, list[dict[str, Any]]], db: dict[str, Any] | None, dataset_hash: str, *,
              git_sha: str | None = None, engine: str | None = None) -> tuple[list[float | None], str]:
    """Шарпи (за період) N прогонів сітки НА ТОМУ САМОМУ наборі (dataset_hash) — вхід DSR.

    Джерела: БД (kind = grid_cell, run_metric.sr_period; одна сітка = (git_sha, seed, engine) —
    ea.pick_trial_group) або вивід сітки exp_search (усі клітинки, `full_sr_period`; невизначений Шарп
    лишається в N). Інший набір → порожньо (DSR тоді не рахується).
    """
    if db:
        srs, grp = ea.pick_trial_group(db.get("grid_srs", []), dataset_hash, git_sha=git_sha, engine=engine)
        if grp is not None:
            others = f"; інших сіток на цьому наборі: {grp['groups'] - 1}" if grp["groups"] > 1 else ""
            return srs, (f"БД: {grp['n']} прогонів kind = grid_cell на тому самому dataset_hash, сітка "
                         f"git_sha `{str(grp['git_sha'])[:12]}…`, seed {grp['seed']}, рушій {grp['engine']}"
                         + others)
    for e in found.get("grid", []):
        p = passport_of(e["data"])
        if p.get("dataset_hash") == dataset_hash and (engine is None or p.get("engine") in (None, engine)):
            cells = [c for c in (e["data"].get("cells") or []) if isinstance(c, dict)]
            if cells and all("full_sr_period" in c for c in cells):
                return ([c.get("full_sr_period") for c in cells],
                        f"`{rel(e['path'])}`: {len(cells)} клітинок сітки, повне вікно")
    return [], f"прогонів сітки на цьому наборі немає — {ea.tbd('grid_108_same_dataset')}"


def _dicts(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        for v in data.values():
            if isinstance(v, list) and v and isinstance(v[0], dict):
                return v
    return []


def section_mine(rep: Report, found: dict[str, list[dict[str, Any]]], kind: str, stem: str, title: str,
                 cols: Sequence[tuple[str, str]]) -> None:
    entries = [e for e in found.get(kind, []) if e["path"].stem == kind]
    if not entries:
        rep.write(stem, title, f"{ea.tbd(kind)} — вивід `scripts/{kind}.py` у --results-dir не знайдено.")
        return
    parts, csv_rows = [], []
    for e in entries:
        summ = e["data"].get("summary") or []
        p = e["data"].get("passport") or {}
        how = (f"вибір {p.get('selection', '?')}" + (f" ({p.get('is_rule')})" if p.get("is_rule") else "")
               + f", контур ризику {p.get('risk_loop', '?')}" + (", smoke" if p.get("smoke") else ""))
        parts += [f"### {p.get('symbol', '?')}: {how}", "", passport_line(e["data"], e["path"]), "",
                  *ea.selection_caveat(p)]
        for scope in ("oos", "full"):
            rs = [r for r in summ if r.get("scope") == scope]
            if rs:
                parts += [f"**{ea.SCOPE_TITLES[scope]}**", "",
                          ea.md_table(["варіант", *[h for _, h in cols]],
                                      [[r.get("label"), *[r.get(k) for k, _ in cols]] for r in rs]), ""]
                csv_rows += [{"source": rel(e["path"]), **r} for r in rs]
        if kind == "cost_models":
            for scope, comp in (e["data"].get("overstatement") or {}).items():
                c = comp.get("zero_vs_full", {}).get("sharpe", {})
                parts.append(f"* {ea.SCOPE_TITLES.get(scope, scope)}: Шарп zero − full = "
                             f"{ea.fmt(c.get('diff'))}, відношення {ea.fmt(c.get('ratio'))}, зміна знака: "
                             f"{ea.fmt(c.get('sign_flip'))}.")
            parts.append("")
    rep.write(stem, title, "\n".join(parts), csv_rows)


def section_var(rep: Report, found: dict[str, list[dict[str, Any]]]) -> None:
    entries = [e for e in found.get("var_backtest", []) if e["path"].stem == "var_backtest"]
    title = "VaR₉₅/CVaR₉₅: історичний проти параметричного, тест Купця"
    if not entries:
        rep.write("var_kupiec", title,
                  f"{ea.tbd('var_backtest')} — вивід `scripts/var_backtest.py` не знайдено.")
        return
    parts, csv_rows = [], []
    for e in entries:
        d = e["data"]
        pp = passport_of(d)
        parts += [f"### {pp.get('symbol') or pp.get('run_id', '?')}"
                  + (f" (W = {pp.get('var_window')}, для барів у позиції W = {pp.get('var_window_active')})"
                     if pp.get("var_window") else ""), "", passport_line(d, e["path"]), "",
                  *ea.selection_caveat(pp)]
        rows = []
        for b in d.get("blocks", []):
            st, roll = b.get("static") or {}, b.get("rolling") or {}
            for meth, name in (("hist", "історичний"), ("param", "параметричний")):
                k = roll.get(meth) or {}
                row = {"scope": b["scope"], "subset": {"all": "усі бари", "active": "бари в позиції"}.get(
                           b["subset"], b["subset"]), "method": name, "n": b["n"],
                       "zero_share": b.get("zero_share"), "kurt": (b.get("moments") or {}).get("kurt"),
                       "var": st.get(f"{meth}_var"), "cvar": st.get("hist_cvar") if meth == "hist" else None,
                       "forecasts": k.get("n"), "breaches": k.get("breaches"), "expected": k.get("expected"),
                       "lr": k.get("lr"),
                       "decision": ("ВІДКИНУТО" if k.get("reject") else "не відкинуто") if k else
                       f"неможливо ({b.get('reason', '')})"}
                rows.append(row)
        parts += [ea.md_table(["область", "підмножина", "метод", "n", "частка r = 0", "куртозис", "VaR₉₅",
                               "CVaR₉₅", "прогнозів", "пробоїв", "очікувано", "LR", "рішення (χ²₁ = 3.841)"],
                              [list(r.values()) for r in rows]), ""]
        for key, ls in (d.get("conclusion") or {}).items():
            parts += [f"**{key}.** " + " ".join(ls), ""]
        csv_rows += [{"source": rel(e["path"]), **r} for r in rows]
    rep.write("var_kupiec", title, "\n".join(parts), csv_rows)


def section_hysteresis(rep: Report, found: dict[str, list[dict[str, Any]]]) -> None:
    entries = [e for e in found.get("hysteresis_cost", []) if e["path"].stem == "hysteresis_cost"]
    title = "Ціна відсутності гістерезису (виміряно) проти твердження §5.9"
    if not entries:
        rep.write("hysteresis", title, f"{ea.tbd('hysteresis_cost')} — вивід `scripts/hysteresis_cost.py` "
                                       "не знайдено.")
        return
    parts, csv_rows = [], []
    for e in entries:
        d = e["data"]
        p = d.get("passport") or {}
        parts += [f"### {p.get('symbol', '?')}, контур ризику: {p.get('risk_loop', '?')}", "",
                  passport_line(d, e["path"]), ""]
        rows = []
        for scope, m in (d.get("measured") or {}).items():
            w, wo = m.get("with", {}), m.get("without", {})
            rows.append({"scope": scope, "fees_pct_day_with": w.get("fees_per_day_pct"),
                         "fees_pct_day_without": wo.get("fees_per_day_pct"),
                         "delta_pct_day": (wo.get("fees_per_day_pct") or 0) - (w.get("fees_per_day_pct") or 0)
                         if w.get("fees_per_day_pct") is not None and wo.get("fees_per_day_pct") is not None
                         else None,
                         "fills_with": w.get("n_fills"), "fills_without": wo.get("n_fills"),
                         "fills_per_bar_without": wo.get("fills_per_bar"),
                         "notional_over_e0": wo.get("notional_per_fill_over_equity"),
                         "brief_claim_pct_day": d.get("brief_claim_pct_per_day")})
        parts += [ea.md_table(["область", "комісії %E₀/добу з петлею", "без петлі", "різниця", "виконань з",
                               "виконань без", "виконань/бар без", "номінал/E₀", "твердження §5.9, %/добу"],
                              [list(r.values()) for r in rows]), ""]
        csv_rows += [{"source": rel(e["path"]), "risk_loop": p.get("risk_loop"), **r} for r in rows]
    rep.write("hysteresis", title, "\n".join(parts), csv_rows)


def section_doc_tables(rep: Report, stem: str, title: str, src: str, heading: str | None = None) -> None:
    path = ROOT / src
    if not path.exists():
        rep.write(stem, title, f"{ea.tbd(stem)} — `{src}` відсутній.")
        return
    tables = ea.extract_md_tables(path.read_text(encoding="utf-8"), heading)
    if not tables:
        rep.write(stem, title, f"{ea.tbd(stem)} — у `{src}` таблиць не знайдено.")
        return
    rep.write(stem, title, f"Джерело: `{src}` (таблиці перенесено дослівно).\n\n" + "\n\n".join(tables))


def section_tests(rep: Report, no_collect: bool, collect_file: Path | None) -> dict[str, Any]:
    brief = (ROOT / "docs" / "BRIEF.md").read_text(encoding="utf-8")
    groups = ea.parse_brief_test_groups(brief)
    if collect_file is not None:
        all_ids = ea.parse_collected(collect_file.read_text(encoding="utf-8"))
        integ: list[str] = []
        default_ids: list[str] | None = None
        tail_all, tail_int, tail_def = f"з файлу `{collect_file}`", "—", "—"
    elif no_collect:
        rep.write("test_groups_summary", "Тести за групами A–N брифінгу (§10), зведення", f"{ea.tbd('pytest_collect')}")
        return {"total": None}
    else:
        all_ids, tail_all = collect("")
        integ, tail_int = collect("integration")
        default_ids, tail_def = collect(None)
    rows = ea.test_group_rows(groups, all_ids)
    integ_set = set(integ)
    total_named = sum(len(g.names) for g in groups)
    present = sum(r["named_present"] for r in rows)
    body = [
        f"Зібрано ({'`pytest --collect-only -q -m \"\"`' if collect_file is None else 'збережений вивід'}): "
        f"**{len(all_ids)}** вузлів ({tail_all}); із них "
        f"інтеграційних (маркер `integration`): {len(integ_set)} ({tail_int}); у типовому прогоні "
        "(`uv run pytest`, відбір `addopts` із pyproject): "
        + (f"{len(default_ids)} ({tail_def})." if default_ids is not None
           else f"{ea.tbd('pytest_default')}."),
        f"Названих у §10 брифінгу тест-функцій: {total_named} (сума заголовків груп: "
        f"{sum(g.declared for g in groups)}; заголовок §10 — «92 кейси», D-02); знайдено дослівно: "
        f"**{present}**.",
        "",
        ea.md_table(["група", "назва", "заявлено в заголовку", "названо в §10", "знайдено дослівно",
                     "вузлів названих (з параметризацією)", "усіх вузлів у файлах групи*"],
                    [[r["group"], r["title"], r["declared"], r["named"], r["named_present"], r["named_items"],
                      r["file_items"]] for r in rows]),
        "",
        "\\* файл належить групі, чиїх дослівних тестів у ньому найбільше (евристика; решта тестів файлу — "
        "додаткові перевірки тієї самої групи).",
        "",
        "Відсутні дослівні назви:",
        "",
        *[f"* {r['group']}: " + ", ".join(f"`{n}`" for n in r["missing"]) for r in rows if r["missing"]],
        *(["* немає"] if not any(r["missing"] for r in rows) else []),
        "",
    ]
    rep.write("test_groups_summary", "Тести за групами A–N брифінгу (§10), зведення", "\n".join(body),
              [{k: (", ".join(v) if isinstance(v, list) else v) for k, v in r.items()} for r in rows])
    return {"total": len(all_ids), "integration": len(integ_set), "named": total_named, "present": present,
            "ids": all_ids, "default": None if default_ids is None else len(default_ids)}


def section_traceability(rep: Report, found: dict[str, list[dict[str, Any]]], db: dict[str, Any] | None,
                         tests: dict[str, Any], cov_file: Path | None) -> None:
    from fuzzhelm.config import load_yaml  # noqa: PLC0415

    rows = []
    ids = set(ea.test_function_name(n) for n in tests.get("ids") or [])
    for no, frag, modules, cands in ea.TRACE_ROWS:
        mods = [m for m in modules if (ROOT / m).exists()]
        miss_mods = [m for m in modules if not (ROOT / m).exists()]
        arts = [f"`{c}`" for c in cands if (ROOT / c).exists()]
        extra: list[str] = []
        if no == 2:
            extra.append("живий testnet-ордер — не виконано: немає testnet-ключів; крок [ЛЮДИНА] "
                         "(`scripts/testnet_one_order.py --confirm` зі своїми ключами в `.env`, EXE-07)")
        if no == 3 and db:
            for rid, m in db.get("metrics", {}).items():
                extra.append(f"`risk_event` прогону `{rid[:8]}…`: {m.get('risk_events')} рядків")
            extra.append(f"6 правил: {len(list((ROOT / 'src/fuzzhelm/risk/rules').glob('[!_]*.py')))} файлів")
        if no == 4:
            extra += ([f"`candle` {c['symbol']} {c['tf']}: {c['n_window']} закритих у вікні "
                       f"`data/dataset_window.json` (усього {c['n']})" for c in db.get("candles", [])] if db
                      else [ea.tbd("db_candle_count")])
        if no == 5:
            n = len(list((ROOT / "fixtures/ws/pathological").glob("*.jsonl.gz")))
            extra.append(f"{n} патологічних сесій")
        if no == 7:
            n_mig = len(list((ROOT / "alembic/versions").glob("0*.py")))
            extra.append(f"{n_mig} ревізій Alembic")
            extra.append(f"таблиць у схемі public (без alembic_version): {db['tables']}" if db
                         else ea.tbd("db_table_count"))
        if no == 8:
            extra.append(f"{len(METRIC_NAMES)} метрик (`backtest.metrics.METRIC_NAMES`)")
            wf = found.get("wf_folds", [])
            extra += [f"`{rel(e['path'])}`" for e in wf] or [ea.tbd("walkforward_6_folds")]
            if db:
                extra += [f"прогін `{b['id']}` (backtest, DONE)" for b in db.get("backtests", [])[:3]]
                extra.append("прогонів фолдів walk-forward у БД (run_metric `wf_fold`): "
                             f"{db.get('wf_fold_runs')}")
        if no == 9:
            rules = load_yaml("rules_mamdani").get("rules", [])
            extra.append(f"{len(rules)} правил у `config/rules_mamdani.yaml`")
            extra.append("CRUD правил через UI — не виконано: Vue-панель — окремий етап (D-06); "
                         "API `/strategies` з валідацією і версіонуванням уже є")
        if no == 10:
            for t in ("test_risk_chain_never_increases_exposure", "test_risk_fsm_transition_table_is_total"):
                if tests.get("total") is None:
                    extra.append(f"`{t}` — {ea.tbd('pytest_collect')}")
                else:
                    extra.append(f"`{t}` {'є' if t in ids else ea.tbd(t)}")
            fsm = ROOT / "docs/diagrams/risk_fsm.puml"
            extra.append(f"`{rel(fsm)}` (згенеровано з `risk/state.py::TRANSITIONS`)" if fsm.exists()
                         else ea.tbd("statechart_diagram"))
        if no == 11:
            if tests.get("total") is not None:
                extra.append(f"{tests['total']} тест-вузлів ({tests['integration']} інтеграційних, у "
                             f"типовому прогоні {tests.get('default')}); дослівних назв §10: "
                             f"{tests['present']} з {tests['named']}")
            extra.append(f"`{rel(cov_file)}`" if cov_file and cov_file.exists() else ea.tbd("pytest_cov"))
        conf = "; ".join([*arts, *extra]) or ea.tbd(f"trace_row_{no}")
        rows.append({"no": no, "fragment": f"«{frag}»", "modules": ", ".join(f"`{m}`" for m in mods)
                     + (f" (відсутні: {', '.join(miss_mods)})" if miss_mods else ""),
                     "brief_claim": ea.TRACE_BRIEF_CLAIMS[no], "artifact": conf})
    body = ("Фрагменти — дослівно з індивідуального завдання (брифінг §2); «підтвердження» — лише наявні "
            "файли, рядки БД і результати експериментів; чого немає — маркер TBD.\n\n"
            + ea.md_table(["№", "фрагмент завдання", "модулі", "що заявлено в брифінгу", "чим підтверджено"],
                          [list(r.values()) for r in rows]))
    rep.write("traceability", "Таблиця трасування індивідуального завдання (брифінг §2)", body, rows)


def section_deviations(rep: Report) -> None:
    items = ea.parse_deviation_titles((ROOT / "docs/deviations.md").read_text(encoding="utf-8"),
                                      "docs/deviations.md")
    for p in sorted((ROOT / "docs/deviations.d").glob("*.md")):
        items += ea.parse_deviation_titles(p.read_text(encoding="utf-8"), rel(p))
    body = f"Усього розходжень з ідентифікатором: **{len(items)}**.\n\n" + ea.md_table(
        ["ID", "назва", "файл"], [[i["id"], i["title"], f"`{i['source']}`"] for i in items])
    rep.write("deviations", "Перелік розходжень «спека ↔ реальність»", body, items)


# ====================================================================== main


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Collect experiment outputs, DB facts and docs/figures reports "
                                             "into docs/report_tables/*.md (+csv); missing -> <<TBD:...>>.")
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--db", action="store_true", help="query PostgreSQL (read-only): runs, metrics, counts")
    src.add_argument("--fixture", action="store_true", help="offline: no DB queries (DB facts become TBD)")
    ap.add_argument("--database-url", default=None)
    ap.add_argument("--symbol", default="BTCUSDT", help="recorded in the passport (tables cover all symbols)")
    ap.add_argument("--seed", type=int, default=None, help="recorded in the passport (no randomness here)")
    ap.add_argument("--workers", type=int, default=0, help="accepted for uniformity (no parallel work here)")
    ap.add_argument("--out", type=Path, default=None,
                    help="default: docs/report_tables (--smoke: artifacts/tmp/export_report_tables)")
    ap.add_argument("--smoke", action="store_true", help="write to artifacts/tmp/export_report_tables")
    ap.add_argument("--results-dir", type=Path, action="append", default=[],
                    help="directory with experiment outputs (repeatable; searched recursively)")
    ap.add_argument("--input", action="append", default=[], metavar="KIND=PATH",
                    help="explicit result file, e.g. pareto=path/to/pareto.json")
    ap.add_argument("--run-id", action="append", default=[], help="DB run(s) for the metrics table")
    ap.add_argument("--no-collect", action="store_true", help="skip pytest --collect-only")
    ap.add_argument("--collect-file", type=Path, default=None,
                    help="saved `pytest --collect-only -q -m \"\"` output (all markers)")
    ap.add_argument("--cov-file", type=Path, default=None, help="saved coverage report (pytest --cov output)")
    args = ap.parse_args(argv)
    full_argv = [sys.argv[0], *(sys.argv[1:] if argv is None else argv)]
    t0 = time.perf_counter()
    default_out = ea.DEFAULT_OUT / "export_report_tables" if args.smoke else ROOT / "docs" / "report_tables"
    out = args.out or default_out
    seed = args.seed if args.seed is not None else ea.profile_seed()
    pp = ea.passport("export_report_tables", full_argv, seed=seed,
                     extra={"results_dirs": [str(d) for d in args.results_dir], "db": bool(args.db)})
    print(f"command: {pp['command']}; git_sha {pp['git_sha']} (dirty={pp['git_dirty']})")
    found = discover(args.results_dir, args.input)
    print("found: " + ", ".join(f"{k} x{len(v)}" for k, v in sorted(found.items())) or "found: nothing")
    db = asyncio.run(db_facts(args.database_url, args.run_id)) if args.db else None
    rep = Report(out)
    section_metrics(rep, found, db)
    for kind in ("wf_folds", "pareto", "sensitivity", "amdahl"):
        section_search(rep, found, kind)
    section_mine(rep, found, "cost_models", "cost_models", "Три моделі витрат виконання (§5.14)",
                 (("sharpe", "Шарп"), ("total_return", "дохідність"), ("max_drawdown", "MaxDD"),
                  ("turnover", "оборот"), ("fees", "комісії, USDT"), ("funding", "фандинг, USDT"),
                  ("n_trades", "угод"), ("psr", "PSR")))
    section_mine(rep, found, "ablation", "ablation", "Ablation: детектори, Мамдані/лінійне, κ, гістерезис",
                 (("sharpe", "Шарп"), ("total_return", "дохідність"), ("max_drawdown", "MaxDD"),
                  ("turnover", "оборот"), ("n_trades", "угод"), ("fees", "комісії, USDT"), ("psr", "PSR")))
    section_var(rep, found)
    section_hysteresis(rep, found)
    section_doc_tables(rep, "pathological", "Патологічні WS-сесії: втрачено / дублів / час відновлення",
                       "docs/figures/ingest_pathological.md")
    section_doc_tables(rep, "mlp_rocauc", "MLP-автокодувальник: ROC-AUC на ін'єкціях",
                       "docs/figures/quality_mlp_rocauc.md")
    section_doc_tables(rep, "calibration", "Калібрування функцій належності (KMeans, перцентилі)",
                       "docs/figures/calibration_report.md")
    tests = section_tests(rep, args.no_collect, args.collect_file)
    section_traceability(rep, found, db, tests, args.cov_file)
    section_deviations(rep)
    tbd_total = sum(n for _, _, n in rep.index)
    idx = "\n".join([
        ea.passport_md(pp), "",
        f"Таблиць: {len(rep.index)}; маркерів TBD разом: **{tbd_total}** (кожен — відсутній вхід, "
        "а не підставлене число).", "",
        ea.md_table(["файл", "таблиця", "TBD"], [[f"`{f}`", t, n] for f, t, n in rep.index]), "",
        "Знайдені файли результатів:", "",
        *[f"* {k}: " + ", ".join(f"`{rel(e['path'])}`" for e in v) for k, v in sorted(found.items())], ""])
    rep.write("index", "Таблиці звіту: зміст і походження", idx)
    (out / "index.json").write_text(json.dumps(ea.sanitize({"passport": pp, "tables": rep.index,
                                                              "tbd_total": tbd_total}),
                                               ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"{len(rep.index)} tables, {tbd_total} TBD markers, wall {time.perf_counter() - t0:.1f} s -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
