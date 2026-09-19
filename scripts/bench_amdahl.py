"""Замір прискорення S(p) пулу процесів на підмножині клітинок сітки і аналіз розриву до межі Амдала; фаза 7.

Найменування: scripts/bench_amdahl.py
Призначення: брифінг §5.16, §12 фаза 7 («S(p) заміряно для p ∈ {1,2,4,8}»), §16 ФК9/ФК12: T(p) для
    p ∈ {1,2,4,8} на фіксованій підмножині клітинок (повторів ≥ 3, медіана), S(p) = T(1)/T(p), НК-оцінка
    послідовної частки f у S ≤ 1/(f + (1−f)/p), метрика Карпа–Флатта і розклад накладних витрат: розмір і
    час pickle numpy-вантажу задачі, старт пулу з імпортами, передача результатів, нерівномірність розкладу,
    «здорожчання» обчислень при p одночасних процесах (гетерогенні ядра P/E, спільна пам'ять). Модель
    процесора, ядра і load average до/після — у виводі; якщо load average > --max-load на старті — відмова
    (або гучне попередження з --force).
Автор: Андрій Жук, 2026.

Запуск (повний, на ТИХІЙ машині):  uv run python scripts/bench_amdahl.py --db --symbol BTCUSDT --cells 16
Швидка перевірка:                   uv run python scripts/bench_amdahl.py --fixture --smoke --force

T(p) — стіна `backtest.parallel.run_parallel(segment_task, tasks, p)` ВКЛЮЧНО зі стартом пулу "spawn"
(це частина послідовних витрат). T(1) — пул з одним процесом (ті самі накладні витрати, що й для p > 1);
окремо — T_seq (workers = 0, у поточному процесі, без пулу) як еталон чистих обчислень. Порядок замірів
чергується (для кожного повтору — усі p), щоб дрейф фонового навантаження не зсував одне p систематично.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import pickle
import platform
import statistics
import subprocess
import sys
import time
from datetime import UTC, datetime
from typing import Any

from fuzzhelm.backtest.engine import BacktestConfig
from fuzzhelm.backtest.experiments_search import (
    add_common_args,
    amdahl_fit,
    fmt,
    git_state,
    json_safe,
    make_task,
    md_table,
    overhead_breakdown,
    profile_seed,
    provenance,
    provenance_md,
    resolve_dataset,
    resolve_out,
    segment_task,
    startup_probe,
    subset_indices,
)
from fuzzhelm.backtest.grid import load_grid_space, make_grid
from fuzzhelm.backtest.parallel import run_parallel

NAME = "amdahl"
SYSCTL_KEYS = ("machdep.cpu.brand_string", "hw.ncpu", "hw.physicalcpu", "hw.logicalcpu", "hw.memsize",
               "hw.perflevel0.name", "hw.perflevel0.physicalcpu", "hw.perflevel1.name",
               "hw.perflevel1.physicalcpu")


def machine_info() -> dict[str, Any]:
    info: dict[str, Any] = {"platform": platform.platform(), "python": sys.version.split()[0],
                            "os_cpu_count": os.cpu_count()}
    for key in SYSCTL_KEYS:
        try:
            r = subprocess.run(["sysctl", "-n", key], capture_output=True, text=True, timeout=5, check=False)
            if r.returncode == 0 and r.stdout.strip():
                info[key] = r.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            pass
    if "machdep.cpu.brand_string" not in info and os.path.exists("/proc/cpuinfo"):
        with open("/proc/cpuinfo", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("model name"):
                    info["cpu_model"] = line.split(":", 1)[1].strip()
                    break
    return info


def load1() -> float:
    return os.getloadavg()[0]


def payload_stats(tasks: list[dict[str, Any]], repeats: int = 5) -> dict[str, float]:
    """Розмір і час pickle.dumps/loads однієї задачі (масиви сегмента + конфіг) у батьківському процесі."""
    t = tasks[0]
    blob = pickle.dumps(t, protocol=pickle.HIGHEST_PROTOCOL)
    arrays = pickle.dumps(t["arrays"], protocol=pickle.HIGHEST_PROTOCOL)
    config = pickle.dumps(t["config"], protocol=pickle.HIGHEST_PROTOCOL)
    dumps, loads = [], []
    for _ in range(repeats):
        a = time.perf_counter()
        b = pickle.dumps(t, protocol=pickle.HIGHEST_PROTOCOL)
        dumps.append(time.perf_counter() - a)
        a = time.perf_counter()
        pickle.loads(b)
        loads.append(time.perf_counter() - a)
    return {"task_bytes": len(blob), "arrays_bytes": len(arrays), "config_bytes": len(config),
            "dumps_s": statistics.median(dumps), "loads_s": statistics.median(loads)}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="FuzzHelm speed-up S(p) of the grid worker pool vs Amdahl's law")
    add_common_args(ap)
    ap.add_argument("--ps", default="1,2,4,8", help="pool sizes (must include 1)")
    ap.add_argument("--cells", type=int, default=16, help="fixed evenly spaced subset of the 108-cell grid")
    ap.add_argument("--repeats", type=int, default=3, help="repeats per p (median is reported), >= 3")
    ap.add_argument("--max-load", type=float, default=1.5, help="refuse if the 1-min load average is higher")
    ap.add_argument("--force", action="store_true", help="measure anyway on a busy machine (loud warning)")
    ap.add_argument("--no-seq", action="store_true", help="skip the in-process T_seq reference")
    args = ap.parse_args(argv)
    argv_full = [sys.argv[0], *(sys.argv[1:] if argv is None else argv)]
    if args.smoke:
        args.cells = min(args.cells, 4)
        if args.ps == "1,2,4,8":
            args.ps = "1,2"
    ps = sorted({int(x) for x in args.ps.split(",")})
    if 1 not in ps:
        raise SystemExit("--ps must include 1 (T(1) is the baseline)")
    if args.repeats < 3:
        print(f"WARNING: --repeats {args.repeats} < 3: the median is not robust", file=sys.stderr)
    load_start = os.getloadavg()
    busy = load_start[0] > args.max_load
    if busy and not args.force:
        print(f"REFUSED: 1-min load average {load_start[0]:.2f} > {args.max_load}: other processes would "
              f"distort T(p). Stop them or pass --force (the output is then marked as measured on a busy "
              f"machine).", file=sys.stderr)
        return 2
    if busy:
        print("!" * 100 + f"\nWARNING: load average {load_start[0]:.2f} > {args.max_load} — T(p) measured "
              "on a BUSY machine; do not report these numbers as the experiment\n" + "!" * 100,
              file=sys.stderr)
    seed = profile_seed("grid") if args.seed is None else args.seed
    gs = git_state()
    out = resolve_out(args, NAME)
    ds = resolve_dataset(args)
    cfg = BacktestConfig.from_profile("grid", cooldown_policy=args.cooldown_policy)
    grid = make_grid(load_grid_space())
    ids = subset_indices(len(grid), args.cells)
    payload = ds.to_payload()
    cfg_d = cfg.to_dict()
    tasks = [make_task(payload, cfg_d, seed, params=grid[i]) for i in ids]
    pay = payload_stats(tasks)
    print(f"dataset {ds.source}: {len(ds)} bars; {len(tasks)} tasks; "
          f"task pickle {pay['task_bytes'] / 1e6:.2f} MB (dumps {pay['dumps_s'] * 1e3:.2f} ms, "
          f"loads {pay['loads_s'] * 1e3:.2f} ms); ps {ps}; "
          f"repeats {args.repeats}; load {load_start[0]:.2f}")

    runs: dict[int, list[dict[str, Any]]] = {p: [] for p in ps}
    startups: dict[int, list[float]] = {p: [] for p in ps}
    seq: list[float] = []
    loads: list[dict[str, Any]] = []
    result_bytes = 0
    ref_sharpe: list[str] | None = None
    for rep in range(args.repeats):
        for p in ps:
            a = time.perf_counter()
            probe = run_parallel(startup_probe, list(range(p)), p, seed=seed)
            startups[p].append(time.perf_counter() - a)
            l0 = load1()
            a = time.perf_counter()
            res = run_parallel(segment_task, tasks, p, seed=seed)
            wall = time.perf_counter() - a
            l1 = load1()
            runs[p].append({"wall_s": wall, "task_walls": [r["wall_s"] for r in res],
                            "task_pids": [r["pid"] for r in res],
                            "probe_pids": len({x["pid"] for x in probe}),
                            "load_before": l0, "load_after": l1})
            loads.append({"rep": rep, "p": p, "before": l0, "after": l1})
            result_bytes = max(result_bytes,
                               *(len(pickle.dumps(r, protocol=pickle.HIGHEST_PROTOCOL)) for r in res))
            sharpe = [repr(r["sharpe"]) for r in res]          # repr: NaN == NaN
            if ref_sharpe is None:
                ref_sharpe = sharpe
            elif sharpe != ref_sharpe:
                raise SystemExit(f"results differ between runs (p={p}) — determinism is broken")
            print(f"  rep {rep} p={p}: T = {wall:.2f} s (startup probe {startups[p][-1]:.2f} s), "
                  f"load {l0:.2f} -> {l1:.2f}")
        if not args.no_seq:
            a = time.perf_counter()
            run_parallel(segment_task, tasks, 0, seed=seed)
            seq.append(time.perf_counter() - a)
            print(f"  rep {rep} in-process: T_seq = {seq[-1]:.2f} s")
    load_end = os.getloadavg()

    def median_run(p: int) -> dict[str, Any]:
        rs = sorted(runs[p], key=lambda r: r["wall_s"])
        return rs[(len(rs) - 1) // 2]

    t_med = {p: statistics.median(r["wall_s"] for r in runs[p]) for p in ps}
    fit = amdahl_fit(t_med)
    compute_p1 = sum(median_run(1)["task_walls"])
    breakdown = {p: overhead_breakdown(p=p, wall_s=median_run(p)["wall_s"],
                                       startup_s=statistics.median(startups[p]),
                                       task_walls=median_run(p)["task_walls"],
                                       task_pids=median_run(p)["task_pids"],
                                       pickle_s_per_task=pay["dumps_s"], compute_s_p1=compute_p1) for p in ps}
    # «структурна» послідовна частка: лише справді послідовні складові T(1) — старт пулу і серіалізація задач
    f_struct = (breakdown[1]["startup_s"] + breakdown[1]["pickle_serial_s"]) / t_med[1]
    pmax = max(ps)
    per_pid: dict[int, list[float]] = {}
    for w, pid in zip(median_run(pmax)["task_walls"], median_run(pmax)["task_pids"], strict=True):
        per_pid.setdefault(pid, []).append(w)
    pid_means = sorted(statistics.fmean(v) for v in per_pid.values())
    t_seq = statistics.median(seq) if seq else None
    created = datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    machine = machine_info()
    prov = provenance(argv_full, ds, cfg, seed=seed, workers=pmax, created_utc=created, git=gs,
                      extra={"script": NAME, "smoke": args.smoke, "cells": ids, "busy_machine": busy})
    result = {
        "provenance": prov, "machine": machine,
        "load": {"start": list(load_start), "end": list(load_end), "per_measurement": loads,
                 "max_load_allowed": args.max_load, "busy_machine": busy},
        "tasks": len(tasks), "bars_per_task": len(ds), "payload": {**pay, "result_bytes_max": result_bytes},
        "repeats": args.repeats, "times_all": {p: [r["wall_s"] for r in runs[p]] for p in ps},
        "startup_all": startups, "t_median": t_med, "t_seq_all": seq, "t_seq_median": t_seq,
        "amdahl": fit, "serial_fraction_structural": f_struct,
        "speedup_vs_seq": {p: (t_seq / t_med[p]) if t_seq else None for p in ps},
        "overhead": breakdown, "per_process_mean_task_s_at_pmax": pid_means,
        "process_speed_spread_at_pmax": (pid_means[-1] / pid_means[0]) if pid_means else None,
    }
    stem = f"{NAME}_{ds.instrument.symbol_venue}"
    (out / f"{stem}.json").write_text(json.dumps(json_safe(result), ensure_ascii=False, indent=1),
                                      encoding="utf-8")
    with (out / f"{stem}.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["p", "T_median_s", "T_all_s", "S", "amdahl_bound", "efficiency", "karp_flatt",
                    "startup_s", "compute_s", "ideal_parallel_s", "makespan_s", "imbalance_s", "residual_s",
                    "pickle_serial_s", "cpu_inflation", "processes_used"])
        for p in ps:
            b = breakdown[p]
            w.writerow([p, t_med[p], " ".join(f"{x:.4f}" for x in result["times_all"][p]), fit["speedup"][p],
                        fit["bound"][p], fit["efficiency"][p], fit["karp_flatt"].get(p, ""), b["startup_s"],
                        b["compute_s"], b["ideal_parallel_s"], b["makespan_s"], b["imbalance_s"],
                        b["residual_s"], b["pickle_serial_s"], b["cpu_inflation"], b["processes_used"]])
    (out / f"{stem}.md").write_text(amdahl_md(result, ps), encoding="utf-8")
    print(f"f (least squares on S) = {fit['serial_fraction']:.4f}; structural f = {f_struct:.4f}; "
          + ", ".join(f"S({p}) = {fit['speedup'][p]:.2f}" for p in ps))
    print(f"load average start {load_start[0]:.2f} end {load_end[0]:.2f}; outputs {out / stem}.*")
    return 0


def amdahl_md(r: dict[str, Any], ps: list[int]) -> str:
    m, fit, pay = r["machine"], r["amdahl"], r["payload"]
    cpu = m.get("machdep.cpu.brand_string") or m.get("cpu_model") or "невідомо"
    cores = (f"{m.get('hw.ncpu', m['os_cpu_count'])} логічних; {m.get('hw.perflevel0.physicalcpu', '?')} "
             f"{m.get('hw.perflevel0.name', 'P')} + {m.get('hw.perflevel1.physicalcpu', '?')} "
             f"{m.get('hw.perflevel1.name', 'E')}" if "hw.perflevel1.physicalcpu" in m
             else f"{m.get('hw.ncpu', m['os_cpu_count'])} логічних")
    busy = r["load"]["busy_machine"]
    sym = r["provenance"]["dataset"]["symbol"]
    lines = [f"# Прискорення S(p) пулу воркерів сітки і закон Амдала ({sym})", ""]
    if busy:
        lines += [f"> **УВАГА: заміряно на ЗАВАНТАЖЕНІЙ машині (load average {r['load']['start'][0]:.2f} > "
                  f"{r['load']['max_load_allowed']}). Ці числа не є результатом експерименту.**", ""]
    lines += [provenance_md(r["provenance"]), "", "## Машина і навантаження", "",
              md_table(["поле", "значення"], [
                  ["процесор", cpu], ["ядра", cores],
                  ["пам'ять", f"{int(m['hw.memsize']) / 2**30:.0f} ГіБ" if "hw.memsize" in m else "—"],
                  ["ОС / Python", f"{m['platform']} / {m['python']}"],
                  ["load average (1/5/15 хв) на старті", " / ".join(f"{x:.2f}" for x in r["load"]["start"])],
                  ["load average наприкінці", " / ".join(f"{x:.2f}" for x in r["load"]["end"])],
                  ["найбільший 1-хв load під час замірів",
                   f"{max(x['after'] for x in r['load']['per_measurement']):.2f}"]]), "",
              "## Задача", "",
              f"Задач (клітинок сітки): {r['tasks']}, барів на задачу: {r['bars_per_task']}; кожна несе "
              "власну копію "
              "масивів: "
              f"pickle задачі {pay['task_bytes'] / 1e6:.3f} МБ (масиви {pay['arrays_bytes'] / 1e6:.3f} МБ, "
              f"конфіг {pay['config_bytes'] / 1e3:.1f} КБ), dumps {pay['dumps_s'] * 1e3:.2f} мс, loads "
              f"{pay['loads_s'] * 1e3:.2f} мс; результат ≤ {pay['result_bytes_max']} Б. Повторів на p: "
              f"{r['repeats']} (медіана).", "", "## T(p), S(p) і межа Амдала", "",
              md_table(["p", "T(p), с (медіана)", "усі повтори, с", "S(p)", "межа 1/(f+(1−f)/p)",
                        "ефективність S/p", "Карп–Флатт e(p)"],
                       [[p, r["t_median"][p], ", ".join(f"{x:.2f}" for x in r["times_all"][p]),
                         fit["speedup"][p], fit["bound"][p], fit["efficiency"][p],
                         fit["karp_flatt"].get(p, "—")] for p in ps]), "",
              f"НК-оцінка послідовної частки (на самих S): **f = {fmt(fit['serial_fraction'])}** "
              f"(SSE = {fmt(fit['sse'])}; гранична S(∞) = 1/f = {fmt(fit['limit_speedup'])}). "
              f"«Структурна» частка — лише старт пулу і послідовна серіалізація задач у T(1): "
              f"f_struct = {fmt(r['serial_fraction_structural'])}.", ""]
    if r["t_seq_median"]:
        lines += [f"Еталон без пулу (у поточному процесі): T_seq = {fmt(r['t_seq_median'])} с; "
                  + ", ".join(f"T_seq/T({p}) = {fmt(r['speedup_vs_seq'][p])}" for p in ps) + ".", ""]
    lines += ["## Розклад накладних витрат (повтор з медіанним T(p))", "",
              md_table(["p", "T(p)", "старт пулу", "Σ задач (у воркерах)", "Σ/p", "makespan",
                        "нерівномірність", "залишок (IPC, результати)", "pickle послідовно",
                        "здорожчання обчислень", "процесів"],
                       [[p, b["wall_s"], b["startup_s"], b["compute_s"], b["ideal_parallel_s"],
                         b["makespan_s"],
                         b["imbalance_s"], b["residual_s"], b["pickle_serial_s"], b["cpu_inflation"],
                         int(b["processes_used"])] for p, b in ((p, r["overhead"][p]) for p in ps)]), "",
              "Усі величини в секундах, крім двох останніх стовпців. «Здорожчання обчислень» = Σ часу задач "
              "при p / Σ при p = 1: частина розриву до межі Амдала, яка НЕ є послідовною частиною програми "
              "(різна швидкість "
              "ядер, спільні кеші і пропускна здатність пам'яті, частота під навантаженням).", ""]
    pm = r["per_process_mean_task_s_at_pmax"]
    if pm:
        lines += [f"Середній час задачі по процесах при p = {max(ps)}: "
                  + ", ".join(f"{x:.2f}" for x in pm)
                  + f" с (найповільніший / найшвидший = {fmt(r['process_speed_spread_at_pmax'])}).", ""]
    return "\n".join(lines)


if __name__ == "__main__":
    sys.exit(main())
