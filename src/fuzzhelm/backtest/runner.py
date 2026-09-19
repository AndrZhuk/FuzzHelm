"""Воркери grid-пошуку і walk-forward: чисті функції верхнього рівня над numpy-масивами (без БД і event loop).

Найменування: backtest/runner.py
Призначення: клітинка сітки = один прогін TradingLoop над сегментом даних → метрики (§5.16); walk-forward:
    на IS кожного фолду — сітка і вибір параметрів, на OOS — оцінка вибраних (і, за бажання, усіх) клітинок;
    замір швидкодії циклу (мкс/бар) для обґрунтування здійсненності сітки.
Автор: Андрій Жук, 2026.

Незалежність від кількості воркерів і порядку: кожна задача — (параметри, масиви сегмента, seed, конфіг як
dict) → детермінований результат (TradingLoop не має глобального стану, ГВЧ — лише PCG64(seed) моделі витрат);
backtest.parallel.run_parallel повертає результати в порядку задач. Усі клітинки однієї сітки отримують той
самий seed (спільні випадкові числа: відмінність метрик — від параметрів, а не від шуму ковзання).

Walk-forward (backtest.walkforward.Fold): IS_i = [s, s+is−E), embargo E = 2·max_lookback, OOS_i = [s+is, …).
IS-прогін прогрівається всередині IS; OOS-прогін стартує за W = прогрів барів до OOS — усередині embargo
(W ≤ E), тож жоден бар IS не впливає ні на ознаки, ні на стан рушія в OOS.
"""

from __future__ import annotations

import argparse
import math
import statistics
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from fuzzhelm.backtest.dataset import Dataset, load_fixture_dataset
from fuzzhelm.backtest.engine import GRID_KEYS, BacktestConfig, run_backtest
from fuzzhelm.backtest.grid import make_grid
from fuzzhelm.backtest.parallel import run_parallel
from fuzzhelm.backtest.pareto import pareto_front
from fuzzhelm.backtest.walkforward import Fold, folds_from_profile


def _config(config: Mapping[str, Any] | BacktestConfig | None) -> BacktestConfig:
    if config is None:
        return BacktestConfig()
    if isinstance(config, BacktestConfig):
        return config
    return BacktestConfig.from_dict(config)


def run_cell(params: Mapping[str, Any], arrays: Mapping[str, Any], seed: int, *,
             config: Mapping[str, Any] | None = None, eval_start: int = 0,
             equity_hash: bool = False) -> dict[str, Any]:
    """Одна клітинка: параметри сітки (n_atr, chi, u_enter, rho_base, lam) над масивами сегмента.

    Повертає 17 метрик + доповнення (sr_period, skew, kurt, psr, n_obs, n_fills, traded_notional, halted),
    усі — float; з `equity_hash=True` ще й SHA-256 кривої капіталу (рядок) для перевірок відтворюваності.
    """
    bad = set(params) - set(GRID_KEYS)
    if bad:
        raise ValueError(f"unknown grid parameters {sorted(bad)}; expected {GRID_KEYS}")
    cfg = _config(config).with_params(**dict(params), record_traces="none")
    ds = Dataset.from_payload(arrays)
    res = run_backtest(ds, cfg, seed, eval_start=eval_start, git=False, hash_equity=equity_hash)
    out: dict[str, Any] = {**res.metrics, **res.extras}
    if equity_hash:
        out["equity_hash"] = res.equity_hash
    return out


def cell_task(task: Mapping[str, Any]) -> dict[str, Any]:
    """Точка входу воркера ProcessPoolExecutor (функція верхнього рівня — pickle, старт "spawn")."""
    return run_cell(task["params"], task["arrays"], task["seed"], config=task.get("config"),
                    eval_start=task.get("eval_start", 0), equity_hash=task.get("equity_hash", False))


def grid_tasks(dataset: Dataset, cells: Sequence[Mapping[str, Any]], seed: int, *,
               config: BacktestConfig | Mapping[str, Any] | None = None, eval_start: int = 0,
               equity_hash: bool = False) -> list[dict[str, Any]]:
    cfg = _config(config).to_dict()
    payload = dataset.to_payload()
    return [{"params": dict(c), "arrays": payload, "seed": seed, "config": cfg, "eval_start": eval_start,
             "equity_hash": equity_hash} for c in cells]


