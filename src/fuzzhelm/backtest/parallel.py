"""Паралельний прогін чистих задач (grid-клітинок) і замір прискорення за законом Амдала.

Найменування: backtest/parallel.py
Призначення: ProcessPoolExecutor(p) над чистими функціями; результат не залежить від числа воркерів;
S(p) = T(1)/T(p) порівнюється з межею Амдала S ≤ 1/(f + (1−f)/p) (брифінг §5.16).
Автор: Андрій Жук, 2026.

Незалежність від числа воркерів: кожна задача перед виконанням отримує СВІЙ контекст —
свіжу копію DECIMAL_CONTEXT і глобальні ГВЧ (random, numpy legacy), пересіяні похідним від
(seed, індекс задачі) зерном. Тому навіть задача, що (всупереч правилам) торкнулась глобального
ГВЧ, дає той самий результат незалежно від того, який воркер і після яких задач її виконав.
Initializer пулу додатково виставляє decimal-контекст і seed процесу (вимога брифінгу).
Воркер — функція верхнього рівня модуля (pickle); старт процесів — "spawn" (безпечно на macOS).
"""

from __future__ import annotations

import decimal
import hashlib
import math
import multiprocessing
import os
import random
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from typing import Any

import numpy as np

from fuzzhelm.core.money import DECIMAL_CONTEXT, setup_decimal_context


def task_seed(base_seed: int, index: int) -> int:
    """Детерміноване зерно задачі: перші 8 байт BLAKE2b(base_seed ‖ index), беззнакове < 2³²."""
    h = hashlib.blake2b(digest_size=8)
    h.update(base_seed.to_bytes(16, "big", signed=True))
    h.update(index.to_bytes(8, "big"))
    return int.from_bytes(h.digest(), "big") % (2**32)


def _seed_globals(seed: int) -> None:
    random.seed(seed)
    np.random.seed(seed % (2**32))  # legacy-ГВЧ numpy — лише як запобіжник для «брудних» задач


def _worker_init(seed: int) -> None:
    setup_decimal_context()
    _seed_globals(seed)


def _invoke(payload: tuple[Callable[[Any], Any], int, Any, int]) -> Any:
    fn, index, task, base_seed = payload
    _seed_globals(task_seed(base_seed, index))
    with decimal.localcontext(DECIMAL_CONTEXT.copy()):
        return fn(task)


def run_parallel[T, R](
    fn: Callable[[T], R],
    tasks: Iterable[T],
    workers: int,
    *,
    seed: int = 0,
    chunksize: int = 1,
    mp_context: str = "spawn",
) -> list[R]:
    """Виконати fn над кожною задачею; результати — у порядку задач незалежно від workers.

    workers ≥ 1 — пул процесів такого розміру; workers = 0 — послідовно в поточному процесі
    (налагодження; стан глобальних ГВЧ викликача відновлюється після прогону).
    """
    items = list(tasks)
    payloads = [(fn, i, t, seed) for i, t in enumerate(items)]
    if workers < 0:
        raise ValueError("workers must be >= 0")
    if workers == 0:
        py_state, np_state = random.getstate(), np.random.get_state()
        try:
            return [_invoke(p) for p in payloads]
        finally:
            random.setstate(py_state)
            np.random.set_state(np_state)
    ctx = multiprocessing.get_context(mp_context)
    with ProcessPoolExecutor(max_workers=workers, mp_context=ctx, initializer=_worker_init,
                             initargs=(seed,)) as ex:
        return list(ex.map(_invoke, payloads, chunksize=chunksize))


def worker_probe(task: Any) -> dict[str, Any]:
    """Діагностична задача: що бачить воркер (контекст Decimal, pid, глобальний ГВЧ)."""
    ctx = decimal.getcontext()
    return {"task": task, "prec": ctx.prec, "rounding": ctx.rounding, "pid": os.getpid(),
            "rand": random.random()}


# ================================================================ закон Амдала


