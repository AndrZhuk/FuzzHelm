"""Рисунок «крива капіталу + просадка + смуги режимів ризику» для прогону (брифінг §12 фаза 6, §14 2.7–2.8).

Найменування: plot_equity.py
Призначення: капітал, просадка і смуги станів автомата ризику (NORMAL / WARNING / COOLDOWN / HALTED) для:
    (а) збереженого прогону з БД за run_id (лише читання: equity_point.risk_state);
    (б) файлу рядів var_series.npz, який пише scripts/var_backtest.py (область full / oos / run);
    (в) прогону, порахованого тут же (--db / --fixture + --params, область full або зчеплені OOS-фолди);
    з --persist (лише --db, повне вікно) — прогін записується в БД існуючим шляхом API
    (api.backtest_runner.DbBacktestRunner: паспорт RUNNING → рушій → workers.persist → DONE) і малюється з БД.
Автор: Андрій Жук, 2026.

Запуск (димовий): uv run python scripts/plot_equity.py --fixture --smoke
                  uv run python scripts/plot_equity.py --run-id 89619416-720b-4880-b4e4-26d2c74bca68
Повний (наступна хвиля): uv run python scripts/plot_equity.py --db --symbol BTCUSDT --params @<обрана точка> \
    --persist --out docs/figures      (друкує run_id; потім --run-id <id> для повторного малювання)
Вивід (--out): equity_<мітка>.png, plot_equity_<мітка>.{json,md}.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path
from typing import Any
from uuid import UUID

import numpy as np

from fuzzhelm.backtest import experiments_analysis as ea

EXPERIMENT = "plot_equity"


def from_npz(path: Path, scope: str) -> dict[str, Any]:
    z = np.load(path)
    keys = {k.split("__", 1)[1]: k for k in z.files if k.startswith(f"{scope}__")}
    if not keys:
        scopes = sorted({k.split("__", 1)[0] for k in z.files})
        raise SystemExit(f"{path}: no scope {scope!r} (have {scopes})")
    s = {k: z[v] for k, v in keys.items()}
    return {"ts_ns": s["ts_ns"].tolist(), "equity": s["equity"], "states": ea.state_names(s["state"]),
            "boundaries": [int(b) for b in s.get("boundaries", np.zeros(0))][1:],
            "label": f"{path.parent.name}_{scope}",       # каталог виводу var_backtest (напр. var_BTCUSDT)
            "source": f"{path} [{scope}]"}


def npz_source_passport(path: Path) -> dict[str, Any]:
    """Паспорт var_backtest, що записав var_series.npz (сусідній var_backtest.json), — у паспорт рисунка."""
    import json  # noqa: PLC0415

    src = path.parent / "var_backtest.json"
    if not src.exists():
        return {}
    sp = json.loads(src.read_text(encoding="utf-8")).get("passport", {})
    return {"source_passport": sp,
            **{k: sp[k] for k in ("dataset_hash", "source", "n_bars", "symbol", "config_hashes") if k in sp}}


def from_db(run_id: str, database_url: str | None,
            data: dict[str, Any] | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    """Крива прогону з БД (лише читання) або з уже прочитаних рядків `data` (ea.read_run)."""
    if data is None:
        data = asyncio.run(ea.load_db_run(UUID(run_id), database_url))
    run, curve = data["run"], data["curve"]
    s = ea.series_from_curve(curve)
    meta = {"run_id": str(run.id), "run_kind": run.kind, "run_status": run.status, "run_git_sha": run.git_sha,
            "dataset_hash": run.dataset_hash.hex(), "config_hashes": {"run": run.config_hash.hex()},
            "equity_hash": run.equity_hash.hex() if run.equity_hash else None, "seed_of_run": run.seed,
            "run_git_dirty": (data.get("metrics") or {}).get("git_dirty")}
    return ({"ts_ns": s["ts_ns"].tolist(), "equity": s["equity"], "states": s["state_names"],
             "boundaries": [], "label": f"run_{str(run.id)[:8]}", "source": f"db run {run.id}"}, meta)


def from_compute(args: argparse.Namespace, argv: list[str],
                 scope: str) -> tuple[dict[str, Any], dict[str, Any]]:
    args.scope = scope
    run = ea.run_variant_experiment(args, EXPERIMENT, argv,
                                    lambda cfg: [ea.Variant("chosen", "обрана конфігурація", cfg)])
    res = run.results["chosen"][scope]
    ret = np.concatenate([r["series"]["returns"] for r in res])
    eq = res[0]["costs"]["equity_start"] * np.concatenate(([1.0], np.cumprod(1.0 + ret)))
    ts = np.concatenate([res[0]["series"]["ts_ns"][:1]] + [r["series"]["ts_ns"][1:] for r in res])
    st = np.concatenate([res[0]["series"]["state"][:1]] + [r["series"]["state"][1:] for r in res])
    bounds, acc = [], 0
    for r in res[:-1]:
        acc += int(r["n_eval_bars"])
        bounds.append(acc)
    meta = {"passport": run.passport, "summary": run.summaries[scope]["chosen"],
            "equity_hashes": [r["equity_hash"] for r in res]}
    return ({"ts_ns": ts.tolist(), "equity": eq, "states": ea.state_names(st), "boundaries": bounds,
             "label": f"{run.dataset.instrument.symbol_venue}_{scope}", "source": run.dataset.source}, meta)


def code_git_runner(gs: dict[str, Any]) -> Any:  # pragma: no cover — імпортує API
    """Підклас DbBacktestRunner, чий паспорт несе git_dirty = зміни КОДУ (поза artifacts/, docs/), як у
    прогонах exp_search, а не сирий `git status`: виводи попередніх кроків хвилі (docs/report_tables,
    artifacts/) не роблять прогін «з незакоміченого коду». Стан git знято ДО обчислень (`gs`)."""
    from fuzzhelm.api.backtest_runner import DbBacktestRunner  # noqa: PLC0415

    class Runner(DbBacktestRunner):
        async def _git(self) -> tuple[str | None, bool | None]:
            return gs["sha"], gs["dirty"]

    return Runner


async def run_or_duplicate(factory: Any, runner_cls: Any, run_id: UUID,
                           spec: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """(результат раннера, None) або (None, опис ідентичного прогону). Ідентичний прогін (ux_run_identity:
    config_hash, dataset_hash, seed, engine, git_sha; kind до ідентичності НЕ входить) читається тією самою
    фабрикою сесій — у режимі rollback це та сама транзакція."""
    from fuzzhelm.api.backtest_runner import DuplicateRunError  # noqa: PLC0415
    from fuzzhelm.config import ROOT  # noqa: PLC0415
    from fuzzhelm.storage.repositories import RunRepo  # noqa: PLC0415

    try:
        return await runner_cls(factory, data_dir=ROOT / "data")(run_id, spec), None
    except DuplicateRunError as e:
        async with factory() as s:
            dup = await RunRepo(s).get(e.existing_id)
        if dup is None:
            raise
        return None, {"duplicate_of": str(dup.id), "duplicate_kind": dup.kind, "duplicate_status": dup.status,
                      "duplicate_equity_hash": dup.equity_hash.hex() if dup.equity_hash else None}


def persist(  # pragma: no cover — пише в БД
        args: argparse.Namespace) -> tuple[str, dict[str, Any], dict[str, Any] | None]:
    """Обрана конфігурація → БД через DbBacktestRunner (той самий шлях, що й POST /backtests: паспорт
    RUNNING → рушій → workers.persist → DONE з journal_head_hash / equity_hash).

    --persist-mode commit — повне вікно data/dataset_window.json, записується назавжди (код без незакомічених
    змін, інакше відмова без --allow-dirty); rollback — той самий шлях у транзакції, яку наприкінці ВІДКОЧЕНО
    (перевірка запису на головній БД без слідів; з --smoke — перші --smoke-days доби вікна): рядки прогону
    перечитуються в тій самій транзакції до відкату.

    Повертає (run_id, info, прочитане_в_транзакції | None). Ідентичний прогін уже є — напр. клітинка сітки
    kind=grid_cell з тими самими параметрами: нового рядка немає, info["duplicate_of"] = його id і
    equity_hash.
    """
    import json  # noqa: PLC0415

    from sqlalchemy.ext.asyncio import async_sessionmaker  # noqa: PLC0415

    from fuzzhelm.api.backtest_runner import PARAM_KEYS  # noqa: PLC0415
    from fuzzhelm.config import ROOT  # noqa: PLC0415
    from fuzzhelm.infra.wallclock import new_run_id  # noqa: PLC0415
    from fuzzhelm.storage.models import APP_ROLE  # noqa: PLC0415
    from fuzzhelm.storage.session import make_engine, session_factory  # noqa: PLC0415

    params = ea.parse_params(args.params)
    bad = set(params) - set(PARAM_KEYS)
    if bad:
        raise SystemExit(f"--persist supports only {sorted(PARAM_KEYS)}; got {sorted(bad)}")
    win = json.loads((ROOT / "data" / "dataset_window.json").read_text(encoding="utf-8"))
    meta = win["symbols"][args.symbol]
    ts_from, ts_to = win["window"]["start_ms"] * 1_000_000, win["window"]["end_ms"] * 1_000_000
    if args.smoke:                      # лише rollback (load_curve це перевіряє)
        ts_to = min(ts_to, ts_from + round(args.smoke_days * 86_400) * 1_000_000_000)
    spec = {"symbol": meta["symbol_canon"], "tf": "1m", "ts_from_ns": ts_from, "ts_to_ns": ts_to,
            "seed": args.seed if args.seed is not None else ea.profile_seed(), "engine": "mamdani",
            "params": params}
    cfg = ea.base_config(params)
    gs = ea.git_state()
    mode = args.persist_mode
    print(f"persist ({mode}) spec: {json.dumps(spec)}; expected config_hash {cfg.config_hash}; "
          f"git_sha {gs['sha']} (code dirty={gs['dirty']})")
    info: dict[str, Any] = {"mode": mode, "spec": spec, "expected_config_hash": cfg.config_hash,
                            "git_sha": gs["sha"], "git_dirty": gs["dirty"],
                            "git_dirty_paths": gs["dirty_paths"]}
    if args.dry_run:
        return "", info, None
    if mode == "commit" and (gs["sha"] is None or gs["dirty"]) and not args.allow_dirty:
        raise SystemExit("refusing to commit a run from uncommitted code (numbers must come from a clean "
                         f"git_sha; dirty: {gs['dirty_paths'][:5]}): commit first or pass --allow-dirty")
    runner_cls = code_git_runner(gs)
    rid = new_run_id()

    async def go() -> tuple[dict[str, Any] | None, dict[str, Any] | None, dict[str, Any] | None]:
        engine = make_engine(args.database_url, role=APP_ROLE, null_pool=True)
        try:
            if mode == "commit":
                return (*await run_or_duplicate(session_factory(engine), runner_cls, rid, spec), None)
            async with engine.connect() as conn:
                trans = await conn.begin()
                try:
                    # кожен session_scope раннера — SAVEPOINT у зовнішній транзакції; відкат — наприкінці
                    factory = async_sessionmaker(bind=conn, expire_on_commit=False, autoflush=False,
                                                 join_transaction_mode="create_savepoint")
                    out, dup = await run_or_duplicate(factory, runner_cls, rid, spec)
                    back = None
                    if out is not None:
                        async with factory() as s:
                            back = await ea.read_run(s, rid)
                    return out, dup, back
                finally:
                    await trans.rollback()
        finally:
            await engine.dispose()

    out, dup, back = asyncio.run(go())
    if out is None:
        assert dup is not None
        print(f"identical run already stored: {dup['duplicate_of']} (kind {dup['duplicate_kind']}, status "
              f"{dup['duplicate_status']}); nothing written")
        info.update(dup)
        return "", info, None
    info.update({"run_id": str(out["run_id"]), "config_hash": out["config_hash"],
                 "dataset_hash": out["dataset_hash"], "equity_hash": out["equity_hash"],
                 "config_hash_matches": out["config_hash"] == cfg.config_hash,
                 "rows": {k: v for k, v in out.items() if k not in ("run_id", "config_hash", "dataset_hash",
                                                                    "equity_hash")}})
    if back is not None:
        run = back["run"]
        info.update({"rolled_back": True, "read_back": {
            "status": run.status, "kind": run.kind, "git_sha": run.git_sha,
            "equity_hash_matches": (run.equity_hash.hex() if run.equity_hash else None) == out["equity_hash"],
            "equity_points": len(back["curve"]), "git_dirty_metric": back["metrics"].get("git_dirty"),
            "n_metrics": len(back["metrics"])}})
        print(f"rolled back run {out['run_id']}: status {run.status}, {len(back['curve'])} equity points "
              f"read back, equity_hash matches {info['read_back']['equity_hash_matches']}, "
              f"config_hash matches {info['config_hash_matches']}; nothing stays in the DB")
    else:
        print(f"persisted run {out['run_id']}: config_hash {out['config_hash']}, "
              f"equity_hash {out['equity_hash']}")
    return str(out["run_id"]), info, back


def load_curve(args: argparse.Namespace, full_argv: list[str],
               ap: argparse.ArgumentParser) -> tuple[dict[str, Any], dict[str, Any]]:
    """Крива для рисунка з обраного джерела (див. докстрінг модуля)."""
    if args.persist:
        if not args.db:
            ap.error("--persist needs --db")
        if args.smoke and args.persist_mode != "rollback":
            ap.error("--persist with --smoke only in --persist-mode rollback (smoke rows never stay)")
        rid, info, back = persist(args)
        if args.dry_run:
            return {}, {"persist": info}
        if back is not None:                     # rollback: рядки перечитано в транзакції до відкату
            data, meta = from_db(rid, args.database_url, back)
            data["label"] += "_rolled_back"
            ds = ea.load_source(args)            # той самий набір, що в експериментах (шлях скриптів)
            info["dataset_hash_matches_experiments"] = ds.dataset_hash == info.get("dataset_hash")
            print(f"dataset_hash of the API path matches the experiment loader: "
                  f"{info['dataset_hash_matches_experiments']}")
        else:
            if not rid and info.get("duplicate_kind") == "backtest":
                rid = info["duplicate_of"]       # той самий бектест уже записано — малюємо його криву з БД
            if rid:
                data, meta = from_db(rid, args.database_url)
            else:                                # ідентичний прогін є: крива — з прогону тут же + звірка хеша
                data, meta = from_compute(args, full_argv, "full")
                dup_hash = info.get("duplicate_equity_hash")
                info["equity_hash_matches_db"] = (None if dup_hash is None
                                                  else meta["equity_hashes"][0] == dup_hash)
                print(f"equity_hash of the computed curve matches the stored run: "
                      f"{info['equity_hash_matches_db']}")
        meta["persist"] = info
        return data, meta
    if args.run_id:
        return from_db(args.run_id, args.database_url)
    if args.series:
        return (from_npz(args.series, args.scope if args.scope != "both" else "full"),
                npz_source_passport(args.series))
    if args.scope == "both":
        ap.error("choose --scope full or --scope oos for a computed curve")
    return from_compute(args, full_argv, args.scope)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Equity + drawdown + risk-state bands for a stored run, a "
                                             "var_series.npz file, or a run computed here.")
    ea.add_common_args(ap, EXPERIMENT)
    ea.add_variant_args(ap, default_scope="full")
    ap.add_argument("--run-id", default=None, help="stored run (read-only DB)")
    ap.add_argument("--series", type=Path, default=None, help="var_series.npz written by var_backtest.py")
    ap.add_argument("--persist", action="store_true",
                    help="with --db: store the full-window run via DbBacktestRunner, then plot it")
    ap.add_argument("--persist-mode", choices=("commit", "rollback"), default="commit",
                    help="commit: store the full-window run for good; rollback: the same write path inside a "
                         "transaction that is rolled back at the end (checks the DB path, leaves no rows; "
                         "allows --smoke)")
    ap.add_argument("--allow-dirty", action="store_true",
                    help="--persist-mode commit from uncommitted code (the passport then says git_dirty = 1)")
    ap.add_argument("--dry-run", action="store_true", help="with --persist: print the spec, write nothing")
    ap.add_argument("--title", default=None)
    args = ap.parse_args(argv)
    full_argv = [sys.argv[0], *(sys.argv[1:] if argv is None else argv)]
    t0 = time.perf_counter()
    seed = args.seed if args.seed is not None else ea.profile_seed()
    data, meta = load_curve(args, full_argv, ap)
    if not data:                                 # --persist --dry-run: лише специфікація
        return 0
    if meta.get("seed_of_run") is not None:
        seed = int(meta["seed_of_run"])          # паспорт рисунка прогону з БД — seed самого прогону
    pp = meta.pop("passport", None) or ea.passport(EXPERIMENT, full_argv, seed=seed, extra=meta)
    fig = args.out / f"equity_{data['label']}.png"
    title = args.title or f"Крива капіталу і режими ризику: {data['source']}"
    ea.plot_equity_figure(fig, data["ts_ns"], data["equity"], states=data["states"], title=title,
                          boundaries=data["boundaries"])
    eq = np.asarray(data["equity"], dtype=np.float64)
    counts = {s.value: data["states"].count(s.value) for s in ea.STATE_ORDER}
    spans = ea.state_spans(data["states"])
    summary = {"points": int(eq.size), "equity_first": eq[0].item(), "equity_last": eq[-1].item(),
               "total_return": (eq[-1] / eq[0] - 1.0).item(),
               "max_drawdown": (1.0 - eq / np.maximum.accumulate(eq)).max().item(),
               "bars_per_state": counts, "state_changes": max(0, len(spans) - 1)}
    md = "\n".join([
        f"# Крива капіталу і режими ризику: {data['source']}", "",
        "Згенеровано `scripts/plot_equity.py`. Автор: Андрій Жук, 2026.", "",
        ea.passport_md(pp), "",
        ea.md_table(["величина", "значення"], [
            ["точок", summary["points"]], ["капітал на початку, USDT", summary["equity_first"]],
            ["капітал наприкінці, USDT", summary["equity_last"]], ["дохідність", summary["total_return"]],
            ["MaxDD", summary["max_drawdown"]], ["змін стану", summary["state_changes"]],
            *[[f"барів у {k}", v] for k, v in counts.items()]]),
        "", f"Рисунок: `{fig}`.", ""])
    result = {"experiment": EXPERIMENT, "passport": pp, "meta": meta, "summary": summary, "figure": str(fig)}
    paths = ea.write_outputs(args.out, f"{EXPERIMENT}_{data['label']}", result=result, md=md)
    print(f"points {summary['points']}, return {summary['total_return']:.6f}, "
          f"maxdd {summary['max_drawdown']:.6f}, "
          f"bars per state {counts}; wall {time.perf_counter() - t0:.1f} s")
    print("wrote " + ", ".join(str(p) for p in [*paths, fig]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