def run_grid(dataset: Dataset, cells: Sequence[Mapping[str, Any]] | None = None, seed: int = 0, *,
             workers: int = 0, config: BacktestConfig | Mapping[str, Any] | None = None, eval_start: int = 0,
             equity_hash: bool = False) -> list[dict[str, Any]]:
    """Метрики кожної клітинки (у порядку `cells`, за замовчуванням — 108 клітинок backtest.grid)."""
    cells = make_grid() if cells is None else list(cells)
    tasks = grid_tasks(dataset, cells, seed, config=config, eval_start=eval_start, equity_hash=equity_hash)
    return run_parallel(cell_task, tasks, workers, seed=seed)


# ====================================================================== walk-forward


@dataclass(frozen=True)
class FoldReport:
    fold: Fold
    is_metrics: list[dict[str, Any]]            # по клітинках, порядок = cells
    selected: int                               # індекс клітинки, обраної на IS
    params: dict[str, Any]
    is_selected: dict[str, Any]
    oos_selected: dict[str, Any]
    is_pareto: list[int]                        # недомінований фронт IS (SR↑, MaxDD↓, Turnover↓)
    oos_metrics: list[dict[str, Any]] | None = None
    oos_pareto: list[int] | None = None


@dataclass(frozen=True)
class WalkForwardResult:
    cells: list[dict[str, Any]]
    folds: list[FoldReport]
    seed: int
    config_hash: str
    warmup_bars: int
    selection: str

    def summary(self) -> dict[str, float]:
        """Середнє і медіана по фолдах IS/OOS для обраних клітинок."""
        def col(side: str, key: str) -> list[float]:
            return [getattr(f, f"{side}_selected")[key] for f in self.folds]
        out: dict[str, float] = {}
        for key in ("sharpe", "total_return", "max_drawdown", "turnover", "n_trades"):
            for side in ("is", "oos"):
                vals = [v for v in col(side, key) if math.isfinite(v)]
                out[f"{side}_{key}_mean"] = statistics.fmean(vals) if vals else math.nan
                out[f"{side}_{key}_median"] = statistics.median(vals) if vals else math.nan
        return out


def _pareto(metrics: Sequence[Mapping[str, Any]]) -> list[int]:
    return pareto_front([(m["sharpe"], m["max_drawdown"], m["turnover"]) for m in metrics])


def select_cell(metrics: Sequence[Mapping[str, Any]], rule: str = "sharpe") -> int:
    """Вибір на IS: argmax Шарпа (NaN — найгірше), рівність → менший оборот → менший індекс сітки.

    argmax Шарпа завжди лежить на фронті Парето (SR↑, MaxDD↓, Turnover↓), тож вибір не суперечить
    фронту; сам фронт повертається у звіт окремо (робоча точка обґрунтовується текстом, §5.16).
    """
    if rule != "sharpe":
        raise ValueError(f"unknown selection rule {rule!r}")

    def key(i: int) -> tuple[float, float, int]:
        m = metrics[i]
        sr = m["sharpe"] if math.isfinite(m["sharpe"]) else -math.inf
        to = m["turnover"] if math.isfinite(m["turnover"]) else math.inf
        return (-sr, to, i)

    return min(range(len(metrics)), key=key)


def _oos_segment(fold: Fold, warmup: int) -> tuple[int, int]:
    start = fold.oos_start - warmup
    if start < fold.is_end:
        raise ValueError(f"fold {fold.index}: embargo {fold.embargo_bars} bars is shorter than the warm-up "
                         f"{warmup} bars — OOS warm-up would read IS bars")
    return start, warmup