def amdahl_bound(p: float, f: float) -> float:
    """S_max(p) = 1/(f + (1−f)/p)."""
    return 1.0 / (f + (1.0 - f) / p)


def speedups(times: Mapping[int, float]) -> dict[int, float]:
    """S(p) = T(1)/T(p); потрібен замір для p = 1."""
    if 1 not in times:
        raise ValueError("T(1) is required")
    t1 = times[1]
    return {p: t1 / t for p, t in sorted(times.items())}


def karp_flatt(p: int, s: float) -> float:
    """Експериментально визначена послідовна частка e = (1/S − 1/p)/(1 − 1/p), p > 1."""
    if p <= 1:
        raise ValueError("Karp–Flatt metric is defined for p > 1")
    return (1.0 / s - 1.0 / p) / (1.0 - 1.0 / p)


def fit_serial_fraction(ps: Sequence[int], ss: Sequence[float]) -> float:
    """f ∈ [0, 1], що мінімізує Σ(S_p − 1/(f + (1−f)/p))² (нелінійні НК на самих S, не на 1/S).

    Одновимірна задача: грубий перебір з кроком 10⁻³ + золотий переріз навколо найкращого вузла.
    """
    if len(ps) != len(ss) or not ps:
        raise ValueError("ps and ss must be non-empty and of equal length")

    def sse(f: float) -> float:
        return math.fsum((s - amdahl_bound(p, f)) ** 2 for p, s in zip(ps, ss, strict=True))

    grid = [i / 1000 for i in range(1001)]
    best = min(grid, key=sse)
    lo, hi = max(0.0, best - 1e-3), min(1.0, best + 1e-3)
    phi = (math.sqrt(5.0) - 1.0) / 2.0
    a, b = lo + (1 - phi) * (hi - lo), lo + phi * (hi - lo)
    fa, fb = sse(a), sse(b)
    for _ in range(80):
        if fa <= fb:
            hi, b, fb = b, a, fa
            a = lo + (1 - phi) * (hi - lo)
            fa = sse(a)
        else:
            lo, a, fa = a, b, fb
            b = lo + phi * (hi - lo)
            fb = sse(b)
    return (lo + hi) / 2.0


@dataclass(frozen=True, slots=True)
class AmdahlReport:
    workers: tuple[int, ...]
    times: dict[int, float]            # T(p), секунди (найкращий з repeats)
    speedup: dict[int, float]          # S(p) = T(1)/T(p)
    serial_fraction: float             # f за НК
    bound: dict[int, float]            # 1/(f + (1−f)/p) з підігнаним f
    karp_flatt: dict[int, float]       # e(p) для p > 1


def measure_speedup[T, R](
    fn: Callable[[T], R],
    tasks: Sequence[T],
    workers: Sequence[int] = (1, 2, 4, 8),
    *,
    seed: int = 0,
    repeats: int = 1,
    timer: Callable[[], float] = time.perf_counter,
) -> AmdahlReport:
    """Заміряти T(p) для кожного p (включно зі стартом пулу — це частина послідовних витрат)."""
    if 1 not in workers:
        raise ValueError("workers must include 1 (T(1) is the baseline)")
    times: dict[int, float] = {}
    for p in workers:
        best = math.inf
        for _ in range(max(1, repeats)):
            t0 = timer()
            run_parallel(fn, tasks, p, seed=seed)
            best = min(best, timer() - t0)
        times[p] = best
    return amdahl_report(times)


def amdahl_report(times: Mapping[int, float]) -> AmdahlReport:
    s = speedups(times)
    ps = sorted(s)
    f = fit_serial_fraction(ps, [s[p] for p in ps])
    return AmdahlReport(
        workers=tuple(ps), times=dict(sorted(times.items())), speedup=s, serial_fraction=f,
        bound={p: amdahl_bound(p, f) for p in ps},
        karp_flatt={p: karp_flatt(p, s[p]) for p in ps if p > 1},
    )
