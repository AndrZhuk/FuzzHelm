"""Walk-forward з embargo: IS-сітка 108 клітинок → фронт Парето → робоча точка за правилом → OOS; фаза 7.

Найменування: scripts/run_walkforward.py
Призначення: таблиця IS vs OOS по 6 фолдах (брифінг §5.16, §14 п. 2.11, §15 сцена 3:20–4:10) з обраними
    параметрами, конкатенована OOS-крива і її метрики (Sharpe, Sortino, MaxDD, Calmar, PSR); порівняння
    Мамдані vs лінійне голосування на тих самих фолдах (--engine both). Паспорти прогонів фолдів (IS і OOS
    обраної клітинки) пишуться в БД як run kind=backtest (workers.persist) з рішеннями, ордерами, капіталом.
Автор: Андрій Жук, 2026.

Запуск (повний):  uv run python scripts/run_walkforward.py --db --symbol BTCUSDT --engine both --workers 8
Швидка перевірка: uv run python scripts/run_walkforward.py --fixture --smoke --workers 2

Фолди — config/profiles/backtest.yaml (IS 15 / OOS 5 / крок 5 діб × 6, embargo = 2·max_lookback = 1046 барів,
D-03); на IS кожного фолду — усі клітинки сітки чистими воркерами (без БД); робоча точка — правилом
experiments_search.choose_from_front (ε-обмеження: max SR серед недомінованих з MaxDD ≤ dd_cap, не argmax SR);
OOS-прогін стартує за W = 523 бари до OOS (прогрів без торгівлі всередині embargo). Виводи: JSON, CSV (фолди,
IS-клітинки, OOS-крива), markdown; запис у БД — --persist commit|dry-run|none (auto: commit для повного --db,
dry-run для --db --smoke — транзакція відкочується, none для --fixture).
commit — лише із закоміченого коду (git_state: зміни поза artifacts/, docs/ → відмова, код 2;
--allow-dirty — свідомий обхід, паспорт тоді несе git_dirty = 1; XS-11).
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import math
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fuzzhelm.backtest.engine import BacktestConfig, run_backtest
from fuzzhelm.backtest.experiments_search import (
    SELECTION_RULE,
    SELECTION_RULE_UK,
    WalkForwardSearch,
    add_common_args,
    commit_refusal,
    default_dd_cap,
    fmt,
    git_state,
    json_safe,
    md_table,
    profile_seed,
    provenance,
    provenance_md,
    psr_statement_uk,
    resolve_dataset,
    resolve_out,
    smoke_folds,
    subset_indices,
    walkforward_search,
)
from fuzzhelm.backtest.grid import load_grid_space, make_grid
from fuzzhelm.backtest.walkforward import folds_from_profile

NAME = "walkforward"
FOLD_KEYS = ("sharpe", "sortino", "max_drawdown", "turnover", "total_return", "n_trades", "psr", "halted")


def _utc(ns: int) -> str:
    return datetime.fromtimestamp(ns / 1e9, tz=UTC).strftime("%Y-%m-%d %H:%M")


def build_folds(ds: Any, cfg: BacktestConfig, smoke: bool) -> list[Any]:
    emb = 2 * cfg.feature_params().max_lookback
    if smoke:
        return smoke_folds(len(ds), embargo_bars=emb, warmup=cfg.resolved_warmup())
    return folds_from_profile(len(ds), max_lookback=cfg.feature_params().max_lookback)


def fold_geometry(ds: Any, folds: list[Any]) -> list[dict[str, Any]]:
    t = ds.t_ns
    return [{"fold": f.index, "is": [f.is_start, f.is_end], "embargo": [f.embargo_start, f.embargo_end],
             "oos": [f.oos_start, f.oos_end], "embargo_bars": f.embargo_bars,
             "is_utc": [_utc(t[f.is_start].item()), _utc(t[f.is_end - 1].item())],
             "oos_utc": [_utc(t[f.oos_start].item()), _utc(t[f.oos_end - 1].item())]} for f in folds]


# ---------------------------------------------------------------------- запис у БД


async def persist_folds(wf: WalkForwardSearch, ds: Any, cfg: BacktestConfig, seed: int,
                        mode: str, gs0: dict[str, Any], allow_dirty: bool) -> list[dict[str, Any]]:
    """IS і OOS обраної клітинки кожного фолду → run kind=backtest (workers.persist), у ОДНІЙ транзакції.

    Прогін повторюється в головному процесі з record_traces=trades (рішення/ордери/капітал для BacktestView);
    крива від режиму трасування не залежить (ENG-10), тож equity_hash OOS звіряється з воркером. Ідентичний
    прогін (ux_run_identity) уже є — новий не пишеться, береться наявний. dry-run — ROLLBACK наприкінці.
    git_sha/git_dirty паспорта — стан ДО обчислень (gs0); дерево змінилось під час прогону → git_dirty = 1, а
    commit без --allow-dirty перетворюється на ROLLBACK (статус rolled_back_tree_changed).
    """
    from fuzzhelm.config import get_settings  # noqa: PLC0415
    from fuzzhelm.core.enums import RunKind, RunStatus  # noqa: PLC0415
    from fuzzhelm.core.journal import JournalEntry  # noqa: PLC0415
    from fuzzhelm.infra.wallclock import SystemClock, new_run_id  # noqa: PLC0415
    from fuzzhelm.storage.models import APP_ROLE  # noqa: PLC0415
    from fuzzhelm.storage.repositories import InstrumentRepo, RunRepo  # noqa: PLC0415
    from fuzzhelm.storage.session import make_engine, session_factory  # noqa: PLC0415
    from fuzzhelm.workers.persist import Passport, create_run, finish_run, persist_backtest  # noqa: PLC0415

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
            for fs in wf.folds:
                f = fs.fold
                for seg, (a, b, ev) in (("is", (f.is_start, f.is_end, 0)),
                                        ("oos", (fs.oos_run_start, f.oos_end, fs.oos_eval_start))):
                    sub = ds.slice(a, b)
                    ccfg = cfg.with_params(**fs.params, record_traces="trades")
                    rec: dict[str, Any] = {"fold": f.index, "segment": seg, "bars": [a, b], "eval_start": ev,
                                           "config_hash": ccfg.config_hash, "dataset_hash": sub.dataset_hash}
                    old = await RunRepo(s).find_by_identity(
                        config_hash=ccfg.config_hash, dataset_hash=sub.dataset_hash, seed=seed,
                        engine=str(ccfg.engine), git_sha=sha)
                    if old is not None:
                        rec.update(run_id=str(old.id), status=f"exists:{old.kind}:{old.status}")
                        out.append(rec)
                        continue
                    rid = new_run_id()
                    await create_run(s, Passport(
                        run_id=rid, kind=RunKind.BACKTEST, config=ccfg.identity_dict(),
                        config_hash=ccfg.config_hash, dataset_hash=sub.dataset_hash, seed=seed,
                        engine=str(ccfg.engine), git_sha=sha, git_dirty=dirty, instrument_id=inst.id,
                        tf=sub.tf,
                        ts_from_ns=sub.t_ns[0].item(), ts_to_ns=sub.t_ns[-1].item() + sub.tf_ns,
                        started_at_ns=clock.now_ns()))
                    entries: list[JournalEntry] = []
                    res = run_backtest(sub, ccfg, seed, eval_start=ev, run_id=rid, git=False,
                                       kind=RunKind.BACKTEST, journal_sink=entries.append)
                    ref = fs.oos_selected if seg == "oos" else fs.is_selected
                    same = (res.equity_hash == ref["equity_hash"]) if seg == "oos" else (
                        _same(res.metrics["sharpe"], ref["sharpe"])
                        and _same(res.metrics["total_return"], ref["total_return"]))
                    pc = await persist_backtest(s, rid, res, instrument_id=inst.id,
                                                symbol=ds.instrument.symbol_canon, journal=entries)
                    await finish_run(s, rid, RunStatus.DONE, journal_head_hash=res.manifest.journal_head_hash,
                                     equity_hash=res.manifest.equity_hash, finished_at_ns=clock.now_ns(),
                                     metrics={"wf_fold": f.index + 0.0,
                                              "wf_oos": 1.0 if seg == "oos" else 0.0,
                                              "wf_cell": wf.cell_ids[fs.choice.index] + 0.0})
                    back = await RunRepo(s).get(rid)      # перечитати в тій самій транзакції
                    rec["read_back_ok"] = bool(back is not None and back.status == RunStatus.DONE.value
                                               and back.equity_hash == bytes.fromhex(res.equity_hash))
                    rec.update(run_id=str(rid), status="written" if mode == "commit" else (
                                   "rolled_back" if mode == "dry-run" else mode),
                               equity_hash=res.manifest.equity_hash, matches_worker=bool(same),
                               counts=pc.as_dict())
                    out.append(rec)
            if mode == "commit":
                await s.commit()
            else:
                await s.rollback()
    finally:
        await engine.dispose()
    return out


def _same(a: float, b: float) -> bool:
    return (math.isnan(a) and math.isnan(b)) or a == b


# ---------------------------------------------------------------------- виводи


def engine_summary(wf: WalkForwardSearch) -> dict[str, Any]:
    c = wf.concat
    return {
        "engine": wf.engine, "config_hash": wf.config_hash, "dd_cap": wf.dd_cap, "warmup_bars": wf.warmup,
        "cells": len(wf.cells), "concat_oos": c,
        "psr_statement": psr_statement_uk(c["psr"], what=f"конкатенований OOS ({wf.engine})"),
        "wall_s": wf.wall_s,
        "folds": [{
            "fold": fs.fold.index, "selected_cell": wf.cell_ids[fs.choice.index], "params": fs.params,
            "choice": {**fs.choice.to_dict(), "index": wf.cell_ids[fs.choice.index],
                       "front": [wf.cell_ids[i] for i in fs.choice.front],
                       "feasible": [wf.cell_ids[i] for i in fs.choice.feasible]},
            "is": {k: fs.is_selected[k] for k in (*FOLD_KEYS, "config_hash")},
            "oos": {k: fs.oos_selected[k] for k in (*FOLD_KEYS, "config_hash", "equity_hash", "final_state")},
            "oos_run_bars": [fs.oos_run_start, fs.fold.oos_end], "oos_eval_start": fs.oos_eval_start,
        } for fs in wf.folds],
    }


def write_engine_outputs(out: Path, stem: str, wf: WalkForwardSearch, prov: dict[str, Any],
                         geom: list[dict[str, Any]],
                         persisted: list[dict[str, Any]] | None) -> dict[str, Any]:
    summ = engine_summary(wf)
    payload = {"provenance": prov, "selection_rule": SELECTION_RULE, "selection_rule_uk": SELECTION_RULE_UK,
               "folds_geometry": geom, **summ, "persisted_runs": persisted}
    (out / f"{stem}.json").write_text(json.dumps(json_safe(payload), ensure_ascii=False, indent=1),
                                      encoding="utf-8")
    rows = wf.fold_rows()
    with (out / f"{stem}_folds.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    with (out / f"{stem}_is_cells.csv").open("w", newline="", encoding="utf-8") as fh:
        w2 = csv.writer(fh)
        keys = ("sharpe", "max_drawdown", "turnover", "total_return", "n_trades", "psr", "halted")
        w2.writerow(["fold", "cell", *wf.cells[0].keys(), *keys, "on_front", "feasible", "selected",
                     "config_hash"])
        for fs in wf.folds:
            front, feas = set(fs.choice.front), set(fs.choice.feasible)
            for i, m in enumerate(fs.is_metrics):
                w2.writerow([fs.fold.index, wf.cell_ids[i], *wf.cells[i].values(), *(m[k] for k in keys),
                             int(i in front), int(i in feas), int(i == fs.choice.index), m["config_hash"]])
    with (out / f"{stem}_oos_equity.csv").open("w", newline="", encoding="utf-8") as fh:
        w3 = csv.writer(fh)
        w3.writerow(["i", "close_ns", "fold", "equity"])
        for i, (ts, fo, e) in enumerate(zip(wf.concat_ts.tolist(), wf.concat_fold.tolist(),
                                            wf.concat_equity.tolist(), strict=True)):
            w3.writerow([i, ts, fo, repr(e)])
    (out / f"{stem}.md").write_text(engine_md(wf, summ, prov, geom, persisted), encoding="utf-8")
    return summ


def engine_md(wf: WalkForwardSearch, summ: dict[str, Any], prov: dict[str, Any], geom: list[dict[str, Any]],
              persisted: list[dict[str, Any]] | None) -> str:
    sym = prov["dataset"]["symbol"]
    lines = [f"# Walk-forward з embargo: {sym}, рушій `{wf.engine}`", "",
             "Згенеровано `scripts/run_walkforward.py`; усі числа — з цього прогону (команда і хеші нижче).",
             "",
             provenance_md(prov), "", "## Фолди", "",
             md_table(["фолд", "IS (бари)", "embargo", "OOS (бари)", "IS, UTC", "OOS, UTC"],
                      [[g["fold"], f"{g['is'][0]}–{g['is'][1]}", g["embargo_bars"],
                        f"{g['oos'][0]}–{g['oos'][1]}",
                        " … ".join(g["is_utc"]), " … ".join(g["oos_utc"])] for g in geom]),
             "", f"Прогрів OOS-прогону: {wf.warmup} барів перед OOS (усередині embargo, без торгівлі).", "",
             "## Правило вибору робочої точки на IS", "", SELECTION_RULE_UK, "",
             f"dd_cap = {fmt(wf.dd_cap)}; клітинок на IS кожного фолду: {len(wf.cells)}.", "",
             "## IS vs OOS обраної клітинки", ""]
    hdr = ["фолд", "клітинка", "n_ATR", "χ", "u_enter", "ρ_base", "λ", "правило", "фронт/доп.",
           "SR IS", "SR OOS", "MaxDD IS", "MaxDD OOS", "оборот IS", "оборот OOS", "дох. IS", "дох. OOS",
           "угод IS", "угод OOS"]
    rows = []
    for fs in wf.folds:
        p, i, o = fs.params, fs.is_selected, fs.oos_selected
        rows.append([fs.fold.index, wf.cell_ids[fs.choice.index], p["n_atr"], p["chi"], p["u_enter"],
                     p["rho_base"], p["lam"], fs.choice.rule,
                     f"{len(fs.choice.front)}/{len(fs.choice.feasible)}",
                     i["sharpe"], o["sharpe"], i["max_drawdown"], o["max_drawdown"],
                     i["turnover"], o["turnover"],
                     i["total_return"], o["total_return"], i["n_trades"], o["n_trades"]])
    lines += [md_table(hdr, rows), "",
              "Шарп — річний (√525600·mean/std хвилинних доходностей); MaxDD — частка; оборот — "
              "капіталів/рік.",
              "", "## Конкатенований OOS (ланцюг доходностей фолдів)", ""]
    c = summ["concat_oos"]
    lines += [md_table(["метрика", "значення"], [[k, c[k]] for k in c]), "", summ["psr_statement"], "",
              "Ланцюг: кожен OOS-прогін стартує зі свіжого капіталу і автомата ризику; склеюються "
              "доходності; позиція, відкрита на кінець фолду, оцінена за останнім close (без комісії "
              "виходу).", ""]
    if persisted is not None:
        lines += ["## Паспорти в БД (run kind = backtest)", "",
                  md_table(["фолд", "сегмент", "run_id", "стан", "equity_hash = воркер"],
                           [[r["fold"], r["segment"], f"`{r['run_id']}`", r["status"],
                             r.get("matches_worker", "—")] for r in persisted]), ""]
    lines += [f"Час: IS-сітка {fmt(wf.wall_s['is_grid'])} с, OOS {fmt(wf.wall_s['oos'])} с.", ""]
    return "\n".join(lines)


def write_comparison(out: Path, stem: str, summs: list[dict[str, Any]], prov: dict[str, Any]) -> None:
    payload = {"provenance": prov, "engines": [{"engine": s["engine"], "config_hash": s["config_hash"],
                                                "concat_oos": s["concat_oos"],
                                                "folds": [{"fold": f["fold"], "is": f["is"], "oos": f["oos"],
                                                           "selected_cell": f["selected_cell"]}
                                                          for f in s["folds"]]} for s in summs]}
    (out / f"{stem}.json").write_text(json.dumps(json_safe(payload), ensure_ascii=False, indent=1),
                                      encoding="utf-8")
    keys = ("sharpe", "sortino", "max_drawdown", "calmar", "total_return", "turnover", "n_trades", "psr")
    prov_cmp = {**prov, "engine": " + ".join(s["engine"] for s in summs),
                "config_hash": "по рушію — у таблиці нижче"}
    lines = [f"# Мамдані vs лінійне голосування на тих самих фолдах ({prov['dataset']['symbol']})", "",
             provenance_md(prov_cmp), "", "## Конкатенований OOS", "",
             md_table(["метрика", *(s["engine"] for s in summs)],
                      [["config_hash", *(f"`{s['config_hash'][:16]}…`" for s in summs)],
                       *([k, *(s["concat_oos"][k] for s in summs)] for k in keys)]), "",
             "## OOS Шарп обраної клітинки по фолдах", "",
             md_table(["фолд", *(f"{s['engine']}: клітинка / SR OOS" for s in summs)],
                      [[k, *(f"{s['folds'][k]['selected_cell']} / {fmt(s['folds'][k]['oos']['sharpe'])}"
                             for s in summs)] for k in range(len(summs[0]["folds"]))]), ""]
    lines += [s["psr_statement"] for s in summs]
    (out / f"{stem}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="FuzzHelm walk-forward with embargo and Pareto-front selection")
    add_common_args(ap)
    ap.add_argument("--engine", choices=("mamdani", "linear", "both"), default="mamdani")
    ap.add_argument("--cells", type=int, default=None, help="evenly spaced subset of the 108-cell grid")
    ap.add_argument("--dd-cap", type=float, default=None,
                    help="MaxDD cap of the selection rule (default state_machine.cool_enter)")
    ap.add_argument("--persist", choices=("auto", "commit", "dry-run", "none"), default="auto")
    ap.add_argument("--allow-dirty", action="store_true",
                    help="allow --persist commit from uncommitted code (passports then carry git_dirty = 1)")
    args = ap.parse_args(argv)
    argv_full = [sys.argv[0], *(sys.argv[1:] if argv is None else argv)]
    if args.smoke and args.cells is None:
        args.cells = 4
    persist = args.persist
    if persist == "auto":
        persist = "none" if args.fixture is not None else ("dry-run" if args.smoke else "commit")
    if persist != "none" and args.fixture is not None and persist == "commit":
        raise SystemExit("--persist commit needs the DB window (--db): fixture data is not the fixed dataset")
    gs = git_state()
    why = commit_refusal(persist, gs, allow_dirty=args.allow_dirty)
    if why:
        print(f"REFUSED: {why}", file=sys.stderr)
        return 2
    seed = profile_seed("backtest") if args.seed is None else args.seed
    out = resolve_out(args, NAME)
    t0 = time.perf_counter()
    ds = resolve_dataset(args)
    print(f"dataset {ds.source}: {len(ds)} bars, dataset_hash {ds.dataset_hash[:16]}… "
          f"({time.perf_counter() - t0:.1f} s)")
    grid = make_grid(load_grid_space())
    ids = subset_indices(len(grid), args.cells)
    cells = [grid[i] for i in ids]
    engines = ("mamdani", "linear") if args.engine == "both" else (args.engine,)
    created = datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    summs: list[dict[str, Any]] = []
    prov: dict[str, Any] = {}
    for eng in engines:
        cfg = BacktestConfig.from_profile("backtest", engine=eng, cooldown_policy=args.cooldown_policy)
        folds = build_folds(ds, cfg, args.smoke)
        dd_cap = default_dd_cap(cfg) if args.dd_cap is None else args.dd_cap
        geom = fold_geometry(ds, folds)
        print(f"[{eng}] {len(folds)} folds x {len(cells)} cells, embargo {folds[0].embargo_bars} bars, "
              f"workers {args.workers}, dd_cap {dd_cap}")
        t1 = time.perf_counter()
        wf = walkforward_search(ds, cfg, folds=folds, cells=cells, seed=seed, workers=args.workers,
                                dd_cap=dd_cap, cell_ids=ids)
        wall = time.perf_counter() - t1
        persisted = None
        if persist != "none":
            t2 = time.perf_counter()
            persisted = asyncio.run(persist_folds(wf, ds, cfg, seed, persist, gs, args.allow_dirty))
            print(f"[{eng}] persisted {len(persisted)} fold runs ({persist}) "
                  f"in {time.perf_counter() - t2:.1f} s")
        prov = provenance(argv_full, ds, cfg, seed=seed, workers=args.workers, created_utc=created, git=gs,
                          extra={"script": NAME, "smoke": args.smoke, "persist": persist,
                                 "cells": ids if len(ids) < len(grid) else "all",
                                 "wall_s": {**wf.wall_s, "total": wall}})
        stem = f"{NAME}_{ds.instrument.symbol_venue}_{eng}"
        summs.append(write_engine_outputs(out, stem, wf, prov, geom, persisted))
        c = wf.concat
        print(f"[{eng}] wall {wall:.1f} s; concatenated OOS: sharpe {c['sharpe']:.3f}, "
              f"max_dd {c['max_drawdown']:.4f}, total_return {c['total_return']:.4f}, psr {c['psr']:.4g}; "
              f"outputs {out / stem}.*")
        for fs in wf.folds:
            print(f"  fold {fs.fold.index}: cell {ids[fs.choice.index]} ({fs.choice.rule}) "
                  f"SR IS {fs.is_selected['sharpe']:.2f} -> OOS {fs.oos_selected['sharpe']:.2f}")
    if len(summs) > 1:
        write_comparison(out, f"{NAME}_{ds.instrument.symbol_venue}_engines", summs, prov)
        print(f"engine comparison: {out}/{NAME}_{ds.instrument.symbol_venue}_engines.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