def run_walkforward(dataset: Dataset, folds: Sequence[Fold] | None = None,
                    cells: Sequence[Mapping[str, Any]] | None = None, seed: int = 0, *, workers: int = 0,
                    config: BacktestConfig | Mapping[str, Any] | None = None, oos_all_cells: bool = False,
                    selection: str = "sharpe") -> WalkForwardResult:
    """IS: сітка на кожному фолді → вибір клітинки; OOS: оцінка вибраної (і всіх — з oos_all_cells).

    folds=None → backtest.walkforward.folds_from_profile (профіль backtest, embargo = 2·max_lookback).
    """
    cfg = _config(config)
    warmup = cfg.resolved_warmup()
    cells = make_grid() if cells is None else [dict(c) for c in cells]
    if folds is None:
        folds = folds_from_profile(len(dataset), max_lookback=cfg.feature_params().max_lookback)
    cfg_d = cfg.to_dict()
    segments = [_oos_segment(f, warmup) for f in folds]      # embargo ≥ прогріву — до будь-якого прогону
    is_tasks: list[dict[str, Any]] = []
    for f in folds:
        if f.is_end - f.is_start <= warmup:
            raise ValueError(f"fold {f.index}: IS window {f.is_end - f.is_start} bars <= warm-up {warmup}")
        payload = dataset.slice(f.is_start, f.is_end).to_payload()
        is_tasks += [{"params": c, "arrays": payload, "seed": seed, "config": cfg_d, "eval_start": 0}
                     for c in cells]
    is_res = run_parallel(cell_task, is_tasks, workers, seed=seed)
    n = len(cells)
    per_fold_is = [is_res[k * n:(k + 1) * n] for k in range(len(folds))]
    chosen = [select_cell(m, selection) for m in per_fold_is]

    oos_tasks: list[dict[str, Any]] = []
    for f, sel, (start, ev) in zip(folds, chosen, segments, strict=True):
        payload = dataset.slice(start, f.oos_end).to_payload()
        todo = cells if oos_all_cells else [cells[sel]]
        oos_tasks += [{"params": c, "arrays": payload, "seed": seed, "config": cfg_d, "eval_start": ev}
                      for c in todo]
    oos_res = run_parallel(cell_task, oos_tasks, workers, seed=seed)
    per = n if oos_all_cells else 1

    reports: list[FoldReport] = []
    for k, (f, sel) in enumerate(zip(folds, chosen, strict=True)):
        oos_k = oos_res[k * per:(k + 1) * per]
        reports.append(FoldReport(
            fold=f, is_metrics=per_fold_is[k], selected=sel, params=dict(cells[sel]),
            is_selected=per_fold_is[k][sel], oos_selected=oos_k[sel] if oos_all_cells else oos_k[0],
            is_pareto=_pareto(per_fold_is[k]),
            oos_metrics=oos_k if oos_all_cells else None,
            oos_pareto=_pareto(oos_k) if oos_all_cells else None,
        ))
    return WalkForwardResult(cells=cells, folds=reports, seed=seed, config_hash=cfg.config_hash,
                             warmup_bars=warmup, selection=selection)


# ====================================================================== замір швидкодії


def bench_loop(dataset: Dataset, cfg: BacktestConfig | None = None, seed: int = 0, *,
               repeats: int = 5) -> dict[str, float]:
    """Медіана часу повного циклу (мкс/бар) з record_traces=none, без паспорта кривої (як у клітинці сітки).

    Перший прогін — розігрів (кеш Decimal-барів і рушія Мамдані), він у медіану не входить.
    """
    cfg = (cfg or BacktestConfig()).with_params(record_traces="none", check_invariants=False)
    run_backtest(dataset, cfg, seed, hash_equity=False)
    times: list[float] = []
    for _ in range(max(1, repeats)):
        t0 = time.perf_counter()
        run_backtest(dataset, cfg, seed, hash_equity=False)
        times.append(time.perf_counter() - t0)
    med = statistics.median(times)
    n = len(dataset)
    return {"bars": n + 0.0, "repeats": len(times) + 0.0, "median_s": med, "us_per_bar": med / n * 1e6,
            "min_us_per_bar": min(times) / n * 1e6, "max_us_per_bar": max(times) / n * 1e6}


async def load_db_window(symbol: str) -> Dataset:  # pragma: no cover — лише CLI заміру, читає БД
    """45-денне вікно з БД (лише читання) + ставки фандингу з data/ — набір data/dataset_window.json.

    Хеш свічок звіряється з data/dataset_window.json (інакше ValueError) — замір іде саме на тих даних,
    на яких рахується експеримент. Імпорти сховища — ліниві: воркери сітки БД не торкаються.
    """
    import json  # noqa: PLC0415

    from fuzzhelm.backtest.manifest import dataset_hash  # noqa: PLC0415
    from fuzzhelm.config import ROOT  # noqa: PLC0415
    from fuzzhelm.ingest.funding import load_funding_json  # noqa: PLC0415
    from fuzzhelm.storage.repositories.candle import CandleRepo  # noqa: PLC0415
    from fuzzhelm.storage.repositories.instrument import InstrumentRepo  # noqa: PLC0415
    from fuzzhelm.storage.session import dispose_default_engine, get_default_factory  # noqa: PLC0415

    win = json.loads((ROOT / "data" / "dataset_window.json").read_text(encoding="utf-8"))
    meta = win["symbols"][symbol]
    lo, hi = win["window"]["start_ms"] * 1_000_000, win["window"]["end_ms"] * 1_000_000
    try:
        async with get_default_factory()() as s:
            row = await InstrumentRepo(s).get_by_canon(meta["symbol_canon"])
            if row is None:
                raise LookupError(f"instrument {meta['symbol_canon']!r} is not in the database")
            arr = await CandleRepo(s).load_arrays(row.id, "1m", lo, hi)
    finally:
        await dispose_default_engine()
    if dataset_hash(arr.columns()) != meta["dataset_hash"]:
        raise ValueError(f"{symbol}: candles in the DB differ from data/dataset_window.json")
    inst = row.to_dto()
    fpath = ROOT / "data" / f"funding_{symbol}.json"
    rates = load_funding_json(fpath, inst).rates if fpath.exists() else None
    return Dataset.from_candle_arrays(arr, inst, funding=rates, source=f"db:{symbol}")


