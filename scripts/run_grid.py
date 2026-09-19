"""Сітка 108 клітинок на 45-денному вікні + конкатенований OOS кожної клітинки, фронт Парето, PSR/DSR; фаза 7.

Найменування: scripts/run_grid.py
Призначення: gate фази 7 «у БД 108 grid_cell-прогонів» (брифінг §12): кожна клітинка — прогін над усім вікном
    (паспорт run kind=grid_cell: config, config_hash, dataset_hash, seed, git_sha, equity_hash; метрики в
    run_metric) і, на тих самих фолдах walk-forward, OOS кожної клітинки, склеєний у одну криву; недомінований
    фронт (SR_OOS↑, MaxDD_OOS↓, Turnover_OOS↓), робоча точка за правилом ε-обмеження, DSR з N = фактичне число
    оцінених клітинок і Var(SR_i) по них (§5.15), PSR обраної точки і пряме твердження, якщо PSR < 0.95.
Автор: Андрій Жук, 2026.

Запуск (повний):  uv run python scripts/run_grid.py --db --symbol BTCUSDT --workers 8
Швидка перевірка: uv run python scripts/run_grid.py --fixture --smoke --workers 2

Покрокової кривої капіталу клітинок у БД немає (record_traces=none, equity_point не пишеться): 108 × 64 800
рядків ≈ 7 млн — дорого і для звіту не потрібно; відтворюваність дає equity_hash у паспорті (крива
перераховується тим самим кодом за config + seed + дані). Метрики конкатенованого OOS пишуться в run_metric
тієї ж клітинки з префіксом `oos_` (OOS — підмножина того самого вікна); прапорці `pareto_oos`,
`selected_oos`, для обраної — `dsr_oos`, `dsr_sr0_oos`, `dsr_n_trials`, `dsr_var_sr_oos`.
--persist auto: commit для повного --db, dry-run (ROLLBACK) для --db --smoke, none для --fixture.
commit — лише із закоміченого коду (git_state: зміни поза artifacts/, docs/ → відмова, код 2;
--allow-dirty — свідомий обхід, паспорт тоді несе git_dirty = 1; XS-11).
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import math
import statistics
import sys
import time
from datetime import UTC, datetime
from typing import Any

from fuzzhelm.backtest.engine import BacktestConfig
from fuzzhelm.backtest.experiments_search import (
    CRITERIA,
    SELECTION_RULE,
    SELECTION_RULE_UK,
    add_common_args,
    choose_from_front,
    commit_refusal,
    concat_by_cell,
    default_dd_cap,
    deflated_sharpe_report,
    fmt,
    front_of,
    git_state,
    json_safe,
    make_task,
    md_table,
    numeric_metrics,
    oos_tasks_all_cells,
    profile_seed,
    provenance,
    provenance_md,
    resolve_dataset,
    resolve_out,
    segment_task,
    significance_statement_uk,
    smoke_folds,
    strip_arrays,
    subset_indices,
)
from fuzzhelm.backtest.grid import load_grid_space, make_grid
from fuzzhelm.backtest.parallel import run_parallel
from fuzzhelm.backtest.walkforward import folds_from_profile

NAME = "grid"
TABLE_KEYS = ("sharpe", "max_drawdown", "turnover", "total_return", "n_trades", "psr")


async def persist_cells(ds: Any, cfg: BacktestConfig, cells: list[dict[str, Any]],
                        full: list[dict[str, Any]], extra: list[dict[str, float]], seed: int, mode: str,
                        started_ns: int, gs0: dict[str, Any], allow_dirty: bool) -> list[dict[str, Any]]:
    """Кожна клітинка → run kind=grid_cell (workers.persist.create_run/finish_run) в одній транзакції.

    Ідентичний прогін (ux_run_identity = config_hash, dataset_hash, seed, engine, git_sha) уже є — нового
    рядка не буде (унікальний індекс), клітинка посилається на наявний (зокрема kind=backtest з тією самою
    конфігурацією); dry-run — ROLLBACK наприкінці. git_sha/git_dirty паспорта — стан ДО обчислень (gs0); якщо
    дерево змінилось під час прогону (інший HEAD або нові зміни в коді) — git_dirty = 1, а commit без
    --allow-dirty перетворюється на ROLLBACK (статус rolled_back_tree_changed).
    """
    from fuzzhelm.config import get_settings  # noqa: PLC0415
    from fuzzhelm.core.enums import RunKind, RunStatus  # noqa: PLC0415
    from fuzzhelm.infra.wallclock import SystemClock, new_run_id  # noqa: PLC0415
    from fuzzhelm.storage.models import APP_ROLE  # noqa: PLC0415
    from fuzzhelm.storage.repositories import InstrumentRepo, RunRepo  # noqa: PLC0415
    from fuzzhelm.storage.session import make_engine, session_factory  # noqa: PLC0415
    from fuzzhelm.workers.persist import Passport, create_run, finish_run  # noqa: PLC0415

    engine = make_engine(get_settings().database_url, role=APP_ROLE, null_pool=True)
    factory = session_factory(engine)
    clock = SystemClock()
    gs1 = git_state()
    sha = gs0["sha"]
    changed = gs1["sha"] != sha or bool(gs1["dirty"])
    dirty = (bool(gs0["dirty"]) or changed) if sha is not None else None
    if mode == "commit" and changed and not allow_dirty:
        print(f"WARNING: the working tree changed during the run ({'; '.join(gs1['dirty_paths'][:5])}): "
              "rolling back instead of committing", file=sys.stderr)
        mode = "rolled_back_tree_changed"
    out: list[dict[str, Any]] = []
    try:
        async with factory() as s:
            inst = await InstrumentRepo(s).get_by_canon(ds.instrument.symbol_canon)
            if inst is None:
                raise SystemExit(f"instrument {ds.instrument.symbol_canon} is not in the DB")
            for cell, res, ext in zip(cells, full, extra, strict=True):
                ccfg = cfg.with_params(**cell, record_traces="none")
                if ccfg.config_hash != res["config_hash"]:
                    raise RuntimeError("worker config_hash differs from the persisted config")
                old = await RunRepo(s).find_by_identity(
                    config_hash=ccfg.config_hash, dataset_hash=ds.dataset_hash, seed=seed,
                    engine=str(ccfg.engine), git_sha=sha)
                if old is not None:
                    out.append({"run_id": str(old.id), "status": f"exists:{old.kind}:{old.status}"})
                    continue
                rid = new_run_id()
                await create_run(s, Passport(
                    run_id=rid, kind=RunKind.GRID_CELL, config=ccfg.identity_dict(),
                    config_hash=ccfg.config_hash,
                    dataset_hash=ds.dataset_hash, seed=seed, engine=str(ccfg.engine), git_sha=sha,
                    git_dirty=dirty, instrument_id=inst.id, tf=ds.tf, ts_from_ns=ds.t_ns[0].item(),
                    ts_to_ns=ds.t_ns[-1].item() + ds.tf_ns, started_at_ns=started_ns))
                await finish_run(s, rid, RunStatus.DONE, equity_hash=res["equity_hash"],
                                 finished_at_ns=clock.now_ns(), metrics={**numeric_metrics(res), **ext})
                back = await RunRepo(s).get(rid)          # перечитати в тій самій транзакції
                ok = (back is not None and back.status == RunStatus.DONE.value and back.kind == "grid_cell"
                      and back.equity_hash == bytes.fromhex(res["equity_hash"])
                      and back.config_hash == bytes.fromhex(ccfg.config_hash))
                n_met = len(await RunRepo(s).get_metrics(rid))
                out.append({"run_id": str(rid), "status": "written" if mode == "commit" else (
                                "rolled_back" if mode == "dry-run" else mode),
                            "read_back_ok": ok, "metrics": n_met})
            if mode == "commit":
                await s.commit()
            else:
                await s.rollback()
    finally:
        await engine.dispose()
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="FuzzHelm 108-cell grid: full window + concatenated OOS, "
                                             "Pareto front, PSR/DSR, persisted grid_cell runs")
    add_common_args(ap)
    ap.add_argument("--engine", choices=("mamdani", "linear"), default="mamdani")
    ap.add_argument("--cells", type=int, default=None, help="evenly spaced subset of the grid (smoke)")
    ap.add_argument("--dd-cap", type=float, default=None,
                    help="MaxDD cap of the selection rule (default state_machine.cool_enter)")
    ap.add_argument("--no-oos", action="store_true",
                    help="skip the walk-forward OOS of every cell (front/DSR on full-window metrics)")
    ap.add_argument("--persist", choices=("auto", "commit", "dry-run", "none"), default="auto")
    ap.add_argument("--allow-dirty", action="store_true",
                    help="allow --persist commit from uncommitted code (passports then carry git_dirty = 1)")
    args = ap.parse_args(argv)
    argv_full = [sys.argv[0], *(sys.argv[1:] if argv is None else argv)]
    if args.smoke and args.cells is None:
        args.cells = 6
    persist = args.persist
    if persist == "auto":
        persist = "none" if args.fixture is not None else ("dry-run" if args.smoke else "commit")
    if args.fixture is not None and persist == "commit":
        raise SystemExit("--persist commit needs the DB window (--db): fixture data is not the fixed dataset")
    gs = git_state()
    why = commit_refusal(persist, gs, allow_dirty=args.allow_dirty)
    if why:
        print(f"REFUSED: {why}", file=sys.stderr)
        return 2
    seed = profile_seed("grid") if args.seed is None else args.seed
    out = resolve_out(args, NAME)
    ds = resolve_dataset(args)
    print(f"dataset {ds.source}: {len(ds)} bars, dataset_hash {ds.dataset_hash[:16]}…")
    grid = make_grid(load_grid_space())
    ids = subset_indices(len(grid), args.cells)
    cells = [grid[i] for i in ids]
    cfg = BacktestConfig.from_profile("grid", engine=args.engine, cooldown_policy=args.cooldown_policy)
    dd_cap = default_dd_cap(cfg) if args.dd_cap is None else args.dd_cap
    warmup = cfg.resolved_warmup()
    emb = 2 * cfg.feature_params().max_lookback
    if args.no_oos:
        folds = []
    elif args.smoke:
        folds = smoke_folds(len(ds), embargo_bars=emb, warmup=warmup)
    else:
        folds = folds_from_profile(len(ds), max_lookback=cfg.feature_params().max_lookback)
    cfg_d = cfg.to_dict()
    payload = ds.to_payload()
    tasks = [make_task(payload, cfg_d, seed, params=c, equity_hash=True) for c in cells]
    tasks += oos_tasks_all_cells(ds, cfg, folds=folds, cells=cells, seed=seed) if folds else []
    print(f"{len(cells)} cells x (full window + {len(folds)} OOS folds) = {len(tasks)} tasks, "
          f"workers {args.workers}")
    started_ns = time.time_ns()
    t0 = time.perf_counter()
    res = run_parallel(segment_task, tasks, args.workers, seed=seed)
    wall = time.perf_counter() - t0
    n = len(cells)
    full = [strip_arrays(r) for r in res[:n]]
    oos = concat_by_cell(res[n:], n, len(folds), ds.bars_per_year) if folds else None
    space_name = "oos" if oos is not None else "full"
    crit = oos if oos is not None else full
    choice = choose_from_front(crit, dd_cap=dd_cap)
    front_full = front_of(full)
    sel = choice.index
    trial = [m["sr_period"] for m in crit]
    c = crit[sel]
    dsr = deflated_sharpe_report(c["sr_period"], int(c["n_obs"]), c["skew"], c["kurt"], trial)
    dsr_full = deflated_sharpe_report(full[sel]["sr_period"], int(full[sel]["n_obs"]), full[sel]["skew"],
                                      full[sel]["kurt"], [m["sr_period"] for m in full])
    what = ("обрана точка, конкатенований OOS" if oos is not None else "обрана точка, усе вікно (in-sample)")
    statement = significance_statement_uk(dsr, what=what)
    statement_full = significance_statement_uk(dsr_full, what="та сама клітинка, усе вікно (in-sample)")

    extra: list[dict[str, float]] = []
    for i in range(n):
        e: dict[str, float] = {"pareto_full": 1.0 if i in front_full else 0.0}
        if oos is not None:
            e.update({f"oos_{k}": v for k, v in oos[i].items()})
            e["pareto_oos"] = 1.0 if i in choice.front else 0.0
            e["selected_oos"] = 1.0 if i == sel else 0.0
        if i == sel:
            sfx = "oos" if oos is not None else "full"
            e.update({f"dsr_{sfx}": dsr["dsr"], f"dsr_sr0_{sfx}": dsr["sr0"],
                      "dsr_n_trials": dsr["n_trials"] + 0.0, f"dsr_var_sr_{sfx}": dsr["var_sr"],
                      f"psr_selected_{sfx}": dsr["psr"]})
        extra.append(e)

    persisted = None
    if persist != "none":
        t1 = time.perf_counter()
        persisted = asyncio.run(persist_cells(ds, cfg, cells, full, extra, seed, persist, started_ns, gs,
                                              args.allow_dirty))
        ok = sum(1 for p in persisted if p.get("read_back_ok"))
        print(f"persisted {len(persisted)} grid_cell runs ({persist}) in {time.perf_counter() - t1:.1f} s; "
              f"read back OK {ok}/{len(persisted)}")

    created = datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    prov = provenance(argv_full, ds, cfg, seed=seed, workers=args.workers, created_utc=created, git=gs,
                      extra={"script": NAME, "smoke": args.smoke, "persist": persist, "wall_s": wall,
                             "cells": ids if len(ids) < len(grid) else "all", "oos_folds": len(folds)})
    rows = []
    for i, cell in enumerate(cells):
        row: dict[str, Any] = {"cell": ids[i], **cell, "config_hash": full[i]["config_hash"],
                               "equity_hash": full[i]["equity_hash"], "wall_s": full[i]["wall_s"],
                               **{f"full_{k}": full[i][k] for k in (*TABLE_KEYS, "sr_period", "halted")},
                               "pareto_full": int(i in front_full)}
        if oos is not None:
            row.update({f"oos_{k}": oos[i][k] for k in (*TABLE_KEYS, "sortino", "calmar", "sr_period")})
            row.update(pareto_oos=int(i in choice.front), feasible_oos=int(i in choice.feasible))
        row["selected"] = int(i == sel)
        if persisted is not None:
            row["run_id"], row["persist_status"] = persisted[i]["run_id"], persisted[i]["status"]
            row["read_back_ok"] = persisted[i].get("read_back_ok", "")
            row["run_metrics"] = persisted[i].get("metrics", "")
        rows.append(row)
    stem = f"{NAME}_{ds.instrument.symbol_venue}_{args.engine}"
    payload_json = {
        "provenance": prov, "selection_rule": SELECTION_RULE, "selection_rule_uk": SELECTION_RULE_UK,
        "criteria": list(CRITERIA), "front_space": space_name, "dd_cap": dd_cap,
        "choice": {**choice.to_dict(), "index": ids[sel], "front": [ids[i] for i in choice.front],
                   "feasible": [ids[i] for i in choice.feasible], "params": cells[sel]},
        "front_full": [ids[i] for i in front_full], "dsr": dsr, "dsr_full_window": dsr_full,
        "statement": statement, "statement_full_window": statement_full,
        "folds": [{"fold": f.index, "oos": [f.oos_start, f.oos_end]} for f in folds], "cells": rows,
    }
    (out / f"{stem}.json").write_text(json.dumps(json_safe(payload_json), ensure_ascii=False, indent=1),
                                      encoding="utf-8")
    with (out / f"{stem}.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    (out / f"{stem}.md").write_text(grid_md(prov, rows, choice, ids, cells, dsr, dsr_full, statement,
                                            statement_full, space_name, dd_cap, persisted), encoding="utf-8")
    print(f"wall {wall:.1f} s; front ({space_name}) {len(choice.front)} cells; chosen cell {ids[sel]} "
          f"({choice.rule}) {cells[sel]}")
    print(f"DSR N={dsr['n_trials']} var_sr={dsr['var_sr']:.3g} SR0={dsr['sr0']:.4g} DSR={dsr['dsr']:.4g} "
          f"PSR={dsr['psr']:.4g}")
    print(statement)
    print(f"outputs {out / stem}.*")
    return 0


def grid_md(prov: dict[str, Any], rows: list[dict[str, Any]], choice: Any, ids: list[int],
            cells: list[dict[str, Any]], dsr: dict[str, Any], dsr_full: dict[str, Any], statement: str,
            statement_full: str, space: str, dd_cap: float, persisted: list[dict[str, Any]] | None) -> str:
    pre = "oos" if space == "oos" else "full"
    where = "конкатенованому OOS" if pre == "oos" else "усьому вікні"
    sel = choice.index
    lines = [f"# Сітка {len(rows)} клітинок: {prov['dataset']['symbol']}, рушій `{prov['engine']}`", "",
             "Згенеровано `scripts/run_grid.py`; усі числа — з цього прогону.", "", provenance_md(prov), "",
             "## Робоча точка з фронту Парето", "",
             f"Критерії фронту: SR↑, MaxDD↓, Turnover↓ на **{where}** ({len(rows)} клітинок; фолдів OOS: "
             f"{prov['oos_folds']}).", "", SELECTION_RULE_UK, "",
             f"dd_cap = {fmt(dd_cap)}; фронт — {len(choice.front)} клітинок, з них у межі dd_cap — "
             f"{len(choice.feasible)}; правило спрацювало як `{choice.rule}`.", "",
             f"**Обрано клітинку {ids[sel]}:** " + ", ".join(f"{k} = {v}" for k, v in cells[sel].items()), ""]
    if pre == "oos":
        lines += ["Точку обрано ЗА OOS-метриками всіх клітинок, тож її OOS-метрики вже не є незалежною "
                  "оцінкою: поправку на вибір із N клітинок дає DSR нижче; незалежна поза-вибіркова оцінка "
                  "ПРОЦЕДУРИ вибору — walk-forward (`scripts/run_walkforward.py`: вибір на IS, оцінка "
                  "на OOS).", ""]
    hdr = ["клітинка", "n_ATR", "χ", "u_enter", "ρ_base", "λ", f"SR {pre}", f"MaxDD {pre}", f"оборот {pre}",
           f"дох. {pre}", "SR усе вікно", "MaxDD усе вікно", "обрана"]
    front_rows = [r for r in rows if r.get(f"pareto_{pre}")]
    lines += ["### Фронт", "", md_table(hdr, [[r["cell"], r["n_atr"], r["chi"], r["u_enter"], r["rho_base"],
                                              r["lam"], r[f"{pre}_sharpe"], r[f"{pre}_max_drawdown"],
                                              r[f"{pre}_turnover"], r[f"{pre}_total_return"],
                                              r["full_sharpe"], r["full_max_drawdown"],
                                              "так" if r["selected"] else ""] for r in front_rows]), ""]
    lines += ["## PSR і DSR (Bailey & López de Prado; Шарп за період, SR* = 0 для PSR)", "",
              md_table(["величина", f"простір вибору ({pre})", "усе вікно (in-sample, довідково)"], [
                  ["N (фактично оцінених клітинок)", dsr["n_trials"], dsr_full["n_trials"]],
                  ["скінченних SR", dsr["n_finite"], dsr_full["n_finite"]],
                  ["Var(SR_i), вибіркова", dsr["var_sr"], dsr_full["var_sr"]],
                  ["SR₀ = E[max SR]", dsr["sr0"], dsr_full["sr0"]],
                  ["ŜR обраної (за період)", dsr["sr_selected"], dsr_full["sr_selected"]],
                  ["n доходностей", dsr["n_obs"], dsr_full["n_obs"]],
                  ["γ₃ / γ₄", f"{fmt(dsr['skew'])} / {fmt(dsr['kurt'])}",
                   f"{fmt(dsr_full['skew'])} / {fmt(dsr_full['kurt'])}"],
                  ["PSR", dsr["psr"], dsr_full["psr"]], ["DSR", dsr["dsr"], dsr_full["dsr"]]]), "",
              f"**{statement}**", "", statement_full, ""]
    srs = [r[f"{pre}_sharpe"] for r in rows if math.isfinite(r[f"{pre}_sharpe"])]
    if srs:
        lines += [f"Розподіл річного SR ({pre}) по {len(srs)} клітинках зі скінченним SR: "
                  f"min {fmt(min(srs))}, медіана {fmt(statistics.median(srs))}, max {fmt(max(srs))}.", ""]
    if persisted is not None:
        st: dict[str, int] = {}
        for p in persisted:
            st[p["status"]] = st.get(p["status"], 0) + 1
        ok = sum(1 for p in persisted if p.get("read_back_ok"))
        lines += ["## Паспорти в БД (run kind = grid_cell)", "",
                  "Стани запису: " + ", ".join(f"{k}: {v}" for k, v in sorted(st.items()))
                  + f"; перечитано в транзакції і збігається з воркером (статус, config_hash, equity_hash): "
                    f"{ok} з {len(persisted)}.", ""]
    lines += ["Повна таблиця клітинок (параметри, метрики всього вікна і OOS, хеші, run_id) — у CSV поруч.",
              ""]
    return "\n".join(lines)


if __name__ == "__main__":
    sys.exit(main())