def _report_run(ds: Dataset, seed: int) -> None:  # pragma: no cover — CLI заміру
    """Повний прогін з трасуванням угод і перевіркою тотожності обліку на кожному барі + шлях автомата."""
    t0 = time.perf_counter()
    res = run_backtest(ds, BacktestConfig().with_params(record_traces="trades", check_invariants=True), seed)
    dt = time.perf_counter() - t0
    m = res.metrics
    print(f"full run (trades traces, invariants every bar): {dt:.2f} s; trades {len(res.trades)}, "
          f"fills {len(res.fills)}, funding charges {len(res.funding)}, final state {res.final_state.value}, "
          f"halted_at {res.halted_at}")
    print(f"total_return {m['total_return']:.6f}  max_drawdown {m['max_drawdown']:.6f}  "
          f"sharpe {m['sharpe']:.3f}  psr {res.extras['psr']:.4f}  equity_hash {res.equity_hash[:16]}…")
    if res.transitions:
        last = res.transitions[-1]
        i_last = int((last.ts_ns - ds.t_ns[0].item()) // ds.tf_ns)
        vetoes = sum(1 for e in res.risk_events if e.rule == "risk_mode" and e.ts_ns > last.ts_ns
                     and e.verdict is not None and e.verdict.value == "VETO")
        print(f"last transition at bar {i_last}: {last.state_from.value} -> {last.state_to.value}, "
              f"DD {last.drawdown:.4f}; bars after it {len(ds) - 1 - i_last}, "
              f"risk_mode VETOs after it {vetoes}")


def main(argv: Sequence[str] | None = None) -> None:  # pragma: no cover — CLI заміру
    ap = argparse.ArgumentParser(
        description="FuzzHelm engine benchmark (fixture klines or the 45-day DB window)")
    ap.add_argument("--repeats", type=int, default=5)
    ap.add_argument("--engine", choices=("mamdani", "linear"), default="mamdani")
    ap.add_argument("--grid-bars", type=int, default=64_800, help="bars per cell for the 108-cell estimate")
    ap.add_argument("--db", metavar="SYMBOL",
                    help="read the 45-day window of SYMBOL (e.g. BTCUSDT) from the DB")
    ap.add_argument("--grid-workers", type=int, default=0,
                    help="also time the real 108-cell grid with N workers")
    ap.add_argument("--report", action="store_true", help="also do one full checked run and print its path")
    ap.add_argument("--seed", type=int, default=20260918)
    args = ap.parse_args(argv)
    if args.db:
        import asyncio  # noqa: PLC0415

        ds = asyncio.run(load_db_window(args.db))
    else:
        ds = load_fixture_dataset()
    print(f"dataset {ds.source}: {len(ds)} bars, dataset_hash {ds.dataset_hash[:16]}…")
    r = bench_loop(ds, BacktestConfig(engine=args.engine), seed=args.seed, repeats=args.repeats)
    for k, v in r.items():
        print(f"{k}: {v:.3f}")
    est = r["us_per_bar"] * 1e-6 * args.grid_bars * 108
    print(f"108 cells x {args.grid_bars} bars, 1 core (extrapolated from the median): {est:.1f} s")
    if args.report:
        _report_run(ds, args.seed)
    if args.grid_workers > 0:
        t0 = time.perf_counter()
        cells = run_grid(ds, None, args.seed, workers=args.grid_workers,
                         config=BacktestConfig(engine=args.engine))
        dt = time.perf_counter() - t0
        trades = sorted(c["n_trades"] for c in cells)
        print(f"grid {len(cells)} cells x {len(ds)} bars, workers={args.grid_workers}: wall {dt:.1f} s; "
              f"n_trades min/median/max {trades[0]:.0f}/{statistics.median(trades):.0f}/{trades[-1]:.0f}")


if __name__ == "__main__":  # pragma: no cover
    main()
